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

Exit codes matter here. A deadman that cannot send is worse than no deadman:
it reports success every quiet morning and only reveals itself on the one
morning it was meant to speak. So a real run exits non-zero whenever the alert
channel is unusable - even on days no alert was due. That turns a silent
misconfiguration into a red X plus GitHub's own failed-workflow email, a backup
channel independent of ntfy. Dry runs only warn, so manual checks stay green.

What that check does and does not cover: it confirms a topic is set and that
the composed publish URL is a well-formed http(s) address. It deliberately does
NOT contact ntfy, because a daily network probe would go red on any transient
blip and teach the reader to ignore the red - the opposite of the point. So a
wrong-but-well-formed topic, or an ntfy outage, is still only discovered when an
alert is actually attempted.

Environment:
    NTFY_TOPIC     required; a real run FAILS without it
    NTFY_SERVER    optional, defaults to https://ntfy.sh
    DASHBOARD_URL  optional; adds the action buttons when set

Read these with `or`, never with os.environ.get's default: the workflow maps
each one from a secret, and GitHub still defines the variable when the secret
does not exist - as an empty string. The default argument therefore never
fires, and "" would silently compose a publish URL of "/topic".
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from pk_time import now as pk_now

BASE_DIR = Path(__file__).parent
STATUS_FILE = BASE_DIR / "timein_status.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"
NOTIF_PREFS_FILE = BASE_DIR / "notification_prefs.json"


def annotate(level, message):
    """Emit a GitHub Actions annotation, so this shows on the run page and in
    the workflow list rather than only in step logs nobody opens."""
    print(f"::{level}::{message}")


def summary(line):
    """Append to the run's job summary, the first thing visible on the run."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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


def env(name, default=""):
    """Read an env var treating "" as absent.

    GitHub defines a variable for every `env:` entry in the workflow even when
    the secret behind it does not exist, handing us "" rather than nothing. So
    os.environ.get(name, default) returns "" and the default never applies.
    """
    return (os.environ.get(name) or default).strip()


def publish_url():
    return f"{env('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{env('NTFY_TOPIC')}"


def channel_problem():
    """Why the alert channel is unusable, or None if it looks sound.

    Deliberately offline-only - see the module docstring on why this does not
    probe ntfy.
    """
    if not env("NTFY_TOPIC"):
        return "NTFY_TOPIC is not set"
    parts = urllib.parse.urlsplit(publish_url())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return (f"NTFY_SERVER is not a usable URL - the publish target would be "
                f"{publish_url()!r}")
    return None


def send_alert(day, day_name):
    message = (
        f"No Time-In recorded for {day_name} {day}. The desktop may be off or "
        "offline. Mark attendance manually if needed."
    )
    headers = {
        "Title": "Time-In Missing - Desktop Offline?",
        "Priority": "high",
        "Tags": "warning,computer",
    }

    dashboard = env("DASHBOARD_URL").rstrip("/")
    if dashboard:
        headers["Click"] = f"{dashboard}/?tab=home"
        headers["Actions"] = (
            f"view, Mark Time-In, {dashboard}/action/timein-now; "
            f"http, Skip Today, {dashboard}/action/skip-date/{day}, method=GET; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )

    # Request() is built inside the try: a malformed URL raises ValueError
    # there, not at send time, and an uncaught traceback would lose the
    # annotation that says attendance is actually missing.
    try:
        req = urllib.request.Request(
            publish_url(), data=message.encode("utf-8"), headers=headers
        )
        urllib.request.urlopen(req, timeout=15)
        print("ALERT SENT:", message)
        return True
    except Exception as e:
        annotate("error", f"Time-In is MISSING for {day} and the ntfy send "
                          f"failed: {e}")
        return False


def deadman_wanted():
    """False when the user has switched the alert off in the dashboard."""
    return load_json(NOTIF_PREFS_FILE).get("preferences", {}).get("deadman_switch", True)


def verdict(today, day):
    """Decide whether an alert is warranted. Returns (alert_needed, reason)."""
    if today.weekday() >= 5 and not is_working_weekend(day):
        return False, "weekend, and not listed as a working weekend"

    holiday = is_holiday(day)
    if holiday:
        return False, f"public holiday ({holiday})"

    blackout = is_blacked_out(day)
    if blackout:
        return False, f"blackout/leave ({blackout})"

    recorded, status_date = timein_recorded(day)
    if recorded:
        return False, f"timein_status.json shows Time-In settled for {day}"

    return True, f"no Time-In for {day} (synced status is for {status_date!r})"


def main():
    dry_run = "--dry-run" in sys.argv
    today = pk_now()
    day = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    header = f"Deadman check for {day_name} {day} at {today:%H:%M:%S} PKT"
    print(header)
    summary(f"### {header}")

    # Checked before the channel is: failing a run daily for a feature the user
    # deliberately switched off would make the red X meaningless, and the red X
    # is the backup channel everything below depends on.
    if not deadman_wanted():
        print("QUIET: deadman_switch is turned off in notification_prefs.json")
        summary("No alert needed - deadman_switch is turned off.")
        return 0

    alert_needed, reason = verdict(today, day)
    if not alert_needed:
        print(f"QUIET: {reason}")
        summary(f"No alert needed - {reason}.")
    else:
        print(f"ALERT: {reason}")
        summary(f"**Time-In is missing** - {reason}.")

    # A deadman that cannot send is worse than no deadman: it reports success
    # every quiet morning and only reveals itself on the one morning it was
    # supposed to speak. So the channel is checked on every run, not just when
    # an alert is due, and a real run FAILS when it is unusable - the red X and
    # GitHub's own failed-workflow email are then the backup channel, entirely
    # independent of ntfy. Dry runs only warn, so manual checks stay green.
    problem = channel_problem()
    if problem:
        # Lead with the attendance fact when there is one. The annotation is
        # the surface people actually read, so it must not bury today being
        # unmarked under a configuration complaint.
        if alert_needed:
            note = (f"Time-In is MISSING for {day_name} {day} AND this deadman "
                    f"could not notify you: {problem}.")
        else:
            note = (f"{problem}, so this deadman cannot notify anyone. "
                    "Add it under Settings > Secrets and variables > Actions.")
        annotate("warning" if dry_run else "error", note)
        summary(f"**Alert channel BROKEN:** {note}")
        if not dry_run:
            return 1

    if not alert_needed:
        return 0

    if dry_run:
        print("DRY RUN: would have sent the alert; nothing was sent")
        return 0

    # Failing here too, so a send that silently drops still surfaces as a red X.
    return 0 if send_alert(day, day_name) else 1


if __name__ == "__main__":
    sys.exit(main())
