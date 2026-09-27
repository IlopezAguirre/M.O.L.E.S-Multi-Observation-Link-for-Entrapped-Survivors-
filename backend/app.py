"""Read-only FastAPI for the MOLES dashboard.

The pipeline writes verdicts into moles.db; this API only reads them and serves
JSON to the frontend, which polls /current + /history. Every row carries its
link_id, and /links reports which links exist so a 3rd node needs no code change.

Run (from this folder):
    uvicorn app:app --reload --port 8000
"""

from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

import db

app = FastAPI(title="MOLES backend", version="0.1")

# Local frontend dev: allow any origin (hackathon, no auth).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Make sure the file/table exist even if the pipeline hasn't run yet, so the API
# returns empty results instead of crashing.
db.init_db()


def _row_to_dict(row):
    """sqlite Row -> plain dict, plus the derived `breathing` verdict."""
    d = dict(row)
    d["breathing"] = db.is_breathing(d.get("freq_hz"), d.get("snr_db"))
    return d


@app.get("/current")
def current(link_id: Optional[str] = Query(default=None)):
    """Most recent window for a link (or overall if link_id omitted). null if empty."""
    conn = db._connect()
    try:
        if link_id:
            row = conn.execute(
                "SELECT * FROM windows WHERE link_id=? ORDER BY timestamp DESC LIMIT 1",
                (link_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM windows ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


@app.get("/history")
def history(
    link_id: Optional[str] = Query(default=None),
    limit: int = Query(default=120, ge=1, le=5000),
):
    """Last N windows, oldest-to-newest so a chart plots left to right. [] if empty."""
    conn = db._connect()
    try:
        if link_id:
            rows = conn.execute(
                "SELECT * FROM windows WHERE link_id=? ORDER BY timestamp DESC LIMIT ?",
                (link_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM windows ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    rows = list(reversed(rows))  # DESC fetch -> reverse to oldest-first
    return [_row_to_dict(r) for r in rows]


@app.get("/links")
def links():
    """Distinct link_ids present, so the frontend discovers how many links exist."""
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT link_id FROM windows ORDER BY link_id"
        ).fetchall()
    finally:
        conn.close()
    return [r["link_id"] for r in rows]
