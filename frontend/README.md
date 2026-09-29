# Dashboard

React (Vite) app. It does no signal processing of its own — it only polls the
backend's read-only API and renders whatever it returns.

```bash
cd backend && uvicorn app:app --port 8000     # backend, from repo root's venv
cd frontend && npm install && npm run dev     # dashboard, http://localhost:5173
```

Build for deploy with `npm run build` (outputs `dist/`).

API base defaults to `http://localhost:8000`; override with `?api=http://host:port`.

## What it shows

Everything comes from three endpoints (see `../backend/app.py` and
`../backend/db.py` for the schema):

- `GET /links` — the distinct `link_id`s that exist (e.g. `["A->B", "A->C"]`).
- `GET /current?link_id=...` — the most recent window row for a link, plus a
  derived `breathing` boolean.
- `GET /history?link_id=...&limit=...` — the last N window rows, oldest first.

Polled every 3 s (`src/useMolesData.js`). One row = one 30 s window for one
directed link: `freq_hz`, `bpm`, `snr_db`, `detected` (the pipeline's raw
flag), `victim_tap`, `pct_valid`, `methods_agree`.

## Layout

- **Frequency & SNR history** (`src/FrequencyHistory.jsx`) — a per-link tab
  selector plus two stacked real-history charts (`freq_hz` and `snr_db` over
  the link's `/history` window rows), with the breathing band shaded and the
  SNR threshold marked. This is **not** a live spectrum: the backend only
  ever stores one peak `freq_hz`/`snr_db` per 30 s window, never the full FFT
  bin array, so there is no per-bin data to plot — this is the closest
  honest equivalent, styled after the pipeline's own matplotlib peak/SNR
  annotation (`esp32-hand_test/moles_monitor.py`'s `FFTPlot`).
- **Pairs** (`src/Pairs.jsx`) — groups a link with its reverse direction when
  both exist and shows whether both, one, or neither currently reads
  breathing.
- **KPIs** (`src/Kpis.jsx`) — links currently breathing, session hit count,
  peak SNR, average valid-sample percentage. Session-only; nothing is
  persisted, so a page reload resets the counters (the backend/database is
  the source of truth for history).
- **Link detail** (`src/LinkDetail.jsx`) — full row for the selected link,
  plus a sparkline of `freq_hz` over its history with the breathing band
  shaded.
- **Detection log** (`src/DetectionLog.jsx`) — a client-side log of
  breathing-state rising edges (false → true) seen while this tab has been
  open. Reset with the sidebar's "Reset log".
- **Thresholds** (`src/Sidebar.jsx`) — displays the band/SNR/window
  constants the backend uses (`backend/db.py`'s `BREATH_FREQ_MIN/MAX` and
  `BREATH_SNR_MIN`). Not adjustable here — the verdict is computed
  server-side.

## What this dashboard does *not* do

The dashboard used to run its own pipeline in-browser: decoding raw CIR
frames from USB serial, a capture file, or a WebSocket; doing phase/magnitude
extraction and a 30 s FFT; and a source-position estimate fit from per-link
ratios (a triangulated dot drawn on a pod mesh diagram). That code (`app.js`,
`charts.js`, `frames.js`, `pipeline.js`, `sim.js`) has been removed along
with the FFT-bins chart, the range/tap chart, the pod mesh diagram, the
source-position dot, and the data-source picker — none of that data exists
in the backend's schema (no spectrum, no taps, no ratios, no raw source), so
there was nothing honest to wire it to. `FrequencyHistory.jsx` is the
real-data replacement for the mesh/FFT panel.

The real signal pipeline still exists — it just runs on the host laptop, not
in the browser: `esp32-hand_test/moles_monitor.py --db` writes verdicts
straight into `backend/moles.db`, which this dashboard reads via the API.
If in-browser raw-capture visualization is needed again, it would need the
backend (or a new endpoint) to expose spectrum/tap data — it isn't there today.
