"""SQLite storage for MOLES breathing-detection window verdicts.

One row per 30-second window per link. The pipeline (moles_monitor.py) writes
here directly by calling insert_window(); the API only reads. No ORM.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "moles.db")

# --- Derived breathing verdict -------------------------------------------------
# The frontend's "breathing?" indicator is DERIVED from freq + SNR, NOT from the
# pipeline's raw `detected` column. We found `detected` currently latches onto the
# slow ~0.1 Hz body/setup DRIFT (it is stable across windows) and reads 0 on the
# real ~0.3 Hz breathing (which arrives in short bursts). is_breathing() ignores
# drift by requiring the peak to sit in the breathing band with a strong SNR.
BREATH_FREQ_MIN = 0.15   # Hz — below this is drift, not respiration
BREATH_FREQ_MAX = 0.50   # Hz — ~9..30 breaths/min
BREATH_SNR_MIN = 18.0    # dB — peak must clear the noise floor this much

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL    NOT NULL,   -- unix time when the window closed
    link_id       TEXT    NOT NULL,   -- "A->B", "A->C", ... (never hardcoded)
    freq_hz       REAL,               -- detected frequency
    bpm           REAL,               -- freq_hz * 60, for humans
    snr_db        REAL,               -- peak strength over noise
    detected      INTEGER,            -- pipeline's raw verdict (1/0) -- see note above
    victim_tap    INTEGER,            -- CIR tap tracked (nullable)
    pct_valid     REAL,               -- % of window samples that were good
    methods_agree INTEGER             -- 1 if the two FFT methods matched (nullable)
);
CREATE INDEX IF NOT EXISTS idx_windows_link_time ON windows(link_id, timestamp);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets the read-only API query while the pipeline is writing, without
    # "database is locked" errors.
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """Create the DB file and table + index if they don't exist yet."""
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_window(timestamp, link_id, freq_hz=None, bpm=None, snr_db=None,
                  detected=None, victim_tap=None, pct_valid=None,
                  methods_agree=None):
    """Insert one window verdict. Call with keyword fields, e.g.

        insert_window(timestamp=time.time(), link_id="A->B",
                      freq_hz=0.33, snr_db=24.2, detected=0, victim_tap=15,
                      pct_valid=99.0, methods_agree=1)

    link_id is required on every row. bpm is filled from freq_hz if omitted.
    """
    if link_id is None:
        raise ValueError("link_id is required on every row")
    if bpm is None and freq_hz is not None:
        bpm = freq_hz * 60.0
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO windows
                 (timestamp, link_id, freq_hz, bpm, snr_db, detected,
                  victim_tap, pct_valid, methods_agree)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, link_id, freq_hz, bpm, snr_db, detected,
             victim_tap, pct_valid, methods_agree),
        )
        conn.commit()
    finally:
        conn.close()


def is_breathing(freq_hz, snr_db):
    """The honest per-window breathing verdict for the frontend.

    True when the peak is in the breathing band AND strong enough — this catches
    the real ~0.3 Hz breathing that the raw `detected` column misses.
    """
    if freq_hz is None or snr_db is None:
        return False
    return (BREATH_FREQ_MIN <= freq_hz <= BREATH_FREQ_MAX) and (snr_db >= BREATH_SNR_MIN)
