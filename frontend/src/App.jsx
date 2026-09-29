import { useMolesData } from "./useMolesData.js";
import Icons from "./Icons.jsx";
import Header from "./Header.jsx";
import Kpis from "./Kpis.jsx";
import FrequencyHistory from "./FrequencyHistory.jsx";
import Pairs from "./Pairs.jsx";
import LinkDetail from "./LinkDetail.jsx";
import DetectionLog from "./DetectionLog.jsx";
import Sidebar from "./Sidebar.jsx";

function banner(linkIds, current) {
  if (!linkIds.length) return { cls: "banner idle", text: "Waiting for the backend to report any links…" };
  const hot = linkIds.filter((l) => current[l] && current[l].breathing);
  if (hot.length) {
    return {
      cls: "banner alert",
      text: "Periodic signal · " + hot.map((l) => l.replace("->", "")).join(", ") + " reads breathing. Not confirmed as a person.",
    };
  }
  return { cls: "banner idle", text: "Listening · no periodic signal on any link" };
}

export default function App() {
  const {
    apiBase, status, linkIds, current, history, log, hitsTotal,
    running, setRunning, selected, setSelected, series, resetLog,
  } = useMolesData();

  const b = banner(linkIds, current);

  return (
    <>
      <Icons />
      <div className="shell">
        <div className="content">
          <Header status={status} running={running} setRunning={setRunning} />

          <main className="main">
            <div className="page-head">
              <h1> - Rhythm detection - </h1>
              <p className={b.cls} role="status" aria-live="polite">{b.text}</p>
            </div>

            <Kpis linkIds={linkIds} current={current} hitsTotal={hitsTotal} series={series} />

            <div className="grid12">
              <section className="card span8" aria-labelledby="freq-t">
                <div className="card-h">
                  <div>
                    <h3 id="freq-t">Frequency &amp; SNR history</h3>
                    <p>{linkIds.length} directed link{linkIds.length === 1 ? "" : "s"} from the backend</p>
                  </div>
                  <span className="card-side">{hitsTotal} hit{hitsTotal === 1 ? "" : "s"} this session</span>
                </div>
                <div className="card-b">
                  {linkIds.length > 0 && (
                    <div className="seg" role="group" aria-label="Select link">
                      {linkIds.map((id) => (
                        <button
                          key={id}
                          type="button"
                          className={"seg-btn" + (id === selected ? " active" : "")}
                          onClick={() => setSelected(id)}
                        >
                          {id.replace("->", " → ")}
                        </button>
                      ))}
                    </div>
                  )}
                  <FrequencyHistory linkId={selected} history={history[selected]} />
                  <Pairs linkIds={linkIds} current={current} />
                  <p className="hint">
                    Each point is one real 30&nbsp;s window from the backend's pipeline — <b>not</b> a live spectrum.
                    The backend stores only the peak frequency and SNR per window, never the full FFT bin array, so
                    this plots those peaks over time instead. Green points read breathing (0.15–0.50&nbsp;Hz band,
                    shaded, and SNR above the dashed threshold).
                  </p>
                </div>
              </section>

              <div className="span4 stack">
                <LinkDetail linkId={selected} current={current[selected]} history={history[selected]} />
                <DetectionLog log={log} />
              </div>
            </div>
          </main>
        </div>

        <Sidebar linkIds={linkIds} current={current} onResetLog={resetLog} apiBase={apiBase} />
      </div>
    </>
  );
}
