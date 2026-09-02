"""
Notification module for the TimeIn Bot.
Sends push notifications via ntfy.sh (primary) and Claude CLI (fallback).
"""

import json
import logging
import socket
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from pk_time import now as pk_now

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
NOTIF_PREFS_FILE = BASE_DIR / "notification_prefs.json"
log = logging.getLogger("attendance_bot")


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def load_notif_prefs():
    try:
        with open(NOTIF_PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def get_dashboard_url():
    config = load_config()
    dash = config.get("dashboard", {})
    port = dash.get("port", 5000)
    ip = dash.get("tailscale_ip") or get_local_ip()
    return f"http://{ip}:{port}"


def pref_enabled(key):
    try:
        prefs = load_notif_prefs().get("preferences", {})
        return prefs.get(key, True)
    except Exception:
        return True


def get_admin_email():
    try:
        return load_notif_prefs().get("admin_email", "")
    except Exception:
        return ""


def send_ntfy(message, title="TimeIn Bot", priority="default", tags=None, actions=None, click=None, delay=None):
    config = load_config()
    ntfy_cfg = config.get("notifications", {})
    topic = ntfy_cfg.get("ntfy_topic", "")
    server = ntfy_cfg.get("ntfy_server", "https://ntfy.sh")

    if not topic:
        log.warning("ntfy topic not configured")
        return False

    url = f"{server}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    if actions:
        headers["Actions"] = actions
    if click:
        headers["Click"] = click
    if delay:
        headers["Delay"] = delay

    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=15)
        log.info("ntfy notification sent")
        return True
    except Exception as e:
        log.warning("ntfy notification failed: %s", e)
        return False


def notify(message, title="TimeIn Bot", priority="default", tags=None, actions=None, click=None, delay=None):
    sent = send_ntfy(message, title=title, priority=priority, tags=tags, actions=actions, click=click, delay=delay)
    if not sent:
        log.warning("ntfy failed, trying Claude CLI fallback")
        try:
            import shutil
            import subprocess
            claude_path = shutil.which("claude")
            if claude_path:
                subprocess.run(
                    [claude_path, "-p",
                     f'Use the PushNotification tool to send this message with status "proactive": {message}',
                     "--allowedTools", "PushNotification"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=0x08000000,
                )
                log.info("Claude CLI fallback notification sent")
        except Exception as e:
            log.warning("Claude CLI fallback failed: %s", e)


def notify_status(mode, action_time):
    pref_key = "timein_success" if mode == "timein" else "timeout_success"
    if not pref_enabled(pref_key):
        return
    label = "Time-In" if mode == "timein" else "Time-Out"
    dashboard = get_dashboard_url()
    if mode == "timein":
        actions = (
            f"view, Time Out Now, {dashboard}/action/timeout-now; "
            f"view, Skip Tomorrow, {dashboard}/action/skip-tomorrow; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    else:
        actions = (
            f"view, Skip Tomorrow, {dashboard}/action/skip-tomorrow; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    notify(
        f"{label} marked at {action_time}",
        title=f"{label} Success",
        tags="white_check_mark",
        click=f"{dashboard}/?tab=home",
        actions=actions,
    )


def notify_skip(mode, reason):
    if not pref_enabled("skip_day"):
        return
    label = "Time-In" if mode == "timein" else "Time-Out"
    dashboard = get_dashboard_url()
    notify(
        f"{label} skipped today - {reason}",
        title=f"{label} Skipped",
        tags="fast_forward",
        click=f"{dashboard}/?tab=holidays",
        actions=(
            f"view, Cancel Skip, {dashboard}/action/cancel-skip-tomorrow; "
            f"view, Manage Leave, {dashboard}/?tab=holidays"
        ),
    )

def notify_wfh(mode, reason):
    if not pref_enabled("skip_day"):
        return
    label = "Time-In" if mode == "timein" else "Time-Out"
    dashboard = get_dashboard_url()
    notify(
        f"{label} skipped - {reason}. Log your WFH hours on the dashboard.",
        title="Working From Home",
        tags="house",
        click=f"{dashboard}/?tab=holidays",
        actions=(
            f"view, Log WFH Hours, {dashboard}/?tab=holidays; "
            f"view, Cancel WFH, {dashboard}/action/cancel-wfh-today"
        ),
    )



def notify_window_missed(mode, cutoff):
    """The window closed before the bot could act, so it marked nothing.

    Deliberately loud and actionable: the whole point of refusing to mark a
    Time-In at 13:03 is that the user gets to decide instead, so the push
    carries a one-tap button that marks it anyway."""
    if not pref_enabled("failure"):
        return
    label = "Time-In" if mode == "timein" else "Time-Out"
    dashboard = get_dashboard_url()
    action_url = f"{dashboard}/action/timein-now" if mode == "timein" else f"{dashboard}/action/timeout-now"
    notify(
        f"{label} NOT marked - the window closed at {cutoff:%H:%M} "
        f"(desktop was asleep or off). Tap to mark it now.",
        title=f"{label} Window Missed",
        priority="high",
        tags="hourglass_flowing_sand",
        click=f"{dashboard}/?tab=home",
        actions=(
            f"view, Mark {label} Now, {action_url}; "
            f"view, Dashboard, {dashboard}/?tab=home"
        ),
    )


def notify_failure(mode, attempts):
    if not pref_enabled("failure"):
        return
    label = "Time-In" if mode == "timein" else "Time-Out"
    dashboard = get_dashboard_url()
    action_url = f"{dashboard}/action/timein-now" if mode == "timein" else f"{dashboard}/action/timeout-now"
    action_label = "Retry Time-In" if mode == "timein" else "Retry Time-Out"
    admin_email = get_admin_email()
    if admin_email:
        today = pk_now().strftime("%Y-%m-%d")
        admin_first = admin_email.split("@")[0].split(".")[0].capitalize()
        subject = f"Attendance Correction Request - {today}"
        body = (
            f"Dear {admin_first},\r\n\r\n"
            f"My {label} for {today} was not recorded correctly. I was present in the office. "
            f"Could you please correct my attendance record?\r\n\r\nThank you."
        )
        # ntfy's Actions header uses commas/semicolons as field separators, so any
        # raw comma in the mailto URL (e.g. "Dear Ameen,") breaks parsing and the
        # whole notification gets rejected with HTTP 400 - must be fully encoded.
        mailto = f"mailto:{admin_email}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"
        actions = (
            f"view, {action_label}, {action_url}; "
            f"view, Email Admin, {mailto}; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    else:
        actions = (
            f"view, {action_label}, {action_url}; "
            f"view, Skip Tomorrow, {dashboard}/action/skip-tomorrow; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    notify(
        f"ALERT: {label} FAILED after {attempts} attempts. Manual action needed!",
        title=f"{label} FAILED",
        priority="urgent",
        tags="rotating_light",
        click=f"{dashboard}/?tab=home",
        actions=actions,
    )


def notify_tomorrow(day_name, date_str, snoozed=False):
    if not pref_enabled("tomorrow_plan"):
        return
    dashboard = get_dashboard_url()
    tag = "calendar" if not snoozed else "calendar,alarm_clock"
    title = "Tomorrow's Plan" if not snoozed else "Tomorrow's Plan (Snoozed)"
    notify(
        f"Bot will run tomorrow ({day_name} {date_str}). Tap to skip.",
        title=title,
        tags=tag,
        click=f"{dashboard}/?tab=home",
        actions=(
            f"view, Skip Tomorrow, {dashboard}/action/skip-tomorrow; "
            f"http, Snooze 2h, {dashboard}/action/snooze-tomorrow, method=GET; "
            f"http, Ignore, {dashboard}/action/ignore-tomorrow, method=GET"
        ),
    )

def notify_tomorrow_wfh(day_name, reason):
    if not pref_enabled("tomorrow_plan"):
        return
    dashboard = get_dashboard_url()
    notify(
        f"Working from home tomorrow ({day_name}) - {reason}. Bot will NOT run.",
        title="WFH Tomorrow",
        tags="house",
        click=f"{dashboard}/?tab=holidays",
        actions=f"view, Cancel WFH, {dashboard}/action/cancel-wfh-tomorrow; view, Dashboard, {dashboard}/?tab=home",
    )



def notify_tomorrow_skipped(day_name, reason):
    dashboard = get_dashboard_url()
    notify(
        f"Bot SKIPPED for tomorrow ({day_name}) - {reason}. Tap to un-skip.",
        title="Tomorrow Skipped",
        tags="no_entry_sign",
        click=f"{dashboard}/?tab=home",
        actions=f"view, Un-skip Tomorrow, {dashboard}/action/cancel-skip-tomorrow; view, Manage Leave, {dashboard}/?tab=holidays",
    )


def notify_tomorrow_holiday(day_name, label, holiday_date=None):
    if not pref_enabled("tomorrow_holiday"):
        return
    dashboard = get_dashboard_url()
    actions = f"view, View Holidays, {dashboard}/?tab=holidays"
    if holiday_date:
        # Confirm / cancel, as asked for: the date set in the app is what the
        # bot will act on, and this is the last chance to overrule it before
        # tomorrow. "Not a holiday" flips the disabled flag so the bot runs.
        actions = (
            f"view, Confirm, {dashboard}/action/confirm-holiday/{holiday_date}; "
            f"view, Not a holiday, {dashboard}/action/toggle-holiday/{holiday_date}; "
            f"view, Holidays, {dashboard}/?tab=holidays"
        )
    notify(
        f"Tomorrow ({day_name}) is {label}. Bot will NOT run.",
        title="Holiday Tomorrow",
        tags="tada",
        click=f"{dashboard}/?tab=holidays",
        actions=actions,
    )


def notify_holiday_reminder(holiday_label, holiday_date, days_until=3, moon_dependent=False):
    if not pref_enabled("holiday_reminder"):
        return
    dashboard = get_dashboard_url()
    prefix = "VERIFY (moon-dependent): " if moon_dependent else ""
    notify(
        f"{prefix}{holiday_label} is in {days_until} days ({holiday_date}). Confirm or change date.",
        title="Holiday Reminder",
        priority="high",
        tags="bell,calendar" if not moon_dependent else "bell,crescent_moon",
        click=f"{dashboard}/?tab=holidays",
        actions=(
            f"view, Confirm, {dashboard}/action/confirm-holiday/{holiday_date}; "
            f"view, +1 Day, {dashboard}/action/shift-holiday/{holiday_date}/1; "
            f"view, -1 Day, {dashboard}/action/shift-holiday/{holiday_date}/-1"
        ),
    )
