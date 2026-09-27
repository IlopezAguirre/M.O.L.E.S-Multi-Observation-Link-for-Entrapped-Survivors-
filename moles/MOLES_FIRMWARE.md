# M.O.L.E.S. — Firmware Bring-Up

The host ESP32 is the **cycle master**: it broadcasts a beacon every 100 ms. Every mole times its slots from that beacon, so each link succeeds or fails on its own. Every exchange is the same SS-TWR as PUPS (poll T1 → responder T2/T3 → initiator T4, ToF, CIR → host).

## Schedule (one 100 ms cycle)

| Slot | Time after beacon | Link | A | B | C | Packet sent by |
|---|---|---|---|---|---|---|
| 0 | +10 ms | 1 `A->B` | **polls B** | answers A | idle | A |
| 1 | +43 ms | 2 `A->C` | **polls C** | idle | answers A | A |
| 2 | +76 ms | 3 `B->C` | idle | **polls C** | answers B | B |

Each link is sampled every 100 ms (10 Hz). `seq` in every packet is the **beacon cycle counter**, so all three links share one timebase. The packet format is unchanged (234 bytes).

To change the layout (e.g. a ring A→B, B→C, C→A), edit `SCHEDULE[]` in `mole.ino`. Keep the Python `LINK_NAMES` and the backend `links` table in sync.

## What changed from PUPS

| | PUPS | MOLES |
|---|---|---|
| Sketches | `mole_a_tx.ino`, `mole_b_rx.ino` | **one** `mole.ino`, set `MOLE_ID` per board |
| Timing | Mole A's own 100 ms loop | **host beacon**, fixed slots |
| Frames | fixed bytes, any responder answers | **addressed** (src/dst IDs); the reply echoes the poll's sequence number |
| Responder radio | always listening | listening **only in its own slot** |
| `seq` | per-mole counter | **shared cycle counter** |
| Host | forwarder | forwarder + **cycle master** + `ALERT` command |

## Files

| File | Flash / run on |
|---|---|
| `mole/mole.ino` | all three moles (change `MOLE_ID`) |
| `host_esp32/host_esp32.ino` | host ESP32 on the laptop |
| `moles_monitor.py`, `run_pipeline.py`, `pups_dsp.py`, `align.py` | laptop (link names set to A->B, A->C, B->C) |

Wiring and host MAC (`20:50:0D:E4:46:40`) are unchanged.

## Flashing

1. Host: flash `host_esp32.ino`.
2. For each mole, set the line in `mole.ino`, then flash:
   `#define MOLE_ID MOLE_A` (then `MOLE_B`, then `MOLE_C`).
   **Label the boards.** A wrong ID is the easiest mistake to make.
3. Each mole prints its schedule on boot (Serial Monitor, 115200). Check that it says the role you expect.

## Bring-up order

1. **Host + A + B only.** Run the monitor. `A->B` should run at ~10 pkt/s. `A->C` should show **no-response** lines every cycle (C is off). That's correct, and it proves the miss path works.
2. **Add C.** All three links should run at ~10 pkt/s with low miss%.
3. Check each mole's status line (below).
4. Record off/on captures with `--record` for the DSP step.

## Mole status line (every ~2 s)

```
[A] cyc 1234 sync beacons 1230 pred 4 lost 0 late-slots 0 | A->B ok 20 miss 0 (0.842 m) | A->C ok 19 miss 1 (1.105 m) |
[C] cyc 1234 sync beacons 1231 pred 3 lost 0 late-slots 0 | answer A: replies 20 late 0 no-poll 0 wrong 0 | answer B: replies 20 ... |
```

| Field | Meaning | Healthy |
|---|---|---|
| `sync` / `PRED` | following the beacon / predicting a missed one | mostly `sync` |
| `pred`, `lost` | predicted cycles, times sync was lost (>3 missed beacons in a row) | small, `lost 0` |
| `late-slots` | slots skipped because the mole got there >4 ms late | 0 |
| `ok` / `miss` | initiator: exchanges that worked / didn't | ok ≈ 10 per second |
| `replies` | responder: answered a poll | ≈ the initiator's `ok` |
| `late` | responder missed the 900 µs reply deadline | 0 |
| `no-poll` | responder's slot passed with no poll heard | 0 while the initiator is on |
| `wrong` | frames heard that weren't addressed to it | low |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Mole prints "no beacon" | Host not running, or channel mismatch |
| One link all misses, responder `no-poll` climbing | Responder has the wrong `MOLE_ID`, or it's off/out of range |
| Responder `replies` fine but initiator `miss` | Reply not reaching the initiator (blocked, range), or frame mismatch from mixed firmware versions: reflash all three |
| Responder `late` climbing | Raise `POLL_RX_TO_RESP_TX_DLY_UUS` and `POLL_TX_TO_RESP_RX_DLY_UUS` by the same amount **on all moles** |
| `late-slots` climbing | Something blocking the loop (don't add prints or delays inside slots) |
| Host status `bad` climbing | Old PUPS firmware still running on some board |

## Tuning knobs (`mole.ino`, keep identical on all moles)

`SLOT0_US` 10 ms, `SLOT_US` 33 ms, `POLL_DELAY_US` 3 ms (initiator waits so the responder is already listening), `LISTEN_US` 15 ms, `LATE_LIMIT_US` 4 ms, `BEACON_GRACE_US` 8 ms, `MAX_PREDICT` 3.

Off-hardware timing check (simulated beacon jitter/loss): 100% of polls land inside the responder's window with up to 3 ms jitter or 5% beacon loss; 99.9% at 20% loss. **Verify on hardware with the status lines.**

## Alert LED (for the exhibit)

Send `ALERT <mask>` + newline to the host over serial (bit 0 = A, bit 1 = B, bit 2 = C; `ALERT 0` clears). The mask rides in every beacon, and alerted moles blink `ALERT_LED_PIN` (GPIO 2, the onboard LED; wire a red LED there). The DSP step will send this automatically.

## Next: DSP + FFT output

- Use the **shared cycle counter** as the timebase for every link, so combined windows line up exactly.
- Use the **tiered combined decision** (CONFIRMED / DETECTED).
- Show per-link spectra plus the combined spectrum.
- Drive `ALERT` from the decision.
