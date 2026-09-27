"""PUPS DSP — per-link pipeline, stages 1-13 of PUPS_PIPELINE.md (host side).

Takes raw mole packets, returns one detection result per analysis window.
No I/O here: moles_monitor.py (live) and run_pipeline.py (replay) feed it.

    pipe = LinkPipeline(DSPConfig())
    for pkt in packets:              # Packet objects, in arrival order
        pipe.push(pkt)
        res = pipe.maybe_evaluate()  # WindowResult every cfg.update_s, else None
        if res: print(res.line())

Rules enforced (PUPS_PIPELINE.md "Design rules"):
  * Time is never compressed: every sample sits at its own time; missing and
    rejected samples are NaN in a mask, never dropped from the timeline.
  * Mark, don't invent: no interpolation unless cfg.interpolate; filled samples
    are counted and reported.
  * No assumed motion frequency: the search band is [cycles_min / T, fs / 2],
    set only by the window length and sample rate.
  * Every stage in STAGES can be bypassed (validation gate G5).

v1 packet fallbacks (until firmware sends the v2 fields):
  * no t_us      -> time = seq / fs. seq counts poll slots on Mole A's schedule,
                    so ESP-NOW losses still land at the right time. The timebase
                    is nominal (the loop scheduler, not esp_timer).
  * no acc_count -> accumulation-count normalization skipped (stage 4 still
                    normalizes by the first-path tap).
  * no flags     -> misses detected from range = NaN / fp_idx = 0xFFFF.

Run the synthetic gate harness:  python pups_dsp.py
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from dataclasses import dataclass, field

import numpy as np

import align as AL

FP_POS = AL.FP_POS
DEG_PER_MM = AL.DEG_PER_MM
TAPS = 56
SAT_LEVEL = 32000             # int16 taps (after the firmware's >>2); at or above = clipped
NOISE_TAPS = (2, 3, 4, 5)     # fp-6..fp-3: pre-first-path, still valid after a +/-2 tap alignment
STAGES = ("align", "deadtap", "phaseref", "clutter", "hampel", "detrend")   # bypassable
PIPELINE_VERSION = "moles-dsp-1"

# MOLES schedule (must match SCHEDULE[] in mole.ino): link_id -> name, (initiator, responder)
LINK_NAMES = {1: "A->B", 2: "A->C", 3: "B->C"}
LINK_MOLES = {1: (1, 2), 2: (1, 3), 3: (2, 3)}        # mole ids: A=1, B=2, C=3
MOLE_NAMES = {1: "A", 2: "B", 3: "C"}


# =============================================================================== data types
@dataclass
class Packet:
    seq: int
    link_id: int
    range_m: float
    fp_idx: int
    cir: np.ndarray                 # complex, 56 taps (cir_i + 1j*cir_q)
    t_us: int | None = None         # v2
    fp_idx_q6: int | None = None    # v2
    acc_count: int | None = None    # v2
    flags: int | None = None        # v2: bit0 response OK, bit1 CIR valid, bit2 saturated


@dataclass
class DSPConfig:
    fs: float = 10.0                # sample rate (Hz), firmware poll rate
    window_s: float = 30.0          # analysis window T (s)
    update_s: float = 1.0           # re-evaluate every (s)
    template_packets: int = 30      # valid packets used to build the alignment template
    max_shift: int = 2              # alignment search radius (taps)
    ref_tap: int = FP_POS           # phase/gain reference tap (stage 4)
    dead_tap_mode: str = "weight"   # "weight" (SNR weighting) | "hard" | "off"
    dead_tap_k: float = 4.0         # "hard" mode: keep taps > k x noise rms
    gate_pct: float | None = None   # disturbance gate on weighted dev (%); None = off
    interpolate: bool = False       # opt-in gap fill (stage 8)
    max_fill: int = 3               # longest gap filled when interpolate=True (samples)
    max_missing_frac: float = 0.10  # window invalid above this missing fraction
    hampel_half: int = 5            # Hampel half-window (samples)
    hampel_nsig: float = 3.5        # Hampel threshold in robust sigmas (MAD-based, relative)
    cycles_min: float = 3.0         # lower band edge = cycles_min / T
    zero_pad: int = 2048            # FFT length for gap-free windows
    pnr_margin_db: float = 16.0     # per-link detection margin (best-of-~15-taps noise reached ~14.8 dB)
    combined_margin_db: float = 12.0  # combined-spectrum margin (3-link quiet max ~8.4 dB in simulation)
    stable_windows: int = 3         # consecutive windows whose peaks must agree
    bypass: frozenset = frozenset() # names from STAGES

    @property
    def f_low(self) -> float:
        return self.cycles_min / self.window_s

    @property
    def f_high(self) -> float:
        return self.fs / 2.0

    @property
    def n_win(self) -> int:
        return int(round(self.window_s * self.fs))


@dataclass
class WindowResult:
    t_end: float
    detected: bool
    peak_hz: float
    pnr_db: float
    amp_rms: float                  # displacement rms over the window
    amp_p2p: float                  # 2.5-97.5 percentile span
    amp_units: str                  # "mm path" (arc demod) or "deg" (tap-phase fallback)
    tap: int                        # chosen tap, as offset from the first path
    mode: str                       # "arc" | "phase"
    arc_deg: float                  # arc span of the chosen tap (deg)
    valid_frac: float               # valid samples / expected, after all masking
    filled_frac: float              # interpolated samples / expected (0 unless opt-in)
    outliers: int                   # samples masked by Hampel
    estimator: str                  # "fft" (gap-free) | "lomb" (gaps)
    stable: bool
    flags: list = field(default_factory=list)
    masked: dict = field(default_factory=dict)     # reason -> count inside this window
    freqs: np.ndarray | None = None
    power: np.ndarray | None = None

    @property
    def per_min(self) -> float:
        return self.peak_hz * 60.0

    def line(self) -> str:
        state = "DETECTED" if self.detected else "no detection"
        amp = (f"{self.amp_p2p:5.2f} mm p2p" if self.amp_units == "mm path"
               else f"{self.amp_p2p:5.1f} deg p2p")
        fl = (" [" + ",".join(self.flags) + "]") if self.flags else ""
        return (f"t={self.t_end:7.1f}s  {state:12s}  peak {self.peak_hz:5.3f} Hz ({self.per_min:5.1f}/min)  "
                f"PNR {self.pnr_db:5.1f} dB  {amp}  tap fp{self.tap:+d}  {self.mode:6s}  "
                f"valid {100 * self.valid_frac:5.1f}%  {self.estimator}{fl}")


# =============================================================================== helpers
def circle_fit(z: np.ndarray):
    """Algebraic (Kasa) circle fit on complex points -> (center, radius, rms residual) or None."""
    x, y = z.real, z.imag
    A = np.c_[x, y, np.ones_like(x)]
    sol = np.linalg.lstsq(A, -(x * x + y * y), rcond=None)[0]
    c = complex(-sol[0] / 2, -sol[1] / 2)
    r2 = abs(c) ** 2 - sol[2]
    if not np.isfinite(r2) or r2 <= 0:
        return None
    r = math.sqrt(r2)
    resid = float(np.sqrt(np.mean((np.abs(z - c) - r) ** 2)))
    return c, r, resid


def robust_keep(z: np.ndarray, k: float = 6.0) -> np.ndarray:
    """False for complex-plane outliers (interference spikes): far from the median point.

    Used before any fit or unwrap, so one spike can't bend the arc fit or leave a
    permanent 2*pi step. Screened samples are masked and counted as outliers.
    """
    med = complex(np.median(z.real), np.median(z.imag))
    d = np.abs(z - med)
    mad = np.median(d)
    return d <= k * mad if mad > 0 else np.ones(len(z), dtype=bool)


def linear_detrend(t: np.ndarray, y: np.ndarray) -> np.ndarray:
    if len(y) < 3:
        return y - np.mean(y)
    return y - np.polyval(np.polyfit(t, y, 1), t)


def lomb_scargle(t: np.ndarray, y: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Classic Lomb-Scargle periodogram: samples at their true times, no filler data."""
    y = y - y.mean()
    w = 2 * np.pi * f[:, None]
    tau = np.arctan2(np.sum(np.sin(2 * w * t), axis=1), np.sum(np.cos(2 * w * t), axis=1)) / (2 * w[:, 0])
    arg = w * (t[None, :] - tau[:, None])
    c, s = np.cos(arg), np.sin(arg)
    return 0.5 * ((c @ y) ** 2 / np.sum(c * c, axis=1) + (s @ y) ** 2 / np.sum(s * s, axis=1))


def hampel_mask(y: np.ndarray, valid: np.ndarray, half: int, nsig: float) -> np.ndarray:
    """True where a valid sample is an outlier vs its local median (threshold relative to local MAD)."""
    out = np.zeros_like(valid)
    idx = np.flatnonzero(valid)
    for i in idx:
        nb = idx[(idx >= i - half) & (idx <= i + half)]
        if len(nb) < 5:
            continue
        med = np.median(y[nb])
        mad = 1.4826 * np.median(np.abs(y[nb] - med))
        if mad > 0 and abs(y[i] - med) > nsig * mad:
            out[i] = True
    return out


def spectrum(t, y, gap_free: bool, cfg: DSPConfig):
    """Stage 12: Hann + zero-padded FFT on gap-free windows, Lomb-Scargle otherwise."""
    if gap_free:
        n = len(y)
        nfft = max(cfg.zero_pad, 1 << (n - 1).bit_length())
        P = np.abs(np.fft.rfft((y - y.mean()) * np.hanning(n), nfft)) ** 2
        return np.fft.rfftfreq(nfft, 1 / cfg.fs), P, "fft"
    f = np.arange(cfg.f_low, cfg.f_high, 1.0 / (cfg.window_s * 4))
    return f, lomb_scargle(t, y, f), "lomb"


def peak_pnr(f: np.ndarray, P: np.ndarray, cfg: DSPConfig):
    """Stage 13 metric: strongest in-band peak vs the median of the rest of the band."""
    band = (f >= cfg.f_low) & (f <= cfg.f_high)
    fb, Pb = f[band], P[band]
    if len(Pb) == 0:
        return float("nan"), float("nan")
    i = int(np.argmax(Pb))
    rest = Pb[np.abs(fb - fb[i]) > 2.0 / cfg.window_s]
    noise = np.median(rest) if len(rest) else np.nan
    if not noise or not np.isfinite(noise) or noise <= 0:
        return float(fb[i]), float("nan")
    return float(fb[i]), float(10 * np.log10(Pb[i] / noise))


# =============================================================================== pipeline
class LinkPipeline:
    """One mole link (e.g. A->B). Stages 1-4 run per packet; 5-13 per window."""

    def __init__(self, cfg: DSPConfig | None = None):
        self.cfg = cfg or DSPConfig()
        unknown = set(self.cfg.bypass) - set(STAGES)
        if unknown:
            raise ValueError(f"unknown bypass stage(s): {sorted(unknown)}; choose from {STAGES}")
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self):
        self.template = None          # magnitude template (align.build_baseline)
        self.noise_rms = None         # complex noise rms from pre-first-path taps
        self.weights = None           # stage-3 tap weights
        self._pending = []            # (t, cir) collected while the template is built
        self.rows = []                # (t, z or None, reason or None)
        self.rejects = Counter()      # all-time masked-sample reasons
        self.align_flags = Counter()
        self._seq_last = None
        self._seq_off = 0
        self._seq0 = None
        self._t_us0 = None
        self._tus_off = 0
        self._tus_last = None
        self.t_last = None
        self._last_eval = None
        self._peaks = deque(maxlen=self.cfg.stable_windows)

    # ------------------------------------------------------------------ stage 8 timebase (per packet)
    def _time(self, pkt: Packet) -> float:
        if pkt.t_us is not None:                      # v2: true sample time from the mole
            u = pkt.t_us + self._tus_off              # a uint32 microsecond stamp wraps every ~71.6 min
            if self._tus_last is not None and self._tus_last - u > 2 ** 31:
                self._tus_off += 2 ** 32
                u += 2 ** 32
            self._tus_last = u
            if self._t_us0 is None:
                self._t_us0 = u
            return (u - self._t_us0) / 1e6
        s = pkt.seq                                   # v1: poll-slot counter, unwrap 16-bit
        if self._seq_last is not None and self._seq_last - s > 32768:
            self._seq_off += 65536
        self._seq_last = s
        u = s + self._seq_off
        if self._seq0 is None:
            self._seq0 = u
        return (u - self._seq0) / self.cfg.fs

    # ------------------------------------------------------------------ stage 1 validate
    @staticmethod
    def _validate(pkt: Packet) -> str | None:
        if pkt.flags is not None:                     # v2
            if not pkt.flags & 0x1:
                return "no_response"
            if not pkt.flags & 0x2:
                return "cir_invalid"
            if pkt.flags & 0x4:
                return "saturated"
        if pkt.fp_idx == 0xFFFF or not np.isfinite(pkt.range_m):
            return "no_response"
        c = pkt.cir
        if not np.all(np.isfinite(c)):
            return "cir_invalid"
        if np.max(np.abs(c.real)) >= SAT_LEVEL or np.max(np.abs(c.imag)) >= SAT_LEVEL:
            return "saturated"
        return None

    def _prep(self, pkt: Packet) -> np.ndarray:
        c = np.asarray(pkt.cir, dtype=complex)
        if pkt.acc_count:                             # v2: divide out accumulation count
            c = c / pkt.acc_count
        return c

    def _mask(self, t: float, reason: str):
        self.rejects[reason] += 1
        self.rows.append((t, None, reason))

    # ------------------------------------------------------------------ public: per packet
    def push(self, pkt: Packet, t: float | None = None) -> None:
        """Add one packet. `t` = shared time from MolesDSP; None = this link's own timebase."""
        if t is None:
            t = self._time(pkt)
        self.t_last = t if self.t_last is None else max(self.t_last, t)
        reason = self._validate(pkt)
        if reason:
            self._mask(t, reason)
        elif self.template is None:
            self._pending.append((t, self._prep(pkt)))
            if len(self._pending) >= self.cfg.template_packets:
                self._build_template()
        else:
            self._process(t, self._prep(pkt))
        horizon = self.t_last - self.cfg.window_s - 2.0
        if self.rows and self.rows[0][0] < horizon:
            self.rows = [r for r in self.rows if r[0] >= horizon]

    def _build_template(self):
        """Alignment template + noise floor + stage-3 weights, from the first valid packets."""
        cfg = self.cfg
        cirs = np.array([c for _, c in self._pending])
        if "align" in cfg.bypass:
            self.template = np.mean(np.abs(cirs), axis=0)
            aligned = cirs
        else:
            self.template = AL.build_baseline(cirs, max_shift=cfg.max_shift).mag
            aligned = np.array([AL.align_window(c, self.template, cfg.max_shift, poor_fit_pct=np.inf).aligned
                                for c in cirs])
        noise = np.abs(aligned[:, list(NOISE_TAPS)])
        self.noise_rms = float(np.sqrt(np.nanmean(noise ** 2)))
        self.weights = self._dead_tap_weights(self.template, self.noise_rms)
        pending, self._pending = self._pending, []
        for t, c in pending:                          # nothing collected during warm-up is lost
            self._process(t, c)

    # ------------------------------------------------------------------ stage 3 dead-tap removal
    def _dead_tap_weights(self, template: np.ndarray, noise_rms: float) -> np.ndarray:
        """Per-tap weights, decided once per link (fixed columns).

        Replace this method to plug in the team's dead-tap module. Contract:
        return an array of 56 non-negative weights; 0 = never a candidate.
        """
        cfg = self.cfg
        if "deadtap" in cfg.bypass or cfg.dead_tap_mode == "off":
            return np.ones(TAPS)
        snr = np.nan_to_num(template / max(noise_rms, 1e-12))
        if cfg.dead_tap_mode == "hard":
            return (snr > cfg.dead_tap_k).astype(float)
        w = np.clip(snr ** 2 - 1.0, 0.0, None)        # noise-only taps (snr ~0.9) -> 0
        return w / w.max() if w.max() > 0 else w

    # ------------------------------------------------------------------ stages 2 + 4 (per packet)
    def _process(self, t: float, c: np.ndarray):
        cfg = self.cfg
        if "align" in cfg.bypass:                                           # stage 2
            aligned = c.copy()
        else:
            r = AL.align_window(c, self.template, cfg.max_shift, poor_fit_pct=np.inf)
            aligned = r.aligned
            if r.flags & AL.FLAG_EDGE_HIT:
                self.align_flags["edge"] += 1
            if r.flags & AL.FLAG_AMBIGUOUS:
                self.align_flags["ambiguous"] += 1

        if cfg.gate_pct is not None:                                        # stage 1 disturbance gate
            if AL.compute_dev(aligned, self.template, self.weights) > cfg.gate_pct:
                self._mask(t, "disturbance")
                return

        if "phaseref" in cfg.bypass:                                        # stage 4
            z = aligned
        else:
            ref = aligned[cfg.ref_tap]
            if not np.isfinite(ref) or abs(ref) < 3.0 * self.noise_rms:
                self._mask(t, "ref_weak")
                return
            z = aligned * np.conj(ref) / abs(ref) ** 2
        self.rows.append((t, z, None))

    # ------------------------------------------------------------------ public: per window
    def maybe_evaluate(self) -> WindowResult | None:
        if self.t_last is None:
            return None
        if self._last_eval is not None and self.t_last - self._last_eval < self.cfg.update_s - 1e-9:
            return None
        res = self.evaluate()
        if res is not None:
            self._last_eval = self.t_last
        return res

    def evaluate(self, t_end: float | None = None) -> WindowResult | None:
        """Stages 5-13 on the window ending at t_end (default: this link's latest sample).

        MolesDSP passes the same t_end to every link, so all windows cover the same
        cycles. Samples missing at the end of a link's window (lost packets) are gaps.
        """
        cfg = self.cfg
        if self.template is None or self.t_last is None:
            return None
        t_end = self.t_last if t_end is None else t_end
        n = cfg.n_win
        t0 = t_end - (n - 1) / cfg.fs
        rows = [r for r in self.rows if r[0] >= t0 - 0.5 / cfg.fs]
        if not rows or min(r[0] for r in rows) > t0 + 1.5 / cfg.fs:
            return None                                   # window not full yet

        # ---- stage 8: place every sample on the grid at its own time; gaps stay NaN
        grid_t = t0 + np.arange(n) / cfg.fs
        Z = np.full((n, TAPS), complex(np.nan, np.nan))
        valid = np.zeros(n, dtype=bool)
        masked = Counter()
        for t, z, reason in rows:
            i = int(round((t - t0) * cfg.fs))
            if not 0 <= i < n:
                continue
            if z is None:
                masked[reason] += 1
            else:
                Z[i], valid[i] = z, True
        masked["lost"] = int(n - valid.sum() - sum(masked.values()))

        # ---- stages 5 + 6: clutter removal per candidate tap, pick the most periodic
        cand = [k for k in range(TAPS)
                if self.weights[k] > 0 and k != cfg.ref_tap
                and np.mean(np.isfinite(Z[valid, k])) >= 0.9]
        if not cand or valid.sum() < 10:
            return self._empty(masked, valid, "no_candidates", t_end)

        # Ranking only: gaps are zero-filled AFTER detrending so one fast FFT can score
        # each tap. This never reaches the output; the chosen tap's final spectrum
        # (stage 12) uses its real samples only (Lomb-Scargle when there are gaps).
        best = None
        nfft = max(cfg.zero_pad, 1 << (n - 1).bit_length())
        f_rank = np.fft.rfftfreq(nfft, 1 / cfg.fs)
        hann = np.hanning(n)
        for k in cand:
            idx = np.flatnonzero(valid & np.isfinite(Z[:, k]))
            y_k, keep, *_ = self._demod(Z[idx, k])
            x = np.zeros(n)
            x[idx[keep]] = linear_detrend(grid_t[idx[keep]], y_k)
            P = np.abs(np.fft.rfft(x * hann, nfft)) ** 2
            _, pnr = peak_pnr(f_rank, P, cfg)
            score = pnr if np.isfinite(pnr) else -np.inf
            if best is None or score > best[0]:
                best = (score, k)
        k = best[1]

        # ---- stages 5 + 7: clutter removal and demodulation of the chosen tap
        idx = np.flatnonzero(valid & np.isfinite(Z[:, k]))
        y_ok, keep, mode, units, arc = self._demod(Z[idx, k])
        pre_screened = int((~keep).sum())
        ok = np.zeros(n, dtype=bool)
        ok[idx[keep]] = True
        flags = [] if mode == "arc" else ["tap_phase_demod"]
        y = np.full(n, np.nan)
        y[ok] = y_ok
        gaps = np.diff(np.flatnonzero(ok))
        if len(gaps) and gaps.max() - 1 > cfg.max_fill:
            flags.append(f"gap_{(gaps.max() - 1) / cfg.fs:.1f}s")

        # ---- stage 8 (opt-in): fill short gaps, flagged
        filled = np.zeros(n, dtype=bool)
        if cfg.interpolate:
            idx = np.flatnonzero(ok)
            for a, b in zip(idx[:-1], idx[1:]):
                if 1 < b - a <= cfg.max_fill + 1:
                    fill = np.arange(a + 1, b)
                    y[fill] = np.interp(fill, [a, b], [y[a], y[b]])
                    filled[fill] = True
            ok = ok | filled

        # ---- stage 9: spike removal (mark, don't replace)
        outl = np.zeros(n, dtype=bool)
        if "hampel" not in cfg.bypass:
            outl = hampel_mask(y, ok & ~filled, cfg.hampel_half, cfg.hampel_nsig)
            ok = ok & ~outl

        # ---- stage 10: linear detrend
        tt, yy = grid_t[ok], y[ok]
        if "detrend" not in cfg.bypass:
            yy = linear_detrend(tt, yy)
        else:
            yy = yy - yy.mean()

        # ---- stages 11 + 12: band limits + spectral estimate
        gap_free = bool(ok.all())
        f, P, est = spectrum(tt, yy, gap_free, cfg)
        if cfg.interpolate and filled.any():          # cross-check filled FFT vs Lomb-Scargle
            f2, P2, _ = spectrum(grid_t[ok & ~filled], linear_detrend(grid_t[ok & ~filled], y[ok & ~filled]),
                                 False, cfg)
            if abs(peak_pnr(f2, P2, cfg)[0] - peak_pnr(f, P, cfg)[0]) > 1.0 / cfg.window_s:
                flags.append("estimators_disagree")

        # ---- stage 13: detect + quality
        pk, pnr = peak_pnr(f, P, cfg)
        self._peaks.append(pk)
        stable = (len(self._peaks) == cfg.stable_windows
                  and np.ptp(np.array(self._peaks)) <= 1.0 / cfg.window_s)
        valid_frac = float(ok.sum() - filled.sum()) / n
        enough = (1.0 - valid_frac) <= cfg.max_missing_frac
        if not enough:
            flags.append("too_many_missing")
        detected = bool(np.isfinite(pnr) and pnr >= cfg.pnr_margin_db and enough and stable)

        lo, hi = np.percentile(yy, [2.5, 97.5]) if len(yy) else (np.nan, np.nan)
        return WindowResult(t_end=float(t_end), detected=detected, peak_hz=pk, pnr_db=pnr,
                            amp_rms=float(np.std(yy)), amp_p2p=float(hi - lo), amp_units=units,
                            tap=k - FP_POS, mode=mode, arc_deg=arc, valid_frac=valid_frac,
                            filled_frac=float(filled.sum()) / n, outliers=int(outl.sum()) + pre_screened,
                            estimator=est, stable=stable, flags=flags, masked=dict(masked),
                            freqs=f, power=P)

    def _demod(self, zk: np.ndarray):
        """Stages 5 + 7 for one tap -> (y, keep, mode, units, span_deg).

        1. Screen complex-plane outliers (spikes) so they can't bend the fit or break unwrap.
        2. Stage 5: if the samples clearly lie on an arc, its center is the static
           component; demod = unwrapped angle around the center, in mm of path ("arc").
        3. Otherwise (arc too short or noisy to fit), demod = the tap's own phase around
           its circular mean ("phase", degrees, not unwrapped). Phase ignores magnitude
           noise (gain, residual misalignment).
        """
        keep = robust_keep(zk)
        zc = zk[keep]
        if "clutter" not in self.cfg.bypass and len(zc) >= 10:
            fit = circle_fit(zc)
            if fit is not None:
                c, r, resid = fit
                spread = float(np.max(np.abs(zc - zc.mean()))) or 1e-12
                if resid / r < 0.2 and r <= 50.0 * spread:          # points really lie on an arc
                    ang = np.unwrap(np.angle(zc - c))
                    return np.degrees(ang) / DEG_PER_MM, keep, "arc", "mm path", float(np.degrees(np.ptp(ang)))
        # No unwrap here: this branch only runs for small motions (large ones fit as
        # arcs), and unwrapping a noisy phase builds a random walk that looks like a
        # strong low-frequency peak. Wrap around the circular mean instead.
        ph = np.angle(zc)
        dev = np.angle(np.exp(1j * (ph - np.angle(np.mean(np.exp(1j * ph))))))
        return np.degrees(dev), keep, "phase", "deg", float(np.degrees(np.ptp(dev)))

    def _empty(self, masked, valid, flag, t_end) -> WindowResult:
        return WindowResult(t_end=float(t_end), detected=False, peak_hz=float("nan"),
                            pnr_db=float("nan"), amp_rms=float("nan"), amp_p2p=float("nan"),
                            amp_units="deg", tap=0, mode="-", arc_deg=float("nan"),
                            valid_frac=float(valid.sum()) / self.cfg.n_win, filled_frac=0.0,
                            outliers=0, estimator="-", stable=False, flags=[flag], masked=dict(masked))


# =============================================================================== multi-link
def combine_links(results: list[WindowResult], cfg: DSPConfig, min_agree: int = 2) -> dict:
    """Combine AFTER the spectra: noise-normalized power averaged on a common grid.

    Never averages raw signals (links see the source with different size and
    sign of phase swing, and at offset sample times).

    Agreement: a link "agrees" if its own peak is within one resolution bin of
    the combined peak. A link whose path misses the source has a random noise
    peak and must not veto the others, so only `min_agree` links (default 2)
    need to agree, not all of them.
    """
    use = [r for r in results if r.freqs is not None and np.isfinite(r.pnr_db)]
    if not use:
        return {"peak_hz": float("nan"), "pnr_db": float("nan"), "agree": False,
                "n_links": 0, "n_agree": 0, "agreeing": []}
    grid = np.arange(cfg.f_low, cfg.f_high, 1.0 / (cfg.window_s * 4))
    acc = np.zeros_like(grid)
    for r in use:
        p = np.interp(grid, r.freqs, r.power)
        acc += p / np.median(p)
    acc /= len(use)
    pk, pnr = peak_pnr(grid, acc, cfg)
    agreeing = [i for i, r in enumerate(use) if abs(r.peak_hz - pk) <= 1.0 / cfg.window_s]
    need = min(min_agree, len(use))
    return {"peak_hz": pk, "pnr_db": pnr, "agree": len(agreeing) >= need,
            "n_links": len(use), "n_agree": len(agreeing), "agreeing": agreeing,
            "grid": grid, "power": acc}


class CombinedDecision:
    """Tiered decision on the combined spectrum, with the same 3-window stability rule.

    CONFIRMED : combined PNR >= combined margin, stable, and >= 2 links peak at the same frequency
    DETECTED  : combined PNR >= combined margin and stable, but only 1 link sees it
                (normal when the other beams' paths miss the source)
    none      : below margin or not stable
    """

    def __init__(self, cfg: DSPConfig):
        self.cfg = cfg
        self.peaks = deque(maxlen=cfg.stable_windows)

    def update(self, results: list[WindowResult]) -> dict:
        c = combine_links(results, self.cfg)
        self.peaks.append(c["peak_hz"])
        stable = (len(self.peaks) == self.cfg.stable_windows
                  and np.ptp(np.array(self.peaks)) <= 1.0 / self.cfg.window_s)
        above = np.isfinite(c["pnr_db"]) and c["pnr_db"] >= self.cfg.combined_margin_db
        if above and stable:
            c["tier"] = "CONFIRMED" if c["n_agree"] >= 2 else "DETECTED"
        else:
            c["tier"] = "none"
        c["stable"] = stable
        return c


# =============================================================================== MOLES (all links)
@dataclass
class MolesResult:
    t_end: float                     # shared time (s) since the first packet, from the beacon cycle
    cycle_end: int                   # beacon cycle at the end of the window (unwrapped)
    tier: str                        # "CONFIRMED" | "DETECTED" | "none" | "warming"
    peak_hz: float
    pnr_db: float                    # combined spectrum, dB above its own noise median
    n_links: int                     # links that contributed to the combined spectrum
    agreeing: list                   # link ids whose own peak matches the combined peak
    alert_mask: int                  # bit (mole_id - 1) set = that mole should flash
    links: dict                      # link_id -> WindowResult (or None while warming)
    excluded: dict                   # link_id -> reason it was left out of combining
    grid: np.ndarray | None = None   # combined spectrum
    power: np.ndarray | None = None
    note: str = ""                   # progress text while warming up

    @property
    def per_min(self) -> float:
        return self.peak_hz * 60.0

    def line(self) -> str:
        if self.tier == "warming":
            return f"t={self.t_end:6.1f}s  WARMING UP  {self.note}"
        ag = " ".join(LINK_NAMES.get(i, str(i)) for i in self.agreeing) or "-"
        al = ",".join(MOLE_NAMES[m] for m in (1, 2, 3) if self.alert_mask >> (m - 1) & 1) or "off"
        return (f"t={self.t_end:6.1f}s  {self.tier:9s}  peak {self.peak_hz:5.3f} Hz ({self.per_min:5.1f}/min)  "
                f"PNR {self.pnr_db:5.1f} dB  agree {len(self.agreeing)}/{self.n_links} [{ag}]  alert {al}")

    def to_json(self, wall_time: float | None = None, run_id: str | None = None) -> dict:
        """Row(s) for the backend's window_results table: one combined + one per link."""
        f = lambda x: None if x is None or (isinstance(x, float) and not np.isfinite(x)) else x
        links = {}
        for lid, r in self.links.items():
            if r is None:
                links[LINK_NAMES.get(lid, str(lid))] = {"link_id": lid, "state": "warming"}
                continue
            links[LINK_NAMES.get(lid, str(lid))] = {
                "link_id": lid, "detected": r.detected, "peak_hz": f(r.peak_hz), "pnr_db": f(r.pnr_db),
                "amp_p2p": f(r.amp_p2p), "amp_units": r.amp_units, "tap": r.tap, "mode": r.mode,
                "valid_frac": f(r.valid_frac), "filled_frac": f(r.filled_frac), "outliers": r.outliers,
                "estimator": r.estimator, "stable": r.stable, "flags": r.flags,
                "excluded": self.excluded.get(lid)}
        return _plain({"run_id": run_id, "wall_time": wall_time, "t_end": self.t_end, "cycle_end": self.cycle_end,
                "tier": self.tier, "peak_hz": f(self.peak_hz), "pnr_db": f(self.pnr_db),
                "agreeing_links": [LINK_NAMES.get(i, str(i)) for i in self.agreeing],
                "n_links": self.n_links, "alert_mask": self.alert_mask, "links": links,
                "pipeline_version": PIPELINE_VERSION})


def _plain(o):
    """numpy scalars/arrays -> plain JSON types; NaN/inf -> None (JSON has no NaN)."""
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.ndarray):
        return _plain(o.tolist())
    return o


class MolesDSP:
    """Every link of one MOLES deployment.

    * Shared timebase: `seq` is the host's beacon cycle, identical across links, so
      every sample is placed at t = cycle / fs on ONE clock (not per-link origins).
    * Synchronized windows: all links are evaluated at the same t_end each update.
    * Combined decision: tiered (CONFIRMED / DETECTED / none) on the combined spectrum.
      Links whose window is invalid (too many missing samples) are left out of combining.
    """

    def __init__(self, cfg: DSPConfig | None = None):
        self.cfg = cfg or DSPConfig()
        self.reset()

    def reset(self):
        self.links: dict[int, LinkPipeline] = {}
        self.decision = CombinedDecision(self.cfg)
        self._u_last = None
        self._seq0 = None
        self.t_now = None
        self._last_eval = None

    def _time(self, seq: int) -> float:
        """Shared 16-bit cycle counter -> seconds on one clock for every link.

        Picks the unwrapped value nearest the latest one seen, so a 16-bit wrap and
        packets from neighbouring cycles arriving slightly out of order both work.
        """
        if self._u_last is None:
            u = seq
        else:
            u = self._u_last - (self._u_last % 65536) + seq
            if u - self._u_last > 32768:
                u -= 65536
            elif self._u_last - u > 32768:
                u += 65536
        self._u_last = u if self._u_last is None else max(self._u_last, u)
        if self._seq0 is None:
            self._seq0 = u
        return (u - self._seq0) / self.cfg.fs

    def push(self, pkt: Packet) -> None:
        t = self._time(pkt.seq)
        lp = self.links.get(pkt.link_id)
        if lp is None:
            lp = self.links[pkt.link_id] = LinkPipeline(self.cfg)
        lp.push(pkt, t=t)
        self.t_now = t if self.t_now is None else max(self.t_now, t)

    def maybe_evaluate(self) -> MolesResult | None:
        if self.t_now is None:
            return None
        # Evaluate the last COMPLETE cycle: the A->C and B->C packets of the newest cycle
        # arrive up to ~70 ms after A->B and may still be in flight.
        t_end = self.t_now - 1.0 / self.cfg.fs
        if t_end < 0:
            return None
        if self._last_eval is not None and t_end - self._last_eval < self.cfg.update_s - 1e-9:
            return None
        self._last_eval = t_end
        return self.evaluate(t_end)

    def evaluate(self, t_end: float) -> MolesResult:
        cfg = self.cfg
        per = {lid: lp.evaluate(t_end) for lid, lp in sorted(self.links.items())}
        use_ids, use, excluded = [], [], {}
        for lid, r in per.items():
            if r is None:
                excluded[lid] = "warming"
            elif "too_many_missing" in r.flags:
                excluded[lid] = "too_many_missing"
            elif r.freqs is None or not np.isfinite(r.pnr_db):
                excluded[lid] = "no_spectrum"
            else:
                use_ids.append(lid)
                use.append(r)
        cycle_end = int(round(t_end * cfg.fs)) + (self._seq0 or 0)
        filled = min(t_end + 1.0 / cfg.fs, cfg.window_s)
        note = (f"window {filled:.0f}/{cfg.window_s:.0f} s" if filled < cfg.window_s
                else f"stability {min(len(self.decision.peaks), cfg.stable_windows)}/{cfg.stable_windows}")
        if not use:
            return MolesResult(t_end, cycle_end, "warming", float("nan"), float("nan"), 0, [], 0,
                               per, excluded, note=note)
        c = self.decision.update(use)
        agreeing = [use_ids[i] for i in c["agreeing"]] if c["tier"] != "none" else []
        mask = 0
        for lid in agreeing:
            for m in LINK_MOLES.get(lid, ()):
                mask |= 1 << (m - 1)
        tier = c["tier"]
        if tier == "none" and len(self.decision.peaks) < cfg.stable_windows:
            tier = "warming"
        note = f"stability {min(len(self.decision.peaks), cfg.stable_windows)}/{cfg.stable_windows}"
        return MolesResult(t_end, cycle_end, tier, c["peak_hz"], c["pnr_db"], len(use), agreeing, mask,
                           per, excluded, c.get("grid"), c.get("power"), note=note)


def simulate_moles(seconds=90.0, amps=(2.0, 1.0, 0.0), f_hz=0.33, loss=0.02, seed=0, seq_start=0,
                   on=None):
    """Interleaved A->B, A->C, B->C packets with a shared beacon-cycle seq (synthetic).

    amps: echo path amplitude (mm) per link while the module moves (0 = that beam misses it).
    on:   list of (start_s, stop_s) when the module runs; None = the whole time.
    """
    pk = []
    for i, (lid, a) in enumerate(zip((1, 2, 3), amps)):
        moving = simulate(seconds=seconds, amp_mm=a, f_hz=f_hz, seed=seed + 17 * i, loss=loss,
                          echo_off=2 + i)
        if on is None:
            chosen = moving
        else:
            still = {p.seq: p for p in simulate(seconds=seconds, amp_mm=0.0, seed=seed + 17 * i + 5,
                                                loss=loss, echo_off=2 + i)}
            chosen = []
            for p in moving:                             # same slot grid; pick per cycle
                src = p if any(a0 <= p.seq / 10.0 < b0 for a0, b0 in on) else still.get(p.seq)
                if src is not None:
                    chosen.append(src)
        for p in chosen:
            p.link_id = lid
            p.seq = (p.seq + seq_start) & 0xFFFF
            pk.append(p)
    order = {1: 0, 2: 1, 3: 2}
    pk.sort(key=lambda p: (((p.seq - seq_start) & 0xFFFF), order[p.link_id]))
    return pk


# =============================================================================== synthetic harness
def simulate(seconds=90.0, fs=10.0, amp_mm=0.0, f_hz=0.3, echo_off=3, echo_amp=150.0,
             noise=10.0, jitter=1.5, loss=0.0, burst_every=0.0, miss=0.0, spikes=0.0, seed=0):
    """Synthetic A->B packets. amp_mm = peak echo path change (0 = actuator off)."""
    rng = np.random.default_rng(seed)
    lam = 46.2
    t_axis = np.arange(TAPS)
    pulse = lambda x: np.exp(-(x / 1.1) ** 2)
    paths = [(FP_POS, 1600.0), (FP_POS + 4, 500.0), (FP_POS + 12, 300.0), (FP_POS + 25, 150.0)]
    pkts = []
    burst_left = 0
    for seq in range(int(seconds * fs)):
        t = seq / fs
        if burst_every and rng.random() < 1.0 / (burst_every * fs):
            burst_left = int(rng.integers(3, 12))
        if burst_left > 0:
            burst_left -= 1
            continue                                             # lost over ESP-NOW
        if rng.random() < loss:
            continue
        if rng.random() < miss:
            pkts.append(Packet(seq & 0xFFFF, 1, float("nan"), 0xFFFF, np.zeros(TAPS, complex)))
            continue
        d = rng.uniform(-jitter, jitter)
        dpath = amp_mm * (np.sin(2 * np.pi * f_hz * t) + 0.25 * np.sin(4 * np.pi * f_hz * t + 0.7))
        phi = 2 * np.pi * dpath / lam
        h = sum(a * pulse(t_axis - (p - d)) for p, a in paths).astype(complex)
        h += echo_amp * pulse(t_axis - (FP_POS + echo_off - d)) * np.exp(1j * (phi + 1.0))
        h *= rng.normal(1.0, 0.05) * np.exp(1j * rng.uniform(0, 2 * np.pi))
        h += rng.normal(0, noise, TAPS) + 1j * rng.normal(0, noise, TAPS)
        if rng.random() < spikes:
            h[FP_POS + echo_off - 1:FP_POS + echo_off + 2] += rng.normal(0, 600, 3) + 1j * rng.normal(0, 600, 3)
        h = np.round(h.real) + 1j * np.round(h.imag)
        pkts.append(Packet(seq & 0xFFFF, 1, 1.0, 745, h))
    return pkts


def run(pkts, cfg: DSPConfig) -> list[WindowResult]:
    pipe = LinkPipeline(cfg)
    out = []
    for p in pkts:
        pipe.push(p)
        r = pipe.maybe_evaluate()
        if r is not None:
            out.append(r)
    return out


def _summ(res, f_true=None, T=30.0):
    live = res[3:]                      # skip windows still filling the stability history
    det = [r for r in live if r.detected]
    rate = len(det) / max(len(live), 1)
    pnr = np.nanmedian([r.pnr_db for r in live]) if live else float("nan")
    ferr = (max(abs(r.peak_hz - f_true) for r in det) if (det and f_true) else float("nan"))
    return rate, pnr, ferr


def _selftest() -> bool:
    """Synthetic versions of validation gates G1, G2, G3, G5 plus gap/spike checks."""
    cfg = DSPConfig()
    res_hz = 1.0 / cfg.window_s
    ok_all = True
    print("pups_dsp self-test (synthetic; the pipeline is never told the motion frequency)\n")

    # G1: quiet scene, several seeds -> no detections
    fa = [_summ(run(simulate(amp_mm=0.0, seed=s, loss=0.02), cfg))[0] for s in range(3)]
    g1 = max(fa) == 0.0
    ok_all &= g1
    print(f"[{'PASS' if g1 else 'FAIL'}] G1 quiet: detection rate per seed {['%.0f%%' % (100 * x) for x in fa]}")

    # G2: on vs off, random unknown frequencies
    rng = np.random.default_rng(42)
    for s in range(3):
        f_true = float(rng.uniform(0.15, 1.2))
        on = _summ(run(simulate(amp_mm=3.0, f_hz=f_true, seed=10 + s, loss=0.02), cfg), f_true)
        off = _summ(run(simulate(amp_mm=0.0, seed=10 + s, loss=0.02), cfg))
        g2 = on[0] >= 0.9 and off[0] == 0.0 and on[2] <= res_hz
        ok_all &= g2
        print(f"[{'PASS' if g2 else 'FAIL'}] G2 on/off  f={f_true:.3f} Hz: on {100 * on[0]:3.0f}% "
              f"(PNR {on[1]:.1f} dB, max freq err {on[2]:.3f} Hz)  off {100 * off[0]:3.0f}% (PNR {off[1]:.1f} dB)")

    # G3: loud-to-quiet sweep, frequency must not move
    f_true = 0.41
    print(f"\n     G3 sweep (f={f_true} Hz, echo path amplitude):")
    g3 = True
    for a in (8, 4, 2, 1, 0.5, 0.25, 0.1):
        rate, pnr, ferr = _summ(run(simulate(amp_mm=a, f_hz=f_true, seed=7, loss=0.02), cfg), f_true)
        if rate > 0 and ferr > res_hz:
            g3 = False
        print(f"       {a:5.2f} mm  detected {100 * rate:3.0f}%  PNR {pnr:5.1f} dB  max freq err "
              f"{'%.3f' % ferr if rate else '  -  '} Hz")
    ok_all &= g3
    print(f"[{'PASS' if g3 else 'FAIL'}] G3 frequency unchanged wherever detected")

    # gaps: bursty loss is survivable; too much loss invalidates windows instead of lying
    r10 = run(simulate(amp_mm=2.0, f_hz=0.3, seed=3, loss=0.03, burst_every=20.0), cfg)
    r40 = run(simulate(amp_mm=2.0, f_hz=0.3, seed=3, loss=0.40), cfg)
    s10, s40 = _summ(r10, 0.3), _summ(r40, 0.3)
    gap_ok = s10[0] >= 0.8 and s40[0] == 0.0 and all("too_many_missing" in r.flags for r in r40[3:])
    ok_all &= gap_ok
    print(f"\n[{'PASS' if gap_ok else 'FAIL'}] gaps: bursty loss (valid {100 * np.mean([r.valid_frac for r in r10]):.0f}%, "
          f"{r10[-1].estimator}) detected {100 * s10[0]:.0f}%;  40% loss -> windows flagged invalid, "
          f"detections {100 * s40[0]:.0f}%")

    # spikes: interference bursts are masked, detection and frequency unchanged
    sp = run(simulate(amp_mm=2.0, f_hz=0.3, seed=4, spikes=0.03, loss=0.02), cfg)
    s_sp = _summ(sp, 0.3)
    sp_ok = s_sp[0] >= 0.8 and s_sp[2] <= res_hz
    ok_all &= sp_ok
    print(f"[{'PASS' if sp_ok else 'FAIL'}] spikes 3%: detected {100 * s_sp[0]:.0f}%, freq err {s_sp[2]:.3f} Hz, "
          f"Hampel masked ~{np.mean([r.outliers for r in sp]):.1f} samples/window")

    # G5: bypass A/B at a quiet-ish amplitude; a stage must not shrink the on/off separation
    print("\n     G5 bypass A/B (1 mm, f=0.3 Hz): on-off PNR separation, stage ON vs bypassed")
    base_on = _summ(run(simulate(amp_mm=1.0, f_hz=0.3, seed=5, loss=0.02), cfg), 0.3)
    base_off = _summ(run(simulate(amp_mm=0.0, seed=5, loss=0.02), cfg))
    full = base_on[1] - base_off[1]
    for st in STAGES:
        c2 = DSPConfig(bypass=frozenset({st}))
        on = _summ(run(simulate(amp_mm=1.0, f_hz=0.3, seed=5, loss=0.02), c2), 0.3)
        off = _summ(run(simulate(amp_mm=0.0, seed=5, loss=0.02), c2))
        sep = on[1] - off[1]
        verdict = "stage helps" if full > sep + 0.5 else ("neutral here" if abs(full - sep) <= 0.5 else "STAGE HURTS")
        if verdict == "STAGE HURTS":
            ok_all = False
        print(f"       {st:9s} with {full:5.1f} dB  bypassed {sep:5.1f} dB   {verdict}")

    print(f"\nOVERALL: {'PASS' if ok_all else 'FAIL'} (synthetic only; hardware gates still required)")
    return ok_all


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
