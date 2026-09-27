import { SNR_MIN } from "./useMolesData.js";

const ICONS = {
  hot: "i-activity",
  hits: "i-zap",
  peak: "i-gauge",
  valid: "i-radio",
};

function ago(arr, n) {
  return arr[Math.max(0, arr.length - 1 - n)] ?? null;
}

function Delta({ dir, text, good }) {
  if (dir === 0 || text === null) return <div className="kpi-delta" />;
  return (
    <div className={"kpi-delta" + (good === null ? "" : good ? " good" : " bad")}>
      <svg className="ic">
        <use href={"#" + (dir > 0 ? "i-up" : "i-down")} />
      </svg>
      <span>{text}</span>
    </div>
  );
}

export default function Kpis({ linkIds, current, hitsTotal, series }) {
  const total = linkIds.length;
  const hot = linkIds.filter((l) => current[l] && current[l].breathing).length;

  const dHot = hot - (ago(series.hot, 10) ?? hot);
  const dHits = hitsTotal - (ago(series.hits, 20) ?? hitsTotal);

  const snrs = linkIds.map((l) => current[l] && current[l].snr_db).filter((v) => v !== null && v !== undefined);
  const peak = snrs.length ? Math.max(...snrs) : null;
  const overThreshold = peak !== null ? peak >= SNR_MIN : null;

  const valids = linkIds.map((l) => current[l] && current[l].pct_valid).filter((v) => v !== null && v !== undefined);
  const validAvg = valids.length ? valids.reduce((a, b) => a + b, 0) / valids.length : null;
  const dValid = validAvg !== null ? validAvg - (ago(series.valid, 10) ?? validAvg) : 0;

  const cards = [
    {
      id: "hot",
      n: 1,
      label: "Links breathing",
      value: total ? hot + "/" + total : "—",
      delta: <Delta dir={Math.sign(dHot)} text={dHot === 0 ? "No change in 30 s" : (dHot > 0 ? "+" : "") + dHot + " vs 30 s ago"} good={dHot === 0 ? null : dHot > 0} />,
    },
    {
      id: "hits",
      n: 2,
      label: "Total hits (session)",
      value: String(hitsTotal),
      delta: <Delta dir={dHits > 0 ? 1 : 0} text={dHits > 0 ? "+" + dHits + " in 60 s" : "None in 60 s"} good={dHits > 0 ? true : null} />,
    },
    {
      id: "peak",
      n: 3,
      label: "Peak SNR",
      value: peak === null ? "—" : peak.toFixed(1) + " dB",
      delta: peak === null ? <Delta dir={0} /> : <Delta dir={overThreshold ? 1 : -1} text={(overThreshold ? "above" : "below") + " " + SNR_MIN + " dB threshold"} good={overThreshold} />,
    },
    {
      id: "valid",
      n: 4,
      label: "Avg valid samples",
      value: validAvg === null ? "—" : validAvg.toFixed(1) + "%",
      delta: <Delta dir={Math.abs(dValid) < 0.05 ? 0 : Math.sign(dValid)} text={Math.abs(dValid) < 0.05 ? null : (dValid >= 0 ? "+" : "") + dValid.toFixed(1) + " pp vs 30 s ago"} good={Math.abs(dValid) < 0.05 ? null : dValid > 0} />,
    },
  ];

  return (
    <div className="kpis" id="kpis">
      {cards.map((k) => (
        <div className="card" key={k.id}>
          <div className="kpi-top">
            <div>
              <p className="kpi-label">{k.label}</p>
              <p className="kpi-value">{k.value}</p>
              {k.delta}
            </div>
            <div
              className="kpi-tile"
              style={{
                background: `color-mix(in oklch, var(--chart-${k.n}) 10%, transparent)`,
                color: `var(--chart-${k.n})`,
              }}
            >
              <svg className="ic ic-20">
                <use href={"#" + ICONS[k.id]} />
              </svg>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
