import Sparkline from "./Sparkline.jsx";
import { fmt, ago } from "./format.js";
import { BAND } from "./useMolesData.js";

export default function LinkDetail({ linkId, current, history }) {
  return (
    <section className="card" aria-labelledby="detail-t">
      <div className="card-h">
        <div>
          <h3 id="detail-t">{linkId ? linkId.replace("->", " → ") : "Link detail"}</h3>
          <p>{linkId ? "latest window from the backend" : "select a link on the mesh"}</p>
        </div>
      </div>
      <div className="card-b">
        {!linkId || !current ? (
          <div className="empty">no windows yet</div>
        ) : (
          <>
            <div className="big">
              {fmt(current.bpm ?? (current.freq_hz !== null ? current.freq_hz * 60 : null), 1)}
              <span className="u">/min</span>
            </div>
            <div className="row">
              <span className="k">frequency</span>
              <span className="v">{fmt(current.freq_hz, 3)} Hz</span>
            </div>
            <div className="row">
              <span className="k">SNR</span>
              <span className="v">{fmt(current.snr_db, 1)} dB</span>
            </div>
            <div className="row">
              <span className="k">valid samples</span>
              <span className="v">{fmt(current.pct_valid, 0)} %</span>
            </div>
            <div className="row">
              <span className="k">victim tap</span>
              <span className="v">
                {current.victim_tap === null || current.victim_tap === undefined ? "—" : "fp+" + current.victim_tap}
              </span>
            </div>
            <Sparkline history={history || []} band={BAND} />
            <div className="foot">
              updated {ago(current.timestamp)} · raw pipeline flag: detected={String(current.detected)}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
