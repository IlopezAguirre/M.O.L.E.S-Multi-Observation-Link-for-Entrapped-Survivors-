# Dashboard

Static, dependency-free dashboard. No build step, no CDN, works offline.

```bash
open frontend/index.html                      # simulator, no hardware needed
open "frontend/index.html?ws=ws://localhost:8000/ws"   # live backend
```

Without `?ws=` it runs a built-in simulator that emits the same snapshots a
backend would. **Thresholds tuned against the simulator are placeholders** until
real captures exist.

## Snapshot contract

The backend sends one JSON object per message (about 2 per second):

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
