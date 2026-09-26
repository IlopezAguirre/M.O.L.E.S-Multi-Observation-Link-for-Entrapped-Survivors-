// canvas charts, no dependencies
(function () {
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const withAlpha = (c, a) => c.replace(/\)\s*$/, ' / ' + a + ')');

  function setup(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    if (!canvas.dataset.h) canvas.dataset.h = canvas.getAttribute('height');
    const h = parseInt(canvas.dataset.h, 10);
    canvas.style.height = h + 'px';
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function drawBars(canvas, spec, meta, overlays, target) {
    const { ctx, w, h } = setup(canvas);
    const pad = { l: 4, r: 4, t: 8, b: 22 };
    const n = spec.length;
    const all = [spec].concat(overlays.map(o => o.spec));
    const top = Math.max(1e-6, ...all.map(s => Math.max(...s))) * 1.1;
    const bw = (w - pad.l - pad.r) / n;
    const Y = v => pad.t + (1 - v / top) * (h - pad.t - pad.b);
    const lo = Math.ceil(meta.band[0] / meta.bin_hz - 1e-9);
    const hi = Math.floor(meta.band[1] / meta.bin_hz + 1e-9);
    let pk = lo;
    for (let k = lo; k <= hi; k++) if (spec[k] > spec[pk]) pk = k;

    ctx.fillStyle = css('--muted');
    ctx.fillRect(pad.l + lo * bw, pad.t, (hi - lo + 1) * bw, h - pad.t - pad.b);

    ctx.strokeStyle = css('--border');
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const gy = Math.round(pad.t + (i / 4) * (h - pad.t - pad.b)) + 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(w - pad.r, gy); ctx.stroke();
    }

    for (let k = 0; k < n; k++) {
      ctx.fillStyle = k === pk ? css('--chart-1') : (k >= lo && k <= hi ? css('--chart-3') : 'oklch(88% 0 0)');
      const y = Y(spec[k]), x = pad.l + k * bw + 1, bh = h - pad.b - y, bwid = Math.max(1, bw - 2);
      ctx.fillRect(x, y, bwid, bh);
    }

    const tk = Math.round(target / meta.bin_hz);
    const tx = pad.l + (tk + 0.5) * bw;
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = css('--destructive');
    ctx.beginPath(); ctx.moveTo(tx, pad.t); ctx.lineTo(tx, h - pad.b); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = css('--destructive');
    ctx.font = '500 11px ' + css('--sans');
    ctx.textAlign = 'left';
    ctx.fillText(target.toFixed(1) + ' Hz', tx + 4, pad.t + 9);

    overlays.forEach(o => {
      ctx.strokeStyle = o.color; ctx.lineWidth = 2;
      ctx.beginPath();
      o.spec.forEach((v, k) => { const x = pad.l + (k + 0.5) * bw; k ? ctx.lineTo(x, Y(v)) : ctx.moveTo(x, Y(v)); });
      ctx.stroke();
    });

    ctx.fillStyle = css('--muted-foreground');
    ctx.font = '12px ' + css('--sans');
    ctx.textAlign = 'center';
    for (let k = 0; k < n; k += 5) ctx.fillText(String(k), pad.l + (k + 0.5) * bw, h - 6);
  }

  function drawSpark(canvas, data, colorVar) {
    const { ctx, w, h } = setup(canvas);
    if (data.length < 2) return;
    const c = css(colorVar);
    const lo = Math.min(...data), hi = Math.max(...data), span = hi - lo || 1;
    const top = 8, bottom = 2;
    const X = i => (i / (data.length - 1)) * w;
    const Y = v => hi === lo ? h * 0.55 : top + (1 - (v - lo) / span) * (h - top - bottom);
    const trace = () => {
      ctx.moveTo(X(0), Y(data[0]));
      for (let i = 1; i < data.length; i++) {
        const mx = (X(i - 1) + X(i)) / 2;
        ctx.bezierCurveTo(mx, Y(data[i - 1]), mx, Y(data[i]), X(i), Y(data[i]));
      }
    };
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, withAlpha(c, 0.2));
    g.addColorStop(1, withAlpha(c, 0));
    ctx.beginPath(); trace(); ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    ctx.fillStyle = g; ctx.fill();
    ctx.beginPath(); trace();
    ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
  }

  function drawTrace(canvas, data, colorVar, unit) {
    const { ctx, w, h } = setup(canvas);
    const vals = data.filter(v => v !== null);
    if (vals.length < 2) return;
    const pad = { l: 44, r: 8, t: 10, b: 8 };
    let lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const m = (hi - lo) * 0.12; lo -= m; hi += m;
    const X = i => pad.l + (i / (data.length - 1 || 1)) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);

    ctx.strokeStyle = css('--border'); ctx.lineWidth = 1;
    [hi, lo].forEach(v => { const y = Math.round(Y(v)) + 0.5; ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke(); });
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(pad.l, Math.round(Y(0)) + 0.5); ctx.lineTo(w - pad.r, Math.round(Y(0)) + 0.5); ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = css('--muted-foreground'); ctx.font = '11px ' + css('--sans'); ctx.textAlign = 'right';
    ctx.fillText(hi.toFixed(1) + ' ' + unit, pad.l - 6, Y(hi) + 4);
    ctx.fillText('0', pad.l - 6, Y(0) + 4);
    ctx.fillText(lo.toFixed(1), pad.l - 6, Y(lo) + 4);

    ctx.strokeStyle = css(colorVar); ctx.lineWidth = 2; ctx.lineJoin = 'round';
    ctx.beginPath();
    let pen = false;
    data.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      if (pen) ctx.lineTo(X(i), Y(v)); else { ctx.moveTo(X(i), Y(v)); pen = true; }
    });
    ctx.stroke();
  }

  const clear = canvas => { setup(canvas); };

  window.MolesCharts = { drawBars, drawSpark, drawTrace, clear, color: css };
})();
