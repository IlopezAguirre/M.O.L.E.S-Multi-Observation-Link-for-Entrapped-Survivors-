# UWB Rubble Mesh

A mesh of UWB radio nodes scattered across rubble that detects the breathing
motion of someone trapped underneath. Bistatic sensing: one node transmits, the
others listen and read the channel impulse response. A chest moving 12 times a
minute changes some CIR taps; an FFT finds the rhythm.

ShellHacks 2026, FIU.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then set SERIAL_PORT
```

Firmware is built in the Arduino IDE with the Makerfabs ESP32 UWB DW3000
library. Override `SS=5`, `RST=27`, `IRQ=34` in the example sketches.

## Layout

```
.
├── firmware/
│   ├── pod/            same sketch on all 3 pods, only MY_ID differs
│   └── host/           runs the slot schedule, prints CSV to serial
├── analysis/
│   ├── reader.py       serial -> parsed reports
│   ├── pipeline.py     mean removal, tap selection, FFT
│   └── plots.py        the three judge-facing charts
├── data/               captured runs (committed — this is our evidence)
├── hardware/
│   └── puck_generator.py
└── docs/
```

## Read this before debugging

**Baud rate.** Default 115200 is about 11.5 KB/s. A CSV line carrying 30 I/Q
taps runs roughly 400 bytes, two receivers report per slot, and slots run at
~30/sec. That's ~24 KB/s — over double the budget. Use **921600** on both the
host `Serial.begin()` and in `.env`, or cut taps per report. Symptom if you get
this wrong: dropped lines and phantom gaps in `seq` that look like radio
problems but aren't.

**No SPI inside the ESP-NOW callback.** It runs in the Wi-Fi task. Set a flag,
handle it in `loop()`.

**First-path amplitude is the wrong signal.** Read a later CIR tap — one that
reflects a path through the debris. Use `dwt_readaccdata()`, not the scalar
diagnostics or ranging output.

**CIR returns all zeros?** Known DW3000 clock-enable-bit issue. Confirm
`dwt_configciadiag()` ran. Check this on day one, not at 2am.

**Window length is 30 seconds, not 15.** Frequency resolution is `1 / window`.
At 30s the bins are 0.033 Hz wide and 0.1-0.5 Hz spans bins 3-15, with the
0.2 Hz target landing in bin 6. At 15s the bins double to 0.067 Hz, 0.1 Hz
falls into bin 1 right against DC where drift lives, and you lose the ability
to separate breathing from slow thermal wander.

## Go / no-go

Phase 2 decides whether this project works. Two labeled 60-second runs on one
fixed two-node link — actuator off, then on — overlaid, plus an FFT from 0 to
1 Hz with a marker at 0.2 Hz. Then a control at 0.5 Hz; the peak must move.

Nothing else gets built until that passes.

## Claiming results

Only claim what's been measured. "We detected a 0.2 Hz signal through the pile
on link A to B" is true and defensible. "We detect breathing under rubble" is
not yet. See CLAUDE.md.

## Backend (live API + dashboard)

A small read-only layer over the pipeline: the monitor writes each 30 s window
verdict to SQLite, FastAPI serves it, and a live page polls it.

```
moles_monitor.py --writes--> backend/moles.db <--reads-- app.py (FastAPI) <--polls-- frontend/live.html
```

FastAPI + uvicorn come from `requirements.txt`. SQLite is stdlib. To run it:

```bash
# 1. fake data across two links (no hardware); also creates the db
cd backend && python3 seed_fake.py

# 2. serve the API (creates the db if missing)
uvicorn app:app --port 8000

# 3. serve the dashboard, then open http://127.0.0.1:8137/live.html
cd ../frontend && python3 -m http.server 8137
```

Feed **real** verdicts by running the monitor with `--db` (writes to
`../backend/moles.db`, the file the API reads):

```bash
cd esp32-hand_test && python3 moles_monitor.py --db
```

Endpoints (JSON): `GET /current?link_id=A->B`, `GET /history?link_id=A->B&limit=120`
(oldest→newest), `GET /links`. An empty db returns `null` / `[]`.

**The `breathing` field.** Every row the API returns includes a derived
`breathing` boolean — **not** the pipeline's raw `detected` column, which latches
onto the slow ~0.1 Hz drift. It is true when `freq_hz` is in `[0.15, 0.50]` Hz
**and** `snr_db >= 18`. The dashboard should key off `breathing`. Thresholds live
in `backend/db.py`.

Files: `backend/db.py` (schema + `init_db` + `insert_window` + `is_breathing`),
`backend/app.py` (the API), `backend/seed_fake.py` (test data),
`frontend/live.html` + `live.js` (the polling page).
