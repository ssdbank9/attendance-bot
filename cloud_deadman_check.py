"""Conditional dead man's switch, run from a GitHub-hosted runner.

Replaces the old fire-and-forget alert. That one was published to ntfy.sh the
night before with a Delay header, so ntfy's servers delivered it at 09:05 no
matter what had happened since - including on days the bot had already marked
attendance perfectly. It could not do otherwise: it was queued eleven hours
before the fact it claimed to report.

This runs after the fact instead, on a GitHub-hosted runner so it still fires
when the desktop is off - which is the only scenario the alert exists for. It
decides from the state the desktop syncs into this repo:

    timein_status.json      did today's Time-In actually land
    holidays.json           is today a public holiday
    blackout.json           is today leave, a sick day, a working weekend
    notification_prefs.json is the deadman_switch alert wanted at all

If the desktop is off, none of that is fresh: timein_status.json still shows
yesterday, so the alert fires - correctly. If the desktop ran and synced, the
status shows today's success and this stays quiet.

Honouring notification_prefs.json also fixes a real bug: the dashboard has
always shown a deadman_switch toggle, but the old code called notify() directly
and never consulted it, so switching it off did nothing.

Stdlib only - no pip install on the runner.

    python cloud_deadman_check.py [--dry-run]

Environment:
    NTFY_TOPIC     required to actually send (GitHub Actions secret)
    NTFY_SERVER    optional, defaults to https://ntfy.sh
    DASHBOARD_URL  optional; adds the action buttons when set
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from pk_time import now as pk_now

BASE_DIR = Path(__file__).parent
STATUS_FILE = BASE_DIR / "timein_status.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"
NOTIF_PREFS_FILE = BASE_DIR / "notification_prefs.json"


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_holiday(day):
    for h in load_json(HOLIDAYS_FILE).get("holidays", []):
        if h.get("date") == day and not h.get("disabled", False):
            return h.get("label", "Public Holiday")
    return None


def is_blacked_out(day):
    data = load_json(BLACKOUT_FILE)
    for d in data.get("dates", []):
        if d.get("date") == day:
            return d.get("reason", "Blackout")
    for r in data.get("ranges", []):
        if r.get("start", "") <= day <= r.get("end", ""):
            return r.get("reason", "Blackout range")
    return None


def is_working_weekend(day):
    return day in load_json(BLACKOUT_FILE).get("working_weekends", [])


def timein_recorded(day):
    """True when the synced status shows today's Time-In settled.

    'skipped' counts: the desktop reached a deliberate decision not to mark,
    which is not something to wake the user about.
    """
    ti = load_json(STATUS_FILE).get("timein", {})
    if ti.get("date") != day:
        return False, ti.get("date")
    return ti.get("status") in ("success", "skipped"), ti.get("date")


def send_alert(day, day_name):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
    if not topic:
        print("NTFY_TOPIC is not set - cannot send. Set it as a repository secret.")
        return False

    message = (
        f"No Time-In recorded for {day_name} {day}. The desktop may be off or "
        "offline. Mark attendance manually if needed."
    )
    headers = {
        "Title": "Time-In Missing - Desktop Offline?",
        "Priority": "high",
        "Tags": "warning,computer",
    }

    dashboard = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
    if dashboard:
        headers["Click"] = f"{dashboard}/?tab=home"
        headers["Actions"] = (
            f"view, Mark Time-In, {dashboard}/action/timein-now; "
            f"http, Skip Today, {dashboard}/action/skip-date/{day}, method=GET; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )

    req = urllib.request.Request(
        f"{server}/{topic}", data=message.encode("utf-8"), headers=headers
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        print("ALERT SENT:", message)
        return True
    except Exception as e:
        print(f"ALERT FAILED to send: {e}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    today = pk_now()
    day = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    print(f"Deadman check for {day_name} {day} at {today:%H:%M:%S} PKT")

    prefs = load_json(NOTIF_PREFS_FILE).get("preferences", {})
    if not prefs.get("deadman_switch", True):
        print("QUIET: deadman_switch is turned off in notification_prefs.json")
        return 0

    if today.weekday() >= 5 and not is_working_weekend(day):
        print("QUIET: weekend, and not listed as a working weekend")
        return 0

    holiday = is_holiday(day)
    if holiday:
        print(f"QUIET: public holiday ({holiday})")
        return 0

    blackout = is_blacked_out(day)
    if blackout:
        print(f"QUIET: blackout/leave ({blackout})")
        return 0

    recorded, status_date = timein_recorded(day)
    if recorded:
        print(f"QUIET: timein_status.json shows Time-In settled for {day}")
        return 0

    print(f"NO Time-In for {day} (synced status is for {status_date!r}) - alerting")
    if dry_run:
        print("DRY RUN: would have sent the alert; nothing was sent")
        return 0
    send_alert(day, day_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
