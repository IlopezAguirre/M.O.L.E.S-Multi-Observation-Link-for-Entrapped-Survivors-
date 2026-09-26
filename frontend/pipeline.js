// turns rows from frames.js or CSV into the snapshots the dashboard draws
(function () {
  const PODS = ['A', 'B', 'C'];
  const ALL_LINKS = [];
  PODS.forEach(tx => PODS.forEach(rx => { if (tx !== rx) ALL_LINKS.push(tx + '>' + rx); }));
  const ONEWAY_LINKS = ['A>B', 'A>C', 'B>C'];

  const WINDOW_S = 30;
  const N_BINS = 31;
  const BAND = [0.1, 0.5];
  const NOMINAL_RATE = 10;
  const CANDIDATES = 12;
  const R_MIN = 0.2;
  const TOP_TAPS = 3;
  const NMAX = 1200;
  const MAX_SHIFT = 2;
  const INT = /^-?\d+$/, NUM = /^-?\d+(\.\d+)?$/;

  function parse(line) {
    const s = line.trim();
    if (!s || s[0] === '#') return null;
    const f = s.split(',').map(x => x.trim());
    if (!NUM.test(f[0])) return null;
    if (f.length < 8 || (f.length - 6) % 2) return { error: 'field count' };
    for (let i = 0; i < 6; i++) if (!INT.test(f[i])) return { error: 'field ' + i };
    for (let i = 6; i < f.length; i++) if (!NUM.test(f[i])) return { error: 'tap value' };
    const v = f.map(Number), taps = (f.length - 6) / 2;
    const re = new Float32Array(taps), im = new Float32Array(taps), mag = new Float32Array(taps);
    for (let k = 0; k < taps; k++) { re[k] = v[6 + 2 * k]; im[k] = v[7 + 2 * k]; mag[k] = Math.hypot(re[k], im[k]); }
    return { kind: 'csv', t: v[0], rx: v[1], tx: v[2], seq: v[3], status: v[4], fp: v[5], range: null, re, im, mag };
  }

  const cache = {};
  function tables(N) {
    if (cache[N]) return cache[N];
    const cos = new Float64Array(N), sin = new Float64Array(N), hann = new Float64Array(N);
    for (let i = 0; i < N; i++) {
      cos[i] = Math.cos(2 * Math.PI * i / N);
      sin[i] = Math.sin(2 * Math.PI * i / N);
      hann[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1));
    }
    return (cache[N] = { cos, sin, hann });
  }

  function spectrum(x, N) {
    const { cos, sin, hann } = tables(N);
    let mean = 0;
    for (let i = 0; i < N; i++) mean += x[i];
    mean /= N;
    const y = new Float64Array(N);
    for (let i = 0; i < N; i++) y[i] = (x[i] - mean) * hann[i];
    const out = new Array(N_BINS);
    for (let k = 0; k < N_BINS; k++) {
      let re = 0, im = 0;
      for (let n = 0; n < N; n++) {
        const idx = (k * n) % N;
        re += y[n] * cos[idx];
        im -= y[n] * sin[idx];
      }
      out[k] = Math.hypot(re, im) / (N / 2);
    }
    return out;
  }

  function median(a) {
    const s = a.slice().sort((p, q) => p - q), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  function analyse(spec, binHz) {
    const lo = Math.ceil(BAND[0] / binHz - 1e-9), hi = Math.floor(BAND[1] / binHz + 1e-9);
    let pk = lo;
    for (let k = lo; k <= hi; k++) if (spec[k] > spec[pk]) pk = k;
    const rest = spec.slice(hi + 1);
    const baseline = (rest.length ? median(rest) : 0) || 1e-9;
    return { peak_hz: pk * binHz, ratio: spec[pk] / baseline };
  }

  const placeholder = () => ({ peak_hz: 0, ratio: 0, loss: 0, spectrum: new Array(N_BINS).fill(0), series: [] });

  function shifted(a, s) {
    if (!s) return a;
    const out = new Float32Array(a.length);
    for (let k = 0; k < a.length; k++) { const j = k + s; if (j >= 0 && j < a.length) out[k] = a[j]; }
    return out;
  }
  function bestShift(mag, ref) {
    const T = ref.length;
    if (T <= 2 * MAX_SHIFT + 4) return 0;
    let best = 0, bestErr = Infinity;
    for (const s of [0, -1, 1, -2, 2]) {
      let err = 0;
      for (let k = MAX_SHIFT; k < T - MAX_SHIFT; k++) err += Math.abs(ref[k] - mag[k + s]);
      if (err < bestErr - 1e-9) { bestErr = err; best = s; }
    }
    return best;
  }

  function phaseVals(take, t, ref) {
    const raw = [];
    let prev = null, u = 0, cs = 0, sn = 0, n = 0;
    take.forEach(s => {
      if (!s) { raw.push(null); return; }
      const zr = s.re[t] * s.re[ref] + s.im[t] * s.im[ref], zi = s.im[t] * s.re[ref] - s.re[t] * s.im[ref];
      const p = Math.atan2(zi, zr), m = Math.hypot(zr, zi) || 1;
      cs += zr / m; sn += zi / m; n++;
      if (prev === null) u = p;
      else { let d = p - prev; d -= 2 * Math.PI * Math.round(d / (2 * Math.PI)); u += d; }
      prev = p;
      raw.push(u);
    });
    let k = 0, sx = 0, sy = 0, sxx = 0, sxy = 0;
    raw.forEach((v, i) => { if (v === null) return; k++; sx += i; sy += v; sxx += i * i; sxy += i * v; });
    const den = k * sxx - sx * sx, b = den ? (k * sxy - sx * sy) / den : 0, a0 = k ? (sy - b * sx) / k : 0;
    return { vals: raw.map((v, i) => (v === null ? null : v - (a0 + b * i))), R: n ? Math.hypot(cs, sn) / n : 0 };
  }
  function variance(vals) {
    let n = 0, s = 0, q = 0;
    vals.forEach(v => { if (v !== null) { n++; s += v; q += v * v; } });
    return n ? q / n - (s / n) * (s / n) : 0;
  }
  function magVals(take, t) {
    let n = 0, sum = 0;
    take.forEach(s => { if (s) { n++; sum += s.m[t]; } });
    const mean = n ? sum / n : 0;
    return take.map(s => (s ? s.m[t] - mean : null));
  }
  function filled(vals, N) {
    const x = new Float64Array(N), off = N - vals.length;
    let prev = vals.find(v => v !== null);
    if (prev === undefined) prev = 0;
    for (let i = 0; i < N; i++) { const v = i >= off ? vals[i - off] : null; if (v !== null) prev = v; x[i] = prev; }
    return x;
  }

  function create(opts) {
    const o = opts || {};
    const source = o.source || 'serial';
    let pollHz = o.pollHz || NOMINAL_RATE;
    let signal = o.signal === 'magnitude' ? 'magnitude' : 'phase';
    let align = o.align !== false;
    let topology = null;
    let hostStatus = '';
    const links = new Map();
    const ids = new Set();
    const stats = { lines: 0, ok: 0, rejected: 0, skipped: 0, gaps: 0, aligned: 0, lastT: 0, taps: 0 };
    let pods = null;

    function ingestRow(r) {
      if (!stats.taps) stats.taps = r.mag.length;
      if (r.mag.length !== stats.taps) { stats.rejected++; return false; }
      if (!topology) topology = r.kind === 'frame' ? 'oneway3' : 'directed6';
      ids.add(r.tx); ids.add(r.rx);
      const key = r.tx + '>' + r.rx;
      let L = links.get(key);
      if (!L) { L = { samples: [], lastRaw: null, lastU: null, wraps: 0, step: null, ref: null, nref: 0, shifts: 0, kind: r.kind }; links.set(key, L); }

      if (L.lastRaw !== null && r.seq < L.lastRaw - 32768) L.wraps++;
      L.lastRaw = r.seq;
      const seqU = r.seq + 65536 * L.wraps;
      if (L.lastU !== null) {
        const delta = seqU - L.lastU;
        if (delta <= 0) {
          if (delta < -1000) { L.samples.length = 0; L.wraps = 0; L.step = null; L.lastU = null; L.ref = null; L.nref = 0; L.lastRaw = r.seq; }
          else { stats.rejected++; return false; }
        } else {
          if (L.step === null || delta < L.step) L.step = delta;
          const missing = Math.round(delta / L.step) - 1;
          if (missing > 300) L.samples.length = 0;
          else for (let i = 0; i < missing; i++) { L.samples.push(null); stats.gaps++; }
        }
      }
      L.lastU = L.lastU === null ? r.seq : seqU;
      const t = r.t != null ? r.t : (L.lastU * 1000) / pollHz;

      if (r.status !== 0) { L.samples.push(null); stats.gaps++; }
      else {
        let { mag, re, im } = r;
        if (align && L.ref) {
          const s = bestShift(mag, L.ref);
          if (s) { mag = shifted(mag, s); re = shifted(re, s); im = shifted(im, s); L.shifts++; stats.aligned++; }
        }
        L.nref++;
        const a = Math.max(0.02, 1 / L.nref);
        if (!L.ref) L.ref = Float32Array.from(mag);
        else for (let k = 0; k < mag.length; k++) L.ref[k] += (mag[k] - L.ref[k]) * a;
        L.samples.push({ t, m: mag, re, im, range: r.range });
      }
      while (L.samples.length > NMAX) L.samples.shift();
      stats.ok++;
      stats.lastT = Math.max(stats.lastT, t);
      return true;
    }

    function ingestLine(line) {
      stats.lines++;
      const r = parse(line);
      if (r === null) { stats.skipped++; return false; }
      if (r.error) { stats.rejected++; return false; }
      return ingestRow(r);
    }

    function linkRate(L) {
      const s = L.samples;
      let i0 = -1, i1 = -1;
      for (let i = 0; i < s.length; i++) if (s[i]) { if (i0 < 0) i0 = i; i1 = i; }
      if (i0 < 0 || i1 - i0 < 10) return null;
      const dt = (s[i1].t - s[i0].t) / 1000;
      return dt > 0 ? (i1 - i0) / dt : null;
    }

    function analyseLink(L, N, binHz) {
      const take = L.samples.slice(-N), valid = take.filter(Boolean);
      if (valid.length < 10) return null;
      const T = valid[0].m.length;
      const meanMag = new Float64Array(T);
      valid.forEach(v => { for (let t = 0; t < T; t++) meanMag[t] += v.m[t]; });
      for (let t = 0; t < T; t++) meanMag[t] /= valid.length;

      const frame = L.kind === 'frame';
      const lo = frame ? Math.min(8, T - 1) : 0, hi = frame ? Math.min(11, T - 1) : T - 1;
      let ref = lo;
      for (let t = lo; t <= hi; t++) if (meanMag[t] > meanMag[ref]) ref = t;

      const maxMag = Math.max(...meanMag);
      let cands = [];
      for (let t = 0; t < T; t++) {
        if (signal === 'phase') {
          if (t === ref || meanMag[t] < 0.02 * maxMag) continue;
          const { vals, R } = phaseVals(take, t, ref);
          if (R >= R_MIN) cands.push({ t, vals, v: variance(vals) });
        } else {
          const vals = magVals(take, t);
          cands.push({ t, vals, v: variance(vals) });
        }
      }
      cands = cands.sort((a, b) => b.v - a.v).slice(0, CANDIDATES);
      if (!cands.length) {
        return { peak_hz: 0, ratio: 0, loss: 1 - valid.length / take.length, spectrum: new Array(N_BINS).fill(0), series: [],
                 range: take.map(s => (s && s.range != null ? s.range : null)), tap: -1, ref_tap: ref, shifts: L.shifts };
      }
      cands.forEach(c => {
        c.spec = spectrum(filled(c.vals, N), N);
        c.score = 0;
        for (let k = 1; k < N_BINS; k++) c.score += c.spec[k] * c.spec[k];
      });
      cands.sort((a, b) => b.score - a.score);
      const top = cands.slice(0, TOP_TAPS);
      const spec = new Array(N_BINS).fill(0).map((_, k) => top.reduce((a, c) => a + c.spec[k], 0) / top.length);
      const a = analyse(spec, binHz), best = top[0];
      return {
        peak_hz: a.peak_hz, ratio: a.ratio,
        loss: 1 - valid.length / take.length,
        spectrum: spec,
        series: best.vals,
        range: take.map(s => (s && s.range != null ? s.range : null)),
        tap: best.t, ref_tap: ref, shifts: L.shifts
      };
    }

    function snapshot() {
      const oneway = topology === 'oneway3';
      const raw = oneway ? [0, 1, 2] : Array.from(ids).sort((a, b) => a - b).slice(0, 3);
      const rates = [];
      links.forEach(L => { const r = linkRate(L); if (r) rates.push(r); });
      const rate = Math.min(40, Math.max(2, rates.length ? median(rates) : NOMINAL_RATE));
      const N = Math.min(NMAX, Math.round(rate * WINDOW_S)), binHz = rate / N;

      const out = {};
      let fill = 1;
      (oneway ? ONEWAY_LINKS : ALL_LINKS).forEach(name => {
        const [tx, rx] = name.split('>'), a = raw[PODS.indexOf(tx)], b = raw[PODS.indexOf(rx)];
        const L = a === undefined || b === undefined ? null : links.get(a + '>' + b);
        const res = L && analyseLink(L, N, binHz);
        if (res) { out[name] = res; fill = Math.min(fill, L.samples.length / N); }
        else { out[name] = placeholder(); fill = 0; }
      });
      const snap = {
        t: stats.lastT / 1000, source, signal, topology: topology || 'directed6',
        sample_rate: +rate.toFixed(2), window_seconds: WINDOW_S, bin_hz: binHz, band: BAND.slice(),
        window_fill: Math.min(1, fill), links: out
      };
      if (pods) snap.pods = pods;
      return snap;
    }

    return {
      stats, parse, ingestLine, ingestRow, snapshot,
      setPods(p) { pods = p || null; },
      setSignal(s) { signal = s === 'magnitude' ? 'magnitude' : 'phase'; },
      setAlign(on) { align = !!on; },
      setPollHz(h) { if (h > 0) pollHz = h; },
      setHostStatus(t) { hostStatus = t || ''; },
      hostStatus: () => hostStatus,
      topology: () => topology,
      idMap() { return Array.from(ids).sort((a, b) => a - b).slice(0, 3); }
    };
  }

  window.MolesPipeline = { create, parse, LINKS: ALL_LINKS, ONEWAY_LINKS, PODS };
})();
