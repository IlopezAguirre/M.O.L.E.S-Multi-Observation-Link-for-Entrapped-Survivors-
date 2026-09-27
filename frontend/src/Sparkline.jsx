// Frequency-over-history sparkline, with the breathing band shaded.
// `band` is [lo, hi] in Hz, matching backend/db.py's BREATH_FREQ_MIN/MAX.
const W = 260;
const H = 54;
const PAD = 4;

export default function Sparkline({ history, band }) {
  const freqs = history.map((r) => r.freq_hz).filter((f) => f !== null && !Number.isNaN(f));
  if (freqs.length < 2) {
    return <div className="foot">not enough history yet</div>;
  }

  const lo = 0;
  const hi = Math.max(0.6, ...freqs);
  const x = (i) => PAD + (i / (history.length - 1)) * (W - 2 * PAD);
  const y = (f) => H - PAD - ((f - lo) / (hi - lo)) * (H - 2 * PAD);
  const yb0 = y(band[0]);
  const yb1 = y(band[1]);

  let points = "";
  history.forEach((r, i) => {
    if (r.freq_hz === null || Number.isNaN(r.freq_hz)) return;
    points += (points ? " " : "") + x(i).toFixed(1) + "," + y(r.freq_hz).toFixed(1);
  });

  return (
    <>
      <svg className="spark" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        <rect
          x="0"
          y={yb1.toFixed(1)}
          width={W}
          height={(yb0 - yb1).toFixed(1)}
          fill="rgba(74,168,255,.10)"
        />
        <polyline points={points} fill="none" stroke="var(--chart-3)" strokeWidth="1.5" />
        {history.map((r, i) =>
          r.freq_hz === null || Number.isNaN(r.freq_hz) ? null : (
            <circle
              key={i}
              cx={x(i).toFixed(1)}
              cy={y(r.freq_hz).toFixed(1)}
              r="2.2"
              fill={r.breathing ? "var(--success)" : "var(--muted-foreground)"}
            />
          )
        )}
      </svg>
      <div className="foot">
        frequency over last {history.length} windows · shaded = breathing band {band[0]}–{band[1]} Hz
      </div>
    </>
  );
}
