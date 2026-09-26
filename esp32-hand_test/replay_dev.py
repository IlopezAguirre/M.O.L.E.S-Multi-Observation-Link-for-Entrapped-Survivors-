#!/usr/bin/env python3
"""
MOLES — replay a recorded capture and show raw vs aligned deviation.

Runs the SAME alignment code as the live monitor (imported from moles_monitor)
over a CSV recorded with `moles_monitor.py --record FILE`. Every number here
comes from real UWB packets off the boards — there is no synthetic data.

It rebuilds the per-link baseline from the first --baseline packets, then scores
the rest, printing per-packet lines and a summary that contrasts:

  raw dev      = deviation with NO fp_idx-drift correction (the old ~40% problem)
  aligned dev  = deviation after the +/-max_shift window search (the fix)

Usage:
  python replay_dev.py data/run1.csv
  python replay_dev.py data/run1.csv --baseline 30 --max-shift 2
  python replay_dev.py data/run1.csv --quiet       # summary only, no per-packet lines
"""

import argparse
import csv
import math
import sys

# Reuse the live monitor's protocol + alignment so replay can never drift from it.
from moles_monitor import (
    Link, TAPS, CIR_PRE, MAX_SHIFT, LINK_NAMES,
    sparkline, meter, RED, YEL, GRN, DIM, BOLD, RST,
)


def read_records(path):
    """Yield (link_id, seq, range_m, fp_idx, ci, cq) from a --record CSV."""
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None or header[:4] != ["seq", "link_id", "range_m", "fp_idx"]:
            sys.exit(f"{RED}{path} is not a moles_monitor --record CSV "
                     f"(unexpected header){RST}")
        for row in reader:
            if len(row) != 4 + 2 * TAPS:
                continue
            seq = int(row[0])
            link_id = int(row[1])
            rng = float(row[2])                 # 'nan' parses fine
            fp_idx = int(row[3])
            ci = [int(x) for x in row[4:4 + TAPS]]
            cq = [int(x) for x in row[4 + TAPS:4 + 2 * TAPS]]
            yield link_id, seq, rng, fp_idx, ci, cq


def main():
    ap = argparse.ArgumentParser(description="Replay a recorded MOLES capture (raw vs aligned dev)")
    ap.add_argument("file", help="capture file from moles_monitor.py --record")
    ap.add_argument("--baseline", type=int, default=30, help="packets averaged for the baseline")
    ap.add_argument("--thresh", type=float, default=None, help="alert threshold in dev%% (default: auto)")
    ap.add_argument("--max-shift", type=int, default=MAX_SHIFT, help="tap-shift search radius (default 2)")
    ap.add_argument("--quiet", action="store_true", help="summary only, no per-packet lines")
    args = ap.parse_args()

    links = {}
    stats = {}   # link_id -> dict of accumulators
    misses = 0
    total = 0

    for link_id, seq, rng, fp_idx, ci, cq in read_records(args.file):
        total += 1
        name = LINK_NAMES.get(link_id, f"L{link_id:02X}")

        # failed UWB exchange — no CIR to score
        if fp_idx == 0xFFFF or math.isnan(rng):
            misses += 1
            continue

        mags = [math.hypot(i, q) for i, q in zip(ci, cq)]
        fpmag = max(mags[CIR_PRE:CIR_PRE + 4])

        lk = links.setdefault(link_id, Link(args.baseline, args.thresh, args.max_shift))
        st = stats.setdefault(link_id, {"name": name, "raw": [], "aln": [],
                                        "shifts": [], "alerts": 0, "scored": 0})

        # ---------- baseline phase
        if lk.base is None:
            if lk.add_baseline(mags, rng, fpmag):
                print(f"{GRN}{BOLD}[{name}] baseline locked{RST}{GRN} from {lk.n_base} packets: "
                      f"range {lk.base_range:.3f} m{RST}")
                print(f"{DIM}   noise dev  aligned avg {lk.noise_avg:.1f}% / max {lk.noise_max:.1f}%   "
                      f"(no alignment: avg {lk.noise_avg_raw:.1f}% / max {lk.noise_max_raw:.1f}%)   "
                      f"-> alert > {lk.thresh:.1f}%{RST}\n")
            continue

        # ---------- scoring phase
        d, sh = lk.aligned_dev(mags, lk.base, lk.max_shift)
        d_raw = lk.dev(mags, lk.base)
        st["raw"].append(d_raw)
        st["aln"].append(d)
        st["shifts"].append(sh)
        st["scored"] += 1
        if d > lk.thresh:
            st["alerts"] += 1

        if not args.quiet:
            color = RED if d > lk.thresh else (YEL if d > 0.6 * lk.thresh else "")
            flag = "  <<< DISTURBED" if d > lk.thresh else ""
            print(f"{color}{seq:05d}  {name}  {rng:6.3f} m  fp {fp_idx:4d}  "
                  f"dev {d:5.1f}% {meter(d, lk.thresh)}  "
                  f"{DIM}(raw {d_raw:4.1f}% sh {sh:+d}){RST}{color}{flag}{RST}"
                  f"  {sparkline(mags, max(lk.base))}")

    # ---------------------------------------------------------------- summary
    print(f"\n{BOLD}==== replay summary — {args.file} ===={RST}")
    print(f"{total} packets, {misses} misses (no UWB response)")
    if not stats:
        sys.exit(f"{YEL}No scored packets. Need at least --baseline+1 valid packets per link.{RST}")

    print(f"\n {'link':<6}{'scored':>8}{'raw avg':>10}{'aligned avg':>13}{'alerts':>9}   shift usage")
    print(" " + "-" * 70)
    for lid, st in stats.items():
        if not st["aln"]:
            print(f" {st['name']:<6}{'(baseline only — capture more packets)':>40}")
            continue
        raw_avg = sum(st["raw"]) / len(st["raw"])
        aln_avg = sum(st["aln"]) / len(st["aln"])
        hist = {s: st["shifts"].count(s) for s in sorted(set(st["shifts"]))}
        hist_str = " ".join(f"{s:+d}:{c}" for s, c in hist.items())
        print(f" {st['name']:<6}{st['scored']:>8}{raw_avg:>9.1f}%{aln_avg:>12.1f}%"
              f"{st['alerts']:>9}   {hist_str}")
    print(" " + "-" * 70)
    print(f"{DIM} raw avg = no drift correction (old behaviour) · aligned avg = after +/-{args.max_shift} "
          f"tap search · shift usage = how often each shift best re-aligned a packet{RST}")


if __name__ == "__main__":
    main()
