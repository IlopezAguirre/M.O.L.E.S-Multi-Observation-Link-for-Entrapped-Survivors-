import { useEffect, useState } from "react";

function hms(d) {
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

export default function Header({ status, running, setRunning }) {
  const [clock, setClock] = useState(hms(new Date()));

  useEffect(() => {
    const id = setInterval(() => setClock(hms(new Date())), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="header">
      <div className="header-left">
        <span className="pill">
          <span className={"dotc " + (status.cls === "ok" ? "ok" : status.cls === "err" ? "hot" : "")} />
          <span>{status.text}</span>
        </span>
      </div>
      <div className="header-right">
        <span className="clock">{clock}</span>
        <button
          type="button"
          className="btn-primary"
          aria-pressed={!running}
          title="Pause or resume polling the backend"
          onClick={() => setRunning((r) => !r)}
        >
          <svg className="ic">
            <use href={running ? "#i-pause" : "#i-play"} />
          </svg>
          <span>{running ? "Pause" : "Resume"}</span>
        </button>
      </div>
    </header>
  );
}
