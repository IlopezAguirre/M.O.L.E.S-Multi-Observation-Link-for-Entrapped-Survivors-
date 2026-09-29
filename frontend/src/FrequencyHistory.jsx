import { BAND, SNR_MIN } from "./useMolesData.js";

// Real per-window history from the backend, plotted over time — NOT a
// per-bin spectrum. The backend only ever stores one peak freq_hz/snr_db per
// 30s window (see backend/db.py), never the full FFT bin array, so a live
// spectrum curve isn't something this dashboard can honestly draw. This is
// the closest real equivalent: frequency and SNR trends for the selected
// link, in the same "peak + threshold" visual language as the pipeline's
// own matplotlib plot (moles_monitor.py's FFTPlot).

const W = 640;
const ML = 46; // margin left
const MR = 54; // margin right (bpm / dB-relative axis)
const MT = 14;
const FREQ_H = 170;
const GAP = 34;
const SNR_H = 100;
const MB = 26;
const H = MT + FREQ_H + GAP + SNR_H + MB;

function hms(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function runs(values) {
  // contiguous index runs where values[i] !== null, for gap-broken polylines
  const out = [];
  let cur = [];
  values.forEach((v, i) => {
    if (v === null || v === undefined || Number.isNaN(v)) {
      if (cur.length) out.push(cur);
      cur = [];
    } else {
      cur.push(i);
    }
  });
  if (cur.length) out.push(cur);
  return out;
}

export default function FrequencyHistory({ linkId, history }) {
  const rows = history || [];
  const n = rows.length;

  if (n < 2) {
    return (
      <div className="freqhist-empty">
        Not enough history yet for {linkId || "this link"} — need at least two 30&nbsp;s windows.
      </div>
    );
  }

  const x = (i) => ML + (i / (n - 1)) * (W - ML - MR);

  const freqs = rows.map((r) => r.freq_hz);
  const finiteFreqs = freqs.filter((f) => f !== null && f !== undefined && !Number.isNaN(f));
  const loF = 0;
  const hiF = Math.max(0.6, BAND[1] + 0.1, ...finiteFreqs);
  const freqTop = MT;
  const yFreq = (v) => freqTop + FREQ_H - ((v - loF) / (hiF - loF)) * FREQ_H;

  const snrs = rows.map((r) => r.snr_db);
  const finiteSnrs = snrs.filter((s) => s !== null && s !== undefined && !Number.isNaN(s));
  const loS = 0;
  const hiS = Math.max(SNR_MIN + 8, ...finiteSnrs, 10);
  const snrTop = MT + FREQ_H + GAP;
  const ySnr = (v) => snrTop + SNR_H - ((v - loS) / (hiS - loS)) * SNR_H;

  const freqRuns = runs(freqs).map((idxs) => idxs.map((i) => `${x(i).toFixed(1)},${yFreq(freqs[i]).toFixed(1)}`).join(" "));
  const snrRuns = runs(snrs).map((idxs) => idxs.map((i) => `${x(i).toFixed(1)},${ySnr(snrs[i]).toFixed(1)}`).join(" "));

  let lastIdx = -1;
  for (let i = n - 1; i >= 0; i--) {
    if (freqs[i] !== null && freqs[i] !== undefined && !Number.isNaN(freqs[i])) {
      lastIdx = i;
      break;
    }
  }
  const last = lastIdx >= 0 ? rows[lastIdx] : null;

  const freqTicks = [0, BAND[0], BAND[1], hiF].filter((v, i, a) => a.indexOf(v) === i);
  const snrTicks = [0, SNR_MIN, hiS].filter((v, i, a) => a.indexOf(v) === i);

  return (
    <div className="freqhist">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={"Frequency and SNR history for " + linkId}>
        {/* breathing band */}
        <rect x={ML} y={yFreq(BAND[1])} width={W - ML - MR} height={yFreq(BAND[0]) - yFreq(BAND[1])} className="band-fill" />
        {/* frequency axes */}
        {freqTicks.map((v) => (
          <g key={"f" + v}>
            <line x1={ML} x2={W - MR} y1={yFreq(v)} y2={yFreq(v)} className="grid-line" />
            <text x={ML - 6} y={yFreq(v)} className="axis-label" textAnchor="end" dominantBaseline="middle">
              {v.toFixed(2)}
            </text>
            <text x={W - MR + 6} y={yFreq(v)} className="axis-label" textAnchor="start" dominantBaseline="middle">
              {(v * 60).toFixed(0)}
            </text>
          </g>
        ))}
        <text x={ML} y={MT - 4} className="axis-title">Hz</text>
        <text x={W - MR} y={MT - 4} className="axis-title" textAnchor="end">/min</text>

        {freqRuns.map((pts, i) => (
          <polyline key={i} points={pts} fill="none" className="freq-line" />
        ))}
        {rows.map((r, i) =>
          r.freq_hz === null || r.freq_hz === undefined || Number.isNaN(r.freq_hz) ? null : (
            <circle key={i} cx={x(i)} cy={yFreq(r.freq_hz)} r={i === lastIdx ? 4 : 2.4} className={r.breathing ? "pt-hot" : "pt-cool"} />
          )
        )}
        {last && (() => {
          const px = x(lastIdx);
          const rightHalf = px > ML + (W - ML - MR) / 2;
          const anchor = rightHalf ? "end" : "start";
          const lx = rightHalf ? px - 10 : px + 10;
          const ly = yFreq(last.freq_hz) - 20;
          return (
            <text x={lx} y={ly} textAnchor={anchor} className="peak-label">
              <tspan x={lx} dy="0">{last.freq_hz.toFixed(3)} Hz · {(last.bpm ?? last.freq_hz * 60).toFixed(1)} /min</tspan>
              {last.snr_db !== null && last.snr_db !== undefined && (
                <tspan x={lx} dy="13">SNR {last.snr_db.toFixed(1)} dB</tspan>
              )}
            </text>
          );
        })()}

        {/* SNR axis */}
        {snrTicks.map((v) => (
          <g key={"s" + v}>
            <line x1={ML} x2={W - MR} y1={ySnr(v)} y2={ySnr(v)} className="grid-line" />
            <text x={ML - 6} y={ySnr(v)} className="axis-label" textAnchor="end" dominantBaseline="middle">
              {v.toFixed(0)}
            </text>
          </g>
        ))}
        <text x={ML} y={snrTop - 4} className="axis-title">SNR (dB)</text>
        <line x1={ML} x2={W - MR} y1={ySnr(SNR_MIN)} y2={ySnr(SNR_MIN)} className="threshold-line" />
        <text x={ML + 4} y={ySnr(SNR_MIN) - 5} className="axis-label" textAnchor="start">breathing SNR min</text>

        {snrRuns.map((pts, i) => (
          <polyline key={i} points={pts} fill="none" className="snr-line" />
        ))}
        {rows.map((r, i) =>
          r.snr_db === null || r.snr_db === undefined || Number.isNaN(r.snr_db) ? null : (
            <circle key={i} cx={x(i)} cy={ySnr(r.snr_db)} r={i === lastIdx ? 3.5 : 2} className={r.breathing ? "pt-hot" : "pt-cool"} />
          )
        )}

        {/* shared time axis */}
        <text x={ML} y={H - 6} className="axis-label" textAnchor="start">{hms(rows[0].timestamp)}</text>
        <text x={(ML + W - MR) / 2} y={H - 6} className="axis-label" textAnchor="middle">{hms(rows[Math.floor((n - 1) / 2)].timestamp)}</text>
        <text x={W - MR} y={H - 6} className="axis-label" textAnchor="end">{hms(rows[n - 1].timestamp)}</text>
      </svg>
    </div>
  );
}
