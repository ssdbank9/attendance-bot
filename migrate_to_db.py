"""
One-time migration: import existing timein_status.json + timein_history.json
into attendance.db, then regenerate both JSON files from the DB so they're
verified consistent going forward.

Safe to re-run: it's idempotent because timein_bot.py's writers are switched
over to the DB in the same change that ships this script, and this script
refuses to run if the events table already has rows (use --force to bypass).
"""
import json
import sys
from pathlib import Path

import attendance_db as db

BASE_DIR = Path(__file__).parent
STATUS_FILE = BASE_DIR / "timein_status.json"
HISTORY_FILE = BASE_DIR / "timein_history.json"


def load_json(path):
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    force = "--force" in sys.argv

    db.init_db()
    conn = db.get_connection()
    existing = conn.execute("SELECT COUNT(*) as c FROM events").fetchone()["c"]
    conn.close()
    if existing and not force:
        print(f"attendance.db already has {existing} events - refusing to re-migrate. Use --force to override.")
        sys.exit(1)

    history = load_json(HISTORY_FILE)
    status = load_json(STATUS_FILE)

    imported = 0

    # 1) Import every successful action_time from timein_history.json.
    for date_str, rec in sorted(history.get("records", {}).items()):
        for mode in ("timein", "timeout"):
            action_time = rec.get(mode)
            if action_time:
                db.record_event(date_str, mode, "success", f"Migrated from history: {mode} at {action_time}", action_time)
                imported += 1

    # 2) Import timein_status.json's current entries for each mode, but only
    #    if they're not already covered by an identical entry from history
    #    (same date + action_time) - this is how we recover entries that were
    #    missing from history (like today's) or carry a non-success status
    #    (failed/skipped) that history never records.
    def already_covered(date_str, mode, action_time):
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM events WHERE date=? AND mode=? AND status='success' AND action_time=? LIMIT 1",
                (date_str, mode, action_time),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    for mode in ("timein", "timeout"):
        entry = status.get(mode)
        if not entry:
            continue
        date_str = entry.get("date")
        action_time = entry.get("action_time")
        if entry.get("status") == "success" and action_time and already_covered(date_str, mode, action_time):
            continue
        db.record_event(date_str, mode, entry.get("status", "success"), entry.get("message", ""), action_time)
        imported += 1

    print(f"Imported {imported} events into {db.DB_FILE}")
    print("Regenerated timein_status.json and timein_history.json from the DB.")


if __name__ == "__main__":
    main()
