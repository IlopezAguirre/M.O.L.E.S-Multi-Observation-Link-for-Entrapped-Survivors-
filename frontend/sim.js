// synthetic snapshots for running without hardware
(function () {
  const PODS = ['A', 'B', 'C'];
  const LINKS = [];
  PODS.forEach(tx => PODS.forEach(rx => { if (tx !== rx) LINKS.push(tx + '>' + rx); }));

  const POD_POS = { A: [0, 0], B: [6, 0], C: [3, 5.2] };
  const SIGMA = 1.6;

  function distToSeg(p, a, b) {
    const abx = b[0] - a[0], aby = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / (abx * abx + aby * aby)));
    return Math.hypot(p[0] - (a[0] + t * abx), p[1] - (a[1] + t * aby));
  }

  const FS = 10;
  const WINDOW_S = 30;
  const N = FS * WINDOW_S;
  const N_BINS = 31;
  const BAND = [0.1, 0.5];
  const SNAP_MS = 500;

  function gauss() {
    const u = 1 - Math.random(), v = Math.random();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }

  const COS = new Float64Array(N), SIN = new Float64Array(N), HANN = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    COS[i] = Math.cos(2 * Math.PI * i / N);
    SIN[i] = Math.sin(2 * Math.PI * i / N);
    HANN[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1));
  }

  function spectrum(x) {
    const mean = x.reduce((a, b) => a + b, 0) / x.length;
    const y = x.map((v, i) => (v - mean) * HANN[i]);
    const out = new Array(N_BINS);
    for (let k = 0; k < N_BINS; k++) {
      let re = 0, im = 0;
      for (let n = 0; n < N; n++) {
        const idx = (k * n) % N;
        re += y[n] * COS[idx];
        im -= y[n] * SIN[idx];
      }
      out[k] = Math.hypot(re, im) / (N / 2);
    }
    return out;
  }

  function median(a) {
    const s = a.slice().sort((p, q) => p - q);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  function analyse(spec) {
    const binHz = FS / N;
    const lo = Math.ceil(BAND[0] / binHz - 1e-9);
    const hi = Math.floor(BAND[1] / binHz + 1e-9);
    let pk = lo;
    for (let k = lo; k <= hi; k++) if (spec[k] > spec[pk]) pk = k;
    const baseline = median(spec.slice(hi + 1)) || 1e-9;
    return { peak_hz: pk * binHz, ratio: spec[pk] / baseline };
  }

  function makeSim() {
    const p = { breathing: true, rate: 0.2, source: [4.6, 0.9], ambient: false };
    const links = {};
    LINKS.forEach((name, i) => {
      const [tx, rx] = name.split('>');
      links[name] = {
        tx: POD_POS[tx], rx: POD_POS[rx],
        noise: 0.5,
        phase: Math.random() * Math.PI * 2,
        loss: 0.01 + Math.random() * 0.03,
        drift: Math.random() * Math.PI * 2,
        fan: Math.random() * Math.PI * 2,
        buf: [],
        last: 0,
        i
      };
    });

    const ampFor = L => 0.03 + 0.9 * Math.exp(-Math.pow(distToSeg(p.source, L.tx, L.rx) / SIGMA, 2));

    let t = 0, timer = null;

    function sample(L) {
      const drift = 0.25 * Math.sin(2 * Math.PI * 0.012 * t + L.drift);
      const breath = p.breathing ? ampFor(L) * Math.sin(2 * Math.PI * p.rate * t + L.phase) : 0;
      const fan = p.ambient ? 0.35 * Math.sin(2 * Math.PI * 0.2 * t + L.fan) : 0;
      return 3 + drift + breath + fan + L.noise * gauss();
    }

    function step() {
      LINKS.forEach(name => {
        const L = links[name];
        const dropped = Math.random() < L.loss;
        const v = sample(L);
        if (!dropped) L.last = v;
        L.buf.push(dropped ? null : v);
        if (L.buf.length > N) L.buf.shift();
      });
      t += 1 / FS;
    }

    function snapshot() {
      const out = {};
      let fill = 1;
      LINKS.forEach(name => {
        const L = links[name];
        fill = Math.min(fill, L.buf.length / N);
        let prev = L.buf.find(v => v !== null) || 0;
        const filled = L.buf.map(v => { if (v === null) return prev; prev = v; return v; });
        while (filled.length < N) filled.unshift(filled[0] || 0);
        const spec = spectrum(filled);
        const a = analyse(spec);
        const mean = filled.reduce((s, v) => s + v, 0) / N;
        const lost = L.buf.filter(v => v === null).length / Math.max(1, L.buf.length);
        out[name] = {
          peak_hz: a.peak_hz,
          ratio: a.ratio,
          loss: lost,
          spectrum: spec,
          series: L.buf.map(v => (v === null ? null : v - mean))
        };
      });
      return {
        t,
        source: 'simulator',
        pods: POD_POS,
        sim_truth: p.breathing ? p.source.slice() : null,
        sample_rate: FS,
        window_seconds: WINDOW_S,
        bin_hz: FS / N,
        band: BAND.slice(),
        window_fill: fill,
        links: out
      };
    }

    return {
      params: p,
      reset() {
        LINKS.forEach(name => { links[name].buf = []; });
        for (let i = 0; i < N; i++) step();
      },
      start(cb) {
        for (let i = 0; i < N; i++) step();
        cb(snapshot());
        timer = setInterval(() => {
          for (let i = 0; i < FS * SNAP_MS / 1000; i++) step();
          cb(snapshot());
        }, SNAP_MS);
      },
      stop() { clearInterval(timer); }
    };
  }

  window.MolesSim = { makeSim, LINKS, PODS };
})();
