// dashboard: data sources, detection state and rendering
(function () {
  const { LINKS, PODS } = window.MolesSim;
  const Charts = window.MolesCharts;
  const $ = id => document.getElementById(id);
  const TARGET_HZ = 0.2;
  const HISTORY = 90;
  const MAX_LOG = 40;
  const SERIES_MAX = 120;
  const RELEASE = 0.8;
  const PERSIST_S = 30;
  const BASE_FACTOR = 3;
  const PAIRS = [['A', 'B'], ['A', 'C'], ['B', 'C']];
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
    hold: {},
    now: 0,
    lastT: null,
    baseline: null,
    series: { hot: [], hits: [], peak: [], loss: [] },
    pods: null,
    podsKnown: false,
    linkSet: LINKS.slice(),
    signal: 'phase',
    sim: null,
    source: 'none',
    srcLabel: ''
  };
  const resetLatch = () => {
    LINKS.forEach(l => { state.latch[l] = false; state.history[l] = []; });
    state.hold = {};
  };
  resetLatch();
  const resetSeries = () => { state.series = { hot: [], hits: [], peak: [], loss: [] }; };

  const L = () => state.linkSet;
  const arrow = l => l.replace('>', '→');
  const short = l => l.replace('>', '');
  const rev = l => l.split('>').reverse().join('>');
  const pad2 = n => String(n).padStart(2, '0');
  const hms = d => pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  const ready = s => s.window_fill >= 0.999;

  const DEFAULT_PODS = { A: [0, 0], B: [6, 0], C: [3, 5.2] };
  const VB = { w: 600, h: 420, margin: 80 };
  const R = 30, OFF = 9;
  const POD_D = 56;
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(k => e.setAttribute(k, attrs[k]));
    if (parent) parent.appendChild(e);
    return e;
  };
  const linkEls = {};
  let lay = null;
  let srcEls = null;
  const podEls = {};

  function makeLayout(pods, key) {
    const pts = PODS.map(id => pods[id]);
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
    const w = Math.max(1e-6, maxx - minx), h = Math.max(1e-6, maxy - miny);
    const scale = Math.min((VB.w - 2 * VB.margin) / w, (VB.h - 2 * VB.margin) / h);
    const ox = (VB.w - w * scale) / 2, oy = (VB.h - h * scale) / 2;
    return { key, scale, toSvg: p => [ox + (p[0] - minx) * scale, oy + (maxy - p[1]) * scale] };
  }

  function buildMesh(pods, key) {
    const svg = $('mesh');
    svg.innerHTML = '';
    Object.keys(linkEls).forEach(k => delete linkEls[k]);
    lay = makeLayout(pods, key);
    L().forEach(name => {
      const [tx, rx] = name.split('>');
      const [x1, y1] = lay.toSvg(pods[tx]), [x2, y2] = lay.toSvg(pods[rx]);
      const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
      const ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
      const off = L().includes(rev(name)) ? OFF : 0;
      const sx = x1 + nx * off + ux * (R + 4), sy = y1 + ny * off + uy * (R + 4);
      const ex = x2 + nx * off - ux * (R + 14), ey = y2 + ny * off - uy * (R + 14);
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
    const g = el('g', { class: 'src', style: 'display:none' }, svg);
    srcEls = {
      g,
      dot: el('circle', { class: 'sdot', r: 8 }, g),
      label: el('text', { class: 'slabel' }, g)
    };
    placePods();
  }

  function buildPods() {
    const layer = $('pods-layer');
    layer.innerHTML = PODS.map(id =>
      '<label class="toggle pod" data-pod="' + id + '">' +
      '<input type="checkbox" aria-label="Pod ' + id + '"><span class="button"></span><span class="label">' + id + '</span></label>').join('');
    PODS.forEach(id => { podEls[id] = layer.querySelector('[data-pod="' + id + '"]'); });
  }

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
      p.style.setProperty('--s', (POD_D * s / 68.8).toFixed(3));
    });
  }

  function applyLayout(pods, known) {
    state.podsKnown = known;
    state.pods = pods;
    const key = JSON.stringify(pods) + '|' + L().join(',');
    if (!lay || lay.key !== key) buildMesh(pods, key);
  }
  function ensureLayout(snap) {
    const names = snap.links ? LINKS.filter(n => n in snap.links) : [];
    state.linkSet = names.length ? names : LINKS.slice();
    applyLayout(snap.pods || DEFAULT_PODS, !!snap.pods);
  }
  function setLinkSet(names) {
    state.linkSet = names.slice();
    applyLayout(state.pods || DEFAULT_PODS, state.podsKnown);
  }

  function distToSeg(p, a, b) {
    const abx = b[0] - a[0], aby = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / (abx * abx + aby * aby)));
    return Math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby));
  }

  function estimateSource(snap) {
    const pods = state.pods;
    const pairs = PAIRS.map(([a, b]) => {
      const have = [snap.links[a + '>' + b], snap.links[b + '>' + a]].filter(Boolean);
      if (!have.length) return null;
      return {
        a: pods[a], b: pods[b],
        s: Math.max(0, have.reduce((t, x) => t + x.ratio, 0) / have.length - 1),
        hot: state.latch[a + '>' + b] || state.latch[b + '>' + a]
      };
    }).filter(Boolean);
    if (!pairs.length || !pairs.some(p => p.hot)) return null;
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
        if (!inside([x, y])) continue;
        const g = pairs.map(p => Math.exp(-Math.pow(distToSeg([x, y], p.a, p.b) / sigma, 2)));
        const gmax = Math.max(...g);
        let err = 0.02 * (1 - gmax);
        pairs.forEach((p, i) => { const d = p.s / smax - g[i] / gmax; err += d * d; });
        if (!best || err < best.err) best = { x, y, err };
      }
    }
    if (!best) return null;
    const hotLinks = L().filter(n => state.latch[n]);
    const hz = hotLinks.reduce((t, n) => t + snap.links[n].peak_hz, 0) / hotLinks.length;
    return { x: best.x, y: best.y, sigma, hz, confirmed: agreeSet(snap).size > 0 };
  }

  function updateSource(snap) {
    if (!srcEls || !lay) return;
    const est = estimateSource(snap);
    const tr = snap.sim_truth;
    const line = $('src-line');
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
      est.hz.toFixed(2) + ' Hz · ' + (twoWay() ? (est.confirmed ? 'both directions' : 'one direction') : (est.confirmed ? 'links agree' : 'single link'));
    if (tr) txt += ' · rig at (' + tr[0].toFixed(1) + ', ' + tr[1].toFixed(1) + ') · error ' + Math.hypot(est.x - tr[0], est.y - tr[1]).toFixed(1) + ' m';
    line.textContent = txt;
  }

  function overThreshold(l, meta, factor) {
    return l.ratio >= state.threshold * factor &&
      l.peak_hz >= meta.band[0] - 1e-9 && l.peak_hz <= meta.band[1] + 1e-9;
  }

  function evalLatch(snap) {
    const risen = [];
    L().forEach(name => {
      const l = snap.links[name];
      if (!l || !ready(snap)) { state.latch[name] = false; return; }
      if (!state.latch[name] && overThreshold(l, snap, 1)) { state.latch[name] = true; risen.push(name); }
      else if (state.latch[name] && !overThreshold(l, snap, RELEASE)) state.latch[name] = false;
    });
    return risen;
  }

  const twoWay = () => L().some(n => L().includes(rev(n)));
  const hzOf = (name, s) => refine(s.links[name], s).hz;
  function agrees(name, s) {
    if (!state.latch[name]) return false;
    if (twoWay()) return L().includes(rev(name)) && !!state.latch[rev(name)];
    return L().some(m => m !== name && state.latch[m] && Math.abs(hzOf(m, s) - hzOf(name, s)) <= s.bin_hz + 1e-9);
  }
  const agreeSet = s => new Set(L().filter(n => agrees(n, s)));

  function logHits(snap, risen) {
    const ag = agreeSet(snap);
    risen.forEach(name => {
      const l = snap.links[name];
      state.hits++;
      state.log.unshift({
        at: new Date(), link: name, peak: l.peak_hz, ratio: l.ratio, tw: twoWay(),
        agree: ag.has(name), with: twoWay() ? [rev(name)] : [...ag].filter(n => n !== name)
      });
    });
    if (state.log.length > MAX_LOG) state.log.length = MAX_LOG;
  }

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

  function groups() {
    if (twoWay()) {
      return PAIRS.filter(([a, b]) => L().includes(a + '>' + b) && L().includes(b + '>' + a))
        .map(([a, b]) => ({ key: a + b, label: a + '–' + b, links: [a + '>' + b, b + '>' + a] }));
    }
    return L().map(n => ({ key: n, label: arrow(n), links: [n] }));
  }
  function groupState(s) {
    const tw = twoWay();
    return groups().map(g => {
      const hits = g.links.map(n => !!state.latch[n]), hz = g.links.map(n => hzOf(n, s));
      const allHit = hits.every(Boolean);
      const dir = tw ? allHit && Math.abs(hz[0] - hz[1]) <= s.bin_hz : agrees(g.links[0], s);
      return { ...g, hits, hz, anyHit: hits.some(Boolean), allHit, dir };
    });
  }

  function updateHold(snap) {
    const now = typeof snap.t === 'number' ? snap.t : Date.now() / 1000;
    if (state.lastT !== null && now - state.lastT > 3) state.hold = {};
    state.lastT = now;
    state.now = now;
    groupState(snap).forEach(g => {
      if (!(ready(snap) && g.dir)) delete state.hold[g.key];
      else if (state.hold[g.key] === undefined) state.hold[g.key] = now;
    });
  }

  function baseFactor(name, s) {
    const base = state.baseline.spec[name];
    if (!base) return Infinity;
    const l = s.links[name], k = Math.round(refine(l, s).hz / s.bin_hz);
    const b = Math.max(base[k - 1] || 0, base[k] || 0, base[k + 1] || 0, 1e-9);
    return (l.spectrum[k] || 0) / b;
  }

  function groupChecks(s) {
    const tw = twoWay();
    return groupState(s).map(g => {
      const held = state.hold[g.key] === undefined ? 0 : Math.max(0, state.now - state.hold[g.key]);
      const persist = g.dir && held >= PERSIST_S;
      const bf = state.baseline ? Math.min(...g.links.map(n => baseFactor(n, s))) : null;
      const baseOk = bf === null ? null : bf >= BASE_FACTOR;
      let verdict, cls;
      if (!g.anyHit) { verdict = 'No signal'; cls = 'fill'; }
      else if (tw && !g.allHit) { verdict = 'One direction'; cls = 'med'; }
      else if (!tw && !g.dir) { verdict = 'Single link'; cls = 'med'; }
      else if (tw && !g.dir) { verdict = 'Frequencies differ'; cls = 'low'; }
      else if (baseOk === false) { verdict = 'Also in baseline'; cls = 'low'; }
      else if (persist && baseOk) { verdict = 'Corroborated'; cls = 'high'; }
      else if (persist) { verdict = 'Persistent · no baseline'; cls = 'med'; }
      else { verdict = 'Agrees · holding'; cls = 'med'; }
      const basisText = tw ? 'Directions ' + (g.anyHit ? (g.dir ? 'agree' : 'differ') : '–') : (g.anyHit ? (g.dir ? 'Another link agrees' : 'No other link at this frequency') : '–');
      const tip = basisText + ' · held ' + held.toFixed(0) + ' / ' + PERSIST_S + ' s · vs baseline ' +
        (bf === null ? 'not captured' : (isFinite(bf) ? bf.toFixed(1) + '×' : 'n/a')) + ' · ' + g.hz.map(h => h.toFixed(3)).join(' / ') + ' Hz';
      return { label: g.label, verdict, cls, tip };
    });
  }

  function updateBaselineStatus() {
    const b = state.baseline;
    $('base-status').textContent = b
      ? 'Baseline captured ' + hms(b.at) + ' · average of ' + b.n + ' snapshots.'
      : 'No baseline. Record the site with nobody there, then capture.';
  }
  function captureBaseline() {
    const n = L().reduce((m, l) => Math.min(m, state.history[l].length), Infinity);
    if (n < 10) { $('base-status').textContent = 'Need about 5 s of full-window data first (' + (isFinite(n) ? n : 0) + ' / 10 snapshots).'; return; }
    const spec = {};
    L().forEach(l => {
      const h = state.history[l];
      spec[l] = h[0].map((_, k) => h.reduce((t, sp) => t + sp[k], 0) / h.length);
    });
    state.baseline = { at: new Date(), n, spec };
    updateBaselineStatus();
    if (state.snap) render();
  }

  function buildKpis() {
    $('kpis').innerHTML = KPIS.map(k =>
      '<div class="card"><div class="kpi-top"><div>' +
      '<p class="kpi-label">' + k.label + '</p><p class="kpi-value" id="k-' + k.id + '-v">–</p>' +
      '<div class="kpi-delta" id="k-' + k.id + '-d"></div></div>' +
      '<div class="kpi-tile" style="background:color-mix(in oklch, var(--chart-' + k.n + ') 10%, transparent);color:var(--chart-' + k.n + ')">' +
      '<svg class="ic ic-20"><use href="#' + k.icon + '"/></svg></div></div>' +
      '<canvas class="kpi-spark" id="k-' + k.id + '-c" height="48"></canvas></div>').join('');
  }

  function setDelta(id, dir, text, good) {
    const d = $('k-' + id + '-d');
    d.className = 'kpi-delta' + (good === null ? '' : good ? ' good' : ' bad');
    d.innerHTML = (dir === 0 ? '' : '<svg class="ic"><use href="#' + (dir > 0 ? 'i-up' : 'i-down') + '"/></svg>') + '<span>' + text + '</span>';
  }

  function renderKpis(s, hot) {
    const S = state.series, ago = (arr, n) => arr[Math.max(0, arr.length - 1 - n)] ?? 0;
    const signed = v => (v > 0 ? '+' : '') + v;
    const act = L();

    const dh = hot - ago(S.hot, 60);
    $('k-hot-v').textContent = hot + '/' + act.length;
    setDelta('hot', Math.sign(dh), dh === 0 ? 'No change in 30 s' : signed(dh) + ' vs 30 s ago', dh === 0 ? null : dh > 0);

    const dt = Math.max(0, state.hits - ago(S.hits, 120));
    $('k-hits-v').textContent = state.hits;
    setDelta('hits', dt > 0 ? 1 : 0, dt > 0 ? '+' + dt + ' in 60 s' : 'None in 60 s', dt > 0 ? true : null);

    const peak = Math.max(...act.map(n => s.links[n].ratio));
    const over = peak / state.threshold;
    $('k-peak-v').textContent = peak.toFixed(1) + '×';
    setDelta('peak', over >= 1 ? 1 : -1, over.toFixed(1) + '× threshold', over >= 1);

    const loss = act.reduce((t, n) => t + s.links[n].loss, 0) / act.length * 100;
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

    $('t-band').textContent = s.band[0].toFixed(2) + '–' + s.band[1].toFixed(2) + ' Hz';
    $('t-rate').textContent = s.sample_rate + ' Hz / link';
    $('t-win').textContent = Math.round(s.sample_rate * s.window_seconds) + ' smp · ' + s.window_seconds + ' s';
    $('t-cycle').textContent = '~' + Math.round(1000 / s.sample_rate) + ' ms';
    $('t-persist').textContent = PERSIST_S + ' s';
    $('t-basef').textContent = BASE_FACTOR + '×';
    $('thr-val').textContent = state.threshold.toFixed(1) + '× bg';

    const ag = agreeSet(s), tw = twoWay(), act = L();
    let hot = 0;
    act.forEach(name => {
      if (state.latch[name]) hot++;
      const g = linkEls[name];
      if (!g) return;
      g.classList.toggle('hot', !!state.latch[name]);
      g.classList.toggle('confirmed', ag.has(name));
      g.classList.toggle('selected', name === state.selected);
    });
    $('mesh-sub').textContent = ({ 3: 'Three', 6: 'Six' }[act.length] || act.length) + (tw ? ' directed' : ' one-way') + ' links between three pods';

    const b = $('banner');
    if (!ready(s)) {
      b.className = 'banner watch';
      b.textContent = 'Filling window · ' + Math.round(s.window_fill * s.window_seconds) + ' / ' + s.window_seconds + ' s. Detection starts once it is full.';
    } else if (ag.size) {
      b.className = 'banner alert';
      b.textContent = tw
        ? 'Periodic signal · ' + PAIRS.filter(([x, y]) => ag.has(x + '>' + y)).map(p => p.join('')).join(', ') + ' agree in both directions. Not confirmed as a person.'
        : 'Periodic signal · ' + [...ag].map(arrow).join(', ') + ' agree on frequency. Not confirmed as a person.';
    } else if (hot) {
      b.className = 'banner watch';
      b.textContent = (tw ? 'One direction only' : 'Single link') + ' · ' + hot + ' link' + (hot > 1 ? 's' : '') + ' above threshold, ' + (tw ? 'no pair agrees' : 'no other link agrees');
    } else {
      b.className = 'banner idle';
      b.textContent = 'Listening · no periodic signal on any link';
    }
    $('mesh-hits').textContent = state.hits + ' hit' + (state.hits === 1 ? '' : 's');
    updateSource(s);

    const dot = { high: 'ok', low: 'hot', med: 'sim', fill: '' };
    $('pairs').innerHTML = groupChecks(s).map(p =>
      '<li title="' + p.tip + '"><span class="dotc ' + dot[p.cls] + '"></span><span class="nm">' + p.label +
      '</span><span class="end badge ' + p.cls + '">' + p.verdict + '</span></li>').join('');

    const e = Math.floor((Date.now() - state.started) / 1000);
    const up = pad2(Math.floor(e / 60)) + ':' + pad2(e % 60);
    footer(up);

    renderKpis(s, hot);
    renderNodes(s);
    placePods();
    renderFft(s);
    renderRange(s);
    renderLog();
  }

  function renderNodes(s) {
    $('nodes').innerHTML = PODS.map(id => {
      const mine = L().filter(l => l.split('>').includes(id));
      const loss = mine.length ? mine.reduce((a, l) => a + s.links[l].loss, 0) / mine.length : 0;
      const hot = mine.filter(l => state.latch[l]).length;
      const pod = podEls[id];
      if (pod) {
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
    $('fft-desc').textContent = arrow(name) + (s.signal ? ' · ' + s.signal : '') + ' · band ' + s.band[0].toFixed(2) + '–' + s.band[1].toFixed(2) + ' Hz';
    const overlays = [];
    if (state.baseline && state.baseline.spec[name]) overlays.push({ spec: state.baseline.spec[name], color: Charts.color('--chart-2') });
    Charts.drawBars($('c-bars'), l.spectrum, s, overlays, TARGET_HZ);
    const j = { link: short(name), ratio: +l.ratio.toFixed(1), freq: +refine(l, s).hz.toFixed(3), loss: +l.loss.toFixed(3) };
    if (l.tap !== undefined) { j.tap = l.tap; j.ref = l.ref_tap; }
    $('json').textContent = JSON.stringify(j);
  }

  function renderRange(s) {
    const card = $('range-card'), l = s.links[state.selected], r = l && l.range;
    const vals = r ? r.filter(v => v !== null) : [];
    card.classList.toggle('hidden', vals.length < 2);
    if (vals.length < 2) return;
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const last = [...r].reverse().find(v => v !== null);
    $('range-desc').textContent = arrow(state.selected) + ' · SS-TWR · offset uncalibrated, so only changes count';
    $('range-now').textContent = last.toFixed(3) + ' m';
    $('range-note').textContent = 'Window mean ' + mean.toFixed(3) + ' m · spread ' + ((Math.max(...vals) - Math.min(...vals)) * 100).toFixed(1) +
      ' cm. A hand over the link makes it read longer and drop rounds.';
    Charts.drawTrace($('c-range'), r.map(v => (v === null ? null : (v - mean) * 100)), '--chart-3', 'cm');
  }

  function renderLog() {
    const list = $('log'), foot = $('log-foot');
    if (!state.log.length) {
      list.innerHTML = '<li class="empty">No detections yet.</li>';
      foot.textContent = 'Waiting for a hit';
      return;
    }
    const why = x => x.tw
      ? (x.agree ? 'both directions with ' + short(rev(x.link)) : 'one direction only')
      : (x.agree ? 'agrees with ' + x.with.map(arrow).join(', ') : 'no other link agrees');
    list.innerHTML = state.log.map(x =>
      '<li data-link="' + x.link + '"><div class="top">' +
      '<span class="id' + (x.agree ? ' both' : '') + '">' + short(x.link) + '</span>' +
      '<span class="time">' + hms(x.at) + '</span><span class="hz">' + x.peak.toFixed(2) + ' Hz</span></div>' +
      '<div class="sub">ratio ' + x.ratio.toFixed(1) + '× bg · HIT · ' + why(x) + '</div></li>').join('');
    let shown = 0, over = false;
    [...list.children].forEach(li => {
      if (over || li.offsetTop + li.offsetHeight > list.clientHeight) { over = true; li.hidden = true; } else shown++;
    });
    foot.textContent = 'Showing ' + shown + ' of ' + state.log.length + ' · newest first';
  }

  function select(name) { state.selected = name; render(); }

  function onSnapshot(snap) {
    if (!state.running) return;
    state.snap = snap;
    ensureLayout(snap);
    if (ready(snap)) {
      L().forEach(name => {
        const h = state.history[name];
        if (snap.links[name]) { h.push(snap.links[name].spectrum); if (h.length > HISTORY) h.shift(); }
      });
    }
    logHits(snap, evalLatch(snap));
    updateHold(snap);
    const ls = L().map(n => snap.links[n]).filter(Boolean), S = state.series;
    S.hot.push(L().filter(n => state.latch[n]).length);
    S.hits.push(state.hits);
    S.peak.push(Math.max(...ls.map(l => l.ratio)));
    S.loss.push(ls.reduce((t, l) => t + l.loss, 0) / ls.length * 100);
    Object.keys(S).forEach(k => { if (S[k].length > SERIES_MAX) S[k].shift(); });
    render();
  }

  const store = {
    get(k) { try { return localStorage.getItem('moles.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('moles.' + k, v); } catch (e) {  } }
  };
  const SOURCE_NAME = { none: 'No source', sim: 'Simulator', serial: 'Serial', csv: 'File replay', ws: 'WebSocket' };
  const SOURCE_TAG = { none: '–', sim: '', serial: 'SR', csv: 'FL', ws: 'WS' };
  const ONEWAY = window.MolesPipeline.ONEWAY_LINKS;
  let active = null;

  function setSource(kind, text) {
    $('source-text').textContent = text;
    $('conn-dot').className = 'dotc ' + kind;
  }
  const srcStatus = t => { $('src-status').textContent = t; };

  function footer(up) {
    const kind = state.source;
    $('f-av').textContent = kind === 'sim' ? 'R' + (state.run + 1) : SOURCE_TAG[kind];
    $('f-name').textContent = kind === 'sim' ? RUN_NAME[state.run] : SOURCE_NAME[kind];
    $('f-sub').textContent = (kind === 'sim' ? RUN_DESC[state.run] : state.srcLabel || '–') + (up ? ' · ' + up : '');
  }

  function readPods() {
    const v = ['pod-a', 'pod-b', 'pod-c'].map(id => $(id).value.trim());
    if (v.some(x => !x)) return null;
    const pts = v.map(x => x.split(/[\s,]+/).filter(Boolean).map(Number));
    if (pts.some(p => p.length !== 2 || p.some(n => !isFinite(n)))) return null;
    return { A: pts[0], B: pts[1], C: pts[2] };
  }
  function podsChanged() {
    store.set('pods', ['pod-a', 'pod-b', 'pod-c'].map(id => $(id).value.trim()).join('|'));
    if (active && active.pl) active.pl.setPods(readPods());
  }

  const pipeOpts = () => ({ signal: state.signal, align: $('opt-align').checked, pollHz: +$('poll-hz').value || 10 });

  function stopSource() {
    if (!active) return;
    try { active.stop(); } catch (e) { console.error('stopping source', e); }
    active = null;
  }

  function showSourceUi() {
    const sim = state.source === 'sim';
    $('seg-runs').classList.toggle('hidden', !sim);
    $('sim-panel').classList.toggle('hidden', !sim);
    $('real-src').classList.toggle('hidden', sim);
    $('btn-disc').classList.toggle('hidden', !['serial', 'csv', 'ws'].includes(state.source));
    document.querySelectorAll('#seg-sim .seg-btn').forEach(b => b.classList.toggle('active', (b.dataset.sim === '1') === sim));
  }

  function renderEmpty() {
    const b = $('banner');
    b.className = 'banner watch';
    b.textContent = state.source === 'none'
      ? 'No data source. Turn the simulator on, connect serial, or load a file.'
      : 'Waiting for data…';
    $('src-line').textContent = '–';
    $('pairs').innerHTML = '';
    $('nodes').innerHTML = '';
    $('mesh-hits').textContent = '0 hits';
    $('fft-desc').textContent = '–';
    $('json').textContent = '';
    KPIS.forEach(k => { $('k-' + k.id + '-v').textContent = '–'; $('k-' + k.id + '-d').innerHTML = ''; Charts.clear($('k-' + k.id + '-c')); });
    Charts.clear($('c-bars'));
    $('range-card').classList.add('hidden');
    LINKS.forEach(n => { if (linkEls[n]) linkEls[n].classList.remove('hot', 'confirmed'); });
    PODS.forEach(id => { if (podEls[id]) podEls[id].querySelector('input').checked = false; });
    if (srcEls) srcEls.g.style.display = 'none';
    placePods();
    renderLog();
    footer('');
  }

  function beginSource(kind, label, links) {
    stopSource();
    state.source = kind;
    state.srcLabel = label || '';
    state.snap = null;
    state.log = []; state.hits = 0; state.started = Date.now();
    resetLatch(); resetSeries();
    setLinkSet(links || LINKS);
    showSourceUi();
    renderEmpty();
  }

  function useSim() {
    beginSource('sim', 'Simulator', LINKS);
    setSource('sim', 'simulator');
    state.sim.start(onSnapshot);
    active = { kind: 'sim', stop: () => state.sim.stop() };
    store.set('sim', '1');
  }

  function goReal(msg) {
    beginSource('none', '', LINKS);
    setSource('down', 'no source');
    srcStatus(msg || 'No source. Connect serial, load a file, or turn the simulator on.');
    store.set('sim', '0');
  }

  function pipelineStatus(pl, dec) {
    let t = pl.stats.ok + ' rows read · ' + pl.stats.rejected + ' rejected · ' + pl.stats.gaps + ' gaps';
    if (dec) t += ' · ' + dec.stats.badChecksum + ' bad frames';
    if (pl.stats.aligned) t += ' · ' + pl.stats.aligned + ' windows realigned';
    const h = pl.hostStatus();
    return h ? t + ' · host: ' + h : t;
  }

  function emitter(pl, dec) {
    return setInterval(() => {
      if (pl.stats.ok > 0) onSnapshot(pl.snapshot());
      if (active && active.pl === pl) srcStatus(pipelineStatus(pl, dec));
    }, 500);
  }

  const makeDecoder = pl => window.MolesFrames.createDecoder({
    onRow: r => pl.ingestRow(r),
    onStatus: s => pl.setHostStatus(s)
  });

  async function connectSerial() {
    if (!('serial' in navigator)) {
      srcStatus('Web Serial is not available here. Use Chrome or Edge on a computer, opened from localhost or https.');
      return;
    }
    let port;
    try { port = await navigator.serial.requestPort(); } catch (e) { srcStatus('No port chosen.'); return; }
    const baud = parseInt($('baud').value, 10) || 921600;
    const binary = $('serial-fmt').value !== 'csv';
    store.set('baud', String(baud));
    beginSource('serial', baud + ' baud · ' + (binary ? 'binary frames' : 'CSV'), binary ? ONEWAY : LINKS);
    try { await port.open({ baudRate: baud }); }
    catch (e) { setSource('down', 'serial · could not open'); srcStatus('Could not open the port: ' + e.message + '. Close anything else using it (the Arduino serial monitor, for example).'); return; }

    const pl = window.MolesPipeline.create(Object.assign({ source: 'serial' }, pipeOpts()));
    pl.setPods(readPods());
    const dec = binary ? makeDecoder(pl) : null;
    let stopped = false, reader = null;
    const timer = emitter(pl, dec);
    active = {
      kind: 'serial', pl, dec,
      stop() {
        stopped = true; clearInterval(timer);
        Promise.resolve(reader && reader.cancel()).catch(() => {})
          .then(() => new Promise(r => setTimeout(r, 60)))
          .then(() => port.close()).catch(() => {});
      }
    };
    setSource('live', 'serial · ' + baud + ' baud');
    srcStatus('Connected. Waiting for ' + (binary ? 'frames' : 'rows') + '…');

    (async () => {
      const text = new TextDecoder();
      let buf = '', errors = 0, ended = false;
      try {
        while (!stopped && !ended && port.readable) {
          reader = port.readable.getReader();
          try {
            for (;;) {
              const { value, done } = await reader.read();
              if (done) { ended = true; break; }
              if (dec) { dec.push(value); continue; }
              buf += text.decode(value, { stream: true });
              let i;
              while ((i = buf.indexOf('\n')) >= 0) { pl.ingestLine(buf.slice(0, i)); buf = buf.slice(i + 1); }
              if (buf.length > 200000) buf = '';
            }
          } catch (e) {
            if (stopped) break;
            if (++errors > 20) throw e;
            buf = '';
            if (dec) dec.reset();
          } finally { reader.releaseLock(); reader = null; }
        }
        if (!stopped) { setSource('down', 'serial · stream ended'); srcStatus('The serial stream ended. ' + pipelineStatus(pl, dec)); }
      } catch (e) {
        if (!stopped) { setSource('down', 'serial · disconnected'); srcStatus('Serial read stopped: ' + e.message); }
      }
    })();
  }

  function loadFile(file) {
    beginSource('csv', file.name, LINKS);
    setSource('sim', 'file · ' + file.name);
    file.arrayBuffer().then(buf => {
      if (state.source !== 'csv') return;
      const bytes = new Uint8Array(buf);
      const pl = window.MolesPipeline.create(Object.assign({ source: 'csv' }, pipeOpts()));
      pl.setPods(readPods());
      const bin = window.MolesFrames.decodeAll(bytes);
      let rows, bad = 0, kind;
      if (bin.rows.length) {
        rows = bin.rows;
        const links = new Set(rows.map(r => r.link_id)).size, poll = +$('poll-hz').value || 10;
        rows.forEach((r, i) => { r.t = (i * 1000) / (poll * links); });
        bad = bin.stats.badChecksum + bin.stats.badPacket;
        if (bin.statuses.length) pl.setHostStatus(bin.statuses[bin.statuses.length - 1]);
        setLinkSet(ONEWAY);
        kind = 'binary frames';
      } else {
        rows = [];
        new TextDecoder().decode(bytes).split(/\r?\n/).forEach(line => {
          const r = pl.parse(line);
          if (r && r.error) bad++; else if (r) rows.push(r);
        });
        kind = 'CSV';
      }
      if (!rows.length) {
        setSource('down', 'file · no data');
        srcStatus('No valid data in ' + file.name + '. Expected the host\'s binary frames (AA 55 …) or CSV: t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, …');
        return;
      }
      replay(pl, rows, file, bad, kind);
    }).catch(e => { setSource('down', 'file · unreadable'); srcStatus('Could not read the file: ' + e.message); });
  }

  function replay(pl, rows, file, bad, kind) {
    const t0 = rows[0].t;
    let i = 0, clock = t0, lastEmit = -Infinity, lastWall = 0;
    const timer = setInterval(() => {
      clock += 50 * (+$('csv-speed').value || 1);
      while (i < rows.length && rows[i].t <= clock) pl.ingestRow(rows[i++]);
      const done = i >= rows.length, now = performance.now();
      if (pl.stats.ok > 0 && (done || (clock - lastEmit >= 500 && now - lastWall >= 100))) {
        onSnapshot(pl.snapshot()); lastEmit = clock; lastWall = now;
      }
      const secs = ((Math.min(clock, rows[rows.length - 1].t) - t0) / 1000).toFixed(0);
      srcStatus((done ? 'Replay finished · ' : 'Replaying · ') + kind + ' · ' + i + ' / ' + rows.length + ' rows · ' + secs + ' s' +
        (bad ? ' · ' + bad + ' rejected' : '') + (pl.hostStatus() ? ' · host: ' + pl.hostStatus() : ''));
      if (done) { clearInterval(timer); setSource('live', 'file · finished'); }
    }, 50);
    active = { kind: 'csv', pl, file, stop: () => clearInterval(timer) };
    srcStatus('Replaying ' + rows.length + ' ' + kind + ' rows' + (bad ? ' (' + bad + ' rejected)' : '') + '…');
  }

  function connectWs(url) {
    if (!/^wss?:\/\//i.test(url)) { srcStatus('The URL must start with ws:// or wss://'); return; }
    store.set('ws', url);
    beginSource('ws', url, LINKS);
    setSource('down', 'connecting · ' + url);
    const pl = window.MolesPipeline.create(Object.assign({ source: 'ws' }, pipeOpts()));
    pl.setPods(readPods());
    const dec = makeDecoder(pl);
    let retry = 0, socket = null, closed = false;
    const timer = emitter(pl, dec);
    const open = () => {
      socket = new WebSocket(url);
      socket.binaryType = 'arraybuffer';
      socket.onopen = () => { retry = 0; setSource('live', 'live · ' + url); srcStatus('Connected. Waiting for data…'); };
      socket.onmessage = ev => {
        if (typeof ev.data !== 'string') { dec.push(new Uint8Array(ev.data)); return; }
        if (ev.data.trimStart()[0] === '{') { try { onSnapshot(JSON.parse(ev.data)); } catch (err) { console.error('bad snapshot', err); } }
        else ev.data.split(/\r?\n/).forEach(l => pl.ingestLine(l));
      };
      socket.onclose = () => { if (closed) return; setSource('down', 'disconnected · retrying'); setTimeout(open, Math.min(5000, 500 * ++retry)); };
      socket.onerror = () => socket.close();
    };
    open();
    active = { kind: 'ws', pl, dec, stop() { closed = true; clearInterval(timer); if (socket) socket.close(); } };
  }

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
      e.preventDefault();
      const id = e.target.closest('.pod').dataset.pod, s = state.snap;
      if (!s) return;
      const strongest = L().filter(l => l.split('>').includes(id)).sort((p, q) => s.links[q].ratio - s.links[p].ratio)[0];
      if (strongest) select(strongest);
    });
    $('log').addEventListener('click', e => {
      const li = e.target.closest('li[data-link]');
      if (li) select(li.dataset.link);
    });
    document.querySelectorAll('.nav-label').forEach(btn => btn.addEventListener('click', () => {
      const collapsed = btn.closest('.nav-section').classList.toggle('collapsed');
      btn.setAttribute('aria-expanded', String(!collapsed));
    }));
    window.addEventListener('resize', () => { if (state.snap) render(); else placePods(); });
    setInterval(() => { if (state.snap && state.running) $('clock').textContent = hms(new Date()); }, 1000);
  }

  function wireSim(sim) {
    const restart = () => { sim.reset(); resetLatch(); resetSeries(); };
    const btns = document.querySelectorAll('#seg-runs .seg-btn');
    btns.forEach(btn => btn.addEventListener('click', () => {
      btns.forEach(x => x.classList.toggle('active', x === btn));
      state.run = +btn.dataset.run;
      Object.assign(sim.params, RUNS[state.run]);
      restart();
    }));
    $('sel-pos').addEventListener('change', () => { sim.params.source = RIG_POS[+$('sel-pos').value].slice(); restart(); });
    $('sim-amb').addEventListener('change', e => { sim.params.ambient = e.target.checked; restart(); });
    Object.assign(sim.params, RUNS[state.run]);
    sim.params.source = RIG_POS[+$('sel-pos').value].slice();
  }

  function setSignal(sig) {
    state.signal = sig === 'magnitude' ? 'magnitude' : 'phase';
    store.set('signal', state.signal);
    document.querySelectorAll('#seg-signal .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.sig === state.signal));
    if (active && active.pl) {
      active.pl.setSignal(state.signal);
      if (active.pl.stats.ok > 0) onSnapshot(active.pl.snapshot());
    }
  }

  function wireSources() {
    document.querySelectorAll('#seg-sim .seg-btn').forEach(b => b.addEventListener('click', () => {
      if (b.dataset.sim === '1') { if (state.source !== 'sim') useSim(); }
      else if (state.source === 'sim') goReal();
    }));
    $('btn-serial').addEventListener('click', connectSerial);
    $('btn-csv').addEventListener('click', () => $('csv-file').click());
    $('csv-file').addEventListener('change', e => {
      const f = e.target.files[0];
      e.target.value = '';
      if (f) loadFile(f);
    });
    $('btn-ws').addEventListener('click', () => connectWs($('ws-url').value.trim()));
    $('ws-url').addEventListener('keydown', e => { if (e.key === 'Enter') connectWs($('ws-url').value.trim()); });
    $('btn-disc').addEventListener('click', () => goReal('Disconnected.'));
    $('baud').addEventListener('change', () => store.set('baud', $('baud').value));
    $('serial-fmt').addEventListener('change', () => store.set('fmt', $('serial-fmt').value));
    $('poll-hz').addEventListener('change', () => store.set('poll', $('poll-hz').value));
    document.querySelectorAll('#seg-signal .seg-btn').forEach(b => b.addEventListener('click', () => setSignal(b.dataset.sig)));
    $('opt-align').addEventListener('change', e => {
      store.set('align', e.target.checked ? '1' : '0');
      if (active && active.kind === 'csv' && active.file) loadFile(active.file);
      else if (active && active.pl) active.pl.setAlign(e.target.checked);
    });
    ['pod-a', 'pod-b', 'pod-c'].forEach(id => $(id).addEventListener('change', podsChanged));
    if ('serial' in navigator) navigator.serial.addEventListener('disconnect', () => {
      if (active && active.kind === 'serial') { setSource('down', 'serial · unplugged'); srcStatus('The serial device was unplugged.'); }
    });
    else $('btn-serial').title = 'Web Serial needs Chrome or Edge, opened from localhost or https';
  }

  buildKpis();
  wire();
  state.sim = window.MolesSim.makeSim();
  wireSim(state.sim);
  wireSources();

  if (store.get('baud')) $('baud').value = store.get('baud');
  if (store.get('fmt')) $('serial-fmt').value = store.get('fmt');
  if (store.get('poll')) $('poll-hz').value = store.get('poll');
  if (store.get('align') === '0') $('opt-align').checked = false;
  if (store.get('ws')) $('ws-url').value = store.get('ws');
  if (store.get('pods')) store.get('pods').split('|').forEach((v, i) => { const el = $(['pod-a', 'pod-b', 'pod-c'][i]); if (el) el.value = v; });
  setSignal(store.get('signal') || 'phase');

  ensureLayout({});
  const q = new URLSearchParams(location.search), wsParam = q.get('ws');
  if (wsParam) { $('ws-url').value = wsParam; connectWs(wsParam); }
  else if (q.get('sim') === 'false' || store.get('sim') === '0') goReal();
  else useSim();
})();
