export function fmt(x, d) {
  return x === null || x === undefined || Number.isNaN(x) ? "—" : Number(x).toFixed(d);
}

export function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? s + " s ago" : Math.round(s / 60) + " min ago";
}
