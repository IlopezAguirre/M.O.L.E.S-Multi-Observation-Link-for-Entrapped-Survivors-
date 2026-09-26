#!/usr/bin/env python3
"""
MOLES / PUPS — live monitor + per-link signal pipeline (single file)

Reads the host ESP32 over USB serial, prints one line per ranging exchange, and
every 30 s runs the PUPS pipeline on the last 30 s of the A->B link (300 samples
at 10 Hz) and draws the FFT. Type  h + Enter  for the legend.

Pipeline (per link, see the PUPS design notes; stage numbers match):

   0  raw capture      every packet -> <run dir>/raw.csv before anything else
   1  validate / gate  UWB miss, non-finite or saturated CIR, ESP-NOW loss -> masked slot
   2  align            fractional first-path alignment vs an aligned baseline
   3  dead taps        noise floor from fp-8..fp-3; taps at the floor are dropped
                       unless they show slow-time motion (revived); SNR weights
   4  phase reference  every tap referenced to the first path (common phase + gain)
   5  static clutter   arc-center (circle) fit on the victim tap
   6  victim tap       live tap with the most slow-time motion over the noise floor
   7  phase -> mm      unwrap, 7.8 deg = 1 mm of path (channel 5)
   8  timebase / gaps  true sample times, validity mask, no filler by default
   9  spike removal    Hampel, MAD-relative threshold
  10  detrend          linear only
  11  band limits      3/T .. fs/2 (from the window, not from an assumed band)
  12  spectrum         Hann + zero-pad FFT if gap-free, Lomb-Scargle if not
  13  detect           strongest in-band peak, SNR margin, valid fraction, stability

  Every stage except capture has a bypass switch (--bypass) for A/B checks, and
  every window reports which stages were active.

  Timebase: the v1 packet has no t_us yet. Mole A polls on a 100 ms grid and
  increments seq on every poll attempt (including misses), so t = seq x 100 ms.
  Host arrival time is logged but never used as a timebase.

   seq  link   range  Δrange    fp     SNR    fp%     dev  sigdev     sh      Δφ    Δpath
 04283  A->B   0.155  -0.181   742    28dB   106%    3.4%    2.1%  +0.40   +12.3°   +1.6mm  ██░░░░░░░░░░

Usage:
  pip install pyserial numpy scipy matplotlib
  python moles_monitor.py --port /dev/cu.usbserial-0001
  python moles_monitor.py --tag T1_onoff             # names the run folder in data/
  python moles_monitor.py --no-lines                 # FFT windows + summaries only
  python moles_monitor.py --replay data/<run>/raw.csv            # re-run a capture
  python moles_monitor.py --replay raw.csv --bypass deadtap      # A/B one stage
  python moles_monitor.py --selftest                 # synthetic signal-survival tests
  python moles_monitor.py --list                     # show serial ports

While running, type a command and press Enter:
  b   re-capture the baseline (keep the link clear for ~10 s)
  h   show the legend
  q   quit
"""

import argparse
import csv
import datetime
import math
import os
import struct
import sys
import threading
import time
import warnings
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lombscargle

# ---------------------------------------------------------------- protocol
SYNC = b"\xAA\x55"
FRAME_PKT, FRAME_TEXT = 0x01, 0x02
MAX_FRAME = 1024

PKT_FMT = "<BBHfH56h56h"              # must match mole_pkt_t
PKT_LEN = struct.calcsize(PKT_FMT)    # 234
MAGIC = 0x4D
TAPS = 56
FP_POS = 8                            # first path sits at tap 8 of the window
MAX_SHIFT = 2                         # tap-shift search radius to cancel fp_idx jitter
LINK_NAMES = {0x01: "A->B", 0x02: "A->C", 0x03: "B->C"}
SAT_LEVEL = 32767                     # |I| or |Q| at int16 full scale = saturated

# ---------------------------------------------------------------- signal constants
FS = 10.0                             # Hz, firmware poll period 100 ms
DT = 1.0 / FS
NOISE_COLS = slice(0, 6)              # fp-8 .. fp-3: before the first path, receiver noise only
SIG_K = 4.0                           # "signal tap" = baseline magnitude > SIG_K x noise rms (~12 dB)
DEAD_SNR = 2.0                        # tap power < 2x noise power (3 dB) = dead at baseline
REVIVE_RATIO = 3.0                    # dead tap comes back if its slow-time motion power > 3x noise
PROBE_SEARCH = range(FP_POS + 1, FP_POS + 7)   # live-line probe: strongest baseline tap fp+1..fp+6
DEG_PER_MM = 360.0 / 46.2             # channel 5: lambda = 46.2 mm -> 7.8 deg per mm of path
CLUTTER_MAX_RATIO = 10.0              # reject arc fit if its center is > 10x the arc spread away
HAMPEL_HALF, HAMPEL_K = 7, 4.0            # stage 9: +/-0.7 s neighbourhood, 4 robust sigmas
ARC_MIN_SNR = 3.0                     # reject arc fit if its radius < 3x noise rms
ARC_MIN_SPAN_DEG = 150.0              # shorter arcs: center is ill-conditioned (biased), use tap phase
BREATH_REF_HZ = (0.1, 0.5)            # shaded on the plot for reference only; never a search band
HEADER_EVERY = 40

STAGES = ("gate", "align", "deadtap", "reference", "clutter", "hampel", "detrend")

# align.AlignResult.flags bits
FLAG_EDGE_HIT = 1   # optimum pinned at the search limit: true offset may be outside it
FLAG_AMBIGUOUS = 2  # a non-adjacent shift scores almost as well: possible first-path flip
FLAG_POOR_FIT = 4   # even the best shift leaves high dev: the mismatch is NOT jitter

# ---------------------------------------------------------------- display
BARS = " ▁▂▃▄▅▆▇█"
RED, YEL, GRN, CYN, DIM, BOLD, RST = ("\033[31m", "\033[33m", "\033[32m", "\033[36m",
                                      "\033[2m", "\033[1m", "\033[0m")

COLS = [("seq", 5), ("link", 4), ("range", 6), ("Δrange", 7), ("fp", 4), ("SNR", 6),
        ("fp%", 5), ("dev", 6), ("sigdev", 6), ("sh", 5), ("Δφ", 7), ("Δpath", 7)]

LEGEND = f"""{BOLD}LEGEND{RST}  (type h + Enter to show again)
 {BOLD}seq{RST}     packet number from Mole A. Gaps show as "(+N lost over ESP-NOW)".
 {BOLD}link{RST}    mole pair, e.g. A->B.
 {BOLD}range{RST}   SS-TWR distance (m). Diagnostics only, never an FFT input.
 {BOLD}Δrange{RST}  range minus baseline (m). A hand BLOCKING the link pushes this UP.
 {BOLD}fp{RST}      first-path tap index in the chip's 1016-tap CIR.
 {BOLD}SNR{RST}     first-path strength over the noise floor (taps fp-8..fp-3), dB. Aim for 20+.
 {BOLD}fp%{RST}     first-path strength vs baseline. Blocking drops it well below 100%.
 {BOLD}dev{RST}     aligned CIR shape change vs baseline (dead taps weighted out).
 {BOLD}sigdev{RST}  same, scored only on taps clearly above the noise floor.
 {BOLD}sh{RST}      fractional tap shift used to cancel first-path jitter (+/-{MAX_SHIFT} max).
 {BOLD}Δφ{RST}      phase change of the probe tap, relative to the first path, vs baseline (deg).
 {BOLD}Δpath{RST}   Δφ as echo path-length change: 7.8° = 1 mm.
 {BOLD}meter{RST}   dev vs alert threshold (full bar = 2x threshold). Red + DISTURBED = over it.
 {BOLD}shape{RST}   CIR magnitude across the window, scaled to the baseline peak.
{BOLD}SUMMARY{RST} (every few seconds, per link)
 pkt/s   received rate (expect 10).  miss% = Mole B didn't answer.  lost% = ESP-NOW loss.
 φσ      spread of Δφ over the summary period. On a STILL scene this is the phase noise
         the FFT will see: ≤5° excellent, ≤15° usable, ≤30° marginal, >30° not FFT-ready.
{BOLD}FFT WINDOW{RST} (every --hop s, default 30 s = 300 samples)
 peak    strongest spectral peak between 3/T and Nyquist (no assumed band), Hz and /min.
 SNR     peak over the median of the rest of the in-band spectrum, dB.
 valid   fraction of the 300 slots with a usable sample. Window invalid if >10% missing.
 DETECTED = SNR above margin AND valid AND same peak as the previous window (±1/T).
"""


def sparkline(mags, scale):
    if scale <= 0:
        return " " * len(mags)
    return "".join(BARS[int(round(min(m / scale, 1.0) * (len(BARS) - 1)))]
                   if np.isfinite(m) else " " for m in mags)


def meter(pct, thresh, width=12):
    filled = int(min(pct / max(thresh * 2, 1e-9), 1.0) * width) if np.isfinite(pct) else 0
    return "█" * filled + "░" * (width - filled)


def header_line():
    return DIM + "  ".join(f"{n:>{w}}" for n, w in COLS) + "  alert" + RST


def row(fields):
    return "  ".join(f"{v:>{w}}" for v, (_, w) in zip(fields, COLS))


def wrap180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def circ_mean_deg(angles):
    z = np.sum(np.exp(1j * np.radians(angles)))
    return float(np.degrees(np.angle(z))) if abs(z) > 0 else 0.0


def circ_std_deg(angles):
    if len(angles) == 0:
        return float("nan")
    R = abs(np.mean(np.exp(1j * np.radians(angles))))
    return float(np.degrees(math.sqrt(-2.0 * math.log(max(min(R, 1.0), 1e-12)))))


def nanmean_quiet(a, axis=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(a, axis=axis)


# ================================================================ stage 2: align
# CIR window alignment. Pure helpers: complex tap arrays in, no I/O.
#
# The firmware anchors each 56-tap window on the detected first path. The
# detector's pick jitters by whole AND fractional taps between packets even when
# nothing moves, so the echo slides inside the window. Every later stage treats
# a column as a fixed delay, so rows are re-aligned before anything else.
#
# Contract: fixed columns (same length out), no invented data (taps shifted in
# from outside the window become NaN), fair scoring (every shift scored on the
# same interior taps), honest flags, phase-safe (pure band-limited delay: never
# rotates or rescales taps, so motion phase on later taps survives — verified by
# the signal-survival self-test, --selftest).

def compute_dev(window, baseline, weights=None) -> float:
    """Deviation (%) between per-tap magnitudes: sum(w|win-base|) / sum(w base) * 100.

    NaN taps are skipped. `weights` down-weights noise-floor taps (stage 3).
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
    """Band-limited shift, y[t] = x(t + s). Circular: the caller masks the edges."""
    k = np.fft.fftfreq(len(x))
    return np.fft.ifft(np.fft.fft(x) * np.exp(2j * np.pi * k * s))


def _score_region(n: int, max_shift: int) -> slice:
    """Interior taps that stay valid for every candidate shift up to max_shift + 0.5."""
    lo, hi = max_shift + 2, n - max_shift - 2
    if hi - lo < 8:
        raise ValueError(f"window of {n} taps is too short for max_shift={max_shift}")
    return slice(lo, hi)


@dataclass
class AlignResult:
    aligned: np.ndarray   # complex, same length as input; NaN where invalid
    valid: np.ndarray     # bool mask of trustworthy taps
    shift: float          # applied shift in taps (aligned[t] = window(t + shift))
    dev_before: float     # dev at shift 0, same interior region
    dev_after: float      # dev after alignment, same interior region
    flags: int            # FLAG_* bits


def align_window(window, baseline, max_shift: int = MAX_SHIFT, fractional: bool = True,
                 weights=None, ambiguity_ratio: float = 1.10,
                 poor_fit_pct: float = 20.0) -> AlignResult:
    """Align one complex CIR window to a baseline (magnitude template).

    1. Integer search over [-max_shift, +max_shift] on one fixed interior region.
       Ties keep the smaller |shift|.
    2. If `fractional`: grid +/-0.5 tap in 0.1 steps around the best integer,
       then a parabolic refinement.
    3. Shift the complex window; taps sourced from outside the window -> NaN.
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


@dataclass
class Baseline:
    mag: np.ndarray        # per-tap mean magnitude of the ALIGNED rows
    shifts: np.ndarray     # shift applied to each input row
    aligned: np.ndarray    # the aligned complex rows themselves
    noise_dev_avg: float   # dev of aligned rows vs the final baseline (quiet-scene noise)
    noise_dev_max: float
    n_rows: int


def build_baseline(windows, max_shift: int = MAX_SHIFT, iterations: int = 3,
                   fractional: bool = True) -> Baseline:
    """Magnitude baseline from ALIGNED rows (median start, align, re-average, repeat).

    Averaging raw (jittered) rows smears the first-path pulse into a template no
    single packet ever matches. Capture only while the scene is still.
    """
    rows = np.asarray(windows, dtype=complex)
    if rows.ndim != 2 or len(rows) < 3:
        raise ValueError("need a 2-D array of at least 3 windows")

    ref = np.median(np.abs(rows), axis=0)
    results: list[AlignResult] = []
    for _ in range(iterations):
        results = [align_window(r, ref, max_shift, fractional, poor_fit_pct=np.inf) for r in rows]
        ref = nanmean_quiet(np.array([np.abs(r.aligned) for r in results]), axis=0)

    devs = np.array([compute_dev(r.aligned, ref) for r in results])
    return Baseline(mag=ref, shifts=np.array([r.shift for r in results]),
                    aligned=np.array([r.aligned for r in results]),
                    noise_dev_avg=float(np.nanmean(devs)), noise_dev_max=float(np.nanmax(devs)),
                    n_rows=len(rows))


class AlignMonitor:
    """Running alignment integrity stats for one link."""

    def __init__(self):
        self.shifts, self.before, self.after = [], [], []
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
        notes = []
        if self._pct(FLAG_EDGE_HIT) > 5:
            notes.append("Many shifts hit the search edge: raise --max-shift, or the first path is jumping.")
        if self._pct(FLAG_AMBIGUOUS) > 5:
            notes.append("Ambiguous alignments: the first path may be flipping between two paths.")
        if self._pct(FLAG_POOR_FIT) > 20:
            notes.append("dev stays high AFTER alignment: the mismatch is not jitter "
                         "(real motion, a disturbance, or gain/accumulation changes).")
        return notes

    def reset(self):
        self.__init__()


# ================================================================ stages 3-13
def phase_reference(aligned):
    """Stage 4: h_k <- h_k conj(h_fp) / |h_fp|^2 (removes common phase and gain)."""
    h_fp = aligned[..., FP_POS:FP_POS + 1]
    return aligned * np.conj(h_fp) / (np.abs(h_fp) ** 2)


def noise_power(rows):
    """Receiver noise power per row from the pre-first-path taps (NaN-aware)."""
    return nanmean_quiet(np.abs(np.atleast_2d(rows)[:, NOISE_COLS]) ** 2, axis=1)


def circle_center(z, noise_pow=None):
    """Stage 5: Kasa arc-center fit. Returns (center, mode); center None = rejected.

    Rejected when the fit is not a trustworthy arc:
      * radius not clearly above the noise: a still tap's noise cloud fits a
        circle around itself, and phase around that center is a random walk
        that fakes a low-frequency peak;
      * arc spans < ARC_MIN_SPAN_DEG: a short noisy arc's center is biased
        (tested: 4 mm read as 12-35 mm, and the peak moved);
      * center inside the point cloud (a short segment fits a small circle
        through its own middle; seen in testing) or points off the circle.
    The caller then uses tap phase: frequency preserved, amplitude a lower bound.
    """
    x, y = z.real, z.imag
    A = np.c_[x, y, np.ones(len(z))]
    try:
        c = np.linalg.lstsq(A, -(x ** 2 + y ** 2), rcond=None)[0]
    except np.linalg.LinAlgError:
        return None, "tap-phase (arc fit failed)"
    center = complex(-c[0] / 2, -c[1] / 2)
    r2 = center.real ** 2 + center.imag ** 2 - c[2]
    zm = z.mean()
    spread = math.sqrt(float(np.mean(np.abs(z - zm) ** 2))) or 1e-12
    if not (np.isfinite(center) and r2 > 0 and abs(center - zm) <= CLUTTER_MAX_RATIO * spread):
        return None, "tap-phase (arc too short to fit)"
    r = math.sqrt(r2)
    dist = np.abs(z - center)
    radial = np.sqrt(np.mean((dist - r) ** 2))
    if radial > 0.25 * r or np.percentile(dist, 2) < 0.5 * r:
        return None, "tap-phase (center inside the point cloud)"
    if noise_pow is not None:
        sigma = math.sqrt(noise_pow / 2)                     # per-axis noise rms
        if r < ARC_MIN_SNR * sigma:
            return None, "tap-phase (no arc above noise)"
        if radial > 2.0 * sigma:
            return None, "tap-phase (arc fit worse than noise)"
    a = np.angle((z - center) * np.conj(zm - center))
    if np.degrees(np.percentile(a, 98) - np.percentile(a, 2)) < ARC_MIN_SPAN_DEG:
        return None, "tap-phase (arc too short to fit)"
    return center, "arc-fit"


def hampel(y, half=HAMPEL_HALF, k=HAMPEL_K):
    """Stage 9: MAD-relative spike detector over valid neighbours. Returns spike mask."""
    spikes = np.zeros(len(y), bool)
    idx = np.flatnonzero(np.isfinite(y))
    for j, i in enumerate(idx):
        nb = y[idx[max(0, j - half):j + half + 1]]
        med = np.median(nb)
        mad = 1.4826 * np.median(np.abs(nb - med))
        if mad > 0 and abs(y[i] - med) > k * mad:
            spikes[i] = True
    return spikes


def fill_short_gaps(y, max_run):
    """Opt-in stage 8 filler: linear interp over NaN runs <= max_run bounded by data."""
    y = y.copy()
    filled = np.zeros(len(y), bool)
    if max_run <= 0:
        return y, filled
    i, n = 0, len(y)
    while i < n:
        if np.isfinite(y[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isfinite(y[j]):
            j += 1
        if i > 0 and j < n and j - i <= max_run:
            y[i:j] = np.interp(np.arange(i, j), [i - 1, j], [y[i - 1], y[j]])
            filled[i:j] = True
        i = j
    return y, filled


@dataclass
class WindowResult:
    link: str
    index: int
    t0: float                      # window start, s since link timeline start
    n: int
    stages: tuple
    t: np.ndarray = None           # sample times relative to window start
    disp: np.ndarray = None        # displacement (mm) after detrend, NaN = masked
    spikes: np.ndarray = None
    filled: np.ndarray = None
    freqs: np.ndarray = None
    power: np.ndarray = None
    tap_snr_db: np.ndarray = None  # static tap SNR (baseline)
    dead: np.ndarray = None
    revived: np.ndarray = None
    motion_ratio: np.ndarray = None
    tap_score: np.ndarray = None
    victim: int = None
    clutter_mode: str = "off"
    estimator: str = "-"
    f_low: float = float("nan")
    f_high: float = float("nan")
    peak_hz: float = float("nan")
    snr_db: float = float("nan")
    snr_margin: float = float("nan")
    noise_level: float = float("nan")
    amp_mm: float = float("nan")
    cross_hz: float = float("nan")  # FFT-after-fill cross-check peak (gappy windows)
    valid_frac: float = 0.0
    filled_frac: float = 0.0
    n_spikes: int = 0
    reasons: dict = field(default_factory=dict)
    snr_ok: bool = False
    valid_ok: bool = False
    stable: bool = None            # None = no previous window to compare
    detected: bool = False
    note: str = ""

    @property
    def bpm(self):
        return self.peak_hz * 60.0


def process_window(Z, ok, dead, tap_snr_db, cfg, prev_peak=None, link="?", index=0, t0=0.0,
                   reasons=None):
    """Stages 3 (revival) and 5-13 on one slow-time x fast-time window.

    Z   : (N, TAPS) complex, rows already gated (1), aligned (2), referenced (4).
          Masked rows are NaN.
    ok  : (N,) bool validity mask, one slot per 100 ms.
    dead: (TAPS,) bool, dead-tap decision held fixed per link (stage 3).
    """
    N = len(ok)
    T = N * DT
    t = np.arange(N) * DT
    r = WindowResult(link=link, index=index, t0=t0, n=N, stages=tuple(cfg.stages),
                     t=t, tap_snr_db=tap_snr_db, dead=dead.copy(),
                     revived=np.zeros(TAPS, bool), reasons=dict(reasons or {}))
    r.f_low, r.f_high = 3.0 / T, FS / 2.0
    r.valid_frac = ok.mean()
    if ok.sum() < max(10, N // 4):
        r.note = "too few valid samples"
        return r

    Zv = Z[ok]
    tv = t[ok]

    # ---- stage 3 (per window): motion check so a weak moving echo is never dropped
    noise_ref = float(np.nanmedian(noise_power(Zv)))
    A = np.c_[tv, np.ones_like(tv)]
    coef = np.linalg.lstsq(A, np.nan_to_num(Zv), rcond=None)[0]
    dyn = nanmean_quiet(np.abs(Zv - A @ coef) ** 2, axis=0)
    r.motion_ratio = dyn / max(noise_ref, 1e-30)
    cand = np.zeros(TAPS, bool)
    cand[FP_POS + 1:] = True
    cand &= np.all(np.isfinite(Zv), axis=0)
    if "deadtap" in cfg.stages:
        r.revived = dead & cand & (r.motion_ratio > REVIVE_RATIO)
        cand &= ~dead | r.revived
    if not cand.any():
        r.note = "no live taps after the first path"
        return r

    # ---- stage 6: victim tap = the most clearly periodic slow-time motion in band.
    # Scored by in-band spectral peak over median (gap-aware), not raw energy: taps
    # on a steep pulse flank carry broadband alignment residual that would
    # otherwise out-score a weak but periodic echo.
    fsel = np.fft.rfftfreq(512, DT)
    fsel = fsel[(fsel >= 3.0 / T) & (fsel <= FS / 2)]
    Zd = Zv - A @ coef
    r.tap_score = np.zeros(TAPS)
    for k in np.flatnonzero(cand):
        p = (lombscargle(tv, Zd[:, k].real, 2 * np.pi * fsel)
             + lombscargle(tv, Zd[:, k].imag, 2 * np.pi * fsel))
        r.tap_score[k] = p.max() / max(np.median(p), 1e-30)
    r.victim = int(np.flatnonzero(cand)[np.argmax(r.tap_score[cand])])
    z = Zv[:, r.victim]

    # ---- stage 5: static clutter (arc-center fit)
    if "clutter" in cfg.stages:
        center, r.clutter_mode = circle_center(z, noise_ref)
    else:
        center, r.clutter_mode = None, "off"
    if center is not None:
        ph = np.angle(z - center)
    else:                                   # frequency kept; amplitude is a lower bound
        ph = np.angle(z * np.conj(z.mean()))

    # ---- stage 7: phase -> path displacement (mm)
    y = np.full(N, np.nan)
    y[ok] = np.degrees(np.unwrap(ph)) / DEG_PER_MM

    # ---- stage 9: Hampel spikes -> masked, never replaced
    r.spikes = hampel(y) if "hampel" in cfg.stages else np.zeros(N, bool)
    r.n_spikes = int(r.spikes.sum())
    y[r.spikes] = np.nan
    real = np.isfinite(y)
    r.valid_frac = real.mean()
    r.valid_ok = (1.0 - r.valid_frac) <= cfg.max_missing

    # ---- stage 10: linear detrend on true times
    if "detrend" in cfg.stages:
        p = np.polyfit(t[real], y[real], 1)
        y[real] -= np.polyval(p, t[real])
    else:
        y[real] -= y[real].mean()

    # ---- stage 8 (opt-in): short-gap filler, flagged
    yf, r.filled = fill_short_gaps(y, cfg.fill_gaps)
    r.filled_frac = r.filled.mean()
    r.disp = yf

    # ---- stages 11-12: band limits + spectrum
    nfft = max(cfg.nfft, N)
    freqs = np.fft.rfftfreq(nfft, DT)[1:]
    if real.all():
        r.estimator = "FFT (Hann, gap-free)"
        spec = np.abs(np.fft.rfft(y * np.hanning(N), nfft)[1:]) ** 2
    else:
        r.estimator = "Lomb-Scargle (gaps)"
        spec = lombscargle(t[real], y[real], 2 * np.pi * freqs, precenter=True)
        if np.isfinite(yf).all():            # cross-check vs FFT after opt-in fill
            cs = np.abs(np.fft.rfft(yf * np.hanning(N), nfft)[1:]) ** 2
            band = (freqs >= r.f_low) & (freqs <= r.f_high)
            r.cross_hz = float(freqs[band][np.argmax(cs[band])])
    r.freqs, r.power = freqs, spec / max(spec.max(), 1e-30)

    # ---- stage 13: detect anywhere in [3/T, fs/2], report the frequency
    band = (freqs >= r.f_low) & (freqs <= r.f_high)
    i = int(np.argmax(np.where(band, r.power, -1)))
    r.peak_hz = float(freqs[i])
    rest = band & (np.abs(freqs - r.peak_hz) > 2.0 / T)
    r.noise_level = float(np.median(r.power[rest])) if rest.any() else float("nan")
    r.snr_db = 10 * math.log10(r.power[i] / max(r.noise_level, 1e-30))
    r.amp_mm = float(np.nanstd(y) * math.sqrt(2))
    r.snr_margin = cfg.snr_db
    r.snr_ok = r.snr_db >= cfg.snr_db
    r.stable = None if prev_peak is None else abs(r.peak_hz - prev_peak) <= 1.0 / T
    r.detected = bool(r.snr_ok and r.valid_ok and r.stable)
    return r


# ================================================================ per-link state
@dataclass
class Config:
    n_base: int = 100
    thresh: float = None
    max_shift: int = MAX_SHIFT
    probe_offset: int = None
    window_s: float = 30.0
    hop_s: float = 30.0
    snr_db: float = 10.0
    max_missing: float = 0.10
    fill_gaps: int = 0
    nfft: int = 2048
    gate_disturbance: bool = False
    stages: tuple = STAGES


class Link:
    """One mole pair: baseline, live metrics, and the rolling slow-time buffer."""

    def __init__(self, name, cfg: Config):
        self.name = name
        self.cfg = cfg
        self.N = int(round(cfg.window_s * FS))
        self.hop = max(1, int(round(cfg.hop_s * FS)))
        self.reset()

    def reset(self):
        self.pre = []                # (abs_seq, raw complex row) captured during baseline
        self.pre_bad = {}            # abs_seq -> reason, during baseline
        self.base = None
        self.last_seq = None
        self.abs_seq = None
        self.lost = 0
        self.miss_streak = 0
        self.slots = {}              # abs_seq -> referenced row, or reason string if masked
        self.next_emit = None
        self.win_index = 0
        self.prev_peak = None
        self.ready = []              # WindowResults waiting to be shown
        self.align_mon = AlignMonitor()
        self.reset_period()

    def reset_period(self):
        self.p_start = time.time()
        self.p_ok = self.p_miss = self.p_lost = 0
        self.p_snr, self.p_dev, self.p_sdev, self.p_dphi = [], [], [], []

    # ------------------------------------------------ timeline (stage 8)
    def _advance_seq(self, seq):
        """Unwrap the 16-bit seq into an absolute sample index. Returns #lost before it."""
        if self.last_seq is None:
            self.abs_seq = 0
            self.last_seq = seq
            return 0
        step = (seq - self.last_seq) & 0xFFFF
        self.last_seq = seq
        if step == 0 or step > 1000:           # duplicate or mole reboot: restart the timeline
            self.slots.clear()
            self.next_emit = None
            self.abs_seq += 1
            self.prev_peak = None
            return 0
        self.abs_seq += step
        return step - 1

    def _put(self, a, value):
        if self.next_emit is None:
            self.next_emit = a + self.N
        self.slots[a] = value
        while self.next_emit is not None and a >= self.next_emit:
            self._emit(self.next_emit)
            self.next_emit += self.hop
            if a - self.next_emit > 10 * self.N:   # long outage: skip empty windows
                self.next_emit = a + self.N
        for k in [k for k in self.slots if k < a - 2 * self.N]:
            del self.slots[k]

    def _emit(self, end):
        start = end - self.N
        Z = np.full((self.N, TAPS), np.nan, complex)
        ok = np.zeros(self.N, bool)
        reasons = {}
        for i in range(self.N):
            v = self.slots.get(start + i, "lost")
            if isinstance(v, str):
                reasons[v] = reasons.get(v, 0) + 1
            else:
                Z[i], ok[i] = v, True
        r = process_window(Z, ok, self.dead, self.tap_snr_db, self.cfg, self.prev_peak,
                           link=self.name, index=self.win_index, t0=start * DT, reasons=reasons)
        if r.snr_ok and r.valid_ok:
            self.prev_peak = r.peak_hz
        elif not r.valid_ok:
            self.prev_peak = None
        self.win_index += 1
        self.ready.append(r)

    # ------------------------------------------------ per-packet entry point
    def feed(self, seq, rng, fp_idx, ci, cq):
        """Returns a dict describing the packet for the terminal line."""
        lost = self._advance_seq(seq)
        out = {"lost": lost, "kind": None}
        if lost:
            self.lost += lost
            self.p_lost += lost
            for k in range(self.abs_seq - lost, self.abs_seq):
                self._mark(k, "lost")

        # ---- stage 1: validate / gate
        ci, cq = np.asarray(ci), np.asarray(cq)
        if fp_idx == 0xFFFF or not math.isfinite(rng):
            self.miss_streak += 1
            self.p_miss += 1
            self._mark(self.abs_seq, "miss")
            out["kind"] = "miss"
            return out
        self.miss_streak = 0
        if "gate" in self.cfg.stages and (np.any(np.abs(ci) >= SAT_LEVEL) or np.any(np.abs(cq) >= SAT_LEVEL)):
            self._mark(self.abs_seq, "saturated")
            out["kind"] = "gated"
            return out
        raw = ci.astype(float) + 1j * cq.astype(float)

        if self.base is None:
            self.pre.append((self.abs_seq, raw, rng))
            out.update(kind="baseline", n=len(self.pre))
            if len(self.pre) >= self.cfg.n_base:
                self._lock_baseline()
                out["kind"] = "locked"
            return out

        # ---- stage 2: align
        res = self._align(raw)
        self.align_mon.update(res)
        live = self._live_metrics(res, rng)
        out.update(kind="live", **live)
        if self.cfg.gate_disturbance and res.flags & FLAG_POOR_FIT:
            self._mark(self.abs_seq, "disturbance")
        else:
            self._put(self.abs_seq, self._reference(res.aligned))
        return out

    def _mark(self, a, reason):
        if self.base is None:
            self.pre_bad[a] = reason
        else:
            self._put(a, reason)

    def _align(self, raw):
        if "align" in self.cfg.stages:
            return align_window(raw, self.base.mag, self.cfg.max_shift, weights=self.weights)
        d = compute_dev(raw, self.base.mag, self.weights)
        return AlignResult(raw.copy(), np.ones(TAPS, bool), 0.0, d, d, 0)

    def _reference(self, aligned):
        return phase_reference(aligned) if "reference" in self.cfg.stages else aligned

    def _lock_baseline(self):
        rows = np.array([r for _, r, _ in self.pre])
        max_shift = self.cfg.max_shift if "align" in self.cfg.stages else 0
        self.base = build_baseline(rows, max_shift, fractional="align" in self.cfg.stages)
        B = self.base.aligned
        self.base_range = float(np.mean([g for _, _, g in self.pre]))

        # ---- stage 3: dead taps, decided once per link from the still baseline
        self.noise_pow = float(np.nanmedian(noise_power(B)))
        self.base_noise = math.sqrt(self.noise_pow)
        tap_pow = nanmean_quiet(np.abs(B) ** 2, axis=0)
        snr = tap_pow / max(self.noise_pow, 1e-30)
        self.tap_snr_db = 10 * np.log10(np.maximum(snr, 1e-6))
        self.dead = ~(snr >= DEAD_SNR)                  # NaN edge columns count as dead
        self.dead[FP_POS] = False
        if "deadtap" in self.cfg.stages:
            self.weights = np.where(self.dead, 0.0, np.clip(1 - 1 / np.maximum(snr, 1e-6), 0, 1))
        else:
            self.dead[:] = False
            self.weights = None
        self.sig_mask = self.base.mag > SIG_K * self.base_noise

        self.base_fpmag = float(np.nanmax(self.base.mag[FP_POS - 1:FP_POS + 4]))
        self.base_snr = 20 * math.log10(max(self.base_fpmag, 1e-9) / max(self.base_noise, 1e-9))
        if self.cfg.probe_offset is not None:
            self.probe = FP_POS + self.cfg.probe_offset
        else:
            self.probe = max(PROBE_SEARCH, key=lambda k: self.base.mag[k])
        self.probe_snr = 20 * math.log10(max(self.base.mag[self.probe], 1e-9) / max(self.base_noise, 1e-9))

        ref = phase_reference(B)
        phases = np.degrees(np.angle(ref[:, self.probe]))
        phases = phases[np.isfinite(phases)]
        self.base_phase = circ_mean_deg(phases)
        self.base_phase_std = circ_std_deg(wrap180(phases - self.base_phase))

        self.noise_avg, self.noise_max = self.base.noise_dev_avg, self.base.noise_dev_max
        raw_devs = [compute_dev(r, self.base.mag) for r in rows]
        self.noise_avg_raw, self.noise_max_raw = float(np.mean(raw_devs)), float(np.max(raw_devs))
        self.noise_sig_avg = float(np.nanmean([compute_dev(a, self.base.mag, self.sig_mask) for a in B]))
        self.thresh = self.cfg.thresh or max(2.0 * self.noise_max, 5.0)

        # The still baseline is real data: seed the 30 s buffer with it (aligned + referenced).
        seeded = {a: row for (a, _, _), row in zip(self.pre, B)}
        for a in range(self.pre[0][0], self.abs_seq + 1):
            if a in seeded:
                self._put(a, self._reference(seeded[a]))
            else:
                self._put(a, self.pre_bad.get(a, "lost"))
        self.pre, self.pre_bad = [], {}
        self.reset_period()

    def _live_metrics(self, res, rng):
        a = res.aligned
        mags = np.abs(a)
        sd = compute_dev(a, self.base.mag, self.sig_mask)
        fpmag = float(np.nanmax(mags[FP_POS - 1:FP_POS + 4]))
        noise = math.sqrt(float(nanmean_quiet(mags[NOISE_COLS] ** 2)))
        snr = 20 * math.log10(max(fpmag, 1e-9) / max(noise, 1e-9))
        h_fp, h_p = a[FP_POS], a[self.probe]
        ph = float(np.degrees(np.angle(h_p * np.conj(h_fp)))) if np.isfinite(h_p) and abs(h_fp) > 0 else None
        dphi = wrap180(ph - self.base_phase) if ph is not None else float("nan")

        self.p_ok += 1
        self.p_snr.append(snr)
        self.p_dev.append(res.dev_after)
        if np.isfinite(sd):
            self.p_sdev.append(sd)
        if ph is not None:
            self.p_dphi.append(dphi)
        return dict(dev=res.dev_after, sigdev=sd, sh=res.shift, fp_pct=100.0 * fpmag / (self.base_fpmag or 1.0),
                    snr=snr, dphi=dphi, drange=rng - self.base_range, mags=mags)


# ================================================================ terminal output
def fft_verdict(phi_std):
    if math.isnan(phi_std):
        return "n/a", DIM
    if phi_std <= 5:
        return "excellent", GRN
    if phi_std <= 15:
        return "usable", GRN
    if phi_std <= 30:
        return "marginal", YEL
    return "not FFT-ready", RED


def print_summary(lk):
    dt = max(time.time() - lk.p_start, 1e-9)
    tried = lk.p_ok + lk.p_miss
    miss = 100.0 * lk.p_miss / tried if tried else 0.0
    lost = 100.0 * lk.p_lost / (tried + lk.p_lost) if (tried + lk.p_lost) else 0.0
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    phi_std = circ_std_deg(lk.p_dphi)
    verdict, vcol = fft_verdict(phi_std)
    print(f"{CYN}── {lk.name} {dt:4.1f}s │ {tried / dt:4.1f} pkt/s  miss {miss:3.0f}%  lost {lost:3.0f}%  "
          f"SNR {avg(lk.p_snr):4.1f} dB  dev {avg(lk.p_dev):5.1f}%  sigdev {avg(lk.p_sdev):5.1f}%  "
          f"φσ {phi_std:5.1f}° ({phi_std / DEG_PER_MM:4.2f} mm){RST}  "
          f"{vcol}phase: {verdict}{RST} {DIM}(if scene is still){RST}")
    lk.reset_period()


def print_locked(lk):
    print(f"\n{GRN}{BOLD}baseline locked for {lk.name}:{RST}{GRN} range {lk.base_range:.3f} m, "
          f"fp_mag {lk.base_fpmag:.0f}, SNR {lk.base_snr:.1f} dB{RST}")
    print(f"{DIM}   noise dev  aligned avg {lk.noise_avg:.1f}% / max {lk.noise_max:.1f}%   "
          f"(no alignment: avg {lk.noise_avg_raw:.1f}% / max {lk.noise_max_raw:.1f}%)   "
          f"signal taps only: avg {lk.noise_sig_avg:.1f}% over {int(lk.sig_mask.sum())} taps{RST}")
    live = int((~lk.dead).sum())
    print(f"{DIM}   dead taps (stage 3): {int(lk.dead.sum())} at the noise floor, {live} kept "
          f"(noise from fp-8..fp-3, dead = < {10 * math.log10(DEAD_SNR):.0f} dB, revived if they move){RST}")
    print(f"{DIM}   phase probe fp+{lk.probe - FP_POS} (SNR {lk.probe_snr:.1f} dB), baseline phase spread "
          f"{lk.base_phase_std:.1f}° ({lk.base_phase_std / DEG_PER_MM:.2f} mm){RST}")
    if lk.probe_snr < 12:
        print(f"{YEL}   probe tap is near the noise floor — its phase will be unreliable{RST}")
    print(f"{GRN}   -> alert at dev > {lk.thresh:.1f}%   first FFT window at "
          f"{lk.N * DT:.0f} s (baseline counts toward it), then every {lk.hop * DT:.0f} s{RST}\n")


def amp_note(r):
    return " (lower bound: no arc fit)" if r.clutter_mode.startswith("tap-phase") else ""


def print_window(r: WindowResult):
    if r.victim is None:
        print(f"\n{YEL}{BOLD}■ {r.link} window #{r.index}{RST}{YEL}  no spectrum: {r.note}  "
              f"(valid {100 * r.valid_frac:.0f}%, masked {r.reasons}){RST}\n")
        return
    if not r.valid_ok:
        state, col = "INVALID (too many gaps)", YEL
    elif r.detected:
        state, col = "DETECTED", GRN
    elif r.snr_ok and r.stable is None:
        state, col = "candidate (needs a 2nd window to confirm)", YEL
    elif r.snr_ok:
        state, col = "candidate (peak moved since last window)", YEL
    else:
        state, col = "no clear peak", DIM
    in_ref = BREATH_REF_HZ[0] <= r.peak_hz <= BREATH_REF_HZ[1]
    print(f"\n{col}{BOLD}■ {r.link} window #{r.index}  t={r.t0:6.1f}-{r.t0 + r.n * DT:6.1f} s  {state}{RST}")
    print(f"  peak {r.peak_hz:.3f} Hz ({r.bpm:4.1f} /min){' in breathing range' if in_ref else ''}   "
          f"SNR {r.snr_db:4.1f} dB (margin {r.snr_margin:.0f} dB)   amp ≈ {r.amp_mm:.2f} mm path{amp_note(r)}")
    print(f"{DIM}  victim tap fp+{r.victim - FP_POS}  clutter {r.clutter_mode}  {r.estimator}"
          f"{'' if not np.isfinite(r.cross_hz) else f'  (FFT-after-fill cross-check {r.cross_hz:.3f} Hz)'}\n"
          f"  valid {100 * r.valid_frac:.0f}%  filled {100 * r.filled_frac:.0f}%  spikes {r.n_spikes}  "
          f"masked {r.reasons or '{}'}  revived taps {list(np.flatnonzero(r.revived) - FP_POS) or '-'}\n"
          f"  band {r.f_low:.2f}-{r.f_high:.1f} Hz  stages on: {','.join(r.stages)}{RST}\n")


# ================================================================ FFT plot
# Palette: reference categorical slots (dataviz palette.md), light surface.
C_SURF, C_TEXT, C_TEXT2, C_GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
C_SERIES, C_ACCENT, C_FILL, C_MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#c3c2b7"
C_GOOD, C_WARN = "#008300", "#c98500"


class FFTPlot:
    def __init__(self, interactive, out_dir=None):
        import matplotlib
        if not interactive:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt
        self.interactive = interactive
        self.out_dir = out_dir
        plt.rcParams.update({
            "figure.facecolor": C_SURF, "axes.facecolor": C_SURF, "savefig.facecolor": C_SURF,
            "axes.edgecolor": C_GRID, "axes.labelcolor": C_TEXT2, "text.color": C_TEXT,
            "xtick.color": C_TEXT2, "ytick.color": C_TEXT2, "axes.grid": True,
            "grid.color": C_GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
            "axes.spines.right": False, "font.size": 9, "axes.titlesize": 10,
            "axes.titleweight": "bold", "axes.titlelocation": "left",
        })
        if interactive:
            plt.ion()
        self.fig = plt.figure(figsize=(11, 7.5))
        gs = self.fig.add_gridspec(2, 2, height_ratios=[1, 1.35], width_ratios=[2.2, 1],
                                   hspace=0.45, wspace=0.22)
        self.ax_t = self.fig.add_subplot(gs[0, :])
        self.ax_f = self.fig.add_subplot(gs[1, 0])
        self.ax_k = self.fig.add_subplot(gs[1, 1])
        if interactive:
            self.fig.canvas.manager.set_window_title("MOLES / PUPS — 30 s FFT")
            self.fig.text(0.5, 0.5, "waiting for the first 30 s window…", ha="center",
                          color=C_TEXT2, fontsize=12)
            plt.show(block=False)
            self.pump()

    def pump(self):
        if self.interactive:
            self.fig.canvas.flush_events()

    def alive(self):
        return not self.interactive or self.plt.fignum_exists(self.fig.number)

    def show(self, r: WindowResult):
        fig = self.fig
        for t in list(fig.texts):
            t.remove()
        for ax in (self.ax_t, self.ax_f, self.ax_k):
            ax.clear()

        if not r.valid_ok or r.victim is None:
            state, scol = "INVALID window" if r.victim is not None else f"no spectrum: {r.note}", C_WARN
        elif r.detected:
            state, scol = "✓ DETECTED", C_GOOD
        elif r.snr_ok:
            state, scol = "? candidate — confirm next window", C_WARN
        else:
            state, scol = "— no clear peak", C_TEXT2
        fig.text(0.06, 0.965, f"{r.link}  ·  window #{r.index}  ·  t = {r.t0:.0f}–{r.t0 + r.n * DT:.0f} s  "
                 f"·  {r.n} samples @ {FS:.0f} Hz", fontsize=12, weight="bold", color=C_TEXT)
        fig.text(0.06, 0.935, state, fontsize=12, weight="bold", color=scol)
        fig.text(0.94, 0.935, f"stages: {', '.join(r.stages)}", fontsize=8, color=C_TEXT2, ha="right")

        if r.victim is None:
            self._draw()
            return r

        # ---- displacement (slow time)
        ax = self.ax_t
        real = np.isfinite(r.disp) & ~r.filled
        ax.plot(r.t, np.where(real | r.filled, r.disp, np.nan), color=C_SERIES, lw=1.6)
        if r.filled.any():
            ax.plot(r.t[r.filled], r.disp[r.filled], "o", ms=5, mfc=C_SURF, mec=C_FILL, mew=1.5,
                    label=f"filled ({r.filled.sum()})")
        if r.spikes is not None and r.spikes.any():
            for x in r.t[r.spikes]:
                ax.axvline(x, color=C_ACCENT, lw=1, alpha=0.6)
            ax.plot([], [], color=C_ACCENT, lw=1, label=f"spike removed ({r.n_spikes})")
        gaps = ~np.isfinite(r.disp)
        if gaps.any():
            ax.fill_between(r.t, 0, 1, where=gaps, transform=ax.get_xaxis_transform(),
                            color=C_MUTED, alpha=0.35, lw=0, step="mid", label=f"masked ({gaps.sum()})")
        ax.set_title(f"Path-length change on victim tap fp+{r.victim - FP_POS} (detrended)")
        ax.set_xlabel("time in window (s)")
        ax.set_ylabel("mm")
        ax.set_xlim(0, r.n * DT)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="upper right", frameon=False, fontsize=8, ncol=3)

        # ---- spectrum
        ax = self.ax_f
        db = 10 * np.log10(np.maximum(r.power, 1e-12))
        ax.axvspan(0, r.f_low, color=C_MUTED, alpha=0.3, lw=0)
        ax.axvspan(*BREATH_REF_HZ, color=C_FILL, alpha=0.10, lw=0)
        ax.plot(r.freqs, db, color=C_SERIES, lw=1.6)
        noise_db = 10 * math.log10(max(r.noise_level, 1e-12))
        ax.axhline(noise_db, color=C_TEXT2, lw=1, ls="--")
        ax.axhline(noise_db + r.snr_margin, color=C_WARN, lw=1, ls=":")
        pk_db = 10 * math.log10(max(np.interp(r.peak_hz, r.freqs, r.power), 1e-12))
        ax.plot([r.peak_hz], [pk_db], "o", ms=8, color=scol if r.valid_ok else C_WARN,
                mec=C_SURF, mew=2, zorder=5)
        ax.annotate(f"{r.peak_hz:.3f} Hz  ·  {r.bpm:.1f} /min\nSNR {r.snr_db:.1f} dB",
                    (r.peak_hz, pk_db), xytext=(12, -4), textcoords="offset points",
                    fontsize=9, color=C_TEXT, va="top")
        fmax = min(r.f_high, max(1.5, 3 * r.peak_hz))
        ax.set_xlim(0, fmax)
        ax.set_ylim(min(noise_db - 15, np.percentile(db, 5)), 3)
        ax.text(r.f_low / 2, 0.02, "< 3\ncycles", transform=ax.get_xaxis_transform(),
                ha="center", va="bottom", fontsize=7, color=C_TEXT2)
        ax.text(np.mean(BREATH_REF_HZ), 0.02, "typical breathing (reference only)",
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=7, color=C_TEXT2)
        ax.text(fmax, noise_db, " noise floor", fontsize=7, color=C_TEXT2, va="bottom", ha="right")
        ax.text(fmax, noise_db + r.snr_margin, " SNR margin", fontsize=7, color=C_WARN, va="bottom", ha="right")
        ax.set_title(f"Spectrum — {r.estimator}")
        ax.set_xlabel(f"frequency (Hz)   ·   resolution 1/T = {1 / (r.n * DT):.3f} Hz")
        ax.set_ylabel("power (dB re peak)")
        sec = ax.secondary_xaxis("top", functions=(lambda f: f * 60, lambda b: b / 60))
        sec.set_xlabel("per minute", color=C_TEXT2, fontsize=8)
        sec.tick_params(colors=C_TEXT2, labelsize=8)

        # ---- taps (stage 3 / 6)
        ax = self.ax_k
        k = np.arange(TAPS) - FP_POS
        colors = np.array([C_SERIES if not d else C_MUTED for d in r.dead], dtype=object)
        colors[r.revived] = C_FILL
        colors[r.victim] = C_ACCENT
        ax.bar(k, np.nan_to_num(r.tap_snr_db, nan=0), color=list(colors), width=0.8)
        ax.axhline(10 * math.log10(DEAD_SNR), color=C_TEXT2, lw=1, ls="--")
        ax.set_title("Tap SNR at baseline")
        ax.set_xlabel("tap (0 = first path)")
        ax.set_ylabel("dB over noise")
        ax.set_xlim(-FP_POS - 1, TAPS - FP_POS)
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(color=C_SERIES, label="live"), Patch(color=C_MUTED, label="dead"),
                           Patch(color=C_FILL, label="revived (moving)"),
                           Patch(color=C_ACCENT, label=f"victim fp+{r.victim - FP_POS}")],
                  loc="upper right", frameon=False, fontsize=7)

        fig.text(0.06, 0.015,
                 f"valid {100 * r.valid_frac:.0f}%   filled {100 * r.filled_frac:.0f}%   "
                 f"spikes {r.n_spikes}   masked {r.reasons or '{}'}   clutter: {r.clutter_mode}   "
                 f"amp ≈ {r.amp_mm:.2f} mm path{amp_note(r)}   stable: "
                 f"{'n/a' if r.stable is None else ('yes' if r.stable else 'no')}",
                 fontsize=8, color=C_TEXT2)
        self._draw()
        return r

    def _draw(self):
        if self.interactive:
            self.fig.canvas.draw_idle()
            self.plt.pause(0.001)

    def save(self, r):
        if self.out_dir:
            p = os.path.join(self.out_dir, f"{r.link.replace('->', '')}_win{r.index:04d}.png")
            self.fig.savefig(p, dpi=110)
            return p


# ================================================================ serial framing
class FrameReader:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                self.buf = self.buf[-1:]
                break
            if len(self.buf) < i + 5:
                self.buf = self.buf[i:]
                break
            ftype = self.buf[i + 2]
            n = self.buf[i + 3] | (self.buf[i + 4] << 8)
            if n > MAX_FRAME:
                self.buf = self.buf[i + 2:]
                continue
            end = i + 5 + n
            if len(self.buf) < end + 1:
                self.buf = self.buf[i:]
                break
            payload = bytes(self.buf[i + 5:end])
            ck = ftype ^ self.buf[i + 3] ^ self.buf[i + 4]
            for b in payload:
                ck ^= b
            if ck == self.buf[end]:
                frames.append((ftype, payload))
                self.buf = self.buf[end + 1:]
            else:
                self.buf = self.buf[i + 2:]
        return frames


# ================================================================ raw capture (stage 0)
RAW_HEADER = (["host_t", "seq", "link_id", "range_m", "fp_idx"]
              + [f"i{k}" for k in range(TAPS)] + [f"q{k}" for k in range(TAPS)])


def open_run_dir(base, tag):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = os.path.join(base, f"{stamp}_{tag}")
    os.makedirs(d, exist_ok=True)
    return d


def read_raw(path):
    """Yield (host_t, seq, link_id, range_m, fp_idx, ci, cq). Accepts old files without host_t."""
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        header = next(rd, None) or []
        off = 1 if header[:1] == ["host_t"] else 0
        if header[off:off + 4] != ["seq", "link_id", "range_m", "fp_idx"]:
            sys.exit(f"{RED}{path} is not a moles_monitor raw capture (unexpected header){RST}")
        for rw in rd:
            if len(rw) != off + 4 + 2 * TAPS:
                continue
            ht = float(rw[0]) if off else float("nan")
            v = rw[off:]
            yield (ht, int(v[0]), int(v[1]), float(v[2]), int(v[3]),
                   [int(x) for x in v[4:4 + TAPS]], [int(x) for x in v[4 + TAPS:4 + 2 * TAPS]])


class WindowLog:
    FIELDS = ["link", "window", "t0_s", "stages", "victim_tap", "clutter", "estimator", "peak_hz",
              "per_min", "snr_db", "amp_mm", "valid_frac", "filled_frac", "spikes", "masked",
              "cross_hz", "snr_ok", "valid_ok", "stable", "detected"]

    def __init__(self, path):
        self.fh = open(path, "w", newline="")
        self.w = csv.writer(self.fh)
        self.w.writerow(self.FIELDS)

    def add(self, r):
        self.w.writerow([r.link, r.index, f"{r.t0:.1f}", "+".join(r.stages),
                         "" if r.victim is None else r.victim - FP_POS, r.clutter_mode, r.estimator,
                         f"{r.peak_hz:.4f}", f"{r.bpm:.2f}", f"{r.snr_db:.2f}", f"{r.amp_mm:.3f}",
                         f"{r.valid_frac:.3f}", f"{r.filled_frac:.3f}", r.n_spikes,
                         ";".join(f"{k}={v}" for k, v in r.reasons.items()), f"{r.cross_hz:.4f}",
                         r.snr_ok, r.valid_ok, r.stable, r.detected])
        self.fh.flush()

    def close(self):
        self.fh.close()


# ================================================================ packet handling (live + replay)
class Session:
    """Shared by live and replay so both run exactly the same code."""

    def __init__(self, args, cfg, plot, wlog):
        self.args, self.cfg, self.plot, self.wlog = args, cfg, plot, wlog
        self.links = {}
        self.live_lines = 0
        self.windows = []

    def packet(self, seq, link_id, rng, fp_idx, ci, cq):
        a = self.args
        name = LINK_NAMES.get(link_id, f"L{link_id:02X}")
        if a.link != "all" and name != a.link:
            return
        lk = self.links.get(link_id)
        if lk is None:
            lk = self.links[link_id] = Link(name, self.cfg)
        o = lk.feed(seq, rng, fp_idx, ci, cq)
        gap = f"  {DIM}(+{o['lost']} lost over ESP-NOW){RST}" if o["lost"] else ""
        summary_due = (lk.base is not None and a.summary > 0 and time.time() - lk.p_start >= a.summary
                       and not a.replay)

        if o["kind"] == "miss":
            if a.lines:
                print(f"{RED}{seq:05d}  {name}  ---- no response from responder (miss x{lk.miss_streak}) "
                      f"— link blocked or B not running ----{RST}{gap}")
        elif o["kind"] == "gated":
            if a.lines:
                print(f"{YEL}{seq:05d}  {name}  ---- CIR saturated, sample masked ----{RST}{gap}")
        elif o["kind"] == "baseline":
            n = o["n"]
            if n == 1 or n % 10 == 0:
                print(f"{DIM}{seq:05d}  {name}  capturing baseline {n:3d}/{self.cfg.n_base} — keep the "
                      f"link clear   range {rng:6.3f} m  fp {fp_idx}{RST}")
        elif o["kind"] == "locked":
            print_locked(lk)
            if a.lines:
                print(header_line())
            self.live_lines = 0
        elif o["kind"] == "live" and a.lines:
            d = o["dev"]
            color, flag = (RED, "  <<< DISTURBED") if d > lk.thresh else ((YEL, "") if d > 0.6 * lk.thresh else ("", ""))
            if self.live_lines and self.live_lines % HEADER_EVERY == 0:
                print(header_line())
            self.live_lines += 1
            fields = [f"{seq:05d}", name, f"{rng:6.3f}", f"{o['drange']:+7.3f}", f"{fp_idx:4d}",
                      f"{o['snr']:4.0f}dB", f"{o['fp_pct']:4.0f}%", f"{d:5.1f}%", f"{o['sigdev']:5.1f}%",
                      f"{o['sh']:+.2f}", f"{o['dphi']:+6.1f}°", f"{o['dphi'] / DEG_PER_MM:+5.1f}mm"]
            shape = "" if a.no_shape else "  " + sparkline(o["mags"], float(np.nanmax(lk.base.mag)))
            print(f"{color}{row(fields)}  {meter(d, lk.thresh)}{flag}{RST}{gap}{shape}")

        if summary_due:
            print_summary(lk)
        while lk.ready:
            r = lk.ready.pop(0)
            self.windows.append(r)
            print_window(r)
            if self.wlog:
                self.wlog.add(r)
            if self.plot:
                self.plot.show(r)
                p = self.plot.save(r)
                if p:
                    print(f"{DIM}  plot -> {p}{RST}")
                if a.replay and a.show:
                    self.plot.plt.pause(a.show_pause)
            if lk.align_mon.shifts:
                print(f"{DIM}  {lk.align_mon.summary()}{RST}")
                for note in lk.align_mon.diagnosis():
                    print(f"{YEL}  ! {note}{RST}")
                lk.align_mon.reset()


# ================================================================ serial helpers
def find_port(list_ports):
    keys = ("usbserial", "SLAB", "CP210", "CH340", "CH910", "wchusbserial", "usbmodem", "USB")
    cands = [p for p in list_ports.comports()
             if any(k.lower() in (p.device + " " + (p.description or "")).lower() for k in keys)]
    if len(cands) == 1:
        return cands[0].device
    if not cands:
        sys.exit("No ESP32 serial port found. Plug in the host ESP32 or pass --port.")
    print("More than one serial port found — pass the host's with --port:")
    for p in cands:
        print(f"  {p.device:35s} {p.description}")
    sys.exit(1)


def open_port(serial, port, baud):
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, baud, 0.05
    s.dtr = False      # don't reset the host ESP32 when the port opens
    s.rts = False
    s.open()
    return s


def stdin_commands(state):
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "q":
            state["quit"] = True
            return
        if cmd == "b":
            state["rebase"] = True
        if cmd == "h":
            state["legend"] = True


# ================================================================ self-tests
def _synth_link_packets(seconds, f_breath, amp_mm, seed, loss=0.03, miss=0.02, jitter=True):
    """Synthetic A->B packets: still paths + one weak echo moving by amp_mm of path."""
    rng = np.random.default_rng(seed)
    t = np.arange(TAPS)
    pulse = lambda x: np.exp(-(x / 1.1) ** 2)
    paths = [(FP_POS, 3000.0), (12, 1200.0), (20, 700.0), (33, 400.0)]
    echo_tap, echo_amp = FP_POS + 3, 300.0
    n = int(seconds * FS)
    for k in range(n):
        if rng.random() < loss:
            continue                                         # ESP-NOW loss: seq gap
        if rng.random() < miss:
            yield k & 0xFFFF, 0x01, float("nan"), 0xFFFF, [0] * TAPS, [0] * TAPS
            continue
        d = rng.uniform(-1.5, 1.5) if jitter else 0.0
        phi = np.radians(amp_mm * DEG_PER_MM) * np.sin(2 * np.pi * f_breath * k * DT)
        h = sum(a * pulse(t - (p - d)) for p, a in paths).astype(complex)
        h += echo_amp * pulse(t - (echo_tap - d)) * np.exp(1j * (0.7 + phi))
        h *= rng.normal(1.0, 0.05) * np.exp(1j * rng.uniform(0, 2 * np.pi))
        h += rng.normal(0, 25, TAPS) + 1j * rng.normal(0, 25, TAPS)
        yield (k & 0xFFFF, 0x01, 1.2 + rng.normal(0, 0.02), 740,
               np.round(h.real).astype(int).tolist(), np.round(h.imag).astype(int).tolist())


def _run_synth(f_breath, amp_mm, seconds=130, seed=3, stages=STAGES):
    cfg = Config(stages=tuple(stages))
    lk = Link("A->B", cfg)
    out = []
    for seq, _, rg, fp, ci, cq in _synth_link_packets(seconds, f_breath, amp_mm, seed):
        lk.feed(seq, rg, fp, ci, cq)
        out.extend(lk.ready)
        lk.ready.clear()
    return out


def _selftest_align(seed=1):
    """Signal-survival test (gate G4 for stage 2): the wobble survives alignment."""
    n, rows, fs = TAPS, 300, FS
    f_wobble, wobble_deg = 0.27, 70.0
    echo_tap = FP_POS + 2
    paths = [(FP_POS, 1000.0), (12, 400.0), (20, 250.0), (33, 150.0)]
    t = np.arange(n)
    pulse = lambda x: np.exp(-(x / 1.1) ** 2)

    def make(jitter):
        r = np.random.default_rng(seed + 7)
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

    def wobble(rows_c):
        z = rows_c[:, echo_tap] * np.conj(rows_c[:, FP_POS]) / np.abs(rows_c[:, FP_POS]) ** 2
        c, _ = circle_center(z)
        ph = np.unwrap(np.angle(z - c))
        ph -= np.polyval(np.polyfit(np.arange(len(ph)), ph, 1), np.arange(len(ph)))
        spec = np.abs(np.fft.rfft(ph * np.hanning(len(ph)), 8192))
        f = np.fft.rfftfreq(8192, 1 / fs)
        band = f >= 3.0 / (rows / fs)
        return float(f[band][np.argmax(spec[band])]), float(np.degrees(np.std(ph)))

    ref_rows, _ = make(False)
    jit_rows, truth = make(True)
    base = build_baseline(jit_rows[:30])
    mon = AlignMonitor()
    aligned = []
    for rw in jit_rows:
        res = align_window(rw, base.mag)
        mon.update(res)
        aligned.append(res.aligned)
    aligned = np.array(aligned)
    err = np.array(mon.shifts) - truth
    err -= err.mean()
    f_ref, a_ref = wobble(ref_rows)
    f_al, a_al = wobble(aligned)
    print(f"  {mon.summary()}")
    print(f"  wobble reference {f_ref:.3f} Hz {a_ref:5.1f}° rms  ->  aligned {f_al:.3f} Hz {a_al:5.1f}° rms")
    res_hz = 1.0 / (rows / fs)
    return {
        "G4 align: dev reduced by alignment": np.nanmedian(mon.after) < 0.5 * np.nanmedian(mon.before),
        "G4 align: shift error < 0.15 tap rms": np.sqrt(np.mean(err ** 2)) < 0.15,
        "G4 align: wobble frequency survives": abs(f_al - f_ref) < res_hz / 2,
        "G4 align: wobble size survives (±15%)": abs(a_al - a_ref) <= 0.15 * a_ref,
    }


def selftest():
    print(f"{BOLD}Self-test (synthetic data — hardware gates G1-G5 still have to be run on real captures){RST}")
    checks = _selftest_align()

    f0 = 0.25
    win = _run_synth(f0, amp_mm=3.0)
    for r in win:
        print(f"  breathing 0.25 Hz, 3 mm   win #{r.index}: peak {r.peak_hz:.3f} Hz  SNR {r.snr_db:4.1f} dB  "
              f"victim fp+{(r.victim or FP_POS) - FP_POS}  valid {100 * r.valid_frac:.0f}%  {r.estimator}  "
              f"detected={r.detected}")
    checks["pipeline: 30 s windows emitted"] = len(win) >= 3
    checks["pipeline: peak within 1/T of truth"] = all(abs(r.peak_hz - f0) <= 1 / 30 for r in win)
    checks["pipeline: detected after 2nd window"] = all(r.detected for r in win[1:])
    checks["pipeline: victim is the moving tap"] = all(r.victim == FP_POS + 3 for r in win)

    limit = None
    for amp in (1.0, 0.5, 0.3, 0.2):
        w = _run_synth(f0, amp_mm=amp, seed=5)
        ok = all(abs(r.peak_hz - f0) <= 1 / 30 for r in w) and all(r.detected for r in w[1:])
        print(f"  sweep {amp:3.1f} mm: peaks {[round(r.peak_hz, 3) for r in w]}  "
              f"SNR {[round(r.snr_db, 1) for r in w]}  {'detected' if ok else 'lost'}")
        if amp == 0.5:
            quiet = w
            checks["G3 sweep: 0.5 mm same frequency, detected"] = ok
        if ok:
            limit = amp
    print(f"  detection limit in this synthetic geometry: {limit} mm of path")

    for amp in (4.0, 30.0):                    # stage 5 regression: short arcs must not inflate
        w = _run_synth(f0, amp_mm=amp, seed=5)
        amps = [round(r.amp_mm, 1) for r in w]
        print(f"  clutter {amp:4.1f} mm: modes {sorted({r.clutter_mode for r in w})}  amp {amps}")
        checks[f"stage 5: {amp:.0f} mm amplitude not overstated"] = all(r.amp_mm <= 1.15 * amp for r in w)
        checks[f"stage 5: {amp:.0f} mm frequency survives"] = all(abs(r.peak_hz - f0) <= 1 / 30 for r in w)
    w = _run_synth(f0, amp_mm=30.0, seed=5)
    checks["stage 5: 30 mm arc-fit amplitude within ±15%"] = all(abs(r.amp_mm - 30) <= 4.5 for r in w)

    still = _run_synth(f0, amp_mm=0.0, seed=7)
    print(f"  still scene: detections {[r.detected for r in still]}  SNR {[round(r.snr_db, 1) for r in still]}")
    checks["G1 still scene: no detections"] = not any(r.detected for r in still)

    for stage in ("deadtap", "hampel", "clutter"):
        off = _run_synth(f0, amp_mm=0.5, seed=5, stages=[s for s in STAGES if s != stage])
        on_snr = np.mean([r.snr_db for r in quiet])
        off_snr = np.mean([r.snr_db for r in off])
        print(f"  G5 bypass {stage:8s}: mean SNR on {on_snr:4.1f} dB / off {off_snr:4.1f} dB")
        checks[f"G5 bypass {stage}: stage does not cost SNR (>-1 dB)"] = on_snr >= off_snr - 1.0

    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return all(checks.values())


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(description="MOLES / PUPS live monitor + 30 s FFT pipeline")
    ap.add_argument("--port", help="serial port of the HOST ESP32")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--link", default="A->B", help="link to process: A->B (default), A->C, B->C, or all")
    ap.add_argument("--baseline", type=int, default=100, help="still packets for the baseline (100 = 10 s)")
    ap.add_argument("--thresh", type=float, default=None, help="dev%% alert threshold (default: auto)")
    ap.add_argument("--max-shift", type=int, default=MAX_SHIFT, help="alignment search radius in taps")
    ap.add_argument("--probe", type=int, default=None, help="live-line phase probe tap, offset after fp")
    ap.add_argument("--window", type=float, default=30.0, help="FFT window T in s (default 30 = 300 samples)")
    ap.add_argument("--hop", type=float, default=30.0, help="seconds between FFT windows (default 30)")
    ap.add_argument("--snr-db", type=float, default=10.0, help="peak-over-noise margin for detection (dB)")
    ap.add_argument("--max-missing", type=float, default=0.10, help="window invalid above this missing fraction")
    ap.add_argument("--fill-gaps", type=int, default=0,
                    help="opt-in: interpolate gaps up to N samples (flagged; default 0 = off)")
    ap.add_argument("--gate-disturbance", action="store_true",
                    help="mask packets whose alignment is a poor fit (hands, people)")
    ap.add_argument("--bypass", default="", help=f"comma list of stages to switch off: {','.join(STAGES)}")
    ap.add_argument("--summary", type=float, default=5.0, help="seconds between link-health summaries (0 = off)")
    ap.add_argument("--no-lines", dest="lines", action="store_false", help="don't print per-packet lines")
    ap.add_argument("--no-shape", action="store_true", help="hide the CIR sparkline")
    ap.add_argument("--no-legend", action="store_true", help="don't print the legend at startup")
    ap.add_argument("--no-plot", action="store_true", help="no plot window (PNGs are still saved)")
    ap.add_argument("--data", default="data", help="folder for run folders (default ./data)")
    ap.add_argument("--tag", default="run", help="run name, e.g. T1_onoff_run03")
    ap.add_argument("--no-record", action="store_true", help="don't record raw packets (not recommended)")
    ap.add_argument("--replay", metavar="RAW_CSV", help="re-run a recorded raw.csv through the pipeline")
    ap.add_argument("--show", action="store_true", help="replay: show each window's plot on screen")
    ap.add_argument("--show-pause", type=float, default=1.5, help="replay --show: seconds per window")
    ap.add_argument("--selftest", action="store_true", help="run synthetic signal-survival tests and exit")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(0 if selftest() else 1)

    bypass = {s.strip() for s in args.bypass.split(",") if s.strip()}
    unknown = bypass - set(STAGES)
    if unknown:
        sys.exit(f"unknown stage(s) {sorted(unknown)}; choose from {', '.join(STAGES)}")
    cfg = Config(n_base=args.baseline, thresh=args.thresh, max_shift=args.max_shift,
                 probe_offset=args.probe, window_s=args.window, hop_s=args.hop, snr_db=args.snr_db,
                 max_missing=args.max_missing, fill_gaps=args.fill_gaps,
                 gate_disturbance=args.gate_disturbance,
                 stages=tuple(s for s in STAGES if s not in bypass))

    if args.replay:
        run_dir = open_run_dir(args.data, "replay_" + os.path.splitext(os.path.basename(
            os.path.dirname(os.path.abspath(args.replay)) or args.replay))[0][:40]
            + ("_no-" + "-".join(sorted(bypass)) if bypass else ""))
        args.lines = False                     # replay: window results only
        plot = FFTPlot(interactive=args.show, out_dir=run_dir)
        wlog = WindowLog(os.path.join(run_dir, "windows.csv"))
        ses = Session(args, cfg, plot, wlog)
        print(f"{BOLD}MOLES / PUPS — replay{RST} {args.replay}  ->  {run_dir}")
        print(f"{DIM}stages on: {','.join(cfg.stages)}{RST}")
        for _, seq, lid, rg, fp, ci, cq in read_raw(args.replay):
            ses.packet(seq, lid, rg, fp, ci, cq)
        wlog.close()
        det = sum(r.detected for r in ses.windows)
        print(f"\n{BOLD}replay done:{RST} {len(ses.windows)} windows, {det} detected  ->  {run_dir}")
        if args.show and ses.windows:
            plot.plt.show()
        return

    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        sys.exit("pyserial is missing. Run:  pip install pyserial")

    if args.list:
        for p in list_ports.comports():
            print(f"{p.device:35s} {p.description}")
        return

    port = args.port or find_port(list_ports)
    try:
        ser = open_port(serial, port, args.baud)
    except serial.SerialException as e:
        sys.exit(f"Could not open {port}: {e}\n(Close the Arduino Serial Monitor if it's open.)")

    run_dir = open_run_dir(args.data, args.tag)
    rec = None
    if not args.no_record:                     # stage 0: raw first, always
        rec = open(os.path.join(run_dir, "raw.csv"), "w", newline="")
        rec_w = csv.writer(rec)
        rec_w.writerow(RAW_HEADER)
    wlog = WindowLog(os.path.join(run_dir, "windows.csv"))
    plot = FFTPlot(interactive=not args.no_plot, out_dir=run_dir)
    ses = Session(args, cfg, plot, wlog)

    print(f"{BOLD}MOLES / PUPS — live monitor{RST}   port={port} baud={args.baud}  link={args.link}")
    print(f"{DIM}run folder {run_dir}  (raw.csv, windows.csv, one PNG per window)   "
          f"stages on: {','.join(cfg.stages)}{RST}")
    print(f"{DIM}commands: b = re-capture baseline, h = legend, q = quit (then Enter){RST}\n")
    if not args.no_legend:
        print(LEGEND)

    state = {"quit": False, "rebase": False, "legend": False}
    threading.Thread(target=stdin_commands, args=(state,), daemon=True).start()
    reader = FrameReader()
    first_status = True
    last_pkt_time = time.time()

    try:
        while not state["quit"] and plot.alive():
            data = ser.read(4096)
            plot.pump()
            if state["rebase"]:
                state["rebase"] = False
                for lk in ses.links.values():
                    lk.reset()
                print(f"\n{YEL}re-capturing baseline — keep the link clear{RST}")
            if state["legend"]:
                state["legend"] = False
                print("\n" + LEGEND)

            for ftype, payload in reader.feed(data):
                if ftype == FRAME_TEXT:
                    text = payload.decode("ascii", "replace")
                    idle = time.time() - last_pkt_time
                    if first_status:
                        print(f"{DIM}[host] {text}{RST}")
                        print(f"{DIM}       (paste this MAC into HOST_MAC in mole_a_tx.ino for unicast){RST}\n")
                        first_status = False
                    elif idle > 1.5:
                        print(f"{YEL}[host] alive, no mole packets for {idle:.0f} s — {text}{RST}")
                    continue
                if ftype != FRAME_PKT or len(payload) != 6 + PKT_LEN:
                    continue
                last_pkt_time = time.time()
                f = struct.unpack(PKT_FMT, payload[6:])
                if f[0] != MAGIC:
                    continue
                link_id, seq, rng, fp_idx = f[1], f[2], f[3], f[4]
                ci, cq = f[5:5 + TAPS], f[5 + TAPS:5 + 2 * TAPS]
                if rec is not None:
                    rec_w.writerow([f"{last_pkt_time:.4f}", seq, link_id, repr(rng), fp_idx, *ci, *cq])
                ses.packet(seq, link_id, rng, fp_idx, ci, cq)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        wlog.close()
        if rec is not None:
            rec.close()
        print(f"\n{DIM}saved run to {run_dir}   (replay: python moles_monitor.py --replay "
              f"{os.path.join(run_dir, 'raw.csv')}){RST}")
        print(f"{DIM}closed {port}{RST}")


if __name__ == "__main__":
    main()
