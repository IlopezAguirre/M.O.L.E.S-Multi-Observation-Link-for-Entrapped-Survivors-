import { BAND, SNR_MIN, WINDOW_S } from "./useMolesData.js";
import { ago } from "./format.js";

export default function Sidebar({ linkIds, current, onResetLog, apiBase }) {
  return (
    <aside className="sidebar" aria-label="Info">
      <div className="sb-head">
        <div>
          <p className="sb-name">MOLES</p>
          <p className="sb-sub">Live · backend API</p>
        </div>
      </div>

      <nav className="sb-nav">
        <section className="nav-section">
          <button type="button" className="nav-label" aria-expanded="true">
            Links
          </button>
          <div className="nav-body">
            <ul className="rowlist" id="nodes">
              {linkIds.length === 0 && <li className="empty">No links reported yet.</li>}
              {linkIds.map((id) => {
                const cur = current[id];
                const breathing = !!(cur && cur.breathing);
                return (
                  <li key={id}>
                    <span className={"dotc " + (breathing ? "hot" : cur ? "ok" : "")} />
                    <div>
                      <span className="nm">{id.replace("->", " → ")}</span>
                      <span className="sub">{cur ? "updated " + ago(cur.timestamp) : "no windows yet"}</span>
                    </div>
                    <span className={"end badge " + (breathing ? "low" : "fill")}>
                      {breathing ? "breathing" : "quiet"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>

        <section className="nav-section">
          <button type="button" className="nav-label" aria-expanded="true">
            Thresholds
          </button>
          <div className="nav-body">
            <dl className="kvlist">
              <div>
                <dt>Breathing band</dt>
                <dd>{BAND[0].toFixed(2)}–{BAND[1].toFixed(2)} Hz</dd>
              </div>
              <div>
                <dt>SNR minimum</dt>
                <dd>{SNR_MIN.toFixed(1)} dB</dd>
              </div>
              <div>
                <dt>Window</dt>
                <dd>{WINDOW_S} s</dd>
              </div>
              <div>
                <dt>Poll interval</dt>
                <dd>3 s</dd>
              </div>
            </dl>
            <p className="nav-note">
              Computed server-side in backend/db.py — not adjustable from this dashboard.
            </p>
          </div>
        </section>

        <section className="nav-section">
          <button type="button" className="nav-label" aria-expanded="true">
            Controls
          </button>
          <div className="nav-body">
            <button type="button" className="nav-item" onClick={onResetLog}>
              Reset log
            </button>
            <p className="nav-note">Backend: {apiBase}</p>
          </div>
        </section>
      </nav>
    </aside>
  );
}
