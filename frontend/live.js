// MOLES live view — polls the read-only backend API and shows each link's
// current breathing verdict + a short history. Self-contained; does not touch
// the main dashboard (app.js), which runs its own in-browser pipeline.
//
// API base defaults to http://localhost:8000; override with ?api=http://host:port
(function () {
  "use strict";

  const API = (new URLSearchParams(location.search).get("api") || "http://localhost:8000")
                .replace(/\/$/, "");
  const POLL_MS = 3000;
  const HISTORY = 30;                 // windows to trend (~15 min at 30 s each)
  const BAND = [0.15, 0.50];          // breathing band the API uses

  const $ = (id) => document.getElementById(id);

  async function get(path) {
    const r = await fetch(API + path);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  function setStatus(text, cls) {
    const el = $("status");
    el.textContent = text;
    el.className = cls || "";
  }

  function fmt(x, d) {
    return (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
  }

  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 60 ? s + " s ago" : Math.round(s / 60) + " min ago";
  }

  // small inline-SVG sparkline of freq over history, with the breathing band shaded
  function sparkline(hist) {
    const W = 260, H = 54, pad = 4;
    const freqs = hist.map((r) => r.freq_hz).filter((f) => f !== null && !Number.isNaN(f));
    if (freqs.length < 2) return '<div class="foot">not enough history yet</div>';
    const lo = 0, hi = Math.max(0.6, ...freqs);
    const x = (i) => pad + (i / (hist.length - 1)) * (W - 2 * pad);
    const y = (f) => H - pad - ((f - lo) / (hi - lo)) * (H - 2 * pad);
    const yb0 = y(BAND[0]), yb1 = y(BAND[1]);
    let pts = "";
    hist.forEach((r, i) => {
      if (r.freq_hz === null || Number.isNaN(r.freq_hz)) return;
      pts += (pts ? " " : "") + x(i).toFixed(1) + "," + y(r.freq_hz).toFixed(1);
    });
    const dots = hist.map((r, i) =>
      (r.freq_hz === null || Number.isNaN(r.freq_hz)) ? "" :
      `<circle cx="${x(i).toFixed(1)}" cy="${y(r.freq_hz).toFixed(1)}" r="2.2" fill="${
        r.breathing ? "var(--ok)" : "var(--dim)"}"/>`).join("");
    return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <rect x="0" y="${yb1.toFixed(1)}" width="${W}" height="${(yb0 - yb1).toFixed(1)}"
            fill="rgba(74,168,255,.10)"/>
      <polyline points="${pts}" fill="none" stroke="var(--cool)" stroke-width="1.5"/>
      ${dots}
    </svg>
    <div class="foot">frequency over last ${hist.length} windows · shaded = breathing band ${BAND[0]}–${BAND[1]} Hz</div>`;
  }

  function card(link, cur, hist) {
    if (!cur) {
      return `<div class="card"><h2>${link}</h2><div class="empty">no windows yet</div></div>`;
    }
    const breathing = !!cur.breathing;
    const bpm = cur.bpm !== null ? cur.bpm : (cur.freq_hz !== null ? cur.freq_hz * 60 : null);
    return `<div class="card">
      <h2>${link} <span class="badge ${breathing ? "on" : ""}">${
        breathing ? "BREATHING" : "no breathing"}</span></h2>
      <div class="big">${fmt(bpm, 1)}<span class="u">/min</span></div>
      <div class="row"><span class="k">frequency</span><span class="v">${fmt(cur.freq_hz, 3)} Hz</span></div>
      <div class="row"><span class="k">SNR</span><span class="v">${fmt(cur.snr_db, 1)} dB</span></div>
      <div class="row"><span class="k">valid samples</span><span class="v">${fmt(cur.pct_valid, 0)} %</span></div>
      <div class="row"><span class="k">victim tap</span><span class="v">${
        cur.victim_tap === null || cur.victim_tap === undefined ? "—" : "fp+" + cur.victim_tap}</span></div>
      ${sparkline(hist)}
      <div class="foot">updated ${ago(cur.timestamp)} · raw pipeline flag: detected=${cur.detected}</div>
    </div>`;
  }

  async function tick() {
    try {
      const links = await get("/links");
      setStatus("connected · " + API, "ok");
      if (!links.length) {
        $("cards").innerHTML =
          '<div class="empty">Backend is up but has no windows yet. ' +
          'Run the pipeline with --db, or seed_fake.py for a demo.</div>';
        return;
      }
      const html = [];
      for (const link of links) {
        const q = "link_id=" + encodeURIComponent(link);
        const [cur, hist] = await Promise.all([
          get("/current?" + q),
          get("/history?" + q + "&limit=" + HISTORY),
        ]);
        html.push(card(link, cur, hist));
      }
      $("cards").innerHTML = html.join("");
    } catch (e) {
      setStatus("waiting for backend at " + API + " (" + e.message + ")", "err");
      // leave the last good render on screen; just keep retrying
    }
  }

  tick();
  setInterval(tick, POLL_MS);
})();
