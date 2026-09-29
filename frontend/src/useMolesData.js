import { useEffect, useRef, useState } from "react";
import { apiBase, getLinks, getCurrent, getHistory } from "./api.js";

// Fixed backend thresholds (backend/db.py). Not client-adjustable: the
// breathing verdict is computed server-side, this just displays what the
// server used.
export const BAND = [0.15, 0.5];
export const SNR_MIN = 18.0;
export const WINDOW_S = 30;

const POLL_MS = 3000;
const HISTORY_LEN = 30;
const MAX_LOG = 40;
const KPI_SERIES_LEN = 60; // ~3 min of ticks at POLL_MS

function emptySeries() {
  return { hot: [], hits: [], peak: [], valid: [] };
}

function push(arr, v) {
  const next = arr.length >= KPI_SERIES_LEN ? arr.slice(1) : arr.slice();
  next.push(v);
  return next;
}

export function reverseLink(linkId) {
  const [tx, rx] = linkId.split("->");
  return rx + "->" + tx;
}

export function useMolesData() {
  const base = useRef(apiBase());
  const [status, setStatus] = useState({ text: "connecting…", cls: "" });
  const [linkIds, setLinkIds] = useState([]);
  const [current, setCurrent] = useState({}); // linkId -> row | null
  const [history, setHistory] = useState({}); // linkId -> rows[]
  const [log, setLog] = useState([]);
  const [hitsTotal, setHitsTotal] = useState(0);
  const [running, setRunning] = useState(true);
  const [selected, setSelected] = useState(null);
  const [series, setSeries] = useState(emptySeries());

  const prevBreathing = useRef({}); // linkId -> bool, for rising-edge detection
  const hitsRef = useRef(0);
  const runningRef = useRef(running);
  runningRef.current = running;

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      if (!runningRef.current) return;
      try {
        const links = await getLinks(base.current);
        if (cancelled) return;
        setStatus({ text: "connected · " + base.current, cls: "ok" });
        setLinkIds(links);

        if (!links.length) {
          setCurrent({});
          setHistory({});
          return;
        }

        const rows = await Promise.all(
          links.map(async (linkId) => {
            const [cur, hist] = await Promise.all([
              getCurrent(base.current, linkId),
              getHistory(base.current, linkId, HISTORY_LEN),
            ]);
            return [linkId, cur, hist];
          })
        );
        if (cancelled) return;

        const nextCurrent = {};
        const nextHistory = {};
        const risen = [];
        rows.forEach(([linkId, cur, hist]) => {
          nextCurrent[linkId] = cur;
          nextHistory[linkId] = hist;
          const was = !!prevBreathing.current[linkId];
          const now = !!(cur && cur.breathing);
          if (!was && now) risen.push({ linkId, cur });
          prevBreathing.current[linkId] = now;
        });

        setCurrent(nextCurrent);
        setHistory(nextHistory);
        setSelected((sel) => (sel && links.includes(sel) ? sel : links[0]));

        if (risen.length) {
          hitsRef.current += risen.length;
          setHitsTotal(hitsRef.current);
          setLog((prev) => {
            const entries = risen.map(({ linkId, cur }) => ({
              at: new Date(),
              link: linkId,
              freqHz: cur.freq_hz,
              bpm: cur.bpm,
              snrDb: cur.snr_db,
              agree: !!(nextCurrent[reverseLink(linkId)] && nextCurrent[reverseLink(linkId)].breathing),
            }));
            return [...entries, ...prev].slice(0, MAX_LOG);
          });
        }

        const hot = links.filter((l) => nextCurrent[l] && nextCurrent[l].breathing).length;
        const snrs = links
          .map((l) => nextCurrent[l] && nextCurrent[l].snr_db)
          .filter((v) => v !== null && v !== undefined);
        const valids = links
          .map((l) => nextCurrent[l] && nextCurrent[l].pct_valid)
          .filter((v) => v !== null && v !== undefined);
        const peak = snrs.length ? Math.max(...snrs) : null;
        const validAvg = valids.length ? valids.reduce((a, b) => a + b, 0) / valids.length : null;
        setSeries((s) => ({
          hot: push(s.hot, hot),
          hits: push(s.hits, hitsRef.current),
          peak: push(s.peak, peak),
          valid: push(s.valid, validAvg),
        }));
      } catch (e) {
        if (!cancelled) {
          setStatus({ text: "waiting for backend at " + base.current + " (" + e.message + ")", cls: "err" });
        }
      }
    }

    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  function resetLog() {
    setLog([]);
    setHitsTotal(0);
    hitsRef.current = 0;
    setSeries(emptySeries());
  }

  return {
    apiBase: base.current,
    status,
    linkIds,
    current,
    history,
    log,
    hitsTotal,
    running,
    setRunning,
    selected,
    setSelected,
    series,
    resetLog,
  };
}
