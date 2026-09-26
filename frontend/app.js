(function () {
  const { LINKS, PODS } = window.MolesSim;
  const Charts = window.MolesCharts;
  const $ = id => document.getElementById(id);
  const TARGET_HZ = 0.2;
  const HISTORY = 90;           // spectra kept per link, used only to average a baseline
  const MAX_LOG = 40;
  const SERIES_MAX = 120;       // 60 s of sparkline at 2 snapshots a second
  const RELEASE = 0.8;          // hysteresis: a hit clears below 80% of threshold
  const PERSIST_S = 30;         // seconds both directions must keep agreeing
  const BASE_FACTOR = 3;        // peak must beat the empty-pile baseline by this much
  const PAIRS = [['A', 'B'], ['A', 'C'], ['B', 'C']];
  // Simulator runs (phase 2), and rig positions in metres for the default pod layout.
  const RUNS = [
    { breathing: false, rate: 0.2 },
    { breathing: true, rate: 0.2 },
    { breathing: true, rate: 0.5 }
  ];
  const RIG_POS = [[4.6, 0.9], [3, 1.8], [1.4, 1.8]];
  const RUN_NAME = ['RUN_01', 'RUN_02', 'RUN_03'];
  const RUN_DESC = ['Baseline · rig off', 'Rig on · 0.2 Hz', 'Control · 0.5 Hz'];
  const KPIS = [
    { id: 'hot',  label: 'Links hit',   icon: 'i-activity', n: 1 },
    { id: 'hits', label: 'Total hits',  icon: 'i-zap',      n: 2 },
    { id: 'peak', label: 'Peak ratio',  icon: 'i-gauge',    n: 3 },
    { id: 'loss', label: 'Packet loss', icon: 'i-radio',    n: 4 }
  ];

  const state = {
    snap: null,
    selected: 'A>B',
    threshold: 4.0,
    run: 1,
    history: {},
    latch: {},
    log: [],
    hits: 0,
    running: true,
    started: Date.now(),
    hold: {},                // pair -> time its directions first agreed, while they keep agreeing
    now: 0,
    lastT: null,
    baseline: null,          // {at, n, spec: {link: [...]}}: average spectra of an empty pile
    series: { hot: [], hits: [], peak: [], loss: [] },   // KPI sparklines, one point per snapshot
    pods: null,
    podsKnown: false,
    sim: null
  };
  const resetLatch = () => {
    LINKS.forEach(l => { state.latch[l] = false; state.history[l] = []; });
    state.hold = {};
  };
  resetLatch();
  const resetSeries = () => { state.series = { hot: [], hits: [], peak: [], loss: [] }; };

  const arrow = l => l.replace('>', '→');
  const short = l => l.replace('>', '');
  const rev = l => l.split('>').reverse().join('>');
  const pad2 = n => String(n).padStart(2, '0');
  const hms = d => pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  const ready = s => s.window_fill >= 0.999;

  // ---- mesh ---------------------------------------------------------------
  // Pod positions arrive in metres (x right, y up) in snapshot.pods. Without
  // them the layout is a placeholder and positions are only relative.
  const DEFAULT_PODS = { A: [0, 0], B: [6, 0], C: [3, 5.2] };
  const VB = { w: 600, h: 420, margin: 80 };
  const R = 30, OFF = 9;
  const POD_D = 56;                 // pod button diameter, in SVG units
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
    if (parent) parent.appendChild(e);
    return e;
  };
  const linkEls = {};
  let lay = null;               // { key, scale, toSvg(m) }
  let srcEls = null;
  const podEls = {};            // pod id -> its button element

  function makeLayout(pods) {
    const pts = PODS.map(id => pods[id]);
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
    const w = Math.max(1e-6, maxx - minx), h = Math.max(1e-6, maxy - miny);
    const scale = Math.min((VB.w - 2 * VB.margin) / w, (VB.h - 2 * VB.margin) / h);
    const ox = (VB.w - w * scale) / 2, oy = (VB.h - h * scale) / 2;
    return {
      key: JSON.stringify(pods), scale,
      toSvg: p => [ox + (p[0] - minx) * scale, oy + (maxy - p[1]) * scale]
    };
  }

  function buildMesh(pods) {
    const svg = $('mesh');
    svg.innerHTML = '';
    lay = makeLayout(pods);
    LINKS.forEach(name => {
      const [tx, rx] = name.split('>');
      const [x1, y1] = lay.toSvg(pods[tx]), [x2, y2] = lay.toSvg(pods[rx]);
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
      const ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
      // each direction sits on its own side so a pair stays readable
      const sx = x1 + nx * OFF + ux * (R + 4), sy = y1 + ny * OFF + uy * (R + 4);
      const ex = x2 + nx * OFF - ux * (R + 14), ey = y2 + ny * OFF - uy * (R + 14);
      const g = el('g', { class: 'link', tabindex: 0, role: 'button', 'aria-label': 'Link ' + arrow(name) }, svg);
      el('line', { x1: sx, y1: sy, x2: ex, y2: ey, class: 'hit' }, g);
      el('line', { x1: sx, y1: sy, x2: ex, y2: ey }, g);
      const tipx = ex + ux * 10, tipy = ey + uy * 10;
      el('polygon', { points: [tipx, tipy, ex + nx * 3.5, ey + ny * 3.5, ex - nx * 3.5, ey - ny * 3.5].join(',') }, g);
      g.addEventListener('click', () => select(name));
      g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(name); } });
      linkEls[name] = g;
    });
    buildPods();
    // estimated source: drawn above the links (the pod buttons sit underneath the SVG)
    const g = el('g', { class: 'src', style: 'display:none' }, svg);
    srcEls = {
      g,
      dot: el('circle', { class: 'sdot', r: 8 }, g),
      label: el('text', { class: 'slabel' }, g),
      truth: el('g', { class: 'truth', style: 'display:none' }, svg)
    };
    el('line', { x1: -7, y1: 0, x2: 7, y2: 0 }, srcEls.truth);
    el('line', { x1: 0, y1: -7, x2: 0, y2: 7 }, srcEls.truth);
    el('text', { x: -10, y: -8 }, srcEls.truth).textContent = 'rig (sim)';
  }

  // Pods are HTML buttons laid over the map, because CSS box-shadow does not
  // apply to SVG shapes. They sit below the SVG so the source dot is never hidden.
  function buildPods() {
    const layer = $('pods-layer');
    layer.innerHTML = PODS.map(id =>
      '<label class="toggle pod" data-pod="' + id + '">' +
      '<input type="checkbox" aria-label="Pod ' + id + '"><span class="button"></span><span class="label">' + id + '</span></label>').join('');
    PODS.forEach(id => { podEls[id] = layer.querySelector('[data-pod="' + id + '"]'); });
  }

  // Line the buttons up with the SVG's pod positions, whatever size the SVG is drawn at.
  function placePods() {
    if (!lay || !state.pods) return;
    const r = $('mesh').getBoundingClientRect(), wr = $('mesh-wrap').getBoundingClientRect();
    const s = Math.min(r.width / VB.w, r.height / VB.h);
    const ox = r.left - wr.left + (r.width - VB.w * s) / 2, oy = r.top - wr.top + (r.height - VB.h * s) / 2;
    PODS.forEach(id => {
      const p = podEls[id];
      if (!p) return;
      const [x, y] = lay.toSvg(state.pods[id]);
      p.style.left = (ox + x * s).toFixed(1) + 'px';
      p.style.top = (oy + y * s).toFixed(1) + 'px';
      p.style.setProperty('--s', (POD_D * s / 68.8).toFixed(3));      // 68.8px is the button size in the imported design
    });
  }

  function ensureLayout(snap) {
    const pods = snap.pods || DEFAULT_PODS;
    state.podsKnown = !!snap.pods;
    state.pods = pods;
    if (!lay || lay.key !== JSON.stringify(pods)) buildMesh(pods);
  }

  // ---- source estimate ----------------------------------------------------
  // ARCHITECTURE.md has no localization step: firmware reads CIR, not ranges.
  // A chest disturbs the links that pass near it, so we look for the point
  // inside the mesh whose distances to the three link segments best explain
  // how strongly each pair reacted. Each pair reads as g = exp(-(d/sigma)^2).
  // This is a model fit, not a measurement, so the UI labels it an estimate.
  function distToSeg(p, a, b) {
    const abx = b[0] - a[0], aby = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / (abx * abx + aby * aby)));
    return Math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby));
  }

  function estimateSource(snap) {
    const pods = state.pods;
    const pairs = PAIRS.map(([a, b]) => {
      const f = snap.links[a + '>' + b], r = snap.links[b + '>' + a];
      return {
        a: pods[a], b: pods[b],
        s: Math.max(0, (f.ratio + r.ratio) / 2 - 1),
        hot: state.latch[a + '>' + b] || state.latch[b + '>' + a]
      };
    });
    if (!pairs.some(p => p.hot)) return null;
    const smax = Math.max(...pairs.map(p => p.s));
    if (smax <= 0) return null;

    const side = pairs.reduce((t, p) => t + Math.hypot(p.a[0] - p.b[0], p.a[1] - p.b[1]), 0) / pairs.length;
    const sigma = 0.27 * side;
    const xs = PODS.map(id => pods[id][0]), ys = PODS.map(id => pods[id][1]);
    const step = side / 50;
    const [pa, pb, pc] = [pods.A, pods.B, pods.C];
    const orient = Math.sign((pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0]));
    const edge = (p, q, r) => orient * ((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / Math.hypot(q[0] - p[0], q[1] - p[1]);
    const inside = pt => edge(pa, pb, pt) >= -0.3 && edge(pb, pc, pt) >= -0.3 && edge(pc, pa, pt) >= -0.3;
    let best = null;
    for (let x = Math.min(...xs) - sigma; x <= Math.max(...xs) + sigma; x += step) {
      for (let y = Math.min(...ys) - sigma; y <= Math.max(...ys) + sigma; y += step) {
        if (!inside([x, y])) continue;          // the source is inside the mesh
        const g = pairs.map(p => Math.exp(-Math.pow(distToSeg([x, y], p.a, p.b) / sigma, 2)));
        const gmax = Math.max(...g);
        let err = 0.02 * (1 - gmax);            // tie-break toward points near a link
        pairs.forEach((p, i) => { const d = p.s / smax - g[i] / gmax; err += d * d; });
        if (!best || err < best.err) best = { x, y, err };
      }
    }
    if (!best) return null;
    const hotLinks = LINKS.filter(n => state.latch[n]);
    const hz = hotLinks.reduce((t, n) => t + snap.links[n].peak_hz, 0) / hotLinks.length;
    return { x: best.x, y: best.y, sigma, hz, confirmed: confirmed().length > 0 };
  }

  function updateSource(snap) {
    if (!srcEls || !lay) return;
    const est = estimateSource(snap);
    const tr = snap.sim_truth;
    const line = $('src-line');
    if (tr) {
      const [tx, ty] = lay.toSvg(tr);
      srcEls.truth.setAttribute('transform', 'translate(' + tx + ',' + ty + ')');
      srcEls.truth.style.display = '';
    } else srcEls.truth.style.display = 'none';

    if (!est) {
      srcEls.g.style.display = 'none';
      line.textContent = 'No source estimate. It appears once a link clears the hit threshold.';
      return;
    }
    const [px, py] = lay.toSvg([est.x, est.y]);
    srcEls.g.style.display = '';
    srcEls.g.setAttribute('class', 'src ' + (est.confirmed ? 'conf' : 'unconf'));
    srcEls.g.setAttribute('transform', 'translate(' + px + ',' + py + ')');
    srcEls.label.setAttribute('x', 16);
    srcEls.label.setAttribute('y', -12);
    srcEls.label.textContent = est.hz.toFixed(2) + ' Hz';

    const unit = state.podsKnown ? ' m' : ' (relative, pod layout not set)';
    let txt = 'Estimated source ≈ (' + est.x.toFixed(1) + ', ' + est.y.toFixed(1) + ')' + unit + ' · ' +
      est.hz.toFixed(2) + ' Hz · ' + (est.confirmed ? 'both directions' : 'one direction');
    if (tr) txt += ' · rig at (' + tr[0].toFixed(1) + ', ' + tr[1].toFixed(1) + ') · error ' + Math.hypot(est.x - tr[0], est.y - tr[1]).toFixed(1) + ' m';
    line.textContent = txt;
  }

  // ---- detection state ----------------------------------------------------
  function overThreshold(l, meta, factor) {
    return l.ratio >= state.threshold * factor &&
      l.peak_hz >= meta.band[0] - 1e-9 && l.peak_hz <= meta.band[1] + 1e-9;
  }

  function evalLatch(snap) {
    const risen = [];
    LINKS.forEach(name => {
      const l = snap.links[name];
      if (!l || !ready(snap)) { state.latch[name] = false; return; }
      if (!state.latch[name] && overThreshold(l, snap, 1)) { state.latch[name] = true; risen.push(name); }
      else if (state.latch[name] && !overThreshold(l, snap, RELEASE)) state.latch[name] = false;
    });
    return risen;
  }

  function logHits(snap, risen) {
    risen.forEach(name => {
      const l = snap.links[name];
      state.hits++;
      state.log.unshift({ at: new Date(), link: name, peak: l.peak_hz, ratio: l.ratio, confirmed: state.latch[rev(name)] });
    });
    if (state.log.length > MAX_LOG) state.log.length = MAX_LOG;
  }

  const confirmed = () => PAIRS.filter(([a, b]) => state.latch[a + '>' + b] && state.latch[b + '>' + a]);

  // ---- verification: direction agreement, persistence, empty baseline ------
  // Sub-bin peak: fit a parabola to the log magnitudes around the FFT peak.
  // Bins are 1/window Hz wide, so without this a 0.25 Hz signal reads 0.233 or 0.267.
  function refine(l, meta) {
    const sp = l.spectrum, k = Math.round(l.peak_hz / meta.bin_hz);
    const fallback = { hz: l.peak_hz, amp: sp[k] || 0 };
    if (k <= 0 || k >= sp.length - 1 || !(sp[k - 1] > 0) || !(sp[k] > 0) || !(sp[k + 1] > 0)) return fallback;
    const la = Math.log(sp[k - 1]), lb = Math.log(sp[k]), lc = Math.log(sp[k + 1]);
    const den = la - 2 * lb + lc;
    if (!(den < 0)) return fallback;
    const d = Math.max(-0.5, Math.min(0.5, 0.5 * (la - lc) / den));
    return { hz: (k + d) * meta.bin_hz, amp: sp[k] };
  }

  // Persistence: how long both directions of a pair have kept agreeing.
  function updateHold(snap) {
    const now = typeof snap.t === 'number' ? snap.t : Date.now() / 1000;
    if (state.lastT !== null && now - state.lastT > 3) state.hold = {};   // a gap means we did not watch that interval
    state.lastT = now;
    state.now = now;
    PAIRS.forEach(([a, b]) => {
      const f = a + '>' + b, r = b + '>' + a, k = a + b;
      const ok = ready(snap) && state.latch[f] && state.latch[r] &&
        Math.abs(refine(snap.links[f], snap).hz - refine(snap.links[r], snap).hz) <= snap.bin_hz;
      if (!ok) delete state.hold[k]; else if (state.hold[k] === undefined) state.hold[k] = now;
    });
  }

  // Current peak strength over the empty-pile baseline at the same frequency.
  function baseFactor(name, s) {
    const l = s.links[name], base = state.baseline.spec[name];
    const k = Math.round(refine(l, s).hz / s.bin_hz);
    const b = Math.max(base[k - 1] || 0, base[k] || 0, base[k + 1] || 0, 1e-9);
    return (l.spectrum[k] || 0) / b;
  }

  function pairChecks(s) {
    return PAIRS.map(([a, b]) => {
      const f = a + '>' + b, r = b + '>' + a, key = a + b;
      const rf = refine(s.links[f], s), rr = refine(s.links[r], s);
      const hf = state.latch[f], hr = state.latch[r], d = rf.hz - rr.hz;
      const dir = hf && hr && Math.abs(d) <= s.bin_hz;
      const held = state.hold[key] === undefined ? 0 : Math.max(0, state.now - state.hold[key]);
      const persist = dir && held >= PERSIST_S;
      const bf = state.baseline ? Math.min(baseFactor(f, s), baseFactor(r, s)) : null;
      const baseOk = bf === null ? null : bf >= BASE_FACTOR;
      let verdict, cls;
      if (!hf && !hr) { verdict = 'No signal'; cls = 'fill'; }
      else if (!(hf && hr)) { verdict = 'One direction'; cls = 'med'; }
      else if (!dir) { verdict = 'Frequencies differ'; cls = 'low'; }
      else if (baseOk === false) { verdict = 'Also in baseline'; cls = 'low'; }
      else if (persist && baseOk) { verdict = 'Corroborated'; cls = 'high'; }
      else if (persist) { verdict = 'Persistent · no baseline'; cls = 'med'; }
      else { verdict = 'Agrees · holding'; cls = 'med'; }
      const tip = 'Directions ' + (hf || hr ? (dir ? 'agree' : 'differ') : '–') + ' · held ' + held.toFixed(0) + ' / ' + PERSIST_S + ' s · vs baseline ' +
        (bf === null ? 'not captured' : bf.toFixed(1) + '×') + ' · ' + rf.hz.toFixed(3) + ' / ' + rr.hz.toFixed(3) + ' Hz';
      return { a, b, verdict, cls, tip };
    });
  }

  function updateBaselineStatus() {
    const b = state.baseline;
    $('base-status').textContent = b
      ? 'Baseline captured ' + hms(b.at) + ' · average of ' + b.n + ' snapshots.'
      : 'No baseline. Record the site with nobody there, then capture.';
  }
  function captureBaseline() {
    const n = LINKS.reduce((m, l) => Math.min(m, state.history[l].length), Infinity);
    if (n < 10) { $('base-status').textContent = 'Need about 5 s of data first (' + n + ' / 10 snapshots).'; return; }
    const spec = {};
    LINKS.forEach(l => {
      const h = state.history[l];
      spec[l] = h[0].map((_, k) => h.reduce((t, sp) => t + sp[k], 0) / h.length);
    });
    state.baseline = { at: new Date(), n, spec };
    updateBaselineStatus();
    if (state.snap) render();
  }

  // ---- render -------------------------------------------------------------
  function buildKpis() {
    $('kpis').innerHTML = KPIS.map(k =>
      '<div class="card"><div class="kpi-top"><div>' +
      '<p class="kpi-label">' + k.label + '</p><p class="kpi-value" id="k-' + k.id + '-v">–</p>' +
      '<div class="kpi-delta" id="k-' + k.id + '-d"></div></div>' +
      '<div class="kpi-tile" style="background:color-mix(in oklch, var(--chart-' + k.n + ') 10%, transparent);color:var(--chart-' + k.n + ')">' +
      '<svg class="ic ic-20"><use href="#' + k.icon + '"/></svg></div></div>' +
      '<canvas class="kpi-spark" id="k-' + k.id + '-c" height="48"></canvas></div>').join('');
  }

  // dir: 1 up, -1 down, 0 flat. good: true green, false red, null neutral.
  function setDelta(id, dir, text, good) {
    const d = $('k-' + id + '-d');
    d.className = 'kpi-delta' + (good === null ? '' : good ? ' good' : ' bad');
    d.innerHTML = (dir === 0 ? '' : '<svg class="ic"><use href="#' + (dir > 0 ? 'i-up' : 'i-down') + '"/></svg>') + '<span>' + text + '</span>';
  }

  function renderKpis(s, hot) {
    const S = state.series, ago = (arr, n) => arr[Math.max(0, arr.length - 1 - n)] ?? 0;
    const signed = v => (v > 0 ? '+' : '') + v;

    const dh = hot - ago(S.hot, 60);
    $('k-hot-v').textContent = hot + '/' + LINKS.length;
    setDelta('hot', Math.sign(dh), dh === 0 ? 'No change in 30 s' : signed(dh) + ' vs 30 s ago', dh === 0 ? null : dh > 0);

    const dt = Math.max(0, state.hits - ago(S.hits, 120));
    $('k-hits-v').textContent = state.hits;
    setDelta('hits', dt > 0 ? 1 : 0, dt > 0 ? '+' + dt + ' in 60 s' : 'None in 60 s', dt > 0 ? true : null);

    const peak = Math.max(...LINKS.map(n => s.links[n].ratio));
    const over = peak / state.threshold;
    $('k-peak-v').textContent = peak.toFixed(1) + '×';
    setDelta('peak', over >= 1 ? 1 : -1, over.toFixed(1) + '× threshold', over >= 1);

    const loss = LINKS.reduce((t, n) => t + s.links[n].loss, 0) / LINKS.length * 100;
    const dl = loss - ago(S.loss, 60);
    $('k-loss-v').textContent = loss.toFixed(1) + '%';
    setDelta('loss', Math.abs(dl) < 0.05 ? 0 : Math.sign(dl), (dl >= 0 ? '+' : '') + dl.toFixed(1) + ' pp vs 30 s ago', Math.abs(dl) < 0.05 ? null : dl < 0);

    Charts.drawSpark($('k-hot-c'), S.hot, '--chart-1');
    Charts.drawSpark($('k-hits-c'), S.hits, '--chart-2');
    Charts.drawSpark($('k-peak-c'), S.peak, '--chart-3');
    Charts.drawSpark($('k-loss-c'), S.loss, '--chart-4');
  }

  function render() {
    const s = state.snap;
    if (!s) return;
    $('clock').textContent = hms(new Date());

    // thresholds
    $('t-band').textContent = s.band[0].toFixed(2) + '–' + s.band[1].toFixed(2) + ' Hz';
    $('t-rate').textContent = s.sample_rate + ' Hz / link';
    $('t-win').textContent = Math.round(s.sample_rate * s.window_seconds) + ' smp · ' + s.window_seconds + ' s';
    $('t-cycle').textContent = '~' + Math.round(1000 / s.sample_rate) + ' ms';
    $('t-persist').textContent = PERSIST_S + ' s';
    $('t-basef').textContent = BASE_FACTOR + '×';
    $('thr-val').textContent = state.threshold.toFixed(1) + '× bg';

    // mesh
    const pairs = confirmed();
    const conf = new Set();
    pairs.forEach(([a, b]) => { conf.add(a + '>' + b); conf.add(b + '>' + a); });
    let hot = 0;
    LINKS.forEach(name => {
      if (state.latch[name]) hot++;
      const g = linkEls[name];
      g.classList.toggle('hot', state.latch[name]);
      g.classList.toggle('confirmed', conf.has(name));
      g.classList.toggle('selected', name === state.selected);
    });

    const b = $('banner');
    if (!ready(s)) {
      b.className = 'banner watch';
      b.textContent = 'Filling window · ' + Math.round(s.window_fill * s.window_seconds) + ' / ' + s.window_seconds + ' s. Detection starts once it is full.';
    } else if (pairs.length) {
      b.className = 'banner alert';
      b.textContent = 'Periodic signal · ' + pairs.map(p => p.join('')).join(', ') + ' agree in both directions. Not confirmed as a person.';
    } else if (hot) {
      b.className = 'banner watch';
      b.textContent = 'One direction only · ' + hot + ' link' + (hot > 1 ? 's' : '') + ' above threshold, no pair agrees';
    } else {
      b.className = 'banner idle';
      b.textContent = 'Listening · no periodic signal on any link';
    }
    $('mesh-hits').textContent = state.hits + ' hit' + (state.hits === 1 ? '' : 's');
    updateSource(s);

    const dot = { high: 'ok', low: 'hot', med: 'sim', fill: '' };
    $('pairs').innerHTML = pairChecks(s).map(p =>
      '<li title="' + p.tip + '"><span class="dotc ' + dot[p.cls] + '"></span><span class="nm">' + p.a + '–' + p.b +
      '</span><span class="end badge ' + p.cls + '">' + p.verdict + '</span></li>').join('');

    // sidebar footer: which run this is and how long it has been up
    const e = Math.floor((Date.now() - state.started) / 1000);
    const up = pad2(Math.floor(e / 60)) + ':' + pad2(e % 60);
    $('f-av').textContent = state.sim ? 'R' + (state.run + 1) : 'LV';
    $('f-name').textContent = state.sim ? RUN_NAME[state.run] : 'Live';
    $('f-sub').textContent = (state.sim ? RUN_DESC[state.run] : 'Backend') + ' · ' + up;

    renderKpis(s, hot);
    renderNodes(s);
    placePods();
    renderFft(s);
    renderLog();
  }

  function renderNodes(s) {
    $('nodes').innerHTML = PODS.map(id => {
      const mine = LINKS.filter(l => l.split('>').includes(id));
      const loss = mine.reduce((a, l) => a + s.links[l].loss, 0) / mine.length;
      const hot = mine.filter(l => state.latch[l]).length;
      const pod = podEls[id];
      if (pod) {                                       // pressed in while any of its links is a hit
        const input = pod.querySelector('input');
        input.checked = hot > 0;
        input.setAttribute('aria-label', 'Pod ' + id + ': ' + hot + ' of ' + mine.length + ' links hit. Show its strongest link.');
      }
      return '<li><span class="dotc ' + (hot ? 'hot' : 'ok') + '"></span>' +
        '<div><span class="nm">Node ' + id + '</span><span class="sub">loss ' + (loss * 100).toFixed(1) + '%</span></div>' +
        '<span class="end badge ' + (hot ? 'low' : 'fill') + '">' + hot + '/' + mine.length + ' hit</span></li>';
    }).join('');
  }

  function renderFft(s) {
    const name = state.selected, l = s.links[name];
    if (!l) return;
    $('fft-desc').textContent = arrow(name) + ' · band ' + s.band[0].toFixed(2) + '–' + s.band[1].toFixed(2) + ' Hz';
    const overlays = [];
    if (state.baseline) overlays.push({ spec: state.baseline.spec[name], color: Charts.color('--chart-2') });   // empty-pile baseline
    Charts.drawBars($('c-bars'), l.spectrum, s, overlays, TARGET_HZ);
    $('json').textContent = JSON.stringify({
      link: short(name), ratio: +l.ratio.toFixed(1), freq: +refine(l, s).hz.toFixed(3), loss: +l.loss.toFixed(3)
    });
  }

  function renderLog() {
    if (!state.log.length) { $('log').innerHTML = '<li class="empty">No detections yet.</li>'; return; }
    $('log').innerHTML = state.log.map(x =>
      '<li data-link="' + x.link + '"><div class="top">' +
      '<span class="id' + (x.confirmed ? ' both' : '') + '">' + short(x.link) + '</span>' +
      '<span class="time">' + hms(x.at) + '</span><span class="hz">' + x.peak.toFixed(2) + ' Hz</span></div>' +
      '<div class="sub">ratio ' + x.ratio.toFixed(1) + '× bg · HIT · ' +
      (x.confirmed ? 'both directions with ' + short(rev(x.link)) : 'one direction only') + '</div></li>').join('');
  }

  function select(name) { state.selected = name; render(); }

  // ---- snapshots ----------------------------------------------------------
  function onSnapshot(snap) {
    if (!state.running) return;
    state.snap = snap;
    ensureLayout(snap);
    LINKS.forEach(name => {
      const h = state.history[name];
      if (snap.links[name]) { h.push(snap.links[name].spectrum); if (h.length > HISTORY) h.shift(); }
    });
    logHits(snap, evalLatch(snap));
    updateHold(snap);
    const ls = LINKS.map(n => snap.links[n]).filter(Boolean), S = state.series;
    S.hot.push(LINKS.filter(n => state.latch[n]).length);
    S.hits.push(state.hits);
    S.peak.push(Math.max(...ls.map(l => l.ratio)));
    S.loss.push(ls.reduce((t, l) => t + l.loss, 0) / ls.length * 100);
    Object.keys(S).forEach(k => { if (S[k].length > SERIES_MAX) S[k].shift(); });
    render();
  }

  function setSource(kind, text) {
    $('source-text').textContent = text;
    $('conn-dot').className = 'dotc ' + kind;
  }

  function connectWs(url) {
    let retry = 0;
    const open = () => {
      const ws = new WebSocket(url);
      ws.onopen = () => { retry = 0; setSource('live', 'live · ' + url); };
      ws.onmessage = ev => { try { onSnapshot(JSON.parse(ev.data)); } catch (err) { console.error('bad snapshot', err); } };
      ws.onclose = () => { setSource('down', 'disconnected · retrying'); setTimeout(open, Math.min(5000, 500 * ++retry)); };
      ws.onerror = () => ws.close();
    };
    open();
  }

  // ---- controls -----------------------------------------------------------
  function wire() {
    $('thr').addEventListener('input', e => {
      state.threshold = parseFloat(e.target.value);
      $('thr-val').textContent = state.threshold.toFixed(1) + '× bg';
      if (state.snap) { evalLatch(state.snap); render(); }
    });
    $('btn-stop').addEventListener('click', () => {
      state.running = !state.running;
      $('stop-label').textContent = state.running ? 'Pause' : 'Resume';
      $('btn-stop').querySelector('use').setAttribute('href', state.running ? '#i-pause' : '#i-play');
      $('btn-stop').setAttribute('aria-pressed', String(!state.running));
    });
    $('btn-reset').addEventListener('click', () => {
      state.log = []; state.hits = 0; state.started = Date.now(); resetSeries();
      if (state.snap) render(); else renderLog();
    });
    $('base-cap').addEventListener('click', captureBaseline);
    $('base-clear').addEventListener('click', () => { state.baseline = null; updateBaselineStatus(); if (state.snap) render(); });
    $('pods-layer').addEventListener('click', e => {
      if (e.target.tagName !== 'INPUT') return;
      e.preventDefault();                                // pressed state comes from the data, not the click
      const id = e.target.closest('.pod').dataset.pod, s = state.snap;
      if (!s) return;
      const strongest = LINKS.filter(l => l.split('>').includes(id)).sort((p, q) => s.links[q].ratio - s.links[p].ratio)[0];
      select(strongest);
    });
    $('log').addEventListener('click', e => {
      const li = e.target.closest('li[data-link]');
      if (li) select(li.dataset.link);
    });
    document.querySelectorAll('.nav-label').forEach(btn => btn.addEventListener('click', () => {
      const collapsed = btn.closest('.nav-section').classList.toggle('collapsed');
      btn.setAttribute('aria-expanded', String(!collapsed));
    }));
    window.addEventListener('resize', () => { if (state.snap) render(); });
    setInterval(() => { if (state.snap && state.running) $('clock').textContent = hms(new Date()); }, 1000);
  }

  function wireSim(sim) {
    const restart = () => { sim.reset(); resetLatch(); resetSeries(); };
    const btns = document.querySelectorAll('.seg-btn');
    btns.forEach(btn => btn.addEventListener('click', () => {
      btns.forEach(x => x.classList.toggle('active', x === btn));
      state.run = +btn.dataset.run;
      Object.assign(sim.params, RUNS[state.run]);
      restart();
    }));
    $('sel-pos').addEventListener('change', () => { sim.params.source = RIG_POS[+$('sel-pos').value].slice(); restart(); });
    $('sim-amb').addEventListener('change', e => { sim.params.ambient = e.target.checked; restart(); });
    Object.assign(sim.params, RUNS[state.run]);                    // matches the active toggle in index.html
    sim.params.source = RIG_POS[+$('sel-pos').value].slice();
  }

  // ---- boot ---------------------------------------------------------------
  buildKpis();
  wire();
  const ws = new URLSearchParams(location.search).get('ws');
  if (ws) {
    $('seg-runs').classList.add('hidden');
    $('sim-panel').classList.add('hidden');
    setSource('down', 'connecting · ' + ws);
    connectWs(ws);
  } else {
    setSource('sim', 'simulator');
    state.sim = window.MolesSim.makeSim();
    wireSim(state.sim);
    state.sim.start(onSnapshot);
  }
})();
