#!/usr/bin/env python3
"""Write a SYNTHETIC MOLES capture (same CSV format as moles_monitor.py --record).

For rehearsing the display without hardware and for building the backend before real data exists.
The data is simulated: never present it as a measurement.

  python make_demo_capture.py data/demo.csv                       # module on 40-110 s, 180 s total
  python make_demo_capture.py data/demo_off.csv --on none         # module never on (quiet run)
  python make_demo_capture.py data/demo.csv --amps 2,1,0 --rate 0.33 --on 40-110,150-200 --seconds 240
Then:
  python run_pipeline.py data/demo.csv --plot --speed 5 --truth-on 40-110
"""
import argparse

import pups_dsp as D


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="output CSV")
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--on", default="40-110", help="module-on intervals 'a-b,c-d' in seconds, or 'none'")
    ap.add_argument("--amps", default="2,1,0", help="echo path amplitude (mm) per link A->B,A->C,B->C")
    ap.add_argument("--rate", type=float, default=0.33, help="module rate (Hz) for the simulation")
    ap.add_argument("--loss", type=float, default=0.02, help="ESP-NOW loss fraction")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    on = [] if a.on == "none" else [tuple(float(x) for x in part.split("-")) for part in a.on.split(",")]
    amps = tuple(float(x) for x in a.amps.split(","))
    pk = D.simulate_moles(seconds=a.seconds, amps=amps, f_hz=a.rate, loss=a.loss, seed=a.seed,
                          seq_start=4000, on=on)
    with open(a.out, "w") as f:
        f.write("seq,link_id,range_m,fp_idx," + ",".join(f"i{k}" for k in range(D.TAPS)) + ","
                + ",".join(f"q{k}" for k in range(D.TAPS)) + "\n")
        for p in pk:
            f.write(f"{p.seq},{p.link_id},{p.range_m!r},{p.fp_idx},"
                    + ",".join(str(int(v)) for v in p.cir.real) + ","
                    + ",".join(str(int(v)) for v in p.cir.imag) + "\n")
    print(f"wrote {len(pk)} synthetic packets ({a.seconds:.0f} s, module on {on or 'never'}) to {a.out}")


if __name__ == "__main__":
    main()
