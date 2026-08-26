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
    bot_config.json         is the bot paused
    notification_prefs.json is the deadman_switch alert wanted at all

Only a legal working day can produce an alert - a normal weekday, no holiday,
no leave, bot not paused. Those quiet cases mirror should_run_today() in
timein_bot.py exactly: if the bot would not have marked attendance today, a
missing Time-In is the expected outcome, not a fault.

If the desktop is off, none of that is fresh: timein_status.json still shows
yesterday, so the alert fires - correctly. If the desktop ran and synced, the
status shows today's success and this stays quiet.

Honouring notification_prefs.json also fixes a real bug: the dashboard has
always shown a deadman_switch toggle, but the old code called notify() directly
and never consulted it, so switching it off did nothing.

Stdlib only - no pip install on the runner.

    python cloud_deadman_check.py [--dry-run]

Exit codes matter here. A real run exits non-zero when an alert was genuinely
due and could not be delivered - that is the case worth a red X and GitHub's
own failed-workflow email, a backup channel independent of ntfy. On a quiet day
(holiday, weekend, paused, or attendance already recorded) an unusable channel
is reported as a warning annotation instead, and the run stays green.

That split is deliberate. Failing every run with an unconfigured channel emails
the user daily, including on holidays where the check correctly said nothing -
the same daily false alarm that made the original dead man's switch worthless.
A failure here should mean "something needed saying and could not be sent".

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
from datetime import datetime, timedelta
from pathlib import Path

from pk_time import now as pk_now, PKT

BASE_DIR = Path(__file__).parent
STATUS_FILE = BASE_DIR / "timein_status.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"
NOTIF_PREFS_FILE = BASE_DIR / "notification_prefs.json"
BOT_CONFIG_FILE = BASE_DIR / "bot_config.json"
PAUSE_MAX_AGE = timedelta(hours=36)
PAUSE_FUTURE_SKEW = timedelta(minutes=5)


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
            f"view, Skip Today, {dashboard}/action/skip-date/{day}; "
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


def confirmed_pause_state(today):
    """Return (pause_is_active, staleness_note).

    A pause suppresses the deadman for as long as it is set - indefinitely. A
    pause is a deliberate decision, so expiring it after a fixed age would nag
    the user daily through any real multi-week pause, which is precisely the
    false-alarm pattern this whole script exists to remove.

    The failure this guards against - a resume whose GitHub sync failed, so
    remote state stays paused and the alert is silenced forever - is now caught
    at its source instead: set_pause.py exits non-zero and the dashboard's
    toggle returns HTTP 502 with a visible toast, so an unsynced resume is
    reported rather than swallowed. Age here is therefore informational: it is
    surfaced as a warning annotation, never as a reason to alert.

    The one hard requirement is a real boolean. A missing, corrupt, or
    non-boolean file must not be able to silence anything, so it reads as
    not-paused.
    """
    data = load_json(BOT_CONFIG_FILE)
    paused = data.get("paused")
    if not isinstance(paused, bool):
        return False, "bot_config.json has no valid boolean paused state"
    if not paused:
        return False, None

    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return True, "pause has no updated_at timestamp, so its age cannot be confirmed"
    try:
        parsed = datetime.fromisoformat(updated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return True, "pause updated_at is not a valid ISO timestamp"
    if parsed.tzinfo is None:
        return True, "pause updated_at has no timezone"

    age = today - parsed.astimezone(PKT).replace(tzinfo=None)
    if age < -PAUSE_FUTURE_SKEW:
        return True, "pause updated_at is unexpectedly in the future"
    if age > PAUSE_MAX_AGE:
        return True, (f"pause has been active and unconfirmed for "
                      f"{age.days}d {age.seconds // 3600}h - if this is not "
                      f"intended, resume the bot and check the sync succeeded")
    return True, None


def verdict(today, day):
    """Decide whether an alert is warranted. Returns (alert_needed, reason).

    The quiet cases mirror should_run_today() in timein_bot.py exactly, in the
    same order: paused, weekend, holiday, blackout. If the bot would not have
    marked attendance today, a missing Time-In is the expected outcome and not
    something to wake anyone about. Only a legal working day - a normal
    weekday, no holiday, no leave, bot not paused - can produce an alert.

    Pause is read from bot_config.json rather than config.json: config.json is
    local-only and gitignored, so it never reaches this runner. Every pause
    path (the dashboard's /action/toggle-pause and /api/toggle-pause, and
    set_pause.py for the workflow) mirrors the flag into bot_config.json and
    syncs it, which is why this can be trusted.

    Returns (alert_needed, reason, note). `note` is advisory only - it is
    surfaced as a warning annotation and never turns a quiet day into an alert.
    """
    pause_active, pause_note = confirmed_pause_state(today)
    if pause_active:
        return False, "the bot is paused (bot_config.json)", pause_note

    if today.weekday() >= 5 and not is_working_weekend(day):
        return False, "weekend, and not listed as a working weekend", pause_note

    holiday = is_holiday(day)
    if holiday:
        return False, f"public holiday ({holiday})", pause_note

    blackout = is_blacked_out(day)
    if blackout:
        return False, f"blackout/leave ({blackout})", pause_note

    recorded, status_date = timein_recorded(day)
    if recorded:
        return False, f"timein_status.json shows Time-In settled for {day}", pause_note

    reason = f"no Time-In for {day} (synced status is for {status_date!r})"
    return True, reason, pause_note


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

    alert_needed, reason, note = verdict(today, day)
    if not alert_needed:
        print(f"QUIET: {reason}")
        summary(f"No alert needed - {reason}.")
    else:
        print(f"ALERT: {reason}")
        summary(f"**Time-In is missing** - {reason}.")

    # Advisory only. A long pause is legitimate and must not alert, but an
    # unexpectedly old one is worth seeing on the run page - that is the shape
    # a resume whose sync silently failed would take.
    if note:
        annotate("warning", f"Pause state: {note}")
        summary(f"Pause state: {note}")

    # The channel is checked on every run, but only a run that actually needed
    # to say something FAILS when it cannot.
    #
    # An earlier version failed every run with an unusable channel, on the
    # reasoning that a deadman which cannot send should reveal itself before
    # the morning it matters. In practice that emailed a workflow failure every
    # weekday - including 2026-08-26, a public holiday where the check had
    # correctly decided to stay quiet - which is precisely the daily false
    # alarm that made the original dead man's switch worthless and trained its
    # reader to ignore it. A red X now means "something needed saying and could
    # not be sent", which is actionable; an unconfigured channel on a quiet day
    # is a warning on the run page instead, visible without generating mail.
    problem = channel_problem()
    if problem:
        if alert_needed:
            # Lead with the attendance fact. The annotation is the surface
            # people actually read, so it must not bury today being unmarked
            # under a configuration complaint.
            note = (f"Time-In is MISSING for {day_name} {day} AND this deadman "
                    f"could not notify you: {problem}.")
            annotate("warning" if dry_run else "error", note)
            summary(f"**Alert channel BROKEN:** {note}")
            if not dry_run:
                return 1
        else:
            note = (f"{problem}, so no alert could be sent if one were due. No "
                    f"alert was needed today ({reason}). Add it under Settings "
                    "> Secrets and variables > Actions.")
            annotate("warning", note)
            summary(f"**Alert channel unusable:** {note}")

    if not alert_needed:
        return 0

    if dry_run:
        print("DRY RUN: would have sent the alert; nothing was sent")
        return 0

    # Failing here too, so a send that silently drops still surfaces as a red X.
    return 0 if send_alert(day, day_name) else 1


if __name__ == "__main__":
    sys.exit(main())
