# Dashboard

Static page. No build step, no dependencies, works offline.

```bash
open frontend/index.html
cd frontend && python3 -m http.server 8080     # or serve it; Web Serial needs localhost
```

URL options: `?sim=false` starts with the simulator off, `?ws=ws://host:port/ws`
connects a WebSocket straight away.

## Data sources

The sidebar has a **Simulator On / Off** switch. With it off, data comes from:

- **USB serial**: Chrome or Edge, from `localhost` or https. Baud defaults to
  921600. Format is binary frames (default) or CSV text. Close the Arduino serial
  monitor first, since only one program can hold the port.
- **Capture file**: a dump of the host's binary stream, or a CSV. Detected
  automatically and replayed at 1x to 10x.
- **WebSocket**: JSON snapshots, CSV lines, or raw binary frames.

Pod positions (metres) are optional. Without them the layout is a placeholder and
the estimated position is labelled relative.

## Input formats

**Binary frames** from the host ESP32: `AA 55 | type | len_lo len_hi | payload | xor`,
with the xor over type, both length bytes and the payload. Type 1 is the sender
MAC (6 bytes) plus `mole_pkt_t` (234 bytes, little endian): magic `0x4D`, `link_id`,
`seq` (u16), `range_m` (f32), `fp_idx` (u16), 56 I values, 56 Q values. `link_id`
1 is A->B, 2 is A->C, 3 is B->C. Type 2 is a status line. Packets carry no
timestamp: live data uses arrival time, a file uses file order at the poll rate.
If `mole_pkt_t` changes, change `frames.js` to match.

**CSV** (from `ARCHITECTURE.md`): `t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, ...`
with six directed links. Header lines and comments are skipped, and a bad row is
counted as rejected.

## Processing

`pipeline.js` turns rows into the snapshots the dashboard draws:

1. Split by link. `seq` is unwrapped across its u16 wrap. A missing round, a
   timeout (NaN range) or a failed CIR (`fp_idx` 0xFFFF) is a gap, never a shift.
2. Align each window to a running reference, up to 2 taps either way, because the
   first-path index can flip by a tap between packets.
3. Take a signal per tap:
   - **Phase** (default): `angle(tap * conj(first-path tap))`, unwrapped and
     detrended. The two moles' clocks are not synced, so each packet has a random
     phase common to all its taps, and measuring against the first path cancels
     it. The first-path tap is the strongest of window taps 8 to 11; for a CSV it
     is the strongest tap.
   - **Magnitude**: `|tap|`.

   Switching re-measures what is already buffered.
4. Choose taps. Phase mode drops taps whose phase against the first path is not
   stable, since a tap with nothing in it has a random phase. Both modes keep the
   highest-variance taps, then the three with the most 0 to 1 Hz power.
5. 30 s FFT, then the peak in 0.1 to 0.5 Hz against the median of the bins above.
   The sample rate is measured, about 10 Hz per link.

## Links and agreement

The firmware has three one-way links, so there is no reverse direction to check.
A link **agrees** when another link is a hit at the same frequency, within one
FFT bin. With two-way links (the simulator, or a six-link CSV) it agrees when its
reverse link is also a hit. The mesh draws whichever links the source provides.

## Detection

A link is a hit when its peak is inside the band and its ratio is at least the
threshold (4.0x, adjustable), with hysteresis at 80%. Nothing is judged until the
30 s window is full. The frequency is refined between FFT bins with a parabola on
the log magnitudes, so 0.25 Hz does not read 0.233 or 0.267.

Each pair (or link, for one-way links) gets a verdict. **Corroborated** needs
three things: the links agree, that has held for 30 s, and the peak is at least 3x
stronger than an empty-pile baseline captured with nobody there. Without a
baseline the best verdict is "Persistent · no baseline". A signal already in the
baseline reads "Also in baseline". This does not confirm a person: a fan or cable
that starts after the baseline would pass too.

The red dot is a model fit, not a measurement. It finds the point inside the
triangle whose distances to the links best explain how strongly each reacted
(`g = exp(-(d/sigma)^2)`, `sigma = 0.27 x mean side`). It needs pod positions.

## Range

`range_m` has its own trace under the FFT, drawn as the change from the window
mean because the antenna delay is uncalibrated. Rounds where B did not answer are
gaps. It only appears for sources that carry a range.

## Snapshot contract

A WebSocket backend can send one JSON object per message instead of raw data.
This is also what the simulator and `pipeline.js` produce.

```json
{
  "t": 123.4,
  "source": "serial",
  "signal": "phase",
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
      "spectrum": [0, 0.1],
      "series": [0.1, null, -0.2],
      "range": [0.34, null, 0.35]
    }
  }
}
```

- `links` holds only the links that exist, keyed `TX>RX`.
- `spectrum` has 31 values, bins 0 to 30 at `bin_hz`, so 0 to 1 Hz.
- `series` and `range` use `null` for a lost round, drawn as a break.
- `ratio` is band peak over background. The dashboard applies the threshold.
- `pods`, `signal`, `range`, and per-link `tap` and `ref_tap` are optional.
- `sim_truth` is simulator only.

## Test data

`tools/make_sample_frames.py` writes a synthetic capture in the binary format
(first-path jitter, random common phase, dropped rounds, NaN ranges, wrapping
`seq`, corrupted frames). `tools/make_sample_csv.py` writes the six-link CSV.
Both are models, not recordings. The frames generator moves phase much more than
magnitude, so it favours the Phase signal by construction.

## Status

Checked against generated data only: the decoder over different chunk sizes,
phase against magnitude, alignment on and off, and the serial (with a stand-in
port), file and WebSocket paths. Not checked: real serial hardware, real
captures, or how real chest motion looks in phase.

Assumed and worth confirming: the first path sits at window tap 8, `seq` steps by
one per poll per link, the poll rate is 10 Hz, and arrival time is steady enough
for live timing. Thresholds are placeholders until there are real captures.
Phase to millimetres, a rolling reference in place of the baseline, and the node
flashing red are not done.
