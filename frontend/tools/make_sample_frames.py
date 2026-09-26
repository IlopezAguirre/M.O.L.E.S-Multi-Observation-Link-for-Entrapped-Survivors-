#!/usr/bin/env python3
"""Write a synthetic capture in the host's binary serial format.

    python3 make_sample_frames.py --seconds 75 -o sample.bin
    python3 make_sample_frames.py --no-rig -o empty.bin

Frames are AA 55 | type | len | payload | xor. Type 1 is the sender MAC plus a
mole_pkt_t (link_id, seq, range_m, fp_idx, 56 I/Q taps), type 2 is a status
line. Links are 1 = A->B, 2 = A->C, 3 = B->C at 10 Hz. Test data only.
"""
import argparse
import cmath
import math
import random
import struct
import sys

POD_POS = [(0.0, 0.0), (6.0, 0.0), (3.0, 5.2)]
LINKS = {1: (0, 1), 2: (0, 2), 3: (1, 2)}
SIGMA = 1.6
TAPS = 56
LOS = 100
VICTIM = LOS + 14
MAC = bytes([0x20, 0x50, 0x0D, 0xE4, 0x46, 0x40])
PKT = struct.Struct(f'<BBHfH{TAPS}h{TAPS}h')

def seg_dist(p, a, b):
    abx, aby = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / (abx * abx + aby * aby)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby))

def frame(ftype, payload):
    head = bytes([ftype, len(payload) & 0xFF, len(payload) >> 8])
    x = 0
    for b in head + payload:
        x ^= b
    return b'\xAA\x55' + head + payload + bytes([x])

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=float, default=75)
    ap.add_argument('--rate', type=float, default=0.2, help='breathing rate in Hz')
    ap.add_argument('--rig', default='4.6,0.9', help='rig position x,y in metres')
    ap.add_argument('--no-rig', action='store_true', help='leave the rig off (an empty-pile capture)')
    ap.add_argument('--static-fraction', type=float, default=0.0,
                    help='static energy in the moving echo\'s tap, as a fraction of its moving part (0 = magnitude blind)')
    ap.add_argument('--jitter', type=float, default=0.3, help='chance the first-path index reads one tap off')
    ap.add_argument('--noise', type=float, default=12.0, help='CIR noise, per I and Q')
    ap.add_argument('--drop', type=float, default=0.02, help='fraction of polls never received')
    ap.add_argument('--timeout', type=float, default=0.01, help='fraction of polls where B never answered')
    ap.add_argument('--corrupt', type=float, default=0.003, help='fraction of frames with a flipped byte')
    ap.add_argument('--seq0', type=int, default=65440, help='starting seq; the default wraps at 65536')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('-o', '--out', default='-', help='output file (default stdout)')
    args = ap.parse_args()

    rng = random.Random(args.seed)
    rig = tuple(float(v) for v in args.rig.split(','))
    total = 2 * 96 + 32
    fixed = {}

    def channel(link):
        if link in fixed:
            return fixed[link]
        static = [0j] * total
        static[LOS] = cmath.rect(3000, rng.uniform(0, 2 * math.pi))
        for off, amp in [(2, 900), (5, 700), (9, 500), (20, 380), (27, 300), (35, 220), (41, 160)]:
            static[LOS + off] = cmath.rect(amp * rng.uniform(0.7, 1.2), rng.uniform(0, 2 * math.pi))
        fixed[link] = (static, rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi))
        return fixed[link]

    out = sys.stdout.buffer if args.out == '-' else open(args.out, 'wb')
    polls = int(args.seconds * 10)
    sent = drops = 0
    for k in range(polls):
        t = k * 0.1
        for link, (tx, rx) in LINKS.items():
            seq = (args.seq0 + k) & 0xFFFF
            if rng.random() < args.drop:
                drops += 1
                continue
            timeout = rng.random() < args.timeout
            if timeout:
                pkt = PKT.pack(0x4D, link, seq, float('nan'), 0xFFFF, *([0] * (2 * TAPS)))
            else:
                static, theta0, phi = channel(link)
                cir = list(static)
                if not args.no_rig:
                    g = math.exp(-(seg_dist(rig, POD_POS[tx], POD_POS[rx]) / SIGMA) ** 2)
                    moving = 260.0 * g
                    ang = theta0 + math.radians(78.0) * math.sin(2 * math.pi * args.rate * t + phi)
                    cir[VICTIM] += cmath.rect(moving, ang) + cmath.rect(moving * args.static_fraction, theta0)
                    cir[VICTIM + 1] += cmath.rect(0.4 * moving, ang + 0.4)
                psi = cmath.rect(1.0, rng.uniform(0, 2 * math.pi))
                fp = LOS + (rng.choice((-1, 1)) if rng.random() < args.jitter else 0)
                win = cir[fp - 8: fp - 8 + TAPS]
                i_vals, q_vals = [], []
                for v in win:
                    v = v * psi + complex(rng.gauss(0, args.noise), rng.gauss(0, args.noise))
                    i_vals.append(max(-32768, min(32767, round(v.real))))
                    q_vals.append(max(-32768, min(32767, round(v.imag))))
                rng_m = 0.34 + 0.0002 * t + rng.gauss(0, 0.004)
                pkt = PKT.pack(0x4D, link, seq, rng_m, fp, *i_vals, *q_vals)
            raw = bytearray(frame(0x01, MAC + pkt))
            if rng.random() < args.corrupt:
                raw[rng.randrange(5, len(raw) - 1)] ^= 0x5A
            if rng.random() < 0.002:
                out.write(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 20))))
            out.write(bytes(raw))
            sent += 1
        if k % 10 == 9:
            out.write(frame(0x02, f'ok pkts={sent} drops={drops}'.encode()))
    if out is not sys.stdout.buffer:
        out.close()
    sys.stderr.write(f'wrote {sent} packets over {args.seconds:.0f} s ({drops} dropped, seq0={args.seq0})\n')

if __name__ == '__main__':
    main()
