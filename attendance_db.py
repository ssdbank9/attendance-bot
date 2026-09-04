"""
SQLite-backed event store for the attendance bot.

Single source of truth for Time-In/Time-Out events. timein_status.json and
timein_history.json are regenerated from this database on every write, so
dashboard.py / cloud_sync.py / notify.py keep reading those exact files in
their existing shape - only timein_bot.py needs to know the DB exists.
"""

import json
import sqlite3
import os
import uuid
from pathlib import Path
from datetime import datetime
from pk_time import now as pk_now

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "attendance.db"
STATUS_FILE = BASE_DIR / "timein_status.json"
HISTORY_FILE = BASE_DIR / "timein_history.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('timein', 'timeout')),
    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'skipped')),
    message TEXT,
    action_time TEXT,
    action_origin TEXT NOT NULL DEFAULT 'bot'
        CHECK(action_origin IN ('bot', 'preexisting', 'unknown', 'wfh')),
    observed_time TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date_mode ON events(date, mode);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
"""


def get_connection():
    conn = sqlite3.connect(str(DB_FILE), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "action_origin" not in columns:
            conn.execute(
                "ALTER TABLE events ADD COLUMN action_origin TEXT NOT NULL "
                "DEFAULT 'bot' CHECK(action_origin IN ('bot', 'preexisting', 'unknown', 'wfh'))"
            )
        if "observed_time" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN observed_time TEXT")
        conn.commit()
    finally:
        conn.close()


def record_event(date_str, mode, status, message, action_time=None,
                 action_origin="bot", observed_time=None):
    """Insert one event row. This is the ONLY write path - nothing else
    should ever write to events directly."""
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO events (date, mode, status, message, action_time, "
            "action_origin, observed_time, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, mode, status, message, action_time, action_origin,
             observed_time,
             pk_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    _export_all()


def get_latest(mode, date_str=None):
    """Latest event for a mode, optionally restricted to a specific date.
    Returns a dict or None."""
    init_db()
    conn = get_connection()
    try:
        if date_str:
            row = conn.execute(
                "SELECT * FROM events WHERE mode=? AND date=? ORDER BY id DESC LIMIT 1",
                (mode, date_str),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM events WHERE mode=? ORDER BY date DESC, id DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_unmatched_successful_timein(before_date):
    """Latest successful Time-In before ``before_date`` with no successful
    Time-Out recorded for the same attendance date. Non-success rows do not
    hide an older dangling session."""
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT ti.*
            FROM events AS ti
            WHERE ti.mode='timein' AND ti.status='success' AND ti.date < ?
              AND NOT EXISTS (
                SELECT 1 FROM events AS tout
                WHERE tout.mode='timeout' AND tout.status='success'
                  AND tout.date=ti.date
              )
            ORDER BY ti.date DESC, ti.id DESC
            LIMIT 1
            """,
            (before_date,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_recent_action_times(mode, before_date, limit=14):
    """Latest successful action_time per distinct date for `mode`, strictly
    before `before_date`, most recent first. Used to detect a marked time
    that's suspiciously identical to a prior day's - a sign randomization
    was bypassed (e.g. another automation firing at a fixed clock time)."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT date, action_time FROM (
                SELECT date, action_time,
                       ROW_NUMBER() OVER (PARTITION BY date ORDER BY id DESC) as rn
                FROM events
                WHERE mode=? AND status='success' AND action_time IS NOT NULL
                  AND action_origin='bot' AND date < ?
            ) WHERE rn = 1
            ORDER BY date DESC LIMIT ?
            """,
            (mode, before_date, limit),
        ).fetchall()
        return [(r["date"], r["action_time"]) for r in rows]
    finally:
        conn.close()


def get_history_range(start_date, end_date):
    """Successful timein/timeout action_times per date within [start, end],
    in the same shape timein_history.json used: {date: {timein: hh:mm:ss,
    timeout: hh:mm:ss}}."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, mode, action_time FROM events "
            "WHERE status='success' AND action_time IS NOT NULL "
            "AND action_origin='bot' AND date >= ? AND date <= ? ORDER BY id ASC",
            (start_date, end_date),
        ).fetchall()
        records = {}
        for r in rows:
            records.setdefault(r["date"], {})[r["mode"]] = r["action_time"]
        return records
    finally:
        conn.close()


def _atomic_write_json(path, data):
    # Unique-per-call temp name: on Windows, concurrent writers sharing one
    # fixed temp filename can collide with PermissionError (mandatory file
    # locking), unlike POSIX where that pattern is usually fine.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def export_status_json():
    """Regenerate timein_status.json from the latest timein/timeout events."""
    latest_timein = get_latest("timein")
    latest_timeout = get_latest("timeout")

    out = {}
    for mode, row in (("timein", latest_timein), ("timeout", latest_timeout)):
        if not row:
            continue
        entry = {
            "date": row["date"],
            "status": row["status"],
            "message": row["message"],
            "timestamp": row["recorded_at"],
            "action_origin": row.get("action_origin", "unknown"),
        }
        if row["action_time"]:
            entry["action_time"] = row["action_time"]
        if row.get("observed_time"):
            entry["observed_time"] = row["observed_time"]
        out[mode] = entry

    _atomic_write_json(STATUS_FILE, out)


def export_history_json():
    """Regenerate timein_history.json from all successful events ever
    recorded, including pre-existing portal entries."""
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, mode, action_time, observed_time, action_origin "
            "FROM events WHERE status='success' ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    records = {}
    for r in rows:
        time_val = r["action_time"] or r["observed_time"]
        if time_val:
            records.setdefault(r["date"], {})[r["mode"]] = time_val
    _atomic_write_json(HISTORY_FILE, {"records": records})


def _export_all():
    export_status_json()
    export_history_json()
