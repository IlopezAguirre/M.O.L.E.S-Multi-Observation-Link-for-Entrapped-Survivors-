// Thin client for the read-only MOLES backend (backend/app.py).
// API base defaults to http://localhost:8000; override with ?api=http://host:port

export function apiBase() {
  const override = new URLSearchParams(window.location.search).get("api");
  return (override || "http://localhost:8000").replace(/\/$/, "");
}

async function getJSON(base, path) {
  const res = await fetch(base + path);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// GET /links -> string[] of distinct link_ids, e.g. ["A->B", "A->C"]
export function getLinks(base) {
  return getJSON(base, "/links");
}

// GET /current?link_id=... -> latest windows row (with derived `breathing`), or null
export function getCurrent(base, linkId) {
  return getJSON(base, "/current?link_id=" + encodeURIComponent(linkId));
}

// GET /history?link_id=...&limit=... -> windows rows oldest-to-newest
export function getHistory(base, linkId, limit) {
  return getJSON(
    base,
    "/history?link_id=" + encodeURIComponent(linkId) + "&limit=" + limit
  );
}
