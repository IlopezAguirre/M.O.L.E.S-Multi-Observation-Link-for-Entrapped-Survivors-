"""M.O.L.E.S. live FFT display (matplotlib).

    plot = MolesPlot(cfg, fmax=2.0, ref_hz=None)
    plot.update(moles_result)   # once per DSP update (1 Hz)
    plot.pump()                 # call often from the serial loop to keep the window responsive

Panels
  top     : spectra, dB above each spectrum's own noise median (so links are comparable).
            thin = each link, thick = combined. Dashed = per-link margin (16 dB),
            dotted = combined margin (12 dB). Grey band = below the resolvable band (3/T).
            Optional green dashed line = the module rate measured INDEPENDENTLY (--ref-hz).
            It is a reference only; the detector never searches for it.
  middle  : timeline of combined PNR (thick) and each link (thin) over the last few minutes,
            background shaded by the decision (red = CONFIRMED, orange = DETECTED, blue = warming).
            Shows exactly when detection starts and clears after the module turns on/off.
            Optional hatched bands = when the module was known to be ON (demo/replay truth).
  bottom-left : the triangle. Red side = link agreeing on the detected peak,
                orange = link sees a peak elsewhere, grey = nothing, dotted = invalid/warming.
                Red mole = told to flash (alert mask).
  bottom-right: tier banner + per-link table.
"""

from __future__ import annotations

from collections import deque

import numpy as np

import pups_dsp as D

LINK_COLORS = {1: "#1f77b4", 2: "#ff7f0e", 3: "#2ca02c"}
TIER_STYLE = {"CONFIRMED": ("#c0392b", "white"), "DETECTED": ("#e67e22", "white"),
              "none": ("#7f8c8d", "white"), "warming": ("#2980b9", "white")}
MOLE_POS = {1: (0.0, 0.0), 2: (1.0, 0.0), 3: (0.5, 0.87)}


def _db(f, P, cfg):
    band = (f >= cfg.f_low) & (f <= cfg.f_high)
    med = np.median(P[band]) if band.any() else np.median(P)
    return 10 * np.log10(np.maximum(P, 1e-30) / max(med, 1e-30))


class MolesPlot:
    def __init__(self, cfg: D.DSPConfig, fmax: float = 2.0, ref_hz: float | None = None,
                 backend: str | None = None, title: str = "M.O.L.E.S. — live FFT",
                 history_s: float = 240.0, truth_on: list | None = None):
        import matplotlib
        if backend:
            matplotlib.use(backend)
        import matplotlib.pyplot as plt
        self.plt = plt
        self.cfg, self.fmax, self.ref_hz = cfg, fmax, ref_hz
        self.interactive = matplotlib.get_backend().lower() != "agg"
        if self.interactive:
            plt.ion()
        self.fig = plt.figure(figsize=(12.5, 9.5))
        self.fig.canvas.manager.set_window_title(title) if hasattr(self.fig.canvas, "manager") and self.fig.canvas.manager else None
        gs = self.fig.add_gridspec(3, 3, height_ratios=[3, 1.4, 2], hspace=0.45, wspace=0.3)
        self.ax_spec = self.fig.add_subplot(gs[0, :])
        self.ax_time = self.fig.add_subplot(gs[1, :])
        self.ax_tri = self.fig.add_subplot(gs[2, 0])
        self.ax_txt = self.fig.add_subplot(gs[2, 1:])
        self.history_s = history_s
        self.truth_on = truth_on or []
        self.hist = deque()          # (t, tier, combined pnr, {link: pnr})
        self.title = title
        self._draw_waiting()

    # ------------------------------------------------------------------ drawing
    def _draw_waiting(self):
        for ax in (self.ax_spec, self.ax_time, self.ax_tri, self.ax_txt):
            ax.clear()
        self.ax_spec.text(0.5, 0.5, "waiting for data…", ha="center", va="center", fontsize=14,
                          transform=self.ax_spec.transAxes)
        self.ax_time.axis("off")
        self.ax_tri.axis("off")
        self.ax_txt.axis("off")
        self._refresh()

    def update(self, res: D.MolesResult):
        cfg = self.cfg
        ax = self.ax_spec
        ax.clear()
        peak = res.peak_hz if np.isfinite(res.peak_hz) else None
        shown_peak = peak if res.tier in ("CONFIRMED", "DETECTED") else 0.0   # never zoom out for a noise peak
        xmax = max(self.fmax, (shown_peak or 0) * 1.3, (self.ref_hz or 0) * 1.3)
        ymax = 30.0

        for lid, r in sorted(res.links.items()):
            if r is None or r.freqs is None:
                continue
            m = r.freqs <= xmax
            y = _db(r.freqs, r.power, cfg)[m]
            ymax = max(ymax, float(np.nanmax(y)) + 3)
            excl = lid in res.excluded
            ax.plot(r.freqs[m], y, lw=1.2, color=LINK_COLORS.get(lid, "k"),
                    ls=":" if excl else "-", alpha=0.45 if excl else 0.85,
                    label=f"{D.LINK_NAMES.get(lid, lid)}" + (f" ({res.excluded[lid]})" if excl else ""))
        if res.grid is not None and res.power is not None:
            m = res.grid <= xmax
            y = _db(res.grid, res.power, cfg)[m]
            ymax = max(ymax, float(np.nanmax(y)) + 3)
            ax.plot(res.grid[m], y, lw=2.6, color="black", label="combined")

        ax.axvspan(0, cfg.f_low, color="0.85", zorder=0)
        ax.text(cfg.f_low / 2, ymax * 0.93, "below\nband", ha="center", va="top", fontsize=8, color="0.4")
        ax.axhline(cfg.pnr_margin_db, color="0.35", ls="--", lw=1)
        ax.text(xmax * 0.995, cfg.pnr_margin_db + 0.4, f"link margin {cfg.pnr_margin_db:.0f} dB",
                ha="right", fontsize=8, color="0.35")
        ax.axhline(cfg.combined_margin_db, color="black", ls=":", lw=1.2)
        ax.text(xmax * 0.995, cfg.combined_margin_db + 0.4, f"combined margin {cfg.combined_margin_db:.0f} dB",
                ha="right", fontsize=8)
        if self.ref_hz:
            ax.axvline(self.ref_hz, color="green", ls="--", lw=1.3)
            ax.text(self.ref_hz, -5.5, f"measured module rate {self.ref_hz:.3f} Hz ",
                    color="green", fontsize=8, va="bottom", ha="right")
        if peak and res.tier in ("CONFIRMED", "DETECTED"):
            ax.axvline(peak, color=TIER_STYLE[res.tier][0], lw=1.5, alpha=0.8)
            ax.annotate(f"{peak:.3f} Hz\n{peak * 60:.1f} /min", xy=(peak, res.pnr_db),
                        xytext=(10, 10), textcoords="offset points", fontsize=10, fontweight="bold",
                        color=TIER_STYLE[res.tier][0])

        ax.set_xlim(0, xmax)
        ax.set_ylim(-6, ymax)
        ax.set_xlabel("frequency (Hz)")
        ax.set_ylabel("dB above noise median")
        ax.grid(alpha=0.3)
        sec = ax.secondary_xaxis("top", functions=(lambda f: f * 60.0, lambda c: c / 60.0))
        sec.set_xlabel("cycles per minute")
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="upper right", fontsize=8, ncol=4, bbox_to_anchor=(1.0, 0.86))
        ax.set_title(f"{self.title}   (window {cfg.window_s:.0f} s, t = {res.t_end:.0f} s)", fontsize=11)

        self._draw_timeline(res)
        self._draw_triangle(res)
        self._draw_text(res)
        self._refresh()

    def _draw_timeline(self, res):
        cfg = self.cfg
        self.hist.append((res.t_end, res.tier, res.pnr_db,
                          {lid: (r.pnr_db if r is not None else np.nan) for lid, r in res.links.items()}))
        while self.hist and self.hist[0][0] < res.t_end - self.history_s:
            self.hist.popleft()
        ax = self.ax_time
        ax.clear()
        t = np.array([h[0] for h in self.hist])
        t0, t1 = max(res.t_end - self.history_s, self.hist[0][0] - 1), res.t_end + 1
        shade = {"CONFIRMED": "#c0392b", "DETECTED": "#e67e22", "warming": "#2980b9"}
        for i, (ti, tier, _, _) in enumerate(self.hist):          # decision background
            if tier in shade:
                w = (t[i + 1] - ti) if i + 1 < len(t) else cfg.update_s
                ax.axvspan(ti, ti + w, color=shade[tier], alpha=0.18, lw=0)
        for a, b in self.truth_on:                                 # known module-on intervals
            if b > t0 and a < t1:
                ax.axvspan(max(a, t0), min(b, t1), fill=False, hatch="///", ec="green", lw=0.8, alpha=0.6)
        for lid, col in LINK_COLORS.items():
            y = np.array([h[3].get(lid, np.nan) for h in self.hist], dtype=float)
            if np.isfinite(y).any():
                ax.plot(t, y, lw=0.9, color=col, alpha=0.7)
        yc = np.array([h[2] for h in self.hist], dtype=float)
        ax.plot(t, yc, lw=2.2, color="black")
        yl = np.array([v for h in self.hist for v in h[3].values()], dtype=float)
        ax.axhline(cfg.combined_margin_db, color="black", ls=":", lw=1.1)
        ax.set_xlim(t0, t1)
        vals = np.r_[yc, yl, 20.0]
        top = np.nanmax(vals[np.isfinite(vals)])
        ax.set_ylim(0, max(20.0, top + 3))
        ax.set_xlabel("time (s)")
        ax.set_ylabel("PNR (dB)")
        ax.grid(alpha=0.3)
        ax.set_title("decision timeline  (black = combined, thin = links, dotted = combined margin"
                     + (", hatched = module ON" if self.truth_on else "") + ")", fontsize=9)

    def _draw_triangle(self, res):
        ax = self.ax_tri
        ax.clear()
        ax.set_aspect("equal")
        ax.axis("off")
        for lid, (m1, m2) in D.LINK_MOLES.items():
            r = res.links.get(lid)
            (x1, y1), (x2, y2) = MOLE_POS[m1], MOLE_POS[m2]
            if r is None or lid in res.excluded:
                col, ls, lw, lab = "0.75", ":", 2, "—"
            elif lid in res.agreeing:
                col, ls, lw, lab = "#c0392b", "-", 3 + max(0.0, (r.pnr_db - 10) / 4), f"{r.pnr_db:.0f} dB"
            elif r.detected:
                col, ls, lw, lab = "#e67e22", "-", 3, f"{r.pnr_db:.0f} dB"
            else:
                col, ls, lw, lab = "0.55", "-", 2, f"{r.pnr_db:.0f} dB" if np.isfinite(r.pnr_db) else "—"
            ax.plot([x1, x2], [y1, y2], color=col, ls=ls, lw=lw, solid_capstyle="round", zorder=1)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, f"{D.LINK_NAMES[lid]}\n{lab}", ha="center", va="center",
                    fontsize=8, bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=3)
        for mid, (x, y) in MOLE_POS.items():
            alert = res.alert_mask >> (mid - 1) & 1
            ax.scatter([x], [y], s=520, color="#c0392b" if alert else "#34495e", zorder=4,
                       edgecolors="white", linewidths=2)
            ax.text(x, y, D.MOLE_NAMES[mid], ha="center", va="center", color="white", fontsize=12,
                    fontweight="bold", zorder=5)
        ax.set_xlim(-0.25, 1.25)
        ax.set_ylim(-0.25, 1.1)
        ax.set_title("links (red = agree on the peak)", fontsize=9)

    def _draw_text(self, res):
        ax = self.ax_txt
        ax.clear()
        ax.axis("off")
        bg, fg = TIER_STYLE.get(res.tier, ("0.5", "white"))
        label = {"CONFIRMED": "CONFIRMED — periodic motion, ≥2 links agree",
                 "DETECTED": "DETECTED — periodic motion, 1 link",
                 "none": "no periodic motion above margin",
                 "warming": "warming up (window filling)"}[res.tier]
        ax.text(0.0, 0.95, label, transform=ax.transAxes, fontsize=13, fontweight="bold", color=fg, va="top",
                bbox=dict(boxstyle="round,pad=0.4", fc=bg, ec="none"))
        if res.tier != "warming" and np.isfinite(res.peak_hz):
            ax.text(0.0, 0.70, f"peak {res.peak_hz:.3f} Hz  ({res.per_min:.1f} /min)    combined PNR "
                    f"{res.pnr_db:.1f} dB    links agreeing {len(res.agreeing)}/{res.n_links}",
                    transform=ax.transAxes, fontsize=10, va="top")
        rows = ["link    state         peak Hz   PNR dB   valid   demod"]
        for lid, r in sorted(res.links.items()):
            name = D.LINK_NAMES.get(lid, str(lid))
            if r is None:
                rows.append(f"{name:6s}  warming")
                continue
            state = res.excluded.get(lid) or ("agrees" if lid in res.agreeing else
                                              "detected" if r.detected else "-")
            rows.append(f"{name:6s}  {state:12s}  {r.peak_hz:7.3f}  {r.pnr_db:7.1f}  "
                        f"{100 * r.valid_frac:5.0f}%   {r.mode}")
        ax.text(0.0, 0.52, "\n".join(rows), transform=ax.transAxes, fontsize=9, family="monospace", va="top")

    # ------------------------------------------------------------------ plumbing
    def _refresh(self):
        self.fig.canvas.draw_idle()
        if self.interactive:
            self.plt.pause(0.001)

    def pump(self):
        if self.interactive:
            self.plt.pause(0.001)

    def save(self, path: str):
        self.fig.savefig(path, dpi=120, bbox_inches="tight")
