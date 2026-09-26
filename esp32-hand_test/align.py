"""CIR window alignment for MOLES.

Pure signal-processing helpers, no I/O and no packet-format knowledge. They take
plain arrays so they slot into any capture pipeline (e.g. moles_monitor.py).

Background: fp_idx (the first-path tap index) drifts by +/-1 or +/-2 between
packets even when the scene is still. That slides the whole 56-tap echo window
against the saved baseline and inflates the deviation score. aligned_dev()
undoes that by searching a small integer shift before scoring.
"""

from __future__ import annotations

import numpy as np


def compute_dev(window, baseline) -> float:
    """Deviation (%) between two equal-length arrays of complex taps.

    Compares per-tap magnitude (|z| = sqrt(i^2 + q^2)):

        sum(|window_mag[t] - baseline_mag[t]|) / sum(baseline_mag[t]) * 100

    Lower is more similar; ~0 means the window matches the baseline.
    """
    window = np.asarray(window)
    baseline = np.asarray(baseline)

    window_mag = np.abs(window)
    baseline_mag = np.abs(baseline)

    denom = float(np.sum(baseline_mag))
    if denom == 0.0:
        return 0.0

    return float(np.sum(np.abs(window_mag - baseline_mag)) / denom * 100.0)


def aligned_dev(window, baseline, max_shift: int = 2) -> tuple[float, int]:
    """Lowest deviation over integer shifts in [-max_shift, +max_shift].

    A shift of `s` compares window[t + s] against baseline[t]. Taps that fall
    off either end of the array for a given shift are dropped from that shift's
    comparison (not zero-padded), so each shift is scored only on its overlap.

    Returns (best_dev, best_shift): the minimum deviation and the shift that
    produced it. Ties keep the smaller |shift| (0 is tried first).
    """
    window = np.asarray(window)
    baseline = np.asarray(baseline)
    n = len(baseline)

    best_dev = np.inf
    best_shift = 0

    # Order shifts so the smallest magnitudes win ties: 0, -1, +1, -2, +2, ...
    shifts = sorted(range(-max_shift, max_shift + 1), key=lambda s: (abs(s), s))

    for shift in shifts:
        if shift >= 0:
            w = window[shift:]
            b = baseline[: n - shift]
        else:
            w = window[: n + shift]
            b = baseline[-shift:]

        if len(b) == 0:  # shift wider than the window: nothing overlaps
            continue

        dev = compute_dev(w, b)
        if dev < best_dev:
            best_dev = dev
            best_shift = shift

    return float(best_dev), int(best_shift)
