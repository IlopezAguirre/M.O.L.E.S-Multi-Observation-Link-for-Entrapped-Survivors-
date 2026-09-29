function hms(d) {
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

export default function DetectionLog({ log }) {
  return (
    <section className="card log-card" aria-labelledby="log-t">
      <div className="card-h">
        <div>
          <h3 id="log-t">Detection log</h3>
          <p>Newest first</p>
        </div>
      </div>
      <div className="card-b flush">
        <ol className="log">
          {log.length === 0 ? (
            <li className="empty">No detections yet.</li>
          ) : (
            log.map((x, i) => (
              <li key={i}>
                <div className="top">
                  <span className={"id" + (x.agree ? " both" : "")}>{x.link.replace("->", "")}</span>
                  <span className="time">{hms(x.at)}</span>
                  <span className="hz">{x.freqHz !== null ? x.freqHz.toFixed(2) : "—"} Hz</span>
                </div>
                <div className="sub">
                  {x.bpm !== null ? x.bpm.toFixed(1) : "—"} bpm · SNR {x.snrDb !== null ? x.snrDb.toFixed(1) : "—"} dB ·{" "}
                  {x.agree ? "reverse direction agrees" : "single direction"}
                </div>
              </li>
            ))
          )}
        </ol>
        <div className="log-foot">
          {log.length === 0 ? "Waiting for a hit" : "Showing " + log.length + " · newest first"}
        </div>
      </div>
    </section>
  );
}
