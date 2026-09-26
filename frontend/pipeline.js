// Browser port of the laptop pipeline in ARCHITECTURE.md. It turns the host's
// CSV stream into the same snapshot the simulator produces, so the dashboard
// does not care where its data came from.
//
//   CSV line:  t_ms, rx_id, tx_id, seq, status, fp_index, I0, Q0, I1, Q1, ...
//
//   1. split by directed link (tx, rx): six streams
//   2. align on seq: a missing round is a gap, never a shift
//   3. per tap, take |I + jQ| and subtract its mean
//   4. keep the highest-variance taps (paths near the victim)
//   5. FFT over a 30 s window
//   6. peak in 0.1-0.5 Hz against the median of the out-of-band bins
(function () {
  const PODS = ['A', 'B', 'C'];
  const LINKS = [];
  PODS.forEach(tx => PODS.forEach(rx => { if (tx !== rx) LINKS.push(tx + '>' + rx); }));

  const WINDOW_S = 30;
  const N_BINS = 31;               // bins 0..30 -> 0..1 Hz at 1/30 Hz per bin
  const BAND = [0.1, 0.5];
  const NOMINAL_RATE = 10;         // samples per second per link, used until the data says otherwise
  const TOP_TAPS = 3;              // highest-variance taps averaged into the spectrum
  const NMAX = 1200;               // samples kept per link
  const INT = /^-?\d+$/, NUM = /^-?\d+(\.\d+)?$/;

  // ---- parsing ------------------------------------------------------------
  // Returns a row, {error}, or null for blank lines, comments and header lines.
  function parse(line) {
    const s = line.trim();
    if (!s || s[0] === '#') return null;
    const f = s.split(',').map(x => x.trim());
    if (!NUM.test(f[0])) return null;                                  // header or firmware log line
    if (f.length < 8 || (f.length - 6) % 2) return { error: 'field count' };
    for (let i = 0; i < 6; i++) if (!INT.test(f[i])) return { error: 'field ' + i };
    for (let i = 6; i < f.length; i++) if (!NUM.test(f[i])) return { error: 'tap value' };
    const v = f.map(Number), taps = (f.length - 6) / 2, mag = new Float32Array(taps);
    for (let k = 0; k < taps; k++) mag[k] = Math.hypot(v[6 + 2 * k], v[7 + 2 * k]);
    return { t: v[0], rx: v[1], tx: v[2], seq: v[3], status: v[4], fp: v[5], mag };
  }

  // ---- spectrum -----------------------------------------------------------
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

  // Mean-removed, Hann-windowed DFT magnitudes for bins 0..N_BINS-1.
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

  // ---- pipeline -----------------------------------------------------------
  function create(opts) {
    const source = (opts && opts.source) || 'serial';
    const links = new Map();        // "tx>rx" (raw ids) -> per-link buffer
    const ids = new Set();
    const stats = { lines: 0, ok: 0, rejected: 0, skipped: 0, gaps: 0, lastT: 0, taps: 0 };
    let pods = null;

    function ingestRow(r) {
      if (!stats.taps) stats.taps = r.mag.length;
      if (r.mag.length !== stats.taps) { stats.rejected++; return false; }   // firmware and parser disagree on N
      ids.add(r.tx); ids.add(r.rx);
      const key = r.tx + '>' + r.rx;
      let L = links.get(key);
      if (!L) { L = { samples: [], lastRaw: null, lastU: null, wraps: 0, step: null }; links.set(key, L); }

      if (L.lastRaw !== null && r.seq < L.lastRaw - 32768) L.wraps++;        // uint16 seq wrapped
      L.lastRaw = r.seq;
      const seqU = r.seq + 65536 * L.wraps;
      if (L.lastU !== null) {
        const delta = seqU - L.lastU;
        if (delta <= 0) {
          if (delta < -1000) { L.samples.length = 0; L.wraps = 0; L.step = null; L.lastU = null; L.lastRaw = r.seq; }   // device restarted
          else { stats.rejected++; return false; }                                                                       // duplicate or out of order
        } else {
          if (L.step === null || delta < L.step) L.step = delta;               // smallest spacing seen is one round
          const missing = Math.round(delta / L.step) - 1;
          if (missing > 300) L.samples.length = 0;                             // long dropout: start over
          else for (let i = 0; i < missing; i++) { L.samples.push(null); stats.gaps++; }
        }
      }
      L.lastU = L.lastU === null ? r.seq : seqU;
      L.samples.push(r.status === 0 ? { t: r.t, m: r.mag } : null);            // timeout and error rounds are gaps too
      if (r.status !== 0) stats.gaps++;
      while (L.samples.length > NMAX) L.samples.shift();
      stats.ok++;
      stats.lastT = r.t;
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
      const T = valid[0].m.length, mean = new Float64Array(T), vr = new Float64Array(T);
      valid.forEach(v => { for (let t = 0; t < T; t++) mean[t] += v.m[t]; });
      for (let t = 0; t < T; t++) mean[t] /= valid.length;
      valid.forEach(v => { for (let t = 0; t < T; t++) { const d = v.m[t] - mean[t]; vr[t] += d * d; } });
      const order = Array.from({ length: T }, (_, i) => i).sort((a, b) => vr[b] - vr[a]).slice(0, TOP_TAPS);

      // gaps are bridged with the previous value for the FFT only, and a short window is padded at the front
      const series = t => {
        const x = new Float64Array(N), off = N - take.length;
        let prev = valid[0].m[t];
        for (let i = 0; i < N; i++) {
          const s = i >= off ? take[i - off] : null;
          if (s) prev = s.m[t];
          x[i] = prev;
        }
        return x;
      };
      const specs = order.map(t => spectrum(series(t), N));
      const spec = new Array(N_BINS).fill(0).map((_, k) => specs.reduce((a, s) => a + s[k], 0) / specs.length);
      const a = analyse(spec, binHz), best = order[0];
      return {
        peak_hz: a.peak_hz, ratio: a.ratio,
        loss: 1 - valid.length / take.length,
        spectrum: spec,
        series: take.map(s => (s ? s.m[best] - mean[best] : null))
      };
    }

    function snapshot() {
      const raw = Array.from(ids).sort((a, b) => a - b).slice(0, 3);    // lowest three ids become A, B, C
      const rates = [];
      links.forEach(L => { const r = linkRate(L); if (r) rates.push(r); });
      const rate = Math.min(40, Math.max(2, rates.length ? median(rates) : NOMINAL_RATE));
      const N = Math.min(NMAX, Math.round(rate * WINDOW_S)), binHz = rate / N;

      const out = {};
      let fill = 1;
      LINKS.forEach(name => {
        const [tx, rx] = name.split('>'), a = raw[PODS.indexOf(tx)], b = raw[PODS.indexOf(rx)];
        const L = a === undefined || b === undefined ? null : links.get(a + '>' + b);
        const res = L && analyseLink(L, N, binHz);
        if (res) { out[name] = res; fill = Math.min(fill, L.samples.length / N); }
        else { out[name] = placeholder(); fill = 0; }
      });
      const snap = { t: stats.lastT / 1000, source, sample_rate: +rate.toFixed(2), window_seconds: WINDOW_S, bin_hz: binHz, band: BAND.slice(), window_fill: Math.min(1, fill), links: out };
      if (pods) snap.pods = pods;
      return snap;
    }

    return {
      stats, parse, ingestLine, ingestRow, snapshot,
      setPods(p) { pods = p || null; },
      idMap() { return Array.from(ids).sort((a, b) => a - b).slice(0, 3); }
    };
  }

  window.MolesPipeline = { create, parse, LINKS, PODS };
})();
