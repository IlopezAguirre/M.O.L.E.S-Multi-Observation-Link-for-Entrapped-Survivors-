# MOLES backend

Tiny read-only layer over the breathing-detection pipeline: SQLite for storage,
FastAPI to serve verdicts to the dashboard. No ORM, no auth, no Docker.

```
moles_monitor.py  --writes-->  moles.db  <--reads--  app.py (FastAPI)  <--polls--  frontend
```

The pipeline writes one row per 30-second window per link; the frontend polls
`/current` and `/history` every few seconds (verdicts only change every 30 s, so
polling is plenty — no websockets needed).

## Setup

```bash
# from the repo root, in your project venv
pip install -r backend/requirements.txt
cd backend
```

## 1. Create the database

```bash
python -c "import db; db.init_db()"        # creates moles.db + the windows table
```

## 2. Seed fake data (no hardware needed)

```bash
python seed_fake.py                        # inserts rows across TWO links: A->B and A->C
```

## 3. Start the server

```bash
uvicorn app:app --reload --port 8000
```

## 4. Hit the endpoints

```bash
curl "http://localhost:8000/links"                       # ["A->B","A->C"]
curl "http://localhost:8000/current?link_id=A->B"        # most recent A->B window
curl "http://localhost:8000/history?link_id=A->B&limit=120"  # oldest->newest for a chart
curl "http://localhost:8000/current"                     # most recent overall
```

Empty DB is handled gracefully: `/current` returns `null`, `/history` and
`/links` return `[]`.

## The `breathing` field (important)

Every row the API returns includes a derived boolean **`breathing`** on top of the
stored columns. It is **not** the pipeline's raw `detected` column. We verified
that `detected` currently latches onto slow ~0.1 Hz body/setup **drift** and reads
`0` on the real ~0.3 Hz breathing (which comes in short bursts). So the frontend
should show breathing from **`breathing`**, which is computed as:

```
freq_hz in [0.15, 0.50] Hz  AND  snr_db >= 18
```

Thresholds live in `db.py` (`BREATH_FREQ_MIN/MAX`, `BREATH_SNR_MIN`) — tune there.
The raw `detected` value is still returned unchanged if you want it.

## Files

| file | what it does |
|---|---|
| `db.py` | SQLite schema + `init_db()`, `insert_window(**fields)`, and `is_breathing()` |
| `app.py` | FastAPI: `GET /current`, `/history`, `/links`; CORS on; adds derived `breathing` |
| `seed_fake.py` | inserts fake rows across two links for testing multi-link before hardware |
| `requirements.txt` | fastapi + uvicorn |
| `moles.db` | the SQLite file (created at runtime; not committed) |

## Wiring it into the pipeline later (NOT done here)

This layer is additive — `moles_monitor.py` is untouched. When you're ready, the
one place to call `insert_window()` is wherever the monitor finalizes a 30-second
window and appends its row to `windows.csv`. Roughly:

```python
# in esp32-hand_test/moles_monitor.py, right after a window verdict is computed:
# import time, sys; sys.path.append("../backend"); import db
# db.insert_window(
#     timestamp=time.time(), link_id=name,      # name is already "A->B" etc.
#     freq_hz=peak_hz, snr_db=snr_db, detected=int(detected),
#     victim_tap=victim_tap, pct_valid=valid_frac * 100, methods_agree=int(stable),
# )
```

Leave it commented until you want the live link.
