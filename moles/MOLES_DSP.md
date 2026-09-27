# M.O.L.E.S. — DSP + FFT Output

The per-link pipeline (stages 1–13, `PUPS_PIPELINE.md`) is unchanged. MOLES adds a system layer on top: every link on one clock, one combined decision, a live display, alerts, and a backend feed.

**Status:** all synthetic tests pass, and the live path works end to end over a simulated serial link. **Nothing here has been verified on hardware yet.**

## What changed from PUPS

| | PUPS | MOLES |
|---|---|---|
| Timebase | each link's own counter | **shared beacon cycle** (`seq`); one clock for all links, 16-bit wrap handled |
| Windows | each link evaluated on its own | **all links evaluated on the same 30 s window**, at the last *complete* cycle (the A→C and B→C packets of the newest cycle may still be in flight) |
| Decision | per link | **tiered combined decision** (below); links with invalid windows are left out of combining |
| Output | terminal lines | terminal + **live FFT display** + **ALERT** to the host + **JSON rows** for the backend |

Code: `MolesDSP` and `MolesResult` in `pups_dsp.py` (the file name is kept so imports don't break), plus `moles_plot.py` for the display. **The `moles/` folder is the current version**; the copies in `pups/` are older.

## Decision rules

Once per second, over the last 30 s:

| Tier | Meaning |
|---|---|
| **CONFIRMED** | Combined spectrum peak ≥ 12 dB above its noise, the same peak for 3 consecutive updates, and **≥ 2 links** peak at that frequency |
| **DETECTED** | Same, but only **1 link** sees it. This is normal when the other beams' paths miss the source |
| **none** | Below margin or not stable |
| **warming** | The window or the stability history is still filling (~33 s after start) |

- No frequency is assumed. The search covers 3/T to fs/2 (0.1–5 Hz), and the **reported peak** is the answer.
- The per-link margin stays at 16 dB. The combined margin is 12 dB because the combined spectrum is quieter (3-link quiet max ≈ 8.4 dB in simulation).
- **Alert mask:** the moles on the agreeing links (bit 0 = A, 1 = B, 2 = C). For example, A→B and A→C agreeing gives mask 7, so all three blink.

## Timing to expect (simulation)

Module switched on at 40 s and off at 110 s:

- **Detection appears ~9–10 s after ON.**
- **It clears ~21–24 s after OFF.** The 30 s window still contains motion until it slides past.
- 0 false detections before ON.

Tell the judges about this lag. It's the price of the 30 s window and the 3-update stability check.

## Running it

**Live (exhibit):**
```bash
python moles_monitor.py --port /dev/cu.usbserial-0001 --dsp --plot --alert \
    --record data/run1.csv --json data/run1.jsonl --ref-hz 0.33
```
- `--dsp` gives one MOLES line per second. Per-packet lines are off; add `--packets` to see them.
- `--plot` opens the live display. `--alert` makes detecting moles blink.
- `--record` writes raw CSV (always do this). `--json` writes backend rows.
- `--ref-hz` draws the **independently measured** module rate as a reference line only; the detector never searches for it.

**Replay a recording:**
```bash
python run_pipeline.py data/run1.csv                          # updates + summary
python run_pipeline.py data/run1.csv --plot --speed 5         # watch it at 5x
python run_pipeline.py data/run1.csv --save-png run1.png --truth-on 40-110
python run_pipeline.py --compare data/off.csv data/on.csv     # gates G1 + G2
```
For `--compare`, the ON capture should have the module running **the whole time**. `--truth-on` shades when you know the module was on (display only).

**Rehearse without hardware (synthetic, never present as data):**
```bash
python make_demo_capture.py data/demo.csv --on 40-110
python run_pipeline.py data/demo.csv --plot --speed 5 --truth-on 40-110 --ref-hz 0.33
```

## Reading the display

- **Top: spectra.** dB above each spectrum's own noise, so links are comparable. Thin lines are the links, thick black is combined. The dashed line is the 16 dB link margin; the dotted line is the 12 dB combined margin. The grey band is below the resolvable band. The peak is labeled in Hz and per minute.
- **Middle: decision timeline.** Combined PNR (black) and each link's PNR (thin) over the last 4 minutes. The background is red for CONFIRMED, orange for DETECTED and blue while warming. Green hatching shows known module-on time. **This panel shows the judges exactly when detection starts and stops.**
- **Bottom left: triangle.** Red sides are links agreeing on the peak (thicker = stronger), orange means a link sees a different peak, grey means nothing, dotted means warming or invalid. A red mole is told to blink.
- **Bottom right:** the banner plus a per-link table (state, peak, PNR, valid %, demod mode).

## Backend contract (`--json`, one row per second)

```json
{
  "run_id": "20260927-143000", "wall_time": 1790000000.0,
  "t_end": 90.0, "cycle_end": 4900,
  "tier": "CONFIRMED", "peak_hz": 0.333, "pnr_db": 22.3,
  "agreeing_links": ["A->B", "A->C"], "n_links": 3, "alert_mask": 7,
  "links": {
    "A->B": {"link_id": 1, "detected": true, "peak_hz": 0.333, "pnr_db": 27.2,
             "amp_p2p": 24.1, "amp_units": "deg", "tap": 2, "mode": "phase",
             "valid_frac": 0.99, "filled_frac": 0.0, "outliers": 3, "estimator": "lomb",
             "stable": true, "flags": ["tap_phase_demod"], "excluded": null},
    "B->C": {"link_id": 3, "state": "warming"}
  },
  "pipeline_version": "moles-dsp-1"
}
```

Rules for the backend:

- `tier` ∈ {CONFIRMED, DETECTED, none, warming}. Store and display it as-is.
- `peak_hz` is a **peak frequency**, not a breathing rate. Per minute = × 60.
- `amp_units` is `"mm path"` (arc demod) or `"deg"` (tap-phase demod). **Always store it with `amp_p2p`.**
- `null` means no value (JSON has no NaN). A link `{"state": "warming"}` has no window yet.
- `excluded`: why a link was left out of combining (`too_many_missing`, `no_spectrum`, `warming`).
- **Raw packets** live in the `--record` CSV (`seq, link_id, range_m, fp_idx, i0..i55, q0..q55`). Treat them as append-only; never recompute or edit them. `seq` = beacon cycle (16-bit).
- Link map: 1 = A→B, 2 = A→C, 3 = B→C (initiator → responder; the CIR is captured at the initiator).

`make_demo_capture.py` + `run_pipeline.py --json` produce realistic rows for building the backend before real data exists. Label them as synthetic.

## Before the exhibit test

1. Measure the body module's rate **independently** (video count, hall sensor, or phone accelerometer), loaded, at the start and end of a run.
2. Record a **quiet** capture (module off) and an **all-on** capture, then run `--compare`. That's the first real G1/G2.
3. Check the per-link lines. If one link is **always excluded** or never agrees, move that mole so its beam passes nearer the module.
4. Only then run the live on/off demo with `--plot --alert`.


## FFT timing: independent 30 s blocks (default since moles-dsp-2-block)

The DSP collects a full 30 s block (300 samples per link at 10 Hz), runs ONE FFT per link plus the
combined spectrum, prints one verdict, then starts the next block. No FFT runs while a block fills.

- Why: every FFT uses 30 s of data either way, so resolution (1/30 s = 0.033 Hz) is identical. The old
  sliding mode re-ran it every second on windows that overlap 29/30, so its "3 windows agree" rule mostly
  re-counted the same samples. Blocks give one independent verdict per 30 s of data.
- The 3-window stability rule is off in block mode (it would cost 90 s). Each block is judged on its
  margins (16 dB per link, 12 dB combined) and link agreement (>=1 link DETECTED, >=2 CONFIRMED).
- Latency: a verdict every 30 s; the module is reported 30-60 s after it starts, depending on where in
  the block it starts. A block that is half on/half off may miss; the next full block catches it.
- Search band is unchanged: the full 3/T .. fs/2 = 0.1-5 Hz (no assumed rate).
- Backend: one JSON row per block, with new fields "block" (1, 2, ...) and "t_start" (s).
- Old behaviour: --fft-mode sliding (monitor and run_pipeline.py).
- Synthetic check: quiet 0/60 blocks false-detected (combined max 8.8 dB); module on at random rates
  0.15-1.5 Hz 30/30 blocks detected, frequency error <= 0.004 Hz. Not yet verified on hardware.

## Beacon counter jumps (moles-dsp-2.1)

Opening the serial port can reset the host ESP32 (DTR/RTS auto-reset), so its beacon cycle restarts
at 0 while the first few mole packets still carry the old count. The DSP used to anchor its clock on
those first packets; every later sample then landed in the "past", t never advanced, and no window or
block ever completed (monitor stuck at "collecting block 1: 0.3 / 30 s"). Sliding mode had the same bug.
Now a jump of > 50 cycles (5 s) confirmed by 6 packets restarts the DSP at block 1 on the new timebase
and prints "[dsp] beacon cycle jumped A -> B"; isolated stale packets are dropped. 16-bit wrap and short
outages are unaffected (tested).

## Person band (v2.2, `moles-dsp-2.2-band`)
The spectrum is still searched from 0.10 Hz to 5 Hz, but only a peak inside the **person band,
0.15–0.40 Hz** (`--band-lo`, `--band-hi`), can be a person (DETECTED / CONFIRMED, LEDs on).

| Zone | Range | Result |
|---|---|---|
| under | < 0.15 Hz | printed as `under-band … (drift, ignored)`; never counts |
| person | 0.15–0.40 Hz | DETECTED / CONFIRMED = person |
| over | > 0.40 Hz | tier `MOTION`: reported, **not** a person, LEDs off |

A person-band peak that sits at 2× or 3× a *stronger* under-band peak is the drift's harmonic
and is rejected (link flag `harmonic_of_drift`). JSON rows gain `person`, `over_hz`, `over_db`,
`under_hz`, `under_db` (combined and per link). The plot shades the person band green.
