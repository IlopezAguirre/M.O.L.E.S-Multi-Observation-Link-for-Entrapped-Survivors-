#!/usr/bin/env python3
"""
MOLES — Phase 1: Hand-Wave Test — live terminal monitor

Reads the host ESP32 over USB serial and prints one line per ranging exchange:

  seq   link  range (delta vs baseline)  first-path  fp_mag (% of baseline)  dev%             CIR shape
  0412  A->B  1.243 m (+0.002)           fp 745      fp_mag  8120 ( 99%)     2.9% (raw 41.6% sh +1)  ▁▁▂█▆▃▂▂▁▁...

  dev% = how different this CIR window is from the empty-link baseline
         (sum of |magnitude - baseline| over all taps / sum of baseline, in %)

         The DW3000's first-path index (fp_idx) jitters by +/-1-2 taps between
         packets even when nothing moves, sliding the whole window against the
         baseline and inflating dev. Before scoring, we search a small tap-shift
         (+/-MAX_SHIFT) and keep the best-matching alignment, so a still scene
         reads near 0% instead of ~40%. "raw" = dev with no shift correction,
         "sh" = the shift that best re-aligned this packet. See align.py for the
         standalone reference version of this search, and replay_dev.py to re-run
         it over a recorded capture (moles_monitor.py --record).

Usage:
  pip install pyserial
  python moles_monitor.py                      # auto-detects the port if only one ESP32 is plugged in
  python moles_monitor.py --port /dev/cu.usbserial-0001
  python moles_monitor.py --record data/run1.csv  # also log every real packet for later replay
  python moles_monitor.py --list               # show serial ports

While running, type a command and press Enter:
  b   re-capture the baseline (keep the link clear for ~3 s)
  q   quit
"""

import argparse
import math
import struct
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Run:  pip install pyserial")

# ---------------------------------------------------------------- protocol
SYNC = b"\xAA\x55"
FRAME_PKT, FRAME_TEXT = 0x01, 0x02
MAX_FRAME = 1024

PKT_FMT = "<BBHfH56h56h"              # must match mole_pkt_t
PKT_LEN = struct.calcsize(PKT_FMT)    # 234
MAGIC = 0x4D
TAPS = 56
CIR_PRE = 8                           # first path sits at tap 8 of the window
MAX_SHIFT = 2                         # tap-shift search radius to cancel fp_idx jitter
LINK_NAMES = {0x01: "A->B", 0x02: "A->C", 0x03: "B->C"}

# ---------------------------------------------------------------- display
BARS = " ▁▂▃▄▅▆▇█"
RED, YEL, GRN, DIM, BOLD, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[0m"


def sparkline(mags, scale):
    if scale <= 0:
        return " " * len(mags)
    out = []
    for m in mags:
        k = int(round(min(m / scale, 1.0) * (len(BARS) - 1)))
        out.append(BARS[k])
    return "".join(out)


def meter(pct, thresh, width=12):
    filled = int(min(pct / max(thresh * 2, 1e-9), 1.0) * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------- serial framing
class FrameReader:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                self.buf = self.buf[-1:]          # keep a possible half sync byte
                break
            if len(self.buf) < i + 5:
                self.buf = self.buf[i:]
                break
            ftype = self.buf[i + 2]
            n = self.buf[i + 3] | (self.buf[i + 4] << 8)
            if n > MAX_FRAME:
                self.buf = self.buf[i + 2:]       # false sync, skip it
                continue
            end = i + 5 + n
            if len(self.buf) < end + 1:
                self.buf = self.buf[i:]
                break
            payload = bytes(self.buf[i + 5:end])
            ck = ftype ^ self.buf[i + 3] ^ self.buf[i + 4]
            for b in payload:
                ck ^= b
            if ck == self.buf[end]:
                frames.append((ftype, payload))
                self.buf = self.buf[end + 1:]
            else:
                self.buf = self.buf[i + 2:]       # bad checksum, resync
        return frames


# ---------------------------------------------------------------- per-link state
class Link:
    def __init__(self, n_base, thresh_override, max_shift=MAX_SHIFT):
        self.n_base = n_base
        self.thresh_override = thresh_override
        self.max_shift = max_shift
        self.reset()

    def reset(self):
        self.base_samples = []   # list of (mags, range, fp_mag)
        self.base = None         # per-tap mean magnitude (drift-aligned)
        self.base_range = None
        self.base_fpmag = None
        self.thresh = None
        self.last_seq = None
        self.lost = 0
        self.miss_streak = 0

    @staticmethod
    def dev(mags, base):
        """Straight-on deviation (%), no drift correction."""
        denom = sum(base) or 1.0
        return 100.0 * sum(abs(m - b) for m, b in zip(mags, base)) / denom

    @staticmethod
    def aligned_dev(mags, base, max_shift):
        """Lowest deviation over integer tap-shifts in [-max_shift, +max_shift].

        A shift s compares mags[t+s] against base[t]; taps that fall off either
        end for that shift are dropped (not zero-padded), so each shift is
        scored only on its overlap. Cancels the +/-1-2 fp_idx jitter that would
        otherwise slide the whole window against the baseline. Returns
        (best_dev, best_shift); ties keep the smaller |shift| (0 tried first).

        Mirrors align.aligned_dev — kept dependency-free (no numpy) so the live
        monitor stays lightweight; align.py is the unit-tested reference.
        """
        n = len(base)
        best_dev, best_shift = float("inf"), 0
        for s in sorted(range(-max_shift, max_shift + 1), key=lambda x: (abs(x), x)):
            if s >= 0:
                w, b = mags[s:], base[:n - s]
            else:
                w, b = mags[:n + s], base[-s:]
            if not b:
                continue
            d = Link.dev(w, b)
            if d < best_dev:
                best_dev, best_shift = d, s
        return best_dev, best_shift

    def add_baseline(self, mags, rng, fpmag):
        self.base_samples.append((mags, rng, fpmag))
        if len(self.base_samples) < self.n_base:
            return False
        n = len(self.base_samples)
        windows = [s[0] for s in self.base_samples]

        # Pass 1: naive per-tap mean as a provisional reference.
        prov = [sum(w[k] for w in windows) / n for k in range(TAPS)]

        # Pass 2: shift each sample onto that reference before averaging, so the
        # first-path peak stays sharp instead of being smeared by fp_idx jitter.
        # Per-tap counts differ at the edges (dropped taps), so divide per tap.
        sums = [0.0] * TAPS
        counts = [0] * TAPS
        for w in windows:
            _, s = self.aligned_dev(w, prov, self.max_shift)
            for t in range(TAPS):
                j = t + s
                if 0 <= j < TAPS:
                    sums[t] += w[j]
                    counts[t] += 1
        self.base = [sums[t] / counts[t] if counts[t] else prov[t] for t in range(TAPS)]

        self.base_range = sum(s[1] for s in self.base_samples) / n
        self.base_fpmag = sum(s[2] for s in self.base_samples) / n

        # Noise floor measured the SAME way live packets are scored (aligned),
        # so the threshold reflects real residual jitter, not the fp_idx drift.
        devs = [self.aligned_dev(w, self.base, self.max_shift)[0] for w in windows]
        self.noise_avg = sum(devs) / n
        self.noise_max = max(devs)
        # For comparison / the judges: the same baseline scored with NO alignment.
        raw = [self.dev(w, self.base) for w in windows]
        self.noise_avg_raw = sum(raw) / n
        self.noise_max_raw = max(raw)
        self.thresh = self.thresh_override or max(2.0 * self.noise_max, 5.0)
        return True


# ---------------------------------------------------------------- helpers
def find_port():
    keys = ("usbserial", "SLAB", "CP210", "CH340", "CH910", "wchusbserial", "usbmodem", "USB")
    cands = [p for p in list_ports.comports()
             if any(k.lower() in (p.device + " " + (p.description or "")).lower() for k in keys)]
    if len(cands) == 1:
        return cands[0].device
    if not cands:
        sys.exit("No ESP32 serial port found. Plug in the host ESP32 or pass --port.")
    print("More than one serial port found — pass the host's with --port:")
    for p in cands:
        print(f"  {p.device:35s} {p.description}")
    sys.exit(1)


def open_port(port, baud):
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 0.05
    s.dtr = False      # don't reset the host ESP32 when the port opens
    s.rts = False
    s.open()
    return s


def stdin_commands(state):
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "q":
            state["quit"] = True
            return
        if cmd == "b":
            state["rebase"] = True


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="MOLES phase 1 hand-wave monitor")
    ap.add_argument("--port", help="serial port of the HOST ESP32")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--baseline", type=int, default=30, help="packets averaged for the baseline (30 = 3 s)")
    ap.add_argument("--thresh", type=float, default=None, help="alert threshold in dev%% (default: auto)")
    ap.add_argument("--max-shift", type=int, default=MAX_SHIFT,
                    help="tap-shift search radius to cancel fp_idx jitter (default 2; 0 = off)")
    ap.add_argument("--no-shape", action="store_true", help="hide the CIR sparkline")
    ap.add_argument("--record", metavar="FILE",
                    help="log every real mole packet to FILE as CSV for replay_dev.py")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = ap.parse_args()

    if args.list:
        for p in list_ports.comports():
            print(f"{p.device:35s} {p.description}")
        return

    port = args.port or find_port()
    try:
        ser = open_port(port, args.baud)
    except serial.SerialException as e:
        sys.exit(f"Could not open {port}: {e}\n(Close the Arduino Serial Monitor if it's open.)")

    rec = None
    if args.record:
        rec = open(args.record, "w")    # one capture per file, CSV (matches data/*.csv evidence)
        rec.write("seq,link_id,range_m,fp_idx,"
                  + ",".join(f"i{k}" for k in range(TAPS)) + ","
                  + ",".join(f"q{k}" for k in range(TAPS)) + "\n")
        print(f"{DIM}recording real packets to {args.record} (CSV){RST}")

    print(f"{BOLD}MOLES phase 1 — hand-wave monitor{RST}   port={port} baud={args.baud}")
    print(f"{DIM}commands: b + Enter = re-capture baseline, q + Enter = quit{RST}\n")

    state = {"quit": False, "rebase": False}
    threading.Thread(target=stdin_commands, args=(state,), daemon=True).start()

    reader = FrameReader()
    links = {}
    first_status = True
    last_pkt_time = time.time()

    try:
        while not state["quit"]:
            data = ser.read(4096)
            if state["rebase"]:
                state["rebase"] = False
                for lk in links.values():
                    lk.reset()
                print(f"\n{YEL}re-capturing baseline — keep the link clear{RST}")

            for ftype, payload in reader.feed(data):

                # ---------- host status text
                if ftype == FRAME_TEXT:
                    text = payload.decode("ascii", "replace")
                    idle = time.time() - last_pkt_time
                    if first_status:
                        print(f"{DIM}[host] {text}{RST}")
                        print(f"{DIM}       (paste this MAC into HOST_MAC in mole_a_tx.ino for unicast){RST}\n")
                        first_status = False
                    elif idle > 1.5:
                        print(f"{YEL}[host] alive, no mole packets for {idle:.0f} s — {text}{RST}")
                    continue

                if ftype != FRAME_PKT or len(payload) != 6 + PKT_LEN:
                    continue

                # ---------- mole packet
                last_pkt_time = time.time()
                f = struct.unpack(PKT_FMT, payload[6:])
                magic, link_id, seq, rng, fp_idx = f[0], f[1], f[2], f[3], f[4]
                if magic != MAGIC:
                    continue
                ci, cq = f[5:5 + TAPS], f[5 + TAPS:5 + 2 * TAPS]
                if rec is not None:                 # log the real packet as CSV for replay
                    rec.write(f"{seq},{link_id},{rng!r},{fp_idx},"
                              + ",".join(map(str, ci)) + ","
                              + ",".join(map(str, cq)) + "\n")
                name = LINK_NAMES.get(link_id, f"L{link_id:02X}")
                lk = links.setdefault(link_id, Link(args.baseline, args.thresh, args.max_shift))

                # ESP-NOW losses show up as seq gaps
                gap = ""
                if lk.last_seq is not None:
                    skipped = (seq - lk.last_seq - 1) & 0xFFFF
                    if 0 < skipped < 1000:
                        lk.lost += skipped
                        gap = f"  {DIM}(+{skipped} lost over ESP-NOW){RST}"
                lk.last_seq = seq

                # failed UWB exchange: Mole B's response never arrived
                if fp_idx == 0xFFFF or math.isnan(rng):
                    lk.miss_streak += 1
                    print(f"{RED}{seq:05d}  {name}  ---- no response from responder "
                          f"(miss x{lk.miss_streak}) — link blocked or B not running ----{RST}{gap}")
                    continue
                lk.miss_streak = 0

                mags = [math.hypot(i, q) for i, q in zip(ci, cq)]
                fpmag = max(mags[CIR_PRE:CIR_PRE + 4])     # strongest tap at the first path

                # ---------- baseline capture
                if lk.base is None:
                    done = lk.add_baseline(mags, rng, fpmag)
                    n = len(lk.base_samples)
                    if not done:
                        if n == 1 or n % 10 == 0:
                            print(f"{DIM}{seq:05d}  {name}  capturing baseline {n:2d}/{lk.n_base} — "
                                  f"keep the link clear   range {rng:6.3f} m  fp {fp_idx}{RST}")
                    else:
                        print(f"\n{GRN}{BOLD}baseline locked for {name}:{RST}{GRN} range "
                              f"{lk.base_range:.3f} m, fp_mag {lk.base_fpmag:.0f}{RST}")
                        print(f"{DIM}   noise dev  aligned avg {lk.noise_avg:.1f}% / max "
                              f"{lk.noise_max:.1f}%   (no alignment: avg {lk.noise_avg_raw:.1f}% / "
                              f"max {lk.noise_max_raw:.1f}%){RST}")
                        print(f"{GRN}   -> alert at dev > {lk.thresh:.1f}%   "
                              f"(fp_idx shift search +/-{lk.max_shift} taps){RST}\n")
                    continue

                # ---------- live line
                d, sh = lk.aligned_dev(mags, lk.base, lk.max_shift)   # drift-corrected
                d_raw = lk.dev(mags, lk.base)                         # for reference
                drange = rng - lk.base_range
                fp_pct = 100.0 * fpmag / (lk.base_fpmag or 1.0)

                if d > lk.thresh:
                    color, flag = RED, "  <<< DISTURBED"
                elif d > 0.6 * lk.thresh:
                    color, flag = YEL, ""
                else:
                    color, flag = "", ""

                shape = "" if args.no_shape else "  " + sparkline(mags, max(lk.base))
                print(f"{color}{seq:05d}  {name}  {rng:6.3f} m ({drange:+.3f})  fp {fp_idx:4d}  "
                      f"fp_mag {fpmag:6.0f} ({fp_pct:4.0f}%)  dev {d:5.1f}% {meter(d, lk.thresh)}  "
                      f"{DIM}(raw {d_raw:4.1f}% sh {sh:+d}){RST}{color}"
                      f"{flag}{RST}{gap}{shape}")

    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        if rec is not None:
            rec.close()
            print(f"{DIM}saved capture to {args.record}{RST}")
        print(f"\n{DIM}closed {port}{RST}")


if __name__ == "__main__":
    main()
