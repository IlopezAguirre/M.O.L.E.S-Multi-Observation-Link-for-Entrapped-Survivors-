#!/usr/bin/env python3
"""
MOLES / PUPS — live terminal monitor

Reads the host ESP32 over USB serial and prints one line per ranging exchange,
plus a link-health summary every few seconds. Type  h + Enter  for the legend.

   seq  link   range  Δrange    fp     SNR    fp%     dev  sigdev   sh      Δφ    Δpath
 04283  A->B   0.155  -0.181   742    28dB   106%   43.4%   12.1%   +1   +12.3°   +1.6mm  ██░░░░░░░░░░

  dev    = aligned shape change vs the baseline over ALL 56 taps (noise taps included)
  sigdev = the same, scored only on taps clearly above the noise floor
  Δφ     = phase change of one probe tap, measured relative to the first path
  Δpath  = Δφ as echo path-length change (7.8° = 1 mm, channel 5)

  The DW3000's first-path index (fp_idx) jitters by +/-1-2 taps between packets
  even when nothing moves, sliding the window against the baseline and inflating
  dev. Before scoring, we search a small tap shift (+/-MAX_SHIFT) and keep the
  best-matching alignment ("sh"). align.py is the reference/tested version of
  this search; replay_dev.py re-runs it over a recorded capture (--record).

Usage:
  pip install pyserial
  python moles_monitor.py --port /dev/cu.usbserial-0001
  python moles_monitor.py --record data/run1.csv   # also log every real packet (raw) for replay
  python moles_monitor.py --probe 2                # phase probe at fp+2 (default: auto)
  python moles_monitor.py --dsp                    # MOLES DSP: all links, combined decision (needs numpy)
  python moles_monitor.py --dsp --plot --alert     # + live FFT window + drive the moles' alert LEDs
  python moles_monitor.py --dsp --json data/run1.jsonl --record data/run1.csv   # backend feed + raw log
  python moles_monitor.py --list                   # show serial ports

While running, type a command and press Enter:
  b   re-capture the baseline (keep the link clear for ~3 s)
  h   show the legend
  q   quit
"""

import argparse
import cmath
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
LINK_NAMES = {0x01: "A->B", 0x02: "A->C", 0x03: "B->C"}   # MOLES schedule: initiator->responder

# ---------------------------------------------------------------- signal constants
NOISE_TAPS = range(0, 4)              # fp-8 .. fp-5: before the first path, receiver noise only
SIG_K = 4.0                           # "signal tap" = baseline magnitude > SIG_K x noise rms (~12 dB)
PROBE_SEARCH = range(CIR_PRE + 1, CIR_PRE + 7)   # auto probe: strongest baseline tap in fp+1..fp+6
DEG_PER_MM = 360.0 / 46.2             # channel 5: lambda = 46.2 mm -> 7.8 deg per mm of path
HEADER_EVERY = 40                     # reprint the column header every N live lines

# ---------------------------------------------------------------- display
BARS = " ▁▂▃▄▅▆▇█"
RED, YEL, GRN, CYN, DIM, BOLD, RST = ("\033[31m", "\033[33m", "\033[32m", "\033[36m",
                                      "\033[2m", "\033[1m", "\033[0m")

# (name, width) — header and rows are built from the same widths so they always line up
COLS = [("seq", 5), ("link", 4), ("range", 6), ("Δrange", 7), ("fp", 4), ("SNR", 6),
        ("fp%", 5), ("dev", 6), ("sigdev", 6), ("sh", 3), ("Δφ", 7), ("Δpath", 7)]

LEGEND = f"""{BOLD}LEGEND{RST}  (type h + Enter to show again)
 {BOLD}seq{RST}     packet number from Mole A. Gaps show as "(+N lost over ESP-NOW)".
 {BOLD}link{RST}    mole pair, e.g. A->B.
 {BOLD}range{RST}   SS-TWR distance (m). Absolute value has an uncalibrated offset; use Δrange.
 {BOLD}Δrange{RST}  range minus baseline (m). A hand BLOCKING the link pushes this UP.
 {BOLD}fp{RST}      first-path tap index in the chip's 1016-tap CIR.
 {BOLD}SNR{RST}     first-path strength over the noise floor (taps before the first path), dB.
         Low SNR = noisy phase and noisy dev. Aim for 20 dB or more.
 {BOLD}fp%{RST}     first-path strength vs baseline. Blocking drops it well below 100%.
 {BOLD}dev{RST}     aligned CIR shape change vs baseline over ALL 56 taps (noise taps included).
 {BOLD}sigdev{RST}  same, scored only on taps clearly above the noise floor. If dev is high but
         sigdev is low, the "deviation" is mostly noise taps, not a real change.
 {BOLD}sh{RST}      tap shift used to cancel first-path jitter (+/-{MAX_SHIFT} max).
 {BOLD}Δφ{RST}      phase change of the probe tap, relative to the first path, vs baseline (deg).
         This is the kind of signal the FFT will use. Magnitude/dev can't see mm motion;
         phase can.
 {BOLD}Δpath{RST}   Δφ as echo path-length change: 7.8° = 1 mm.
 {BOLD}meter{RST}   dev vs alert threshold (full bar = 2x threshold). Red + DISTURBED = over it.
 {BOLD}shape{RST}   CIR magnitude across the window, scaled to the baseline peak.
{BOLD}DSP{RST} (with --dsp, once per second, all links on one clock, over the last 30 s)
 tier    CONFIRMED = combined peak >= 12 dB, stable, >=2 links agree. DETECTED = same, 1 link.
 peak    strongest periodic motion found anywhere in the band (no assumed rate).
 PNR     peak vs the rest of the spectrum. DETECTED needs PNR >= margin (16 dB),
         <=10% missing samples, and the same peak in 3 consecutive windows.
 mode    arc = amplitude in mm of echo path; phase = small motion, amplitude in degrees.
{BOLD}SUMMARY{RST} (every few seconds, per link)
 pkt/s   received rate (expect 10).  miss% = Mole B didn't answer.  lost% = ESP-NOW loss.
 φσ      spread of Δφ over the summary period. On a STILL scene this is the phase noise
         the FFT will see: ≤5° excellent, ≤15° usable, ≤30° marginal, >30° not FFT-ready.
         During motion φσ is large by design.
"""


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


def header_line():
    return DIM + "  ".join(f"{n:>{w}}" for n, w in COLS) + "  alert" + RST


def row(fields):
    return "  ".join(f"{v:>{w}}" for v, (_, w) in zip(fields, COLS))


def wrap180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def circ_mean_deg(angles):
    z = sum(cmath.exp(1j * math.radians(a)) for a in angles)
    return math.degrees(cmath.phase(z)) if abs(z) > 0 else 0.0


def circ_std_deg(angles):
    if not angles:
        return float("nan")
    R = abs(sum(cmath.exp(1j * math.radians(a)) for a in angles)) / len(angles)
    return math.degrees(math.sqrt(-2.0 * math.log(max(min(R, 1.0), 1e-12))))


def fp_mag_at(mags, s):
    """Strongest tap around the first path AFTER applying shift s (fp-1 .. fp+3)."""
    return max(mags[j] for j in range(CIR_PRE - 1 + s, CIR_PRE + 4 + s) if 0 <= j < TAPS)


def noise_rms(mags):
    return math.sqrt(sum(mags[t] ** 2 for t in NOISE_TAPS) / len(NOISE_TAPS))


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
    def __init__(self, n_base, thresh_override, max_shift=MAX_SHIFT, probe_offset=None):
        self.n_base = n_base
        self.thresh_override = thresh_override
        self.max_shift = max_shift
        self.probe_offset = probe_offset
        self.reset()

    def reset(self):
        self.base_samples = []   # list of (mags, range, fp_mag, cplx)
        self.base = None         # per-tap mean magnitude (drift-aligned)
        self.base_range = None
        self.base_fpmag = None
        self.thresh = None
        self.sig_mask = None     # taps clearly above the noise floor
        self.probe = None        # probe tap (window index) for phase
        self.base_phase = None   # probe phase rel. to first path, baseline mean (deg)
        self.last_seq = None
        self.lost = 0
        self.miss_streak = 0
        self.reset_period()

    def reset_period(self):
        self.p_start = time.time()
        self.p_ok = self.p_miss = self.p_lost = 0
        self.p_snr, self.p_dev, self.p_sdev, self.p_dphi = [], [], [], []

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
        scored only on its overlap. Returns (best_dev, best_shift); ties keep
        the smaller |shift| (0 tried first).

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

    def sig_dev(self, mags, s):
        """dev at shift s, scored only on signal taps (baseline above the noise floor)."""
        num = den = 0.0
        for t in range(TAPS):
            j = t + s
            if self.sig_mask[t] and 0 <= j < TAPS:
                num += abs(mags[j] - self.base[t])
                den += self.base[t]
        return 100.0 * num / den if den > 0 else float("nan")

    def probe_phase(self, cplx, s):
        """Probe-tap phase relative to the first-path tap (deg), with shift s applied."""
        fp_j, p_j = CIR_PRE + s, self.probe + s
        if not (0 <= fp_j < TAPS and 0 <= p_j < TAPS):
            return None
        h_fp, h_p = cplx[fp_j], cplx[p_j]
        if abs(h_fp) == 0 or abs(h_p) == 0:
            return None
        return math.degrees(cmath.phase(h_p * h_fp.conjugate()))

    def add_baseline(self, mags, rng, fpmag, cplx):
        self.base_samples.append((mags, rng, fpmag, cplx))
        if len(self.base_samples) < self.n_base:
            return False
        n = len(self.base_samples)
        windows = [s[0] for s in self.base_samples]

        # Pass 1: naive per-tap mean as a provisional reference.
        prov = [sum(w[k] for w in windows) / n for k in range(TAPS)]

        # Pass 2: shift each sample onto that reference before averaging, so the
        # first-path peak stays sharp instead of being smeared by fp_idx jitter.
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

        # Noise floor from the pre-first-path taps; signal taps sit clearly above it.
        self.base_noise = math.sqrt(sum(noise_rms(w) ** 2 for w in windows) / n)
        self.base_snr = 20 * math.log10(max(self.base_fpmag, 1e-9) / max(self.base_noise, 1e-9))
        self.sig_mask = [self.base[t] > SIG_K * self.base_noise for t in range(TAPS)]

        # Phase probe: user offset, or the strongest baseline tap just after the first path.
        if self.probe_offset is not None:
            self.probe = CIR_PRE + self.probe_offset
        else:
            self.probe = max(PROBE_SEARCH, key=lambda t: self.base[t])
        self.probe_snr = 20 * math.log10(max(self.base[self.probe], 1e-9) / max(self.base_noise, 1e-9))

        # Noise measured the SAME way live packets are scored (aligned).
        shifts = [self.aligned_dev(w, self.base, self.max_shift)[1] for w in windows]
        self.base_fpmag = sum(fp_mag_at(w, s) for w, s in zip(windows, shifts)) / n
        self.base_snr = 20 * math.log10(max(self.base_fpmag, 1e-9) / max(self.base_noise, 1e-9))
        devs = [self.aligned_dev(w, self.base, self.max_shift)[0] for w in windows]
        self.noise_avg = sum(devs) / n
        self.noise_max = max(devs)
        raw = [self.dev(w, self.base) for w in windows]
        self.noise_avg_raw = sum(raw) / n
        self.noise_max_raw = max(raw)
        sdevs = [self.sig_dev(w, s) for w, s in zip(windows, shifts)]
        self.noise_sig_avg = sum(sdevs) / n

        phases = [self.probe_phase(smp[3], s) for smp, s in zip(self.base_samples, shifts)]
        phases = [p for p in phases if p is not None]
        self.base_phase = circ_mean_deg(phases) if phases else 0.0
        self.base_phase_std = circ_std_deg([wrap180(p - self.base_phase) for p in phases])

        self.thresh = self.thresh_override or max(2.0 * self.noise_max, 5.0)
        return True


def fft_verdict(phi_std):
    if math.isnan(phi_std):
        return "n/a", DIM
    if phi_std <= 5:
        return "excellent", GRN
    if phi_std <= 15:
        return "usable", GRN
    if phi_std <= 30:
        return "marginal", YEL
    return "not FFT-ready", RED


def print_summary(lk, name):
    dt = max(time.time() - lk.p_start, 1e-9)
    tried = lk.p_ok + lk.p_miss
    rate = tried / dt
    miss = 100.0 * lk.p_miss / tried if tried else 0.0
    lost = 100.0 * lk.p_lost / (tried + lk.p_lost) if (tried + lk.p_lost) else 0.0
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    phi_std = circ_std_deg(lk.p_dphi)
    verdict, vcol = fft_verdict(phi_std)
    print(f"{CYN}── {name} {dt:4.1f}s │ {rate:4.1f} pkt/s  miss {miss:3.0f}%  lost {lost:3.0f}%  "
          f"SNR {avg(lk.p_snr):4.1f} dB  dev {avg(lk.p_dev):5.1f}%  sigdev {avg(lk.p_sdev):5.1f}%  "
          f"φσ {phi_std:5.1f}° ({phi_std / DEG_PER_MM:4.2f} mm){RST}  "
          f"{vcol}phase: {verdict}{RST} {DIM}(if scene is still){RST}")
    lk.reset_period()


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
        if cmd == "h":
            state["legend"] = True


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="MOLES / PUPS live monitor")
    ap.add_argument("--port", help="serial port of the HOST ESP32")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--baseline", type=int, default=30, help="packets averaged for the baseline (30 = 3 s)")
    ap.add_argument("--thresh", type=float, default=None, help="alert threshold in dev%% (default: auto)")
    ap.add_argument("--max-shift", type=int, default=MAX_SHIFT,
                    help="tap-shift search radius to cancel fp_idx jitter (default 2; 0 = off)")
    ap.add_argument("--probe", type=int, default=None,
                    help="phase probe tap as an offset after the first path (default: auto, fp+1..fp+6)")
    ap.add_argument("--summary", type=float, default=5.0,
                    help="seconds between link-health summaries (0 = off)")
    ap.add_argument("--no-shape", action="store_true", help="hide the CIR sparkline")
    ap.add_argument("--no-legend", action="store_true", help="don't print the legend at startup")
    ap.add_argument("--record", metavar="FILE",
                    help="log every real mole packet to FILE as CSV for replay_dev.py")
    ap.add_argument("--dsp", action="store_true",
                    help="run the MOLES DSP (pups_dsp.py) live: one combined result per second")
    ap.add_argument("--dsp-margin", type=float, default=16.0, help="per-link detection margin (dB)")
    ap.add_argument("--combined-margin", type=float, default=12.0, help="combined-spectrum margin (dB)")
    ap.add_argument("--packets", action="store_true",
                    help="with --dsp: also print one line per packet (off by default: 30 lines/s)")
    ap.add_argument("--plot", action="store_true", help="with --dsp: live FFT window (matplotlib)")
    ap.add_argument("--plot-fmax", type=float, default=2.0, help="plot x-axis max (Hz); grows to show the peak")
    ap.add_argument("--ref-hz", type=float, default=None,
                    help="module rate measured INDEPENDENTLY (drawn as a reference line only)")
    ap.add_argument("--alert", action="store_true",
                    help="with --dsp: send ALERT <mask> to the host so detecting moles blink")
    ap.add_argument("--json", metavar="FILE", help="with --dsp: append one JSON line per DSP update (backend feed)")
    ap.add_argument("--run-id", default=None, help="run id written into --json rows (default: start time)")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = ap.parse_args()

    dsp = dsp_cfg = None
    if args.dsp:
        try:
            import pups_dsp as dsp                 # lazy: the plain monitor stays numpy-free
        except ImportError as e:
            sys.exit(f"--dsp needs pups_dsp.py, align.py and numpy next to this file ({e})")
        dsp_cfg = dsp.DSPConfig(pnr_margin_db=args.dsp_margin, combined_margin_db=args.combined_margin)
    for flag in ("plot", "alert", "json", "packets"):
        if getattr(args, flag) and not args.dsp:
            sys.exit(f"--{flag} needs --dsp")

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
        rec = open(args.record, "w")    # raw packets only, never processed values
        rec.write("seq,link_id,range_m,fp_idx,"
                  + ",".join(f"i{k}" for k in range(TAPS)) + ","
                  + ",".join(f"q{k}" for k in range(TAPS)) + "\n")
        print(f"{DIM}recording real packets to {args.record} (CSV){RST}")

    print(f"{BOLD}MOLES / PUPS — live monitor{RST}   port={port} baud={args.baud}")
    print(f"{DIM}commands: b = re-capture baseline, h = legend, q = quit (then Enter){RST}\n")
    if not args.no_legend:
        print(LEGEND)

    state = {"quit": False, "rebase": False, "legend": False}
    threading.Thread(target=stdin_commands, args=(state,), daemon=True).start()

    reader = FrameReader()
    links = {}
    moles = dsp.MolesDSP(dsp_cfg) if dsp is not None else None   # all links, shared clock (--dsp)
    show_pkts = not args.dsp or args.packets
    plot = None
    if args.plot:
        import moles_plot
        plot = moles_plot.MolesPlot(dsp_cfg, fmax=args.plot_fmax, ref_hz=args.ref_hz)
    jfile = open(args.json, "a") if args.json else None
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    sent_mask = None
    last_pump = 0.0
    first_status = True
    last_pkt_time = time.time()
    live_lines = 0

    try:
        while not state["quit"]:
            data = ser.read(4096)
            if plot is not None and time.time() - last_pump > 0.1:
                plot.pump()
                last_pump = time.time()
            if state["rebase"]:
                state["rebase"] = False
                for lk in links.values():
                    lk.reset()
                if moles is not None:
                    moles.reset()
                print(f"\n{YEL}re-capturing baseline — keep the link clear{RST}")
            if state["legend"]:
                state["legend"] = False
                print("\n" + LEGEND)
                live_lines = 0

            for ftype, payload in reader.feed(data):

                # ---------- host status text
                if ftype == FRAME_TEXT:
                    text = payload.decode("ascii", "replace")
                    idle = time.time() - last_pkt_time
                    if first_status:
                        print(f"{DIM}[host] {text}{RST}")
                        print(f"{DIM}       (this MAC must match HOST_MAC in mole.ino){RST}\n")
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
                if rec is not None:                 # log the raw packet as CSV for replay
                    rec.write(f"{seq},{link_id},{rng!r},{fp_idx},"
                              + ",".join(map(str, ci)) + ","
                              + ",".join(map(str, cq)) + "\n")
                name = LINK_NAMES.get(link_id, f"L{link_id:02X}")
                lk = links.setdefault(link_id, Link(args.baseline, args.thresh, args.max_shift, args.probe))

                # ESP-NOW losses show up as seq gaps
                gap = ""
                if lk.last_seq is not None:
                    skipped = (seq - lk.last_seq - 1) & 0xFFFF
                    if 0 < skipped < 1000:
                        lk.lost += skipped
                        lk.p_lost += skipped
                        gap = f"  {DIM}(+{skipped} lost over ESP-NOW){RST}"
                lk.last_seq = seq

                if moles is not None:                   # DSP sees every packet, misses included
                    moles.push(dsp.Packet(seq, link_id, rng, fp_idx,
                                          dsp.np.array(ci, dtype=float) + 1j * dsp.np.array(cq, dtype=float)))
                    mres = moles.maybe_evaluate()
                    if mres is not None:
                        col = {"CONFIRMED": RED, "DETECTED": YEL, "none": CYN, "warming": DIM}[mres.tier]
                        print(f"{col}{BOLD}MOLES{RST}{col}  {mres.line()}{RST}")
                        for lid, wr in sorted(mres.links.items()):
                            if wr is not None and mres.tier != "warming":
                                note = mres.excluded.get(lid, "agrees" if lid in mres.agreeing else "")
                                print(f"{DIM}   {dsp.LINK_NAMES.get(lid, lid):5s} {wr.line()[11:]}  {note}{RST}")
                        if jfile is not None:
                            jfile.write(dsp.json.dumps(mres.to_json(wall_time=time.time(), run_id=run_id)) + "\n")
                            jfile.flush()
                        if plot is not None:
                            plot.update(mres)
                        if args.alert and mres.alert_mask != sent_mask:
                            ser.write(f"ALERT {mres.alert_mask}\n".encode())
                            sent_mask = mres.alert_mask

                summary_due = (lk.base is not None and args.summary > 0
                               and time.time() - lk.p_start >= args.summary)

                # failed UWB exchange: Mole B's response never arrived
                if fp_idx == 0xFFFF or math.isnan(rng):
                    lk.miss_streak += 1
                    lk.p_miss += 1
                    if show_pkts:
                        print(f"{RED}{seq:05d}  {name}  ---- no response from responder "
                              f"(miss x{lk.miss_streak}) — link blocked or responder not answering ----{RST}{gap}")
                    if summary_due:
                        print_summary(lk, name)
                    continue
                lk.miss_streak = 0

                cplx = [complex(i, q) for i, q in zip(ci, cq)]
                mags = [abs(z) for z in cplx]
                fpmag = fp_mag_at(mags, 0)       # provisional; baseline recomputes it aligned

                # ---------- baseline capture
                if lk.base is None:
                    done = lk.add_baseline(mags, rng, fpmag, cplx)
                    n = len(lk.base_samples)
                    if not done:
                        if show_pkts and (n == 1 or n % 10 == 0):
                            print(f"{DIM}{seq:05d}  {name}  capturing baseline {n:2d}/{lk.n_base} — "
                                  f"keep the link clear   range {rng:6.3f} m  fp {fp_idx}{RST}")
                    else:
                        n_sig = sum(lk.sig_mask)
                        print(f"\n{GRN}{BOLD}baseline locked for {name}:{RST}{GRN} range "
                              f"{lk.base_range:.3f} m, fp_mag {lk.base_fpmag:.0f}, "
                              f"SNR {lk.base_snr:.1f} dB{RST}")
                        print(f"{DIM}   noise dev  aligned avg {lk.noise_avg:.1f}% / max "
                              f"{lk.noise_max:.1f}%   (no alignment: avg {lk.noise_avg_raw:.1f}% / "
                              f"max {lk.noise_max_raw:.1f}%)   signal taps only: avg "
                              f"{lk.noise_sig_avg:.1f}% over {n_sig} taps{RST}")
                        print(f"{DIM}   phase probe fp+{lk.probe - CIR_PRE} (SNR {lk.probe_snr:.1f} dB), "
                              f"baseline phase spread {lk.base_phase_std:.1f}° "
                              f"({lk.base_phase_std / DEG_PER_MM:.2f} mm){RST}")
                        if lk.probe_snr < 12:
                            print(f"{YEL}   probe tap is near the noise floor — its phase will be "
                                  f"unreliable (try --probe or move the moles closer){RST}")
                        print(f"{GRN}   -> alert at dev > {lk.thresh:.1f}%   "
                              f"(fp_idx shift search +/-{lk.max_shift} taps){RST}\n")
                        if show_pkts:
                            print(header_line())
                        live_lines = 0
                        lk.reset_period()
                    continue

                # ---------- live line
                d, sh = lk.aligned_dev(mags, lk.base, lk.max_shift)   # drift-corrected
                fpmag = fp_mag_at(mags, sh)                           # first path at its aligned spot
                sd = lk.sig_dev(mags, sh)
                drange = rng - lk.base_range
                fp_pct = 100.0 * fpmag / (lk.base_fpmag or 1.0)
                snr = 20 * math.log10(max(fpmag, 1e-9) / max(noise_rms(mags), 1e-9))
                ph = lk.probe_phase(cplx, sh)
                dphi = wrap180(ph - lk.base_phase) if ph is not None else float("nan")

                lk.p_ok += 1
                lk.p_snr.append(snr)
                lk.p_dev.append(d)
                if not math.isnan(sd):
                    lk.p_sdev.append(sd)
                if ph is not None:
                    lk.p_dphi.append(dphi)

                if d > lk.thresh:
                    color, flag = RED, "  <<< DISTURBED"
                elif d > 0.6 * lk.thresh:
                    color, flag = YEL, ""
                else:
                    color, flag = "", ""

                if show_pkts and live_lines and live_lines % HEADER_EVERY == 0:
                    print(header_line())
                live_lines += 1

                fields = [f"{seq:05d}", name, f"{rng:6.3f}", f"{drange:+7.3f}", f"{fp_idx:4d}",
                          f"{snr:4.0f}dB", f"{fp_pct:4.0f}%", f"{d:5.1f}%", f"{sd:5.1f}%",
                          f"{sh:+d}", f"{dphi:+6.1f}°", f"{dphi / DEG_PER_MM:+5.1f}mm"]
                shape = "" if args.no_shape else "  " + sparkline(mags, max(lk.base))
                if show_pkts:
                    print(f"{color}{row(fields)}  {meter(d, lk.thresh)}{flag}{RST}{gap}{shape}")

                if summary_due:
                    print_summary(lk, name)

    except KeyboardInterrupt:
        pass
    finally:
        if args.alert and sent_mask:
            ser.write(b"ALERT 0\n")                   # don't leave moles blinking
        ser.close()
        if jfile is not None:
            jfile.close()
        if rec is not None:
            rec.close()
            print(f"{DIM}saved capture to {args.record}{RST}")
        print(f"\n{DIM}closed {port}{RST}")


if __name__ == "__main__":
    main()
