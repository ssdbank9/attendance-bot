"""
Notification module for the TimeIn Bot.
Sends push notifications via ntfy.sh (primary) and Claude CLI (fallback).
"""

import json
import logging
import socket
import urllib.request
import urllib.error
from pathlib import Path

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
            f"http, Skip Tomorrow, {dashboard}/action/skip-tomorrow, method=GET; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    else:
        actions = (
            f"http, Skip Tomorrow, {dashboard}/action/skip-tomorrow, method=GET; "
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
            f"http, Cancel Skip, {dashboard}/action/cancel-skip-tomorrow, method=GET; "
            f"view, Manage Leave, {dashboard}/?tab=holidays"
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
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        admin_first = admin_email.split("@")[0].split(".")[0].capitalize()
        subject = f"Attendance Correction Request - {today}"
        mailto = f"mailto:{admin_email}?subject={subject}&body=Dear {admin_first},%0D%0A%0D%0AMy {label} for {today} was not recorded correctly. I was present in the office. Could you please correct my attendance record?%0D%0A%0D%0AThank you."
        actions = (
            f"view, {action_label}, {action_url}; "
            f"view, Email Admin, {mailto}; "
            f"view, Dashboard, {dashboard}/?tab=home"
        )
    else:
        actions = (
            f"view, {action_label}, {action_url}; "
            f"http, Skip Tomorrow, {dashboard}/action/skip-tomorrow, method=GET; "
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


def notify_tomorrow(day_name, date_str):
    if not pref_enabled("tomorrow_plan"):
        return
    dashboard = get_dashboard_url()
    notify(
        f"Bot will run tomorrow ({day_name} {date_str}). Tap to skip.",
        title="Tomorrow's Plan",
        tags="calendar",
        click=f"{dashboard}/?tab=home",
        actions=(
            f"http, Skip Tomorrow, {dashboard}/action/skip-tomorrow, method=GET; "
            f"view, Dashboard, {dashboard}/?tab=home"
        ),
    )


def notify_tomorrow_skipped(day_name, reason):
    dashboard = get_dashboard_url()
    notify(
        f"Bot SKIPPED for tomorrow ({day_name}) - {reason}. Tap to un-skip.",
        title="Tomorrow Skipped",
        tags="no_entry_sign",
        click=f"{dashboard}/?tab=home",
        actions=f"http, Un-skip Tomorrow, {dashboard}/action/cancel-skip-tomorrow, method=GET; view, Manage Leave, {dashboard}/?tab=holidays",
    )


def notify_tomorrow_holiday(day_name, label, holiday_date=None):
    if not pref_enabled("tomorrow_holiday"):
        return
    dashboard = get_dashboard_url()
    actions = f"view, View Holidays, {dashboard}/?tab=holidays"
    if holiday_date:
        actions = (
            f"http, Disable, {dashboard}/action/toggle-holiday/{holiday_date}, method=GET; "
            f"http, +1 Day, {dashboard}/action/shift-holiday/{holiday_date}/1, method=GET; "
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
            f"http, Confirm, {dashboard}/action/confirm-holiday/{holiday_date}, method=GET; "
            f"http, +1 Day, {dashboard}/action/shift-holiday/{holiday_date}/1, method=GET; "
            f"http, -1 Day, {dashboard}/action/shift-holiday/{holiday_date}/-1, method=GET"
        ),
    )


def schedule_deadman_switch(tomorrow_str, day_name):
    """Schedule a 9:05 AM alert on ntfy.sh servers for tomorrow.
    If the PC is on and the bot runs, the success notification makes this redundant.
    If the PC is off, this still fires from ntfy.sh cloud — alerting the user."""
    from datetime import datetime, timedelta

    tomorrow = datetime.strptime(tomorrow_str, "%Y-%m-%d")
    deliver_at = tomorrow.replace(hour=9, minute=5)
    now = datetime.now()
    delay_secs = int((deliver_at - now).total_seconds())
    if delay_secs <= 0:
        return

    dashboard = get_dashboard_url()
    notify(
        f"Time-In may not have run today ({day_name} {tomorrow_str}). "
        f"Your PC might be off. Mark attendance manually if needed.",
        title="Time-In Alert - PC Offline?",
        priority="high",
        tags="warning,computer",
        click=f"{dashboard}/?tab=home",
        actions=(
            f"view, Mark Time-In, {dashboard}/action/timein-now; "
            f"http, Skip Today, {dashboard}/action/skip-date/{tomorrow_str}, method=GET; "
            f"view, Dashboard, {dashboard}/?tab=home"
        ),
        delay=f"{delay_secs}s",
    )
    log.info("Dead man's switch scheduled for %s 09:05 (%ds delay)", tomorrow_str, delay_secs)