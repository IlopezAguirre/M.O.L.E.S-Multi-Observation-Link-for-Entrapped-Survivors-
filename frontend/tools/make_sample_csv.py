#!/usr/bin/env python3
"""Write a synthetic host CSV for trying the dashboard without hardware.

    python3 make_sample_csv.py --seconds 75 > sample.csv

Line format: t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, ... I29, Q29
Six directed links with dropped rounds, timeouts and a wrapping seq. Test data only.
"""
import argparse
import math
import random

POD_POS = [(0.0, 0.0), (6.0, 0.0), (3.0, 5.2)]
SIGMA = 1.6
N_TAPS = 30
SIGNAL_TAPS = (10, 11, 12, 13)

def dist_to_segment(p, a, b):
    abx, aby = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / (abx * abx + aby * aby)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=float, default=75)
    ap.add_argument('--rate', type=float, default=0.2, help='breathing rate in Hz')
    ap.add_argument('--rig', default='4.6,0.9', help='rig position x,y in metres')
    ap.add_argument('--no-rig', action='store_true', help='leave the rig off (an empty-pile recording)')
    ap.add_argument('--signal', choices=['both', 'magnitude', 'phase'], default='both',
                    help='what the breathing-like signal moves in the signal taps')
    ap.add_argument('--first-id', type=int, default=0, help='id of the first pod (0 or 1)')
    ap.add_argument('--seq0', type=int, default=65400, help='starting seq; the default wraps at 65536')
    ap.add_argument('--drop', type=float, default=0.02, help='fraction of rounds never reported')
    ap.add_argument('--timeout', type=float, default=0.01, help='fraction of rounds reported as status 1')
    ap.add_argument('--header', action='store_true', help='write a header line first')
    ap.add_argument('--seed', type=int, default=1)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    rig = tuple(float(v) for v in args.rig.split(','))
    static = {}
    if args.header:
        cols = ['t_ms', 'rx_id', 'tx_id', 'seq', 'status', 'fp_index']
        cols += ['%s%d' % (c, i) for i in range(N_TAPS) for c in 'IQ']
        print(','.join(cols))

    slots = int(args.seconds * 1000 / 33)
    for s in range(slots):
        t_ms = s * 33
        tx = s % 3
        for rx in range(3):
            if rx == tx:
                continue
            if rng.random() < args.drop:
                continue
            seq = (args.seq0 + s) & 0xFFFF
            status = 1 if rng.random() < args.timeout else 0
            if status:
                taps = [0] * (2 * N_TAPS)
            else:
                d = dist_to_segment(rig, POD_POS[tx], POD_POS[rx])
                amp = 0.0 if args.no_rig else 30.0 * math.exp(-(d / SIGMA) ** 2)
                phase_t = 2 * math.pi * args.rate * (t_ms / 1000.0) + (tx * 2 + rx)
                taps = []
                for k in range(N_TAPS):
                    base, ph = static.setdefault((tx, rx, k), (rng.uniform(300, 2500), rng.uniform(0, 2 * math.pi)))
                    sig = k in SIGNAL_TAPS
                    mag = base + (amp * math.sin(phase_t) if sig and args.signal != 'phase' else 0.0)
                    if sig and args.signal != 'magnitude':
                        ph += 1.2 * (amp / 30.0) * math.sin(phase_t)
                    taps.append(int(mag * math.cos(ph) + rng.gauss(0, 8)))
                    taps.append(int(mag * math.sin(ph) + rng.gauss(0, 8)))
            print(','.join(str(v) for v in [t_ms, args.first_id + rx, args.first_id + tx, seq, status, 4] + taps))

if __name__ == '__main__':
    main()
