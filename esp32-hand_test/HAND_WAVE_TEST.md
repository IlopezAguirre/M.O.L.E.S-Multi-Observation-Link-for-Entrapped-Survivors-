# MOLES — Phase 1: Hand-Wave Test

**Goal:** prove that a hand disturbing the UWB link between two stationary moles shows up live, in the CIR and the range, on a laptop terminal.

**Scope:** fixed roles (no round robin), one link (A→B), ESP-NOW to a host ESP32, terminal output only. No FFT, no backend, no raw data file.

---

## 1. System

```
  [MOLE A]  ~~~~~~~ UWB, channel 5 (6.5 GHz) ~~~~~~~  [MOLE B]
  ESP32 + DWM3000EVB     poll  ------------->        ESP32 + DWM3000EVB
  SS-TWR initiator       <-------------  response    SS-TWR responder
      |                                              (T2, T3 inside)
      |  ESP-NOW, 2.4 GHz, one 234-byte packet per exchange, 10 Hz
      v
  [HOST ESP32] ---- USB serial, 921600 baud ---->  laptop: moles_monitor.py
                                                   (VS Code terminal)
```

One exchange (every 100 ms):

1. **A's DW3000** sends a poll and records **T1** (poll TX).
2. **B's DW3000** records **T2** (poll RX). B's ESP32 schedules the reply 900 µs later at **T3**, packs T2 + T3 into the response, and B's DW3000 sends it at exactly T3.
3. **A's DW3000** records **T4** (response RX) and captures the **CIR** of that packet. A's ESP32 computes `ToF = ((T4 − T1) − (T3 − T2)) / 2`, `range = ToF × c`, reads a 56-tap CIR window around the first path, and sends it all to the host over ESP-NOW.

## 2. Files

| File | Flash / run on | Role |
|---|---|---|
| `mole_a_tx/mole_a_tx.ino` | Mole A ESP32 | Initiator: ranging, CIR read, ESP-NOW sender |
| `mole_b_rx/mole_b_rx.ino` | Mole B ESP32 | Responder: timestamps poll, sends response |
| `host_esp32/host_esp32.ino` | Host ESP32 (on laptop USB) | ESP-NOW receiver → framed binary over USB |
| `moles_monitor.py` | Laptop | Decodes frames, baselines the CIR, prints live lines |

## 3. Packet (ESP-NOW, Mole A → Host)

```c
typedef struct __attribute__((packed)) {
  uint8_t  magic;         // 0x4D ('M'), filters stray ESP-NOW traffic
  uint8_t  link_id;       // 0x01 = A->B
  uint16_t seq;           // +1 every poll attempt; gaps = lost ESP-NOW packets
  float    range_m;       // NAN if B didn't answer
  uint16_t fp_idx;        // first-path tap index; 0xFFFF if B didn't answer
  int16_t  cir_i[56];     // CIR real, window = taps [fp_idx-8 .. fp_idx+47]
  int16_t  cir_q[56];     // CIR imaginary
} mole_pkt_t;             // 234 bytes, fits one 250-byte ESP-NOW frame
```

CIR taps are the DW3000's 18-bit signed values shifted right by 2 to fit `int16`. Each tap is ~1 ns of delay (~30 cm of extra path length).

## 4. Hardware

- 2× Qorvo DWM3000EVB (Mole A, Mole B)
- 3× ESP32 DevKit, **classic ESP32-WROOM-32** (pins below assume this; S3/C3 boards need different GPIOs)
- 2× USB power banks for the moles, 1× USB cable host → laptop
- Jumper wires

### Wiring (identical for Mole A and Mole B)

| DWM3000EVB signal | EVB header position | ESP32 GPIO |
|---|---|---|
| IRQ | CON1 (upper-right), 1st from bottom | 34 |
| WAKEUP | CON1, 2nd from bottom | not connected |
| SPICSn | CON1, 3rd from bottom | 5 |
| SPIMOSI | CON1, 4th from bottom | 23 |
| SPIMISO | CON1, 5th from bottom | 19 |
| SPICLK | CON1, 6th from bottom | 18 |
| GND | CON1, 7th from bottom | GND |
| RSTn | CON4 (lower-right), top pin | 27 |
| 3V3 | CON2 (left), 4th pin | 3V3 |

WAKEUP can stay unconnected because the firmware never puts the DW3000 to sleep. The host ESP32 has no UWB module.

## 5. Software setup

1. **Arduino IDE 2.x** with the Espressif **ESP32 core** (Boards Manager → "esp32 by Espressif"). Compile-tested on 3.3.12; 2.x also works.
2. **DW3000 library:** download `github.com/Makerfabs/Makerfabs-ESP32-UWB-DW3000` as a zip, copy its `Dw3000` folder into `~/Documents/Arduino/libraries/`, restart the IDE.
3. Board setting: **ESP32 Dev Module**.
4. Laptop: Python 3 and `pip install pyserial`.

## 6. Flash and bring-up

1. **Host:** flash `host_esp32.ino`, leave it plugged into the laptop.
2. **Mole B:** flash `mole_b_rx.ino`. Serial Monitor at 115200 should show `[B] ready, listening for polls`.
3. **Mole A:** flash `mole_a_tx.ino`. Serial Monitor at 115200 prints `[A] ok=... miss=...` every 2 s. `ok` should climb by ~20 per line (10 Hz) with `miss` near zero.
4. Unplug A and B from the laptop and move them to power banks.
5. **Close the Arduino Serial Monitor** (it locks the port), then run:

```bash
python moles_monitor.py --list                          # find the host port
python moles_monitor.py --port /dev/cu.usbserial-XXXX   # macOS example
```

6. Optional: the monitor prints the host's MAC on startup. Paste it into `HOST_MAC` in `mole_a_tx.ino` and reflash A. Unicast gets ACKs and retries; broadcast (the default) doesn't.

Commands while running: `b` + Enter re-captures the baseline, `q` + Enter quits.

## 7. Reading the terminal

```
00412  A->B   1.243 m (+0.002)  fp  745  fp_mag   8120 (  99%)  dev   2.9% █░░░░░░░░░░░     ▁▅█▅▁  ▁▂▃▂▁   ▁▁▁
00415  A->B   1.402 m (+0.161)  fp  747  fp_mag   3310 (  41%)  dev  33.1% ████████████  <<< DISTURBED ...
00416  A->B  ---- no response from responder (miss x1) — link blocked or B not running ----
```

| Column | Meaning | What a hand does |
|---|---|---|
| `range (Δ)` | SS-TWR distance, and change vs baseline | Jumps **longer** when the direct path is blocked |
| `fp` | First-path tap index | Shifts later if the chip locks onto a reflection |
| `fp_mag (%)` | Strongest tap at the first path, % of baseline | **Drops** sharply when blocked |
| `dev %` | Σ\|CIR magnitude − baseline\| ÷ Σ baseline, over all 56 taps | **Spikes** on any disturbance, blocking or not |
| sparkline | CIR magnitude shape across the window | Peak collapses (blocking) or a new bump appears (hand beside the link) |
| `no response` | B's response never arrived | Full block or B down |
| `(+N lost over ESP-NOW)` | Seq gap between A and host | ESP-NOW loss, not a UWB event |

The alert threshold is set automatically to **2× the worst dev seen during baseline** (minimum 5%). Override with `--thresh 15`.

## 8. Test procedure

**Setup**
- Mole A and Mole B **1.0 m apart** (mark the spots with tape), same height, antennas facing each other.
- Keep the link at least 30 cm from the laptop, walls and table edges.
- Nobody within ~0.5 m of the link while the baseline captures.

**Baseline**
Start the monitor and wait for `baseline locked`. Write down baseline range, fp_mag, noise avg/max, and threshold. Re-baseline (`b`) any time the moles or the room layout move.

**Trials** (3 reps each; wait for dev to settle between reps)

| ID | Action | Expected |
|---|---|---|
| T0 | Idle 30 s, nothing moves | dev stays under threshold, no misses (false-alarm check) |
| T1 | Flat hand fully blocking the midpoint, hold 3 s, remove | fp_mag drops well under 100%, dev spikes, range jumps positive, possible misses |
| T2 | Slow pass through the midpoint (~1 s to cross) | dev rises and falls smoothly |
| T3 | Fast wave through the midpoint | Brief spike, 1–3 lines at 10 Hz |
| T4 | Hand 10 cm **beside** the link (not blocking), moving slowly toward/away | fp_mag near 100%, dev moderate, new bump a few taps after the peak |
| T5 | Full block near A, then midpoint, then near B | Compare dev and fp_mag for each position |
| T6 | Remove hand after any trial | dev back under threshold within ~1 s |
| T7 (optional) | Repeat T1 at 0.5 m and 2.0 m spacing (re-baseline each) | How detection strength scales with distance |

**Results**

| Trial | Rep | Peak dev % | Min fp_mag % | Max Δrange (m) | Misses | Detected? | Notes |
|---|---|---|---|---|---|---|---|
| T0 | 1 | | | | | | |
| T1 | 1 | | | | | | |
| T1 | 2 | | | | | | |
| T1 | 3 | | | | | | |
| T2 | 1 | | | | | | |
| T3 | 1 | | | | | | |
| T4 | 1 | | | | | | |
| T5 A/mid/B | 1 | | | | | | |
| T6 | 1 | | | | | | |

## 9. Pass criteria

- **T0:** no more than 1 line over threshold in 30 s.
- **T1:** every block detected (dev over threshold or a miss) within 3 lines (0.3 s).
- **T6:** recovery under threshold within 1 s of removing the hand.
- **ESP-NOW:** under 5% lost packets.
- **T4:** record whether off-axis motion is visible at all. This is the closest phase-1 proxy for breathing under rubble and decides what phase 2 needs.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `DW3000 IDLE FAILED` / `INIT FAILED` | Wiring, 3V3/GND, CS or RST on the wrong pin |
| A: `ok=0`, `miss` climbing | B isn't running, or check B's `late` counter |
| B: `late` climbing | ESP32 too slow for the 900 µs reply window. Raise `POLL_RX_TO_RESP_TX_DLY_UUS` in B **and** `POLL_TX_TO_RESP_RX_DLY_UUS` in A by the same amount (e.g. +300) |
| B: `polls=0` | A isn't transmitting, or the `dwt_config_t` blocks differ between files |
| Monitor only shows `[host] alive, no mole packets` | ESP-NOW channel mismatch, typo in `HOST_MAC` (go back to broadcast), or A isn't powered |
| Monitor shows nothing / can't open port | Wrong port, wrong baud, or Arduino Serial Monitor still open |
| dev high right after baseline | Something moved during baseline capture; type `b` |
| Range reads e.g. 1.4 m at 1.0 m | Expected: antenna delay is uncalibrated. Phase 1 only uses Δrange |
| Lines too wide for the terminal | `--no-shape` hides the sparkline |

## 11. Phase-1 limitations

- `dev` uses CIR **magnitude** only. Millimeter-scale motion (breathing) mostly shows up in **phase**, measured relative to the first-path tap because the two moles' clocks aren't synced. That comes in phase 2.
- The CIR window re-aligns to the detected first path every packet. Under a full block the detector can lock onto a later reflection, which is also why range jumps.
- Pins assume a classic ESP32. S3/C3 boards need different GPIOs (GPIO34 doesn't exist on them).

## 12. Adding Mole C later

Keep the same packet. Give each link its own `LINK_ID` (`0x02` = A→C, `0x03` = B→C, already named in the monitor). The host already forwards the sender's MAC with every packet, and the monitor keeps a separate baseline per link.
