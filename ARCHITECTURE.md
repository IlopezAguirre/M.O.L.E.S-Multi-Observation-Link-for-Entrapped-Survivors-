# System Architecture

Diagrams below render automatically on GitHub. No image files, no tooling.

## Components

```mermaid
flowchart TD
    subgraph pods["UWB mesh — channel 5, 6.5 GHz"]
        A["Pod A<br/>ESP32 + DWM3000EVB"]
        B["Pod B<br/>ESP32 + DWM3000EVB"]
        C["Pod C<br/>ESP32 + DWM3000EVB"]
    end

    H["Host ESP32<br/>runs the slot schedule"]
    L["Laptop<br/>Python: FFT + dashboard"]
    V["Saline bag + cam motor<br/>own battery, no data link"]

    H <-->|ESP-NOW 2.4 GHz| A
    H <-->|ESP-NOW 2.4 GHz| B
    H <-->|ESP-NOW 2.4 GHz| C
    H -->|CSV over USB serial| L

    A -.UWB.- B
    B -.UWB.- C
    A -.UWB.- C

    V -.modulates the links.- B
```

The saline bag has no wire going anywhere. It is what the system looks at, not
part of the system.

## One slot, step by step

Each slot is 33 ms. The host owns the schedule; all pods run identical
firmware and only `MY_ID` differs.

```mermaid
sequenceDiagram
    participant L as Laptop
    participant H as Host
    participant A as Pod A
    participant B as Pod B
    participant C as Pod C

    Note over H,C: slot N — tx_id = A
    H->>A: broadcast {seq, tx_id=A}
    H->>B: broadcast {seq, tx_id=A}
    H->>C: broadcast {seq, tx_id=A}
    Note over A: waits ~3 ms
    A-->>B: UWB frame {tx_id, seq}
    A-->>C: UWB frame {tx_id, seq}
    Note over B,C: RX enabled, ~20 ms timeout<br/>read CIR via dwt_readaccdata()
    B->>H: report {CIR taps}
    C->>H: report {CIR taps}
    H->>L: 2 CSV lines
    Note over H,C: slot N+1 — tx_id = B
```

With 3 pods this gives **6 directed links** (A→B, B→A, A→C, C→A, B→C, C→B).
Direction matters — real motion should appear in both directions of a link,
which is our main false-positive filter.

## Interface contract

**This is the part that breaks a project.** If the firmware's CSV and the
Python parser disagree, nothing works and it wastes an hour. Any change here
gets announced to the whole team.

Firmware report struct:

```c
struct Report {
  uint8_t  rx_id;      // who heard it
  uint8_t  tx_id;      // who sent it
  uint16_t seq;        // which round
  uint8_t  status;     // 0 = OK, 1 = timeout, 2 = error
  uint16_t fp_index;   // first-path position in the CIR
  int16_t  taps[N][2]; // CIR window, I and Q
};
```

One CSV line per report, printed by the host:

```
t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, I1, Q1, ... I29, Q29
```

Fixed for now: `N = 30` taps. ESP-NOW caps at 250 bytes, so the ceiling is
about 60 taps.

**Open item:** the taps window should start at an offset from `fp_index`, not
at the first path itself — first-path amplitude is the wrong signal. Make that
offset a `#define` so it can be swept from Python without re-architecting.

## Laptop pipeline

```mermaid
flowchart TD
    S["Serial reader<br/>921600 baud"] --> P["Split by link (tx, rx)<br/>6 streams"]
    P --> Q["Align on seq<br/>mark gaps, never shift"]
    Q --> M["Subtract each tap's mean<br/>static rubble to zero"]
    M --> T["Pick highest-variance taps<br/>paths near the victim"]
    T --> F["FFT over 30 s window"]
    F --> D{"Peak in 0.1–0.5 Hz<br/>vs baseline?"}
    D -->|yes| R["Light that link red<br/>on dashboard"]
    D -->|no| S
```

Window is **30 seconds**, not 15. Frequency resolution is `1 / window`, so 30 s
gives 0.033 Hz bins and puts the 0.2 Hz target in bin 6, with the 0.1–0.5 Hz
band spanning bins 3–15. At 15 s the bins double and 0.1 Hz collapses into
bin 1, right against DC where slow drift lives.

## Ownership

| Area | Owner |
|---|---|
| Node timing, TX/RX cycling, CIR read, ESP-NOW | Timothy |
| Serial reader, pipeline, dashboard, plots | host software |
| Puck, mesh lines, demo pile, breathing rig | form factor |
| Repo, docs, architecture, integration | Ian |
