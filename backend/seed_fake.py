"""Seed the DB with realistic fake window rows across TWO links.

Lets you confirm /links and per-link filtering work with more than one link
before the hardware exists. Run:  python seed_fake.py

Note on the values: some rows are ~0.3 Hz breathing with detected=0 on purpose —
that's the real breath the pipeline's raw flag misses. The API's derived
`breathing` field catches those via freq+SNR, while the steady ~0.1 Hz drift rows
(detected=1) correctly read breathing=false.
"""

import random
import time

import db

# (link_id, [ (freq_hz, snr_db, raw_detected), ... ] newest last)
PLAN = [
    ("A->B", [(0.11, 26.0, 1), (0.12, 25.0, 1), (0.33, 24.0, 0),
              (0.32, 23.0, 0), (0.10, 27.0, 1)]),
    ("A->C", [(0.10, 20.0, 1), (0.30, 22.0, 0), (0.31, 21.0, 0), (0.11, 19.0, 1)]),
]


def main():
    db.init_db()
    now = time.time()
    n = 0
    for link_id, seq in PLAN:
        for i, (freq_hz, snr_db, detected) in enumerate(seq):
            db.insert_window(
                timestamp=now - (len(seq) - i) * 30.0,   # 30 s apart, ending "now"
                link_id=link_id,
                freq_hz=freq_hz,
                snr_db=snr_db,
                detected=detected,
                victim_tap=random.randint(9, 16),
                pct_valid=round(random.uniform(95.0, 100.0), 1),
                methods_agree=detected,
            )
            n += 1
    links = [p[0] for p in PLAN]
    print(f"seeded {n} rows across {len(links)} links: {links}")
    print("try:  GET /links  ->", links)
    print("      GET /current?link_id=A->B  (most recent row, with derived 'breathing')")


if __name__ == "__main__":
    main()
