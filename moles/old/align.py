"""CIR window alignment for PUPS (MOLES prototype) — stage 2 of the per-link pipeline.

Pure signal-processing helpers: no I/O, no packet-format knowledge. Inputs are
complex tap arrays (cir_i + 1j*cir_q), one row per packet.

Why this stage exists
---------------------
The firmware anchors each 56-tap window on the detected first path
(window[FP_POS] = first path). The detector's pick jitters by whole AND
fractional taps between packets even when nothing moves, so the echo slides
inside the window. Every later stage treats a column as a fixed delay
("tap k = k taps after the first path"), so rows must be re-aligned before
anything else touches them.

Data-integrity contract
-----------------------
* Fixed columns:  output rows keep the input length; tap k means the same
                  delay in every row.
* No invented data: taps shifted in from outside the window become NaN and are
                  reported in `valid`. Never zero-filled, never wrapped around.
* Fair scoring:   every candidate shift is scored on the SAME interior taps, so
                  a shift can't win just by dropping noisy edge taps.
* Honest flags:   alignment it isn't sure about is flagged, not hidden.
* Phase-safe:     shifting is a pure delay (band-limited fractional shift). It
                  never rotates or rescales taps, so motion phase on later taps
                  survives. Verified by the signal-survival self-test:
                      python align.py

Why phase (not dev / magnitude) is what the FFT needs
-----------------------------------------------------
* One tap = ~30 cm. A few cm of motion moves an echo ~0.07 tap: magnitude
  barely changes, so dev can't see it.
* Phase moves 7.8 deg per mm of path (channel 5, lambda = 46.2 mm): the same
  motion swings phase by tens to hundreds of degrees.
* dev sums every tap, including noise-floor taps; phase is read on the one or
  two taps that actually carry the echo.
dev remains useful for alignment scoring and disturbance detection (hands,
people). It is not an FFT input.
"""

from __future__ import annotations

from dataclasses import dataclass

import warnings

import numpy as np

FP_POS = 8                       # first-path column in the firmware window
DEG_PER_MM = 360.0 / 46.2        # phase change per mm of path, channel 5

# AlignResult.flags bits
FLAG_EDGE_HIT = 1   # optimum pinned at the search limit: true offset may be outside it
FLAG_AMBIGUOUS = 2  # a non-adjacent shift scores almost as well: possible first-path flip
FLAG_POOR_FIT = 4   # even the best shift leaves high dev: the mismatch is NOT jitter


# --------------------------------------------------------------------------- scoring
def compute_dev(window, baseline, weights=None) -> float:
    """Deviation (%) between per-tap magnitudes of two equal-length arrays.

        sum(w * |win_mag - base_mag|) / sum(w * base_mag) * 100

    NaN taps (invalid after alignment) are skipped. `weights` lets a later
    stage (dead-tap removal / SNR weighting) down-weight noise-floor taps.
    Returns NaN when there is nothing to score, never a fake "perfect" 0.
    """
    wm = np.abs(np.asarray(window))
    bm = np.abs(np.asarray(baseline))
    w = np.ones_like(bm, dtype=float) if weights is None else np.asarray(weights, dtype=float)

    ok = np.isfinite(wm) & np.isfinite(bm) & np.isfinite(w)
    denom = float(np.sum(w[ok] * bm[ok]))
    if denom <= 0.0:
        return float("nan")
    return float(np.sum(w[ok] * np.abs(wm[ok] - bm[ok])) / denom * 100.0)


def _delay(x: np.ndarray, s: float) -> np.ndarray:
    """Band-limited shift, y[t] = x(t + s). Circular: the caller masks the edges.

    The CIR is band-limited (~500 MHz signal sampled at ~1 GS/s), so an FFT
    phase ramp is an exact fractional delay for interior taps and an exact roll
    for integer s. It changes timing only, never tap phase or magnitude.
    """
    k = np.fft.fftfreq(len(x))
    return np.fft.ifft(np.fft.fft(x) * np.exp(2j * np.pi * k * s))


def _score_region(n: int, max_shift: int) -> slice:
    """Interior taps that stay valid for every candidate shift up to max_shift + 0.5."""
    lo, hi = max_shift + 2, n - max_shift - 2
    if hi - lo < 8:
        raise ValueError(f"window of {n} taps is too short for max_shift={max_shift}")
    return slice(lo, hi)


# --------------------------------------------------------------------------- alignment
@dataclass
class AlignResult:
    aligned: np.ndarray   # complex, same length as input; NaN where invalid
    valid: np.ndarray     # bool mask of trustworthy taps
    shift: float          # applied shift in taps (aligned[t] = window(t + shift))
    dev_before: float     # dev at shift 0, same interior region
    dev_after: float      # dev after alignment, same interior region
    flags: int            # FLAG_* bits


def align_window(window, baseline, max_shift: int = 2, fractional: bool = True,
                 weights=None, ambiguity_ratio: float = 1.10,
                 poor_fit_pct: float = 20.0) -> AlignResult:
    """Align one complex CIR window to a baseline (magnitude template).

    1. Integer search over [-max_shift, +max_shift], all scored on one fixed
       interior region. Ties keep the smaller |shift|.
    2. If `fractional`: grid search +/-0.5 tap in 0.1 steps around the best
       integer, then a parabolic refinement.
    3. Shift the complex window by the result; mark taps sourced from outside
       the window as NaN.

    `ambiguity_ratio` and `poor_fit_pct` are starting values for the flags,
    not claims about the channel. Tune them on quiet hardware captures.
    """
    x = np.asarray(window, dtype=complex)
    b = np.abs(np.asarray(baseline))
    n = len(b)
    if len(x) != n:
        raise ValueError("window and baseline lengths differ")
    if not np.all(np.isfinite(x)):
        raise ValueError("window has non-finite taps: drop/mask bad packets before alignment")

    region = _score_region(n, max_shift)
    w = None if weights is None else np.asarray(weights, dtype=float)[region]

    def score(s: float) -> float:
        return compute_dev(_delay(x, s)[region], b[region], w)

    ints = list(range(-max_shift, max_shift + 1))
    d_int = {s: score(s) for s in ints}
    best_int = min(ints, key=lambda s: (d_int[s], abs(s)))

    flags = 0
    far = [d_int[s] for s in ints if abs(s - best_int) >= 2]
    if far and min(far) <= ambiguity_ratio * d_int[best_int]:
        flags |= FLAG_AMBIGUOUS

    shift, dev_after = float(best_int), d_int[best_int]
    if fractional:
        grid = best_int + np.linspace(-0.5, 0.5, 11)
        d = np.array([score(s) for s in grid])
        i = int(np.argmin(d))
        shift, dev_after = float(grid[i]), float(d[i])
        if 0 < i < len(grid) - 1:
            den = d[i - 1] - 2 * d[i] + d[i + 1]
            if den > 0:
                s_ref = shift + 0.5 * (d[i - 1] - d[i + 1]) / den * (grid[1] - grid[0])
                d_ref = score(s_ref)
                if d_ref <= dev_after:
                    shift, dev_after = float(s_ref), float(d_ref)

    # Edge: optimum pinned at the outer limit of the search, so the true offset
    # may lie beyond it. (Integer-only mode can't tell, so it flags any edge pick.)
    edge_limit = max_shift + 0.45 if fractional else max_shift
    if max_shift > 0 and abs(shift) >= edge_limit:
        flags |= FLAG_EDGE_HIT
    if dev_after > poor_fit_pct:
        flags |= FLAG_POOR_FIT

    aligned = _delay(x, shift)
    src = np.arange(n) + shift
    guard = 0 if float(shift).is_integer() else 1   # fractional shifts also blur the edge tap
    valid = (src >= guard) & (src <= n - 1 - guard)
    aligned[~valid] = complex(np.nan, np.nan)

    return AlignResult(aligned, valid, shift, float(d_int[0]), float(dev_after), flags)


def aligned_dev(window, baseline, max_shift: int = 2) -> tuple[float, int]:
    """Backward-compatible wrapper: (best_dev, best_integer_shift).

    Differs from the original version in one way: every shift is now scored on
    the same interior taps (fair comparison), instead of on its own overlap.
    """
    r = align_window(window, baseline, max_shift=max_shift, fractional=False,
                     poor_fit_pct=np.inf)
    return r.dev_after, int(round(r.shift))


# --------------------------------------------------------------------------- baseline
@dataclass
class Baseline:
    mag: np.ndarray        # per-tap mean magnitude of the ALIGNED rows
    shifts: np.ndarray     # shift applied to each input row
    noise_dev_avg: float   # dev of aligned rows vs the final baseline (quiet-scene noise)
    noise_dev_max: float
    n_rows: int


def build_baseline(windows, max_shift: int = 2, iterations: int = 3,
                   fractional: bool = True) -> Baseline:
    """Build a magnitude baseline from ALIGNED rows.

    Averaging raw (jittered) rows smears the first-path pulse into a template
    no single packet ever matches, which keeps dev high even for a still scene.
    Here: start from the per-tap median, align every row to it, re-average,
    repeat. Capture only while the scene is still (actuator off, nobody near).
    """
    rows = np.asarray(windows, dtype=complex)
    if rows.ndim != 2 or len(rows) < 3:
        raise ValueError("need a 2-D array of at least 3 windows")

    ref = np.median(np.abs(rows), axis=0)
    results: list[AlignResult] = []
    for _ in range(iterations):
        results = [align_window(r, ref, max_shift, fractional, poor_fit_pct=np.inf) for r in rows]
        mags = np.array([np.abs(r.aligned) for r in results])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN edge columns
            ref = np.nanmean(mags, axis=0)

    devs = np.array([compute_dev(r.aligned, ref) for r in results])
    return Baseline(mag=ref, shifts=np.array([r.shift for r in results]),
                    noise_dev_avg=float(np.nanmean(devs)), noise_dev_max=float(np.nanmax(devs)),
                    n_rows=len(rows))


# --------------------------------------------------------------------------- integrity
class AlignMonitor:
    """Running integrity stats for one link. Call update() per packet."""

    def __init__(self):
        self.shifts: list[float] = []
        self.before: list[float] = []
        self.after: list[float] = []
        self.flag_counts = {FLAG_EDGE_HIT: 0, FLAG_AMBIGUOUS: 0, FLAG_POOR_FIT: 0}

    def update(self, r: AlignResult) -> None:
        self.shifts.append(r.shift)
        self.before.append(r.dev_before)
        self.after.append(r.dev_after)
        for f in self.flag_counts:
            if r.flags & f:
                self.flag_counts[f] += 1

    def _pct(self, flag: int) -> float:
        return 100.0 * self.flag_counts[flag] / max(len(self.shifts), 1)

    def summary(self) -> str:
        if not self.shifts:
            return "align: no packets"
        s = np.array(self.shifts)
        return (f"align: n={len(s)}  shift mean {s.mean():+.2f} std {s.std():.2f} taps  "
                f"dev median {np.nanmedian(self.before):.1f}% -> {np.nanmedian(self.after):.1f}%  "
                f"edge {self._pct(FLAG_EDGE_HIT):.0f}%  ambiguous {self._pct(FLAG_AMBIGUOUS):.0f}%  "
                f"poor-fit {self._pct(FLAG_POOR_FIT):.0f}%")

    def diagnosis(self) -> list[str]:
        """Plain-language hints. Thresholds (5%, 20%) are starting values."""
        notes = []
        if self._pct(FLAG_EDGE_HIT) > 5:
            notes.append("Many shifts hit the search edge: raise max_shift, or the first path is jumping more than expected.")
        if self._pct(FLAG_AMBIGUOUS) > 5:
            notes.append("Ambiguous alignments: the first path may be flipping between two paths. Alignment cannot fix that.")
        if self._pct(FLAG_POOR_FIT) > 20:
            notes.append("dev stays high AFTER alignment: the remaining mismatch is not jitter. "
                         "Check noise-floor taps (stage 3), gain / accumulation count (stage 4), "
                         "a baseline built from unaligned rows, or real motion in the scene.")
        return notes


def phase_stability_deg(aligned_rows, ref_tap: int = FP_POS, taps=None) -> np.ndarray:
    """FFT-readiness check: per-tap phase spread (deg) after first-path referencing.

    Run on a QUIET capture. Each tap is referenced to `ref_tap`, which removes
    the random per-packet phase of the unsynchronized oscillators. The circular
    standard deviation of what remains is the phase noise the FFT will see.
    Divide by DEG_PER_MM for path-length noise in mm.
    """
    rows = np.asarray(aligned_rows, dtype=complex)
    taps = np.arange(rows.shape[1]) if taps is None else np.asarray(taps)
    ref = rows[:, ref_tap][:, None]
    z = rows[:, taps] * np.conj(ref) / np.abs(ref) ** 2
    unit = np.exp(1j * np.angle(z))
    unit[~np.isfinite(z)] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        R = np.abs(np.nanmean(unit, axis=0))
    return np.degrees(np.sqrt(-2.0 * np.log(np.clip(R, 1e-12, 1.0))))


# --------------------------------------------------------------------------- self-test
def _selftest(seed: int = 1) -> bool:
    """Signal-survival test (validation gate G4 for stage 2).

    Synthetic still scene + first-path jitter (whole AND fractional taps) +
    random per-packet phase and gain + noise + ONE weak echo whose phase
    wobbles at a frequency the test does not reveal to the aligner.
    Pass = dev drops AND the wobble's frequency and size survive alignment,
    matching a jitter-free reference run.
    """
    rng = np.random.default_rng(seed)
    n, rows, fs = 56, 300, 10.0
    f_wobble, wobble_deg = 0.27, 70.0            # arbitrary test values
    echo_tap = FP_POS + 2
    paths = [(FP_POS, 1000.0), (12, 400.0), (20, 250.0), (33, 150.0)]
    t = np.arange(n)
    pulse = lambda x: np.exp(-(x / 1.1) ** 2)

    def make(jitter: bool) -> tuple[np.ndarray, np.ndarray]:
        r = np.random.default_rng(seed + 7)       # same noise for both runs
        out, truth = [], []
        for k in range(rows):
            d = r.uniform(-2.0, 2.0) if jitter else 0.0
            theta, gain = r.uniform(0, 2 * np.pi), r.normal(1.0, 0.05)
            phi = np.radians(wobble_deg) * np.sin(2 * np.pi * f_wobble * k / fs)
            h = sum(a * pulse(t - (p - d)) for p, a in paths).astype(complex)
            h += 120.0 * pulse(t - (echo_tap - d)) * np.exp(1j * phi)
            h = gain * np.exp(1j * theta) * h
            h += r.normal(0, 12, n) + 1j * r.normal(0, 12, n)
            out.append(h)
            truth.append(-d)
        return np.array(out), np.array(truth)

    def wobble(rows_c: np.ndarray) -> tuple[float, float]:
        """Echo-tap phase -> circle-fit static removal -> spectrum peak."""
        z = rows_c[:, echo_tap] * np.conj(rows_c[:, FP_POS]) / np.abs(rows_c[:, FP_POS]) ** 2
        A = np.c_[z.real, z.imag, np.ones(len(z))]
        c = np.linalg.lstsq(A, -(z.real ** 2 + z.imag ** 2), rcond=None)[0]
        center = -c[0] / 2 - 1j * c[1] / 2
        ph = np.unwrap(np.angle(z - center))
        ph -= np.polyval(np.polyfit(np.arange(len(ph)), ph, 1), np.arange(len(ph)))
        spec = np.abs(np.fft.rfft(ph * np.hanning(len(ph)), 8192))
        f = np.fft.rfftfreq(8192, 1 / fs)
        band = f >= 3.0 / (rows / fs)             # >= 3 cycles in window, per pipeline rules
        return float(f[band][np.argmax(spec[band])]), float(np.degrees(np.std(ph)))

    ref_rows, _ = make(jitter=False)
    jit_rows, truth = make(jitter=True)

    base = build_baseline(jit_rows[:30])          # built from jittered rows, like hardware
    mon = AlignMonitor()
    aligned = []
    for row in jit_rows:
        res = align_window(row, base.mag)
        mon.update(res)
        aligned.append(res.aligned)
    aligned = np.array(aligned)

    # The baseline's own position is arbitrary (built from jittered rows), so the
    # aligner is correct up to one constant offset. Score the per-packet error.
    shift_err = np.array(mon.shifts) - truth
    offset = float(np.mean(shift_err))
    shift_err = shift_err - offset
    f_ref, a_ref = wobble(ref_rows)
    f_raw, a_raw = wobble(jit_rows)
    f_al, a_al = wobble(aligned)
    ps = phase_stability_deg(aligned, taps=[echo_tap])[0]

    print("align.py self-test (synthetic)")
    print(f"  baseline noise dev    avg {base.noise_dev_avg:.1f}%  max {base.noise_dev_max:.1f}%")
    print(f"  {mon.summary()}")
    print(f"  shift error           rms {np.sqrt(np.mean(shift_err ** 2)):.3f} taps "
          f"(after removing baseline offset {offset:+.2f})")
    print(f"  wobble  reference     {f_ref:.3f} Hz  {a_ref:5.1f} deg rms")
    print(f"  wobble  unaligned     {f_raw:.3f} Hz  {a_raw:5.1f} deg rms")
    print(f"  wobble  aligned       {f_al:.3f} Hz  {a_al:5.1f} deg rms")
    print(f"  echo-tap phase spread {ps:.1f} deg (includes the wobble itself)")

    res_hz = 1.0 / (rows / fs)
    checks = {
        "dev reduced by alignment": np.nanmedian(mon.after) < 0.5 * np.nanmedian(mon.before),
        "shift error < 0.15 tap rms": np.sqrt(np.mean(shift_err ** 2)) < 0.15,
        "wobble frequency survives": abs(f_al - f_ref) < res_hz / 2,
        "wobble size survives (+/-15%)": abs(a_al - a_ref) <= 0.15 * a_ref,
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return all(checks.values())


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
