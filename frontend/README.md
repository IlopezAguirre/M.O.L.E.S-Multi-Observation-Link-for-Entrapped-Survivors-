# Dashboard

Static, dependency-free dashboard. No build step, no CDN, works offline.

```bash
open frontend/index.html                                # simulator on
cd frontend && python3 -m http.server 8080              # or serve it (Web Serial wants localhost)
open "frontend/index.html?sim=false"                    # start with the simulator off
open "frontend/index.html?ws=ws://localhost:8000/ws"    # connect a WebSocket straight away
```

## Data sources

The **Data source** panel in the sidebar has a **Simulator On / Off** switch (it
remembers your choice). With it Off, the dashboard takes the host's real CSV
from any of three places, and everything below the source is identical:

| Source | How | Notes |
|---|---|---|
| **USB serial** | *Connect serial* | Web Serial: Chrome or Edge on a computer, opened from `localhost` or `https`. Set the baud to the firmware's (921600). Close the Arduino serial monitor first, since only one program can hold the port. |
| **CSV file** | *Load CSV file…* | A recorded run, replayed at 1×–10× (data time, so the 30 s window and persistence behave as they did live). |
| **WebSocket** | *Connect WebSocket* | The server sends either CSV lines (one or many per message) or whole JSON snapshots (see below). Reconnects automatically. |

The CSV is the firmware's contract from `ARCHITECTURE.md`, one line per report:

```
t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, I1, Q1, ... I(N-1), Q(N-1)
```

`pipeline.js` is a browser port of the laptop pipeline in `ARCHITECTURE.md`:
split by directed link, align on `seq`, subtract each tap's mean, keep the
highest-variance taps, 30 s FFT, peak in 0.1–0.5 Hz against the background.

- **Header lines, blank lines and `#` comments are skipped.** A malformed row is
  counted as rejected, not fatal; the counts show under the source buttons.
- **`N` (taps per report) is whatever the first row has.** A later row with a
  different count is rejected, so a firmware and parser mismatch shows up
  immediately rather than as garbage.
- **Pod ids:** the three lowest ids seen become A, B, C, so 0–2 and 1–3 both work.
- **`seq`** is unwrapped across its `uint16` wrap. A missing round is a gap
  (never shifted), and `status` 1 (timeout) or 2 (error) rows count as gaps. A
  long dropout or a `seq` that jumps back starts that link over.
- **Sample rate** is measured from `t_ms`, not assumed (10 Hz per link is the
  nominal figure); the 30 s window and bin width follow from it.
- **`fp_index` is read but not used.** Taps are chosen by variance, which is how
  the open item about the tap offset gets sidestepped for now.
- **Pod positions** are optional inputs (metres). Without them the layout is a
  placeholder and the estimated position is labelled relative.

`python3 tools/make_sample_csv.py --seconds 75 > sample.csv` writes a synthetic
firmware-format CSV (dropped rounds, timeouts, a wrapping `seq`) so the loader
can be tried without hardware. `--no-rig` makes an empty-pile recording for the
baseline. It is test data, not a recording.

Everything here is verified against synthetic data only. The serial path has
been exercised with a stand-in port, not real hardware. **Thresholds tuned
against the simulator are placeholders** until real captures exist.

## Snapshot contract

A WebSocket backend can skip the CSV and send one JSON object per message
(about 2 per second). This is also what the simulator and `pipeline.js` produce:

```json
{
  "t": 123.4,
  "source": "serial",
  "sample_rate": 10,
  "window_seconds": 30,
  "bin_hz": 0.0333,
  "band": [0.1, 0.5],
  "pods": { "A": [0, 0], "B": [6, 0], "C": [3, 5.2] },
  "window_fill": 1.0,
  "links": {
    "A>B": {
      "peak_hz": 0.2,
      "ratio": 6.3,
      "loss": 0.02,
      "spectrum": [0, 0.1, ...],
      "series": [0.1, null, -0.2, ...]
    }
  }
}
```

- `links` keys are `TX>RX`, six of them for three pods.
- `spectrum` has 31 values: bins 0..30, `bin_hz` apart, so 0–1 Hz.
- `series` is the selected tap, mean removed, `null` where a round was lost.
  Gaps are drawn as breaks, never bridged.
- `ratio` is band peak over baseline. The dashboard applies the detection-ratio
  slider itself, so the backend does not need to know the threshold.
- Detection is held off until `window_fill` reaches 1.
- A pair is confirmed only when both directions are above threshold.

- `pods` is optional: pod positions in metres (x right, y up), measured on site.
  It drives the mesh layout and the source dot. Without it the layout is a
  placeholder and the dot is labelled "relative".
- `sim_truth` is simulator-only (the true rig position). Never send it from a
  real backend.

## Source dot

`ARCHITECTURE.md` has no localization step: the firmware reads CIR, not ranges.
The dot is therefore an **estimate** fitted in the browser. A chest disturbs the
links that pass near it, so the dashboard finds the point whose distances to the
three link segments best explain how strongly each pair reacted
(`g = exp(-(d/sigma)^2)`, `sigma = 0.27 × mean side`). It needs `pods`, at least
one hit link, and is drawn solid when a pair is confirmed, dashed otherwise.
Against the simulator it lands within about 0.2–1.1 m; a real pile will differ,
so do not quote it as a measured position until it is checked on hardware.

## Frequency verification

Under the mesh, each pair (A–B, A–C, B–C) shows a verdict chip. **Corroborated**
needs three checks: (1) both directions above the hit threshold, inside the band
and within one FFT bin of each other (frequency is refined between bins with a
parabola on the log magnitudes); (2) that held for 30 s; (3) the peak is at
least 3× stronger than an **empty-pile baseline**, captured with the "Capture
baseline" button while nobody is there. A signal already in the baseline reads
"Also in baseline"; without a baseline the best verdict is "Persistent · no
baseline". Hover a chip for the numbers. Corroborated means a consistent
periodic signal that is new since the baseline. It does not confirm a person:
a fan or cable that started after the baseline would pass. It works from the
`spectrum` array already in the snapshot.

Changing this shape means telling whoever owns the pipeline.
