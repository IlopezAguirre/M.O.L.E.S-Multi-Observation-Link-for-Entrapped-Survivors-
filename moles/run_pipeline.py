#!/usr/bin/env python3
"""Replay raw MOLES captures through the DSP (all links on one clock, combined decision).

Captures come from:  python moles_monitor.py --record data/run1.csv   (or make_demo_capture.py)

  python run_pipeline.py data/run1.csv                        # per-update results + summary
  python run_pipeline.py data/run1.csv --every 10             # print every 10th update
  python run_pipeline.py data/run1.csv --plot --speed 5       # watch the FFT display, 5x real time
  python run_pipeline.py data/run1.csv --save-png out.png     # render the final display to a file
  python run_pipeline.py data/run1.csv --json out.jsonl       # backend rows (same as live --json)
  python run_pipeline.py data/run1.csv --bypass hampel        # gate G5: same data, stage off
  python run_pipeline.py --compare data/off.csv data/on.csv   # gates G1 + G2 on real data

The raw file is never modified. Every report states which stages were active.
"""

import argparse
import csv
import json
import sys
import time
from collections import Counter

import numpy as np

import pups_dsp as D


def load_csv(path: str) -> list:
    """Raw capture -> [Packet, ...] in file (arrival) order. Accepts v1 or v2 columns."""
    pkts = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            cir = (np.array([float(row[f"i{k}"]) for k in range(D.TAPS)])
                   + 1j * np.array([float(row[f"q{k}"]) for k in range(D.TAPS)]))
            opt = lambda key: int(row[key]) if row.get(key) not in (None, "") else None
            pkts.append(D.Packet(seq=int(row["seq"]), link_id=int(row["link_id"]),
                                 range_m=float(row["range_m"]), fp_idx=int(row["fp_idx"]), cir=cir,
                                 t_us=opt("t_us"), fp_idx_q6=opt("fp_idx_q6"),
                                 acc_count=opt("acc_count"), flags=opt("flags")))
    return pkts


def make_cfg(a) -> D.DSPConfig:
    return D.DSPConfig(window_s=a.window, update_s=a.update, pnr_margin_db=a.margin,
                       combined_margin_db=a.combined_margin, interpolate=a.interp,
                       gate_pct=a.gate, dead_tap_mode=a.deadtap, bypass=frozenset(a.bypass or []))


def process(path, cfg, every=0, plot=None, speed=0.0, jfile=None, run_id=None):
    """Run one capture through MolesDSP. Returns (results, moles)."""
    pkts = load_csv(path)
    moles = D.MolesDSP(cfg)
    results = []
    t_wall0 = time.time()
    for p in pkts:
        moles.push(p)
        r = moles.maybe_evaluate()
        if r is None:
            continue
        results.append(r)
        if every and (len(results) - 1) % every == 0:
            print(r.line())
        if jfile is not None:
            jfile.write(json.dumps(r.to_json(run_id=run_id)) + "\n")
        if plot is not None:
            if speed > 0:                                   # pace the replay
                wait = t_wall0 + r.t_end / speed - time.time()
                if wait > 0:
                    time.sleep(wait)
            plot.update(r)
    return results, moles


def summarize(label, results, moles, cfg) -> dict:
    live = [r for r in results if r.tier != "warming"]
    n = max(len(live), 1)
    tiers = Counter(r.tier for r in live)
    det = [r for r in live if r.tier in ("CONFIRMED", "DETECTED")]
    print(f"\n== {label}: {len(results)} updates ({len(live)} after warm-up), "
          f"combined margin {cfg.combined_margin_db:.0f} dB")
    print(f"   combined  CONFIRMED {100 * tiers['CONFIRMED'] / n:5.1f}%   DETECTED {100 * tiers['DETECTED'] / n:5.1f}%"
          f"   none {100 * tiers['none'] / n:5.1f}%")
    if det:
        f = np.array([r.peak_hz for r in det])
        print(f"   peak      {np.median(f):.3f} Hz ({60 * np.median(f):.1f}/min), spread {np.ptp(f):.3f} Hz "
              f"(resolution {1 / cfg.window_s:.3f} Hz)")
    for lid, lp in sorted(moles.links.items()):
        rs = [r.links.get(lid) for r in live if r.links.get(lid) is not None]
        name = D.LINK_NAMES.get(lid, str(lid))
        if not rs:
            print(f"   {name:5s}     no evaluated windows")
            continue
        agree = sum(lid in r.agreeing for r in live) / n
        excl = Counter(r.excluded.get(lid) for r in live if lid in r.excluded)
        print(f"   {name:5s}     median PNR {np.nanmedian([w.pnr_db for w in rs]):5.1f} dB   agrees "
              f"{100 * agree:5.1f}%   valid {100 * np.mean([w.valid_frac for w in rs]):5.1f}%   "
              f"demod {dict(Counter(w.mode for w in rs))}   masked {dict(lp.rejects) or 'none'}"
              + (f"   excluded {dict(excl)}" if excl else ""))
    return {"rate": len(det) / n, "tiers": tiers, "live": len(live)}


def main():
    ap = argparse.ArgumentParser(description="Replay raw MOLES captures through the DSP")
    ap.add_argument("capture", nargs="?", help="CSV from moles_monitor.py --record")
    ap.add_argument("--compare", nargs=2, metavar=("OFF_CSV", "ON_CSV"),
                    help="module-off vs module-on captures (gates G1 + G2)")
    ap.add_argument("--window", type=float, default=30.0, help="analysis window T (s)")
    ap.add_argument("--update", type=float, default=1.0, help="evaluate every (s)")
    ap.add_argument("--margin", type=float, default=16.0, help="per-link margin (dB)")
    ap.add_argument("--combined-margin", type=float, default=12.0, help="combined-spectrum margin (dB)")
    ap.add_argument("--interp", action="store_true", help="opt-in short-gap interpolation (flagged)")
    ap.add_argument("--gate", type=float, default=None, help="disturbance gate on weighted dev (%%)")
    ap.add_argument("--deadtap", choices=["weight", "hard", "off"], default="weight")
    ap.add_argument("--bypass", action="append", choices=list(D.STAGES), help="bypass a stage (repeatable)")
    ap.add_argument("--every", type=int, default=1, help="print every Nth update (0 = summary only)")
    ap.add_argument("--plot", action="store_true", help="show the live FFT display while replaying")
    ap.add_argument("--speed", type=float, default=0.0, help="with --plot: replay speed (0 = as fast as possible)")
    ap.add_argument("--save-png", metavar="FILE", help="render the final display to FILE (no window)")
    ap.add_argument("--ref-hz", type=float, default=None, help="independently measured module rate (reference line)")
    ap.add_argument("--truth-on", default=None, help="known module-on intervals, e.g. '40-110,150-200' (display only)")
    ap.add_argument("--json", metavar="FILE", help="write one JSON row per update (backend feed)")
    a = ap.parse_args()

    if not a.capture and not a.compare:
        ap.error("give a capture file or --compare OFF_CSV ON_CSV")
    cfg = make_cfg(a)
    active = [s for s in D.STAGES if s not in cfg.bypass]
    print(f"stages active: {', '.join(active)}   bypassed: {', '.join(sorted(cfg.bypass)) or 'none'}")
    print(f"band {cfg.f_low:.3f}-{cfg.f_high:.2f} Hz (T={cfg.window_s:.0f}s, fs={cfg.fs:.0f}Hz), link margin "
          f"{cfg.pnr_margin_db:.0f} dB, combined margin {cfg.combined_margin_db:.0f} dB")

    if a.compare:
        off = summarize("OFF", *process(a.compare[0], cfg), cfg)
        on = summarize("ON ", *process(a.compare[1], cfg), cfg)
        g1 = off["rate"] == 0.0
        g2 = on["rate"] >= 0.9 and g1
        print("\n== gates (combined decision)")
        print(f"   G1 off: false-detection updates {100 * off['rate']:.1f}%   [{'PASS' if g1 else 'FAIL'}]")
        print(f"   G2 on : detected {100 * on['rate']:.1f}% "
              f"(CONFIRMED {100 * on['tiers']['CONFIRMED'] / max(on['live'], 1):.1f}%)   "
              f"[{'PASS' if g2 else 'CHECK'}]")
        print("   Compare the detected frequency with the module's independently measured rate.")
        return

    plot = None
    if a.plot or a.save_png:
        import moles_plot
        truth = []
        if a.truth_on:
            for part in a.truth_on.split(","):
                lo, hi = part.split("-")
                truth.append((float(lo), float(hi)))
        plot = moles_plot.MolesPlot(cfg, ref_hz=a.ref_hz, backend=None if a.plot else "Agg",
                                    truth_on=truth, title=f"M.O.L.E.S. — replay {a.capture}")
    jfile = open(a.json, "w") if a.json else None
    results, moles = process(a.capture, cfg, a.every, plot, a.speed if a.plot else 0.0, jfile,
                             run_id=a.capture)
    if jfile:
        jfile.close()
    summarize("RUN", results, moles, cfg)
    if plot is not None and a.save_png:
        plot.save(a.save_png)
        print(f"\nsaved display to {a.save_png}")
    if plot is not None and a.plot:
        print("\nclose the plot window to exit")
        plot.plt.ioff()
        plot.plt.show()


if __name__ == "__main__":
    sys.exit(main())
