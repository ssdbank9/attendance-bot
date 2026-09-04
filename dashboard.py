"""
TimeIn Bot Web Dashboard.
Access from phone at http://YOUR_PC_IP:5000 on the same WiFi.
Provides status view, quick actions, holiday/blackout management, analytics.
"""

import csv
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import subprocess
import sys
import random
import threading
from datetime import datetime, timedelta
from pk_time import now as pk_now, PKT
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from flask import (Flask, redirect, url_for, jsonify, request, Response,
                   send_from_directory, session)

from console_guard import silence
silence(Path(__file__).parent / "timein_logs" / "dashboard_stdout.log")
# pythonw.exe leaves stdout/stderr as None; see console_guard.py

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

def _system_python():
    """System python.exe for launching the bot (needs selenium/requests)."""
    if sys.prefix != sys.base_prefix:
        base = Path(sys.base_prefix) / "python.exe"
        if base.exists():
            return str(base)
    return sys.executable

STATUS_FILE = BASE_DIR / "timein_status.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"
HISTORY_FILE = BASE_DIR / "timein_history.json"
NOTIF_PREFS_FILE = BASE_DIR / "notification_prefs.json"
AUTH_TOKEN_FILE = BASE_DIR / ".dashboard_auth_token"
_LEAVE_UPDATE_LOCK = threading.Lock()

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def _load_or_create_auth_token():
    """Load the dashboard token without ever emitting it to logs or HTML."""
    env_token = os.environ.get("ATTENDANCE_DASHBOARD_TOKEN", "").strip()
    if env_token:
        if len(env_token) < 16:
            raise RuntimeError("ATTENDANCE_DASHBOARD_TOKEN must contain at least 16 characters")
        return env_token

    if AUTH_TOKEN_FILE.exists():
        token = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            if len(token) < 16:
                raise RuntimeError("Dashboard authentication token is too short")
            return token

    token = secrets.token_urlsafe(32)
    try:
        with open(AUTH_TOKEN_FILE, "x", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(AUTH_TOKEN_FILE, 0o600)
        except OSError:
            pass
        print(f"Dashboard login token created in {AUTH_TOKEN_FILE.name}; read it locally to sign in.")
        return token
    except FileExistsError:
        token = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            if len(token) < 16:
                raise RuntimeError("Dashboard authentication token is too short")
            return token
        raise RuntimeError("Dashboard authentication token file is empty")


_DASHBOARD_AUTH_TOKEN = _load_or_create_auth_token()
app.secret_key = hashlib.sha256(
    ("attendance-dashboard-session:" + _DASHBOARD_AUTH_TOKEN).encode("utf-8")
).digest()


@app.before_request
def require_dashboard_authentication():
    """Protect every dashboard route except the login entry point itself."""
    if request.endpoint in ("login", "action_snooze_tomorrow", "action_ignore_tomorrow"):
        return None
    if session.get("dashboard_authenticated") is True:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": "Authentication required"}), 401
    next_path = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=next_path))


@app.after_request
def add_dashboard_security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    next_path = request.values.get("next", "/")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    def _sign_in():
        session.clear()
        session["dashboard_authenticated"] = True
        session.permanent = True

    # Token in the query string, so the phone's home-screen shortcut can point
    # at /login?token=... and re-authenticate itself on every launch. An
    # installed PWA does not reliably keep its cookie jar between launches (iOS
    # gives the standalone web app its own storage and evicts it freely), which
    # is why the session cookie alone kept dropping and the login prompt kept
    # coming back. The redirect lands on a clean URL so the token is not left in
    # the address bar or in history; Referrer-Policy: no-referrer already stops
    # it leaking outbound, and every response is Cache-Control: no-store.
    if request.method == "GET":
        supplied = request.args.get("token", "")
        if supplied and hmac.compare_digest(supplied, _DASHBOARD_AUTH_TOKEN):
            _sign_in()
            return redirect(next_path)
        if supplied:
            error = "Invalid login token"

    if request.method == "POST":
        supplied = request.form.get("token", "")
        if hmac.compare_digest(supplied, _DASHBOARD_AUTH_TOKEN):
            _sign_in()
            return redirect(next_path)
        error = "Invalid login token"
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    safe_next = html.escape(next_path, quote=True)
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attendance Dashboard Login</title><style>
body{{font-family:system-ui;background:#f1f5f1;margin:0;display:grid;place-items:center;min-height:100vh}}
form{{background:white;padding:1.5rem;border-radius:12px;box-shadow:0 4px 20px #0002;width:min(86vw,360px)}}
input,button{{box-sizing:border-box;width:100%;padding:.75rem;margin-top:.65rem;font:inherit}}
button{{background:#2d5f2e;color:white;border:0;border-radius:7px;font-weight:700}}
.error{{color:#a11}}</style></head><body><form method="post">
<h2>Attendance Dashboard</h2><p>Enter the login token stored on the dashboard computer.</p>
{error_html}<input type="password" name="token" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="{safe_next}"><button type="submit">Sign in</button>
</form></body></html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

STATIC_DIR = BASE_DIR / "static"

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(STATIC_DIR), filename)

@app.route("/manifest.json")
def manifest():
    m = {
        "name": "Attendance Management",
        "short_name": "Attendance",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#2d5f2e",
        "theme_color": "#2d5f2e",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return jsonify(m)

LINKED_HOLIDAY_PREFIXES = ["Eid ul-Fitr", "Eid ul-Adha", "Ashura"]
TARGET_MINUTES = 550  # 9h 10m


def load_json(path):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_config():
    return load_json(CONFIG_FILE)


def load_notif_prefs():
    return load_json(NOTIF_PREFS_FILE)


def _sync_notif_prefs_to_cloud():
    try:
        from cloud_sync import sync_notification_prefs
        sync_notification_prefs()
    except Exception:
        pass


def get_next_workdays(n=5):
    days = []
    # Start at today, not tomorrow. Today belongs on this panel - the one day
    # whose attendance you actually want to see was the one day missing from
    # it. pk_now() is Pakistan local time, so today drops off the list
    # only when the PKT day ends at local midnight.
    d = pk_now()
    today_str = d.strftime("%Y-%m-%d")
    status = load_json(STATUS_FILE)
    holidays = load_json(HOLIDAYS_FILE)
    hol_dates = {h["date"] for h in holidays.get("holidays", []) if not h.get("disabled", False)}
    blackout = load_json(BLACKOUT_FILE)
    bl_entries = {b["date"]: b for b in blackout.get("dates", [])}
    bl_dates = set(bl_entries.keys())
    bl_ranges = blackout.get("ranges", [])
    working_wkends = set(blackout.get("working_weekends", []))
    wfh_entries = {w["date"]: w for w in blackout.get("wfh", [])}
    wfh_ranges = blackout.get("wfh_ranges", [])
    while len(days) < n:
        ds = d.strftime("%Y-%m-%d")
        skip = None
        is_wkend = d.weekday() >= 5
        if is_wkend and ds not in working_wkends:
            skip = "Weekend"
        elif ds in hol_dates:
            skip = "Holiday"
        elif ds in bl_dates:
            entry = bl_entries[ds]
            lt = entry.get("leave_type")
            if lt:
                type_short = {"casual": "CL", "sick": "SL", "earned": "EL", "other": "Leave"}.get(lt, "Leave")
                days_lbl = " (½)" if entry.get("days") == 0.5 else ""
                skip = f"{type_short}{days_lbl}"
            else:
                skip = "Blackout"
        else:
            for r in bl_ranges:
                if r["start"] <= ds <= r["end"]:
                    skip = "Leave"
                    break
        wfh = False
        if not skip:
            if ds in wfh_entries:
                wfh = True
            else:
                for wr in wfh_ranges:
                    if wr.get("start", "") <= ds <= wr.get("end", ""):
                        wfh = True
                        break
        entry = {"date": ds, "day": d.strftime("%a"), "label": d.strftime("%b %d"),
                 "skip": skip, "wfh": wfh, "is_weekend": d.weekday() >= 5,
                 "working_weekend": d.weekday() >= 5 and ds in working_wkends,
                 "is_today": ds == today_str, "attendance": None}
        if entry["is_today"]:
            ti = status.get("timein", {})
            if ti.get("date") == today_str and ti.get("status") == "success":
                entry["attendance"] = "Present"
        days.append(entry)
        d += timedelta(days=1)
    return days


def get_upcoming_holidays(n=10):
    data = load_json(HOLIDAYS_FILE)
    today = pk_now().strftime("%Y-%m-%d")
    future = [h for h in data.get("holidays", []) if h["date"] >= today]
    future.sort(key=lambda x: x["date"])
    return future[:n]


def get_active_blackouts():
    data = load_json(BLACKOUT_FILE)
    today = pk_now().strftime("%Y-%m-%d")
    dates = [d for d in data.get("dates", []) if d["date"] >= today]
    ranges = [r for r in data.get("ranges", []) if r["end"] >= today]
    wfh_dates = [d for d in data.get("wfh", []) if d["date"] >= today]
    wfh_ranges = [r for r in data.get("wfh_ranges", []) if r["end"] >= today]
    return dates, ranges, wfh_dates, wfh_ranges


def _clock_row():
    """Read-only line showing whether the laptop clock is on Pakistan time.

    Deliberately information, not a control. A timezone PICKER would be a
    footgun: the attendance window is fixed by AKU, so the only correct choice
    is always Pakistan and any other choice silently marks you at the wrong
    hour. What the user actually needs is to SEE a drift.

    It still matters after the one-shot was made offset-aware, because Windows
    Task Scheduler fires the 08:45 / 20:00 base triggers on host wall-clock by
    design - so a drifted laptop starts the bot at the wrong Pakistan moment,
    even though the randomized alarm it then sets is correct."""
    local = datetime.now().astimezone()
    local_off = local.utcoffset() or timedelta(0)
    pkt_off = timedelta(hours=5)
    hours = local_off.total_seconds() / 3600
    label = f"UTC{hours:+g}".replace("+0", "+0")
    if local_off == pkt_off:
        return (
            f'<div class="clock-ok">Laptop clock: Pakistan time ({label}) &mdash; correct</div>'
        )
    diff = (local_off - pkt_off).total_seconds() / 3600
    return (
        f'<div class="clock-warn"><b>Laptop clock is {label}, not Pakistan time (UTC+5).</b> '
        f'It is {abs(diff):g}h {"ahead of" if diff > 0 else "behind"} Pakistan, so the daily '
        f'8:45 AM / 8:00 PM start fires at the wrong Pakistan moment. Set Windows '
        f'back to (UTC+05:00) Islamabad, Karachi.</div>'
    )


def _valid_date(value):
    """True only for a real YYYY-MM-DD date.

    Unvalidated dates were written straight into holidays.json / blackout.json,
    and the render helpers strptime() them unconditionally - so a single bad
    value made the WHOLE dashboard return 500 until the file was hand-edited,
    including the Pause button."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _run_bot_now(mode, wait_seconds=90):
    """Run the bot and report what ACTUALLY happened.

    Returns (ok, message, died_silently). ok is True/False, or None while it is
    still running. died_silently is True only when the child could not report
    for itself, so the caller must send the push instead of double-notifying.

    Launches the bot with Popen and polls for completion. If the bot is still
    running after wait_seconds, leaves it alive (unlike subprocess.run which
    kills on TimeoutExpired) and reports "still running".
    """
    label = "Time-In" if mode == "timein" else "Time-Out"
    today = pk_now().strftime("%Y-%m-%d")
    try:
        proc = subprocess.Popen(
            [_system_python(), str(BASE_DIR / "timein_bot.py"), mode, "--now"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return False, f"{label} could not start: {type(exc).__name__}", True

    import time as _time
    deadline = _time.monotonic() + wait_seconds
    while _time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            break
        rec = load_json(STATUS_FILE).get(mode, {})
        if rec.get("date") == today and rec.get("status") == "success":
            when = rec.get("action_time") or rec.get("observed_time") or "?"
            return True, f"{label} marked at {when}", False
        _time.sleep(3)

    if proc.poll() is None:
        return None, f"{label} is still running - refresh in a moment", False

    if proc.returncode != 0:
        out = (proc.stderr.read() if proc.stderr else "") or ""
        tail = out.strip().splitlines()
        detail = tail[-1][:160] if tail else f"exit code {proc.returncode}"
        return False, f"{label} FAILED to run: {detail}", True

    rec = load_json(STATUS_FILE).get(mode, {})
    if rec.get("date") == today and rec.get("status") == "success":
        when = rec.get("action_time") or rec.get("observed_time") or "?"
        return True, f"{label} marked at {when}", False
    if rec.get("date") == today and rec.get("status") in ("failed", "skipped"):
        return False, rec.get("message") or f"{label} did not complete", False
    out = (proc.stdout.read() if proc.stdout else "") or ""
    tail = out.strip().splitlines()
    return False, (tail[-1][:160] if tail else f"{label} did not complete"), False


def run_manage(script, *args):
    cmd = [sys.executable, str(BASE_DIR / script)] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _sync_holidays_to_cloud():
    try:
        from cloud_sync import sync_holidays
        sync_holidays()
    except Exception:
        pass


def _write_leave_balance(config):
    """Write leave_balance.json from config for mobile sync."""
    import json as _j
    lb = config.get("leave_balance", {})
    lb_path = BASE_DIR / "leave_balance.json"
    with open(lb_path, "w", encoding="utf-8") as f:
        _j.dump(lb, f, indent=2)


def _sync_leave_balance_to_cloud():
    try:
        from cloud_sync import sync_leave_balance
        sync_leave_balance()
    except Exception:
        pass


def _sync_status_to_cloud():
    try:
        from cloud_sync import sync_status
        sync_status()
    except Exception:
        pass


def _sync_blackout_to_cloud():
    try:
        from cloud_sync import sync_blackout
        sync_blackout()
    except Exception:
        pass


def _count_working_days(start_str, end_str):
    """Count weekdays in [start, end] excluding holidays."""
    hol_data = load_json(HOLIDAYS_FILE)
    hol_dates = {h["date"] for h in hol_data.get("holidays", []) if not h.get("disabled")}
    bl_data = load_json(BLACKOUT_FILE)
    working_wkends = set(bl_data.get("working_weekends", []))
    count = 0
    cur = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    while cur <= end:
        ds = cur.strftime("%Y-%m-%d")
        is_wkend = cur.weekday() >= 5
        if is_wkend and ds not in working_wkends:
            pass
        elif ds in hol_dates:
            pass
        else:
            count += 1
        cur += timedelta(days=1)
    return count


def should_notify_holiday_change():
    return load_notif_prefs().get("preferences", {}).get("holiday_change", True)


def get_linked_prefix(label):
    for prefix in LINKED_HOLIDAY_PREFIXES:
        if label.startswith(prefix):
            return prefix
    return None

# --- Action routes ---

@app.route("/action/skip-tomorrow")
def action_skip_tomorrow():
    # The result was previously discarded, so a failed save still produced a
    # "Skip Confirmed" push - and the bot would then mark attendance on a day
    # the user believed was blocked off.
    ok, out = run_manage("manage_blackout.py", "skip", "tomorrow", "Phone skip")
    if not ok:
        return redirect(url_for("dashboard",
                                msg=f"Could NOT skip tomorrow: {out or 'the change was not saved'}"))
    _sync_blackout_to_cloud()
    from notify import notify
    notify("Tomorrow skipped from dashboard.", title="Skip Confirmed", tags="fast_forward")
    return redirect(url_for("dashboard", msg="Tomorrow skipped"))

@app.route("/action/cancel-skip-tomorrow")
def action_cancel_skip_tomorrow():
    ok, out = run_manage("manage_blackout.py", "cancel", "tomorrow")
    if not ok:
        return redirect(url_for("dashboard",
                                msg=f"Could NOT un-skip tomorrow: {out or 'the change was not saved'}"))
    _sync_blackout_to_cloud()
    from notify import notify
    notify("Tomorrow un-skipped from dashboard.", title="Skip Cancelled", tags="white_check_mark")
    return redirect(url_for("dashboard", msg="Tomorrow un-skipped"))

@app.route("/action/snooze-tomorrow")
def action_snooze_tomorrow():
    from notify import notify_tomorrow
    tomorrow = pk_now() + timedelta(days=1)
    day_name = tomorrow.strftime("%A")
    date_str = tomorrow.strftime("%Y-%m-%d")
    from notify import send_ntfy, get_dashboard_url
    dashboard = get_dashboard_url()
    send_ntfy(
        "Bot will run tomorrow ({day} {dt}). Tap to skip.".format(day=day_name, dt=date_str),
        title="Tomorrow\'s Plan (Snoozed)",
        tags="calendar,alarm_clock",
        click="{dash}/?tab=home".format(dash=dashboard),
        actions=(
            "view, Skip Tomorrow, {dash}/action/skip-tomorrow; "
            "view, Snooze 2h, {dash}/action/snooze-tomorrow"
        ).format(dash=dashboard),
        delay="2h",
    )
    if request.headers.get("User-Agent", "").startswith("ntfy/"):
        return "Snoozed", 200
    return redirect(url_for("dashboard", msg="Snoozed - reminder in 2 hours"))

@app.route("/action/ignore-tomorrow")
def action_ignore_tomorrow():
    if request.headers.get("User-Agent", "").startswith("ntfy/"):
        return "OK", 200
    return redirect(url_for("dashboard", msg="Notification dismissed"))

@app.route("/action/skip-date/<date>")
def action_skip_date(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    ok, out = run_manage("manage_blackout.py", "skip", date, "Dashboard skip")
    if not ok:
        return redirect(url_for("dashboard", msg=f"Could NOT skip {date}: {out or 'not saved'}"))
    _sync_blackout_to_cloud()
    return redirect(url_for("dashboard", msg=f"{date} skipped"))

@app.route("/action/cancel-skip/<date>")
def action_cancel_skip(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    ok, out = run_manage("manage_blackout.py", "cancel", date)
    if not ok:
        return redirect(url_for("dashboard", msg=f"Could NOT un-skip {date}: {out or 'not saved'}"))
    _sync_blackout_to_cloud()
    return redirect(url_for("dashboard", msg=f"{date} un-skipped"))

@app.route("/action/confirm-holiday/<date>")
def action_confirm_holiday(date):
    data = load_json(HOLIDAYS_FILE)
    for h in data.get("holidays", []):
        if h["date"] == date:
            h["confirmed"] = True
    save_json(HOLIDAYS_FILE, data)
    _sync_holidays_to_cloud()
    if should_notify_holiday_change():
        from notify import notify
        notify(f"Holiday confirmed: {date}", title="Holiday Confirmed", tags="white_check_mark")
    return redirect(url_for("dashboard"))

@app.route("/action/shift-holiday/<date>/<direction>")
def action_shift_holiday(date, direction):
    try:
        offset = int(direction)
    except ValueError:
        return redirect(url_for("dashboard"))
    data = load_json(HOLIDAYS_FILE)
    target = None
    for h in data.get("holidays", []):
        if h["date"] == date:
            target = h
            break
    if not target:
        return redirect(url_for("dashboard"))
    group_prefix = get_linked_prefix(target.get("label", ""))
    target_day = datetime.strptime(date, "%Y-%m-%d")
    shifted = []
    for h in data.get("holidays", []):
        should_shift = (h["date"] == date)
        if group_prefix and h.get("label", "").startswith(group_prefix):
            # Same occurrence only. Matching the label prefix alone swept up
            # EVERY year: on 2026-08-28 a +1 shift of 2027-03-08 from the phone
            # also moved 2026 Eid ul-Fitr from 21/22/23 to 22/23/24 March.
            # Eid and Ashura are multi-day blocks that must move together, but
            # only within one occurrence - members sit a couple of days apart,
            # so a week is a generous bound that can never reach another year.
            try:
                same_occurrence = abs(
                    (datetime.strptime(h["date"], "%Y-%m-%d") - target_day).days
                ) <= 7
            except ValueError:
                same_occurrence = False
            if same_occurrence:
                should_shift = True
        if should_shift:
            old = datetime.strptime(h["date"], "%Y-%m-%d")
            new_date = (old + timedelta(days=offset)).strftime("%Y-%m-%d")
            h["date"] = new_date
            if not h.get("moon_dependent"):
                h["confirmed"] = True
            shifted.append(h.get("label", ""))
    data["holidays"].sort(key=lambda x: x["date"])
    save_json(HOLIDAYS_FILE, data)
    _sync_holidays_to_cloud()
    from notify import notify
    if should_notify_holiday_change() and group_prefix:
        notify(f"All {group_prefix} days shifted by {'+' if offset > 0 else ''}{offset} day(s)", title="Holiday Shifted", tags="calendar")
    elif should_notify_holiday_change():
        notify(f"Holiday shifted: {target.get('label', '')} by {'+' if offset > 0 else ''}{offset} day", title="Holiday Shifted", tags="calendar")
    return redirect(url_for("dashboard"))

@app.route("/action/update-credentials", methods=["POST"])
def action_update_credentials():
    new_uid = request.form.get("user_id", "").strip()
    new_pw = request.form.get("password", "").strip()
    if not new_uid or not new_pw:
        return redirect(url_for("dashboard"))
    config = load_config()
    config["credentials"]["user_id"] = new_uid
    config["credentials"]["password"] = new_pw
    save_json(CONFIG_FILE, config)
    from notify import notify
    notify("Attendance credentials updated locally.", title="Credentials Changed", tags="key")
    return redirect(url_for("dashboard", msg="Credentials saved locally; remote secret sync is disabled"))

@app.route("/action/update-portal", methods=["POST"])
def action_update_portal():
    new_user = request.form.get("portal_user", "").strip()
    new_pw = request.form.get("portal_pass", "").strip()
    if not new_user or not new_pw:
        return redirect(url_for("dashboard"))
    config = load_config()
    if "portal" not in config:
        config["portal"] = {"enabled": True, "url": "https://one.aku.edu/Pages/homepk.aspx"}
    config["portal"]["username"] = new_user
    config["portal"]["password"] = new_pw
    save_json(CONFIG_FILE, config)
    from notify import notify
    notify("Portal credentials updated locally.", title="Portal Updated", tags="key")
    return redirect(url_for("dashboard", msg="Portal credentials saved"))

@app.route("/action/update-windows", methods=["POST"])
def action_update_windows():
    ti_start = request.form.get("ti_start", "").strip()
    ti_end = request.form.get("ti_end", "").strip()
    to_start = request.form.get("to_start", "").strip()
    to_end = request.form.get("to_end", "").strip()
    if not all([ti_start, ti_end, to_start, to_end]):
        return redirect(url_for("dashboard"))
    config = load_config()
    config["timein"]["window_start"] = ti_start
    config["timein"]["window_end"] = ti_end
    ti_s = datetime.strptime(ti_start, "%H:%M")
    ti_e = datetime.strptime(ti_end, "%H:%M")
    ti_pe = ti_s + (ti_e - ti_s) * 0.75
    config["timein"]["primary_end"] = ti_pe.strftime("%H:%M")
    config["timeout"]["window_start"] = to_start
    config["timeout"]["window_end"] = to_end
    to_s = datetime.strptime(to_start, "%H:%M")
    to_e = datetime.strptime(to_end, "%H:%M")
    to_pe = to_s + (to_e - to_s) * 0.75
    config["timeout"]["primary_end"] = to_pe.strftime("%H:%M")
    save_json(CONFIG_FILE, config)
    from notify import notify
    notify(f"Time windows updated. In: {ti_start}-{ti_end}, Out: {to_start}-{to_end}", title="Windows Updated", tags="clock3")
    try:
        from cloud_sync import sync_time_windows
        sync_time_windows(ti_end, to_end)
    except Exception:
        pass
    return redirect(url_for("dashboard", msg="Time windows saved + synced to cloud"))

@app.route("/action/add-holiday", methods=["POST"])
def action_add_holiday():
    date = request.form.get("hol_date", "").strip()
    label = request.form.get("hol_label", "").strip()
    moon = request.form.get("hol_moon") == "on"
    if not date or not label:
        return redirect(url_for("dashboard"))
    # manage_holidays.add_holiday() strptime-validates; this form bypassed it
    # entirely, and render_holidays() then strptime()s unconditionally - one
    # bad value here 500s the whole dashboard.
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    data = load_json(HOLIDAYS_FILE)
    if not data.get("holidays"):
        data["holidays"] = []
    for h in data["holidays"]:
        if h["date"] == date:
            return redirect(url_for("dashboard"))
    entry = {"date": date, "label": label, "confirmed": not moon}
    if moon:
        entry["moon_dependent"] = True
    data["holidays"].append(entry)
    data["holidays"].sort(key=lambda x: x["date"])
    save_json(HOLIDAYS_FILE, data)
    _sync_holidays_to_cloud()
    from notify import notify
    if should_notify_holiday_change():
        notify(f"Holiday added: {date} - {label}", title="Holiday Added", tags="calendar")
    return redirect(url_for("dashboard", msg="Holiday added"))

@app.route("/action/toggle-holiday/<date>")
def action_toggle_holiday(date):
    data = load_json(HOLIDAYS_FILE)
    for h in data.get("holidays", []):
        if h["date"] == date:
            h["disabled"] = not h.get("disabled", False)
            state = "disabled" if h["disabled"] else "enabled"
            save_json(HOLIDAYS_FILE, data)
            _sync_holidays_to_cloud()
            from notify import notify
            if should_notify_holiday_change():
                notify(f"Holiday {state}: {date} - {h.get('label', '')}", title=f"Holiday {state.title()}", tags="calendar")
            break
    return redirect(url_for("dashboard"))

@app.route("/action/add-working-weekend/<date>")
def action_add_working_weekend(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    data = load_json(BLACKOUT_FILE)
    ww = data.get("working_weekends", [])
    if date not in ww:
        ww.append(date)
        ww.sort()
        data["working_weekends"] = ww
        save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"Working weekend added: {date}", title="Working Weekend", tags="calendar")
    return redirect(url_for("dashboard"))

@app.route("/action/remove-working-weekend/<date>")
def action_remove_working_weekend(date):
    data = load_json(BLACKOUT_FILE)
    ww = data.get("working_weekends", [])
    if date in ww:
        ww.remove(date)
        data["working_weekends"] = ww
        save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"Working weekend removed: {date}", title="Weekend Restored", tags="calendar")
    return redirect(url_for("dashboard"))


@app.route("/action/mark-leave/<date>")
def action_mark_leave(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    leave_type = request.args.get("type", "casual")
    days = float(request.args.get("days", "1"))
    if days not in (0.5, 1):
        days = 1
    type_map = {"casual": "casual", "sick": "sick", "earned": "earned", "other": None}
    bal_key = type_map.get(leave_type)
    type_labels = {"casual": "Casual Leave", "sick": "Sick Leave", "earned": "Earned Leave", "other": "Other Leave"}
    reason = type_labels.get(leave_type, "Leave")
    if days == 0.5:
        reason += " (Half Day)"
    # Serialize the read/validate/commit sequence so two duplicate requests
    # cannot both pass validation and deduct the balance twice.
    with _LEAVE_UPDATE_LOCK:
        config = load_config()
        lb = config.get("leave_balance", {})
        data = load_json(BLACKOUT_FILE)
        if not data.get("dates"):
            data["dates"] = []

        # Validate every rejection condition before changing either file.
        if any(d["date"] == date for d in data["dates"]):
            return redirect(url_for("dashboard", msg=f"Date {date} is already blocked"))
        if bal_key and bal_key in lb:
            remaining = lb[bal_key].get("remaining", 0)
            if remaining < days:
                return redirect(url_for("dashboard", msg=f"Insufficient {reason} balance: {remaining} days left"))

        entry = {"date": date, "reason": reason, "leave_type": leave_type,
                 "days": days, "added": pk_now().strftime("%Y-%m-%d %H:%M")}
        data["dates"].append(entry)
        data["dates"].sort(key=lambda d: d["date"])

        # Commit the blackout first: if that write fails, no balance can be
        # lost. The lock keeps the paired balance update in the same operation.
        save_json(BLACKOUT_FILE, data)
        if bal_key and bal_key in lb:
            val = remaining - days
            lb[bal_key]["remaining"] = int(val) if val == int(val) else val
            save_json(CONFIG_FILE, config)
        _write_leave_balance(config)

    _sync_blackout_to_cloud()
    _sync_leave_balance_to_cloud()
    from notify import notify
    notify(f"{reason}: {date} ({days}d)", title="Leave Marked", tags="palm_tree")
    return redirect(url_for("dashboard", msg=f"{reason} marked for {date}"))

@app.route("/action/cancel-leave/<date>")
def action_cancel_leave(date):
    config = load_config()
    data = load_json(BLACKOUT_FILE)
    removed = None
    new_dates = []
    for d in data.get("dates", []):
        if d["date"] == date and d.get("leave_type"):
            removed = d
        else:
            new_dates.append(d)
    if not removed:
        return redirect(url_for("dashboard", msg=f"No leave entry found for {date}"))

    data["dates"] = new_dates
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    lb = config.get("leave_balance", {})
    lt = removed.get("leave_type")
    days = removed.get("days", 1)
    if lt and lt in lb:
        val = lb[lt].get("remaining", 0) + days
        lb[lt]["remaining"] = int(val) if val == int(val) else val
        save_json(CONFIG_FILE, config)
    _write_leave_balance(config)
    _sync_leave_balance_to_cloud()
    from notify import notify
    notify(f"Leave cancelled: {date}", title="Leave Cancelled", tags="white_check_mark")
    return redirect(url_for("dashboard"))

@app.route("/action/save-leave-balance", methods=["POST"])
def action_save_leave_balance():
    def _num(v):
        f = float(v)
        return int(f) if f == int(f) else f
    config = load_config()
    lb = config.get("leave_balance", {})
    lb["year"] = int(request.form.get("lb_year", lb.get("year", 2026)))
    for lt in ("casual", "sick", "earned"):
        if lt not in lb:
            lb[lt] = {}
        lb[lt]["annual"] = _num(request.form.get(f"lb_{lt}_annual", lb[lt].get("annual", 0)))
        lb[lt]["remaining"] = _num(request.form.get(f"lb_{lt}_remaining", lb[lt].get("remaining", 0)))
    if "earned" in lb:
        lb["earned"]["carry_forward_limit"] = int(request.form.get("lb_carry_limit", lb["earned"].get("carry_forward_limit", 5)))
        lb["earned"]["carried_forward"] = _num(request.form.get("lb_carried", lb["earned"].get("carried_forward", 0)))
    config["leave_balance"] = lb
    save_json(CONFIG_FILE, config)
    _write_leave_balance(config)
    _sync_leave_balance_to_cloud()
    return redirect(url_for("dashboard", msg="Leave balance saved"))

@app.route("/action/add-leave", methods=["POST"])
def action_add_leave():
    start = request.form.get("leave_start", "").strip()
    end = request.form.get("leave_end", "").strip()
    leave_type = request.form.get("leave_type", "other").strip()
    reason = request.form.get("leave_reason", "").strip()
    if not reason:
        type_labels = {"casual": "Casual Leave", "sick": "Sick Leave", "earned": "Earned Leave", "other": "Leave"}
        reason = type_labels.get(leave_type, "Leave")
    if not start:
        return redirect(url_for("dashboard"))
    if not end:
        end = start
    if not _valid_date(start) or not _valid_date(end):
        return redirect(url_for("dashboard", msg="Leave dates must be real dates (YYYY-MM-DD)"))
    if start > end:
        start, end = end, start
    with _LEAVE_UPDATE_LOCK:
        config = load_config()
        lb = config.get("leave_balance", {})
        data = load_json(BLACKOUT_FILE)
        if not data.get("dates"):
            data["dates"] = []
        if not data.get("ranges"):
            data["ranges"] = []
        type_map = {"casual": "casual", "sick": "sick", "earned": "earned", "other": None}
        bal_key = type_map.get(leave_type)
        if start == end:
            for d in data["dates"]:
                if d["date"] == start:
                    return redirect(url_for("dashboard", msg=f"Date {start} is already blocked"))
            days = 1
            if bal_key and bal_key in lb:
                remaining = lb[bal_key].get("remaining", 0)
                if remaining < days:
                    return redirect(url_for("dashboard", msg=f"Insufficient {reason} balance: {remaining} days left"))
            entry = {"date": start, "reason": reason, "leave_type": leave_type,
                     "days": days, "added": pk_now().strftime("%Y-%m-%d %H:%M")}
            data["dates"].append(entry)
            data["dates"].sort(key=lambda d: d["date"])
        else:
            for r in data["ranges"]:
                if r["start"] == start and r["end"] == end:
                    return redirect(url_for("dashboard", msg="This range is already blocked"))
            days = _count_working_days(start, end)
            if days == 0:
                return redirect(url_for("dashboard", msg="No working days in that range"))
            if bal_key and bal_key in lb:
                remaining = lb[bal_key].get("remaining", 0)
                if remaining < days:
                    return redirect(url_for("dashboard", msg=f"Insufficient {reason} balance: {remaining} days left (need {days})"))
            entry = {"start": start, "end": end, "reason": reason, "leave_type": leave_type,
                     "days": days, "added": pk_now().strftime("%Y-%m-%d %H:%M")}
            data["ranges"].append(entry)
            data["ranges"].sort(key=lambda r: r["start"])
        save_json(BLACKOUT_FILE, data)
        if bal_key and bal_key in lb:
            val = lb[bal_key].get("remaining", 0) - days
            lb[bal_key]["remaining"] = int(val) if val == int(val) else val
            save_json(CONFIG_FILE, config)
        _write_leave_balance(config)
    _sync_blackout_to_cloud()
    _sync_leave_balance_to_cloud()
    from notify import notify
    label = f"{start}" if start == end else f"{start} to {end}"
    notify(f"{reason}: {label} ({days}d)", title="Leave Marked", tags="palm_tree")
    return redirect(url_for("dashboard", msg=f"{reason} marked: {label} ({days} working days)"))

@app.route("/action/cancel-range/<start>/<end>")
def action_cancel_range(start, end):
    with _LEAVE_UPDATE_LOCK:
        config = load_config()
        data = load_json(BLACKOUT_FILE)
        removed = None
        new_ranges = []
        for r in data.get("ranges", []):
            if r["start"] == start and r["end"] == end and removed is None:
                removed = r
            else:
                new_ranges.append(r)
        if not removed:
            return redirect(url_for("dashboard", msg=f"No range found for {start} to {end}"))
        data["ranges"] = new_ranges
        save_json(BLACKOUT_FILE, data)
        lt = removed.get("leave_type")
        days = removed.get("days", 0)
        lb = config.get("leave_balance", {})
        type_map = {"casual": "casual", "sick": "sick", "earned": "earned"}
        bal_key = type_map.get(lt)
        if bal_key and bal_key in lb and days:
            val = lb[bal_key].get("remaining", 0) + days
            lb[bal_key]["remaining"] = int(val) if val == int(val) else val
            save_json(CONFIG_FILE, config)
        _write_leave_balance(config)
    _sync_blackout_to_cloud()
    _sync_leave_balance_to_cloud()
    from notify import notify
    refund_msg = f" ({days}d refunded)" if days and bal_key else ""
    notify(f"Leave cancelled: {start} to {end}{refund_msg}", title="Leave Cancelled", tags="white_check_mark")
    return redirect(url_for("dashboard", msg=f"Leave cancelled: {start} to {end}{refund_msg}"))


@app.route("/action/add-wfh", methods=["POST"])
def action_add_wfh():
    start = request.form.get("wfh_start", "").strip()
    end = request.form.get("wfh_end", "").strip()
    reason = request.form.get("wfh_reason", "").strip() or "Work from home"
    if not start:
        return redirect(url_for("dashboard"))
    if not end:
        end = start
    if not _valid_date(start) or not _valid_date(end):
        return redirect(url_for("dashboard", msg="WFH dates must be real dates (YYYY-MM-DD)"))
    if start > end:
        start, end = end, start
    data = load_json(BLACKOUT_FILE)
    if "wfh" not in data:
        data["wfh"] = []
    if "wfh_ranges" not in data:
        data["wfh_ranges"] = []
    if start == end:
        for d in data["wfh"]:
            if d["date"] == start:
                return redirect(url_for("dashboard", msg=f"WFH already marked for {start}"))
        entry = {"date": start, "reason": reason,
                 "added": pk_now().strftime("%Y-%m-%d %H:%M")}
        data["wfh"].append(entry)
        data["wfh"].sort(key=lambda d: d["date"])
        days = 1
    else:
        for r in data["wfh_ranges"]:
            if r["start"] == start and r["end"] == end:
                return redirect(url_for("dashboard", msg="WFH range already exists"))
        days = _count_working_days(start, end)
        if days == 0:
            return redirect(url_for("dashboard", msg="No working days in that range"))
        entry = {"start": start, "end": end, "reason": reason, "days": days,
                 "added": pk_now().strftime("%Y-%m-%d %H:%M")}
        data["wfh_ranges"].append(entry)
        data["wfh_ranges"].sort(key=lambda r: r["start"])
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    label = f"{start}" if start == end else f"{start} to {end}"
    notify(f"WFH: {label} ({days}d)", title="WFH Marked", tags="house")
    return redirect(url_for("dashboard", msg=f"WFH marked: {label} ({days} working days)"))


@app.route("/action/quick-wfh/<date>")
def action_quick_wfh(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg="Invalid date"))
    data = load_json(BLACKOUT_FILE)
    if "wfh" not in data:
        data["wfh"] = []
    for d in data["wfh"]:
        if d["date"] == date:
            return redirect(url_for("dashboard", msg=f"Already WFH on {date}"))
    for r in data.get("wfh_ranges", []):
        if r.get("start", "") <= date <= r.get("end", ""):
            return redirect(url_for("dashboard", msg=f"Already WFH (range) on {date}"))
    data["wfh"].append({"date": date, "reason": "Work from home",
                        "added": pk_now().strftime("%Y-%m-%d %H:%M")})
    data["wfh"].sort(key=lambda d: d["date"])
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"WFH: {date}", title="WFH Marked", tags="house")
    return redirect(url_for("dashboard", msg=f"WFH marked for {date}"))


@app.route("/action/cancel-wfh/<date>")
def action_cancel_wfh(date):
    if not _valid_date(date):
        return redirect(url_for("dashboard", msg=f"Not a valid date: {date}"))
    data = load_json(BLACKOUT_FILE)
    new_wfh = [d for d in data.get("wfh", []) if d["date"] != date]
    if len(new_wfh) == len(data.get("wfh", [])):
        return redirect(url_for("dashboard", msg=f"No WFH entry found for {date}"))
    data["wfh"] = new_wfh
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"WFH cancelled: {date}", title="WFH Cancelled", tags="white_check_mark")
    return redirect(url_for("dashboard", msg=f"WFH cancelled: {date}"))


@app.route("/action/cancel-wfh-range/<start>/<end>")
def action_cancel_wfh_range(start, end):
    data = load_json(BLACKOUT_FILE)
    removed = None
    new_ranges = []
    for r in data.get("wfh_ranges", []):
        if r["start"] == start and r["end"] == end and removed is None:
            removed = r
        else:
            new_ranges.append(r)
    if not removed:
        return redirect(url_for("dashboard", msg=f"No WFH range found for {start} to {end}"))
    data["wfh_ranges"] = new_ranges
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"WFH range cancelled: {start} to {end}", title="WFH Cancelled", tags="white_check_mark")
    return redirect(url_for("dashboard", msg=f"WFH range cancelled: {start} to {end}"))


@app.route("/action/cancel-wfh-today")
def action_cancel_wfh_today():
    today_str = pk_now().strftime("%Y-%m-%d")
    data = load_json(BLACKOUT_FILE)
    new_wfh = [d for d in data.get("wfh", []) if d["date"] != today_str]
    removed = len(new_wfh) < len(data.get("wfh", []))
    if not removed:
        in_range = False
        for r in data.get("wfh_ranges", []):
            if r["start"] <= today_str <= r["end"]:
                in_range = True
                break
        if in_range:
            msg = "Today is part of a WFH range. Cancel the full range from the dashboard."
            if request.headers.get("User-Agent", "").startswith("ntfy/"):
                return msg, 200
            return redirect(url_for("dashboard", msg=msg))
        msg = "No WFH entry for today"
        if request.headers.get("User-Agent", "").startswith("ntfy/"):
            return msg, 200
        return redirect(url_for("dashboard", msg=msg))
    data["wfh"] = new_wfh
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"WFH cancelled for today ({today_str})", title="WFH Cancelled", tags="white_check_mark")
    if request.headers.get("User-Agent", "").startswith("ntfy/"):
        return "WFH cancelled for today", 200
    return redirect(url_for("dashboard", msg=f"WFH cancelled for today ({today_str})"))


@app.route("/action/cancel-wfh-tomorrow")
def action_cancel_wfh_tomorrow():
    tomorrow_str = (pk_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    data = load_json(BLACKOUT_FILE)
    new_wfh = [d for d in data.get("wfh", []) if d["date"] != tomorrow_str]
    removed = len(new_wfh) < len(data.get("wfh", []))
    if not removed:
        in_range = False
        for r in data.get("wfh_ranges", []):
            if r["start"] <= tomorrow_str <= r["end"]:
                in_range = True
                break
        if in_range:
            msg = "Tomorrow is part of a WFH range. Cancel the full range from the dashboard."
            if request.headers.get("User-Agent", "").startswith("ntfy/"):
                return msg, 200
            return redirect(url_for("dashboard", msg=msg))
        msg = "No WFH entry for tomorrow"
        if request.headers.get("User-Agent", "").startswith("ntfy/"):
            return msg, 200
        return redirect(url_for("dashboard", msg=msg))
    data["wfh"] = new_wfh
    save_json(BLACKOUT_FILE, data)
    _sync_blackout_to_cloud()
    from notify import notify
    notify(f"WFH cancelled for tomorrow ({tomorrow_str})", title="WFH Cancelled", tags="white_check_mark")
    if request.headers.get("User-Agent", "").startswith("ntfy/"):
        return "WFH cancelled for tomorrow", 200
    return redirect(url_for("dashboard", msg=f"WFH cancelled for tomorrow ({tomorrow_str})"))


@app.route("/action/wfh-hours", methods=["POST"])
def action_wfh_hours():
    """Save WFH hours (analytics only) - user picks both times."""
    today_str = pk_now().strftime("%Y-%m-%d")
    ti_val = request.form.get("wfh_ti", "").strip()
    to_val = request.form.get("wfh_to", "").strip()
    if not ti_val:
        return redirect(url_for("dashboard", msg="Enter a Time-In for WFH hours"))
    from attendance_db import record_event
    parts = []
    ti_time = ti_val + ":00"
    record_event(today_str, "timein", "success", "WFH hours (manual)",
                 action_time=ti_time, action_origin="wfh")
    parts.append(f"In={ti_val}")
    if to_val:
        to_time = to_val + ":00"
        record_event(today_str, "timeout", "success", "WFH hours (manual)",
                     action_time=to_time, action_origin="wfh")
        parts.append(f"Out={to_val}")
    try:
        from cloud_sync import sync_status, push_all
        sync_status()
        push_all()
    except Exception:
        pass
    return redirect(url_for("dashboard", msg=f"WFH hours saved: {', '.join(parts)}"))


@app.route("/action/wfh-record-now", methods=["POST"])
def action_wfh_record_now():
    """Record current time as WFH time-in or time-out."""
    today_str = pk_now().strftime("%Y-%m-%d")
    now_time = pk_now().strftime("%H:%M:%S")
    mode = request.form.get("mode", "timein")
    if mode not in ("timein", "timeout"):
        return redirect(url_for("dashboard", msg="Invalid mode"))
    label = "Time-In" if mode == "timein" else "Time-Out"
    from attendance_db import record_event
    record_event(today_str, mode, "success", f"WFH {label} (record now)",
                 action_time=now_time, action_origin="wfh")
    try:
        from cloud_sync import sync_status, push_all
        sync_status()
        push_all()
    except Exception:
        pass
    return redirect(url_for("dashboard", msg=f"WFH {label} recorded at {now_time[:5]}"))


@app.route("/api/wfh-clock-status")
def api_wfh_clock_status():
    today_str = pk_now().strftime("%Y-%m-%d")
    from attendance_db import get_latest
    ti = get_latest("timein", today_str)
    to = get_latest("timeout", today_str)
    wfh_in = ti if ti and ti.get("action_origin") == "wfh" else None
    wfh_out = to if to and to.get("action_origin") == "wfh" else None
    is_wfh_today = False
    data = load_json(BLACKOUT_FILE)
    for d in data.get("wfh", []):
        if d["date"] == today_str:
            is_wfh_today = True
            break
    if not is_wfh_today:
        for r in data.get("wfh_ranges", []):
            if r["start"] <= today_str <= r["end"]:
                is_wfh_today = True
                break
    return json.dumps({
        "is_wfh_today": is_wfh_today,
        "clocked_in": wfh_in["action_time"] if wfh_in else None,
        "clocked_out": wfh_out["action_time"] if wfh_out else None,
    })


@app.route("/api/wfh-entries")
def api_wfh_entries():
    data = load_json(BLACKOUT_FILE)
    return json.dumps({
        "wfh": data.get("wfh", []),
        "wfh_ranges": data.get("wfh_ranges", []),
    })

@app.route("/api/leave-summary")
def api_leave_summary():
    config = load_config()
    lb = config.get("leave_balance", {})
    data = load_json(BLACKOUT_FILE)
    leaves = [d for d in data.get("dates", []) if d.get("leave_type")]
    for r in data.get("ranges", []):
        if r.get("leave_type"):
            leaves.append(r)
    summary = {}
    for lt in ("casual", "sick", "earned", "other"):
        bal = lb.get(lt, {})
        used = sum(d.get("days", 1) for d in leaves if d.get("leave_type") == lt)
        summary[lt] = {
            "annual": bal.get("annual", 0),
            "remaining": bal.get("remaining", 0),
            "used_via_bot": used,
        }
    return json.dumps({"year": lb.get("year", 2026), "types": summary, "leaves": leaves})

@app.route("/mobile")
def mobile_dashboard():
    mobile_path = BASE_DIR / "mobile_dashboard.html"
    if mobile_path.exists():
        with open(mobile_path, "r", encoding="utf-8") as f:
            return f.read()
    return "Mobile dashboard not found", 404


@app.route("/action/sync-from-cloud")
def action_sync_from_cloud():
    try:
        from cloud_sync import pull_from_cloud
        results = pull_from_cloud()
        msgs = [f"{k}: {v['message']}" for k, v in results.items()]
        return redirect(url_for("dashboard", msg="Cloud sync: " + ", ".join(msgs)))
    except Exception as e:
        return redirect(url_for("dashboard", msg=f"Sync failed: {e}"))


@app.route("/action/push-to-cloud")
def action_push_to_cloud():
    try:
        from cloud_sync import push_all
        results = push_all()
        ok_count = sum(1 for v in results.values() if v["ok"])
        return redirect(url_for("dashboard", msg=f"Pushed {ok_count}/{len(results)} files to cloud"))
    except Exception as e:
        return redirect(url_for("dashboard", msg=f"Push failed: {e}"))


def _sync_pause_state(paused, state_label):
    """Mirror a timestamped pause state to GitHub, retrying once and returning
    the real result so callers cannot report a false success."""
    from cloud_sync import sync_github_file, _gh_config

    # Laptop-only install: there is no cloud to mirror to, so an absent remote
    # is "not applicable", not a failure. Calling it a failure made the phone
    # Pause button answer HTTP 502 on every single tap.
    repo, token = _gh_config()
    if not repo or not token:
        return True, "local only (cloud sync not configured)"

    payload = {
        "paused": paused,
        "updated_at": pk_now().replace(tzinfo=PKT).isoformat(timespec="seconds"),
    }
    last_message = "unknown sync failure"
    for _attempt in range(2):
        try:
            ok, message = sync_github_file(
                "bot_config.json",
                json.dumps(payload, indent=2),
                f"Bot {state_label} from dashboard",
            )
        except Exception as exc:
            ok, message = False, str(exc)
        if ok:
            return True, message
        last_message = message
    return False, last_message


@app.route("/action/toggle-pause")
def action_toggle_pause():
    config = load_config()
    config["paused"] = not config.get("paused", False)
    save_json(CONFIG_FILE, config)
    state = "PAUSED" if config["paused"] else "RESUMED"
    sync_ok, sync_message = _sync_pause_state(config["paused"], state.lower())
    from notify import notify
    if config["paused"]:
        notify("Bot PAUSED - no attendance will be marked until resumed.", title="Bot Paused", priority="high", tags="pause_button")
    else:
        notify("Bot RESUMED - attendance marking is active again.", title="Bot Resumed", tags="arrow_forward")
    if not sync_ok:
        return redirect(url_for(
            "dashboard",
            msg=f"Bot {state} locally; GitHub pause-state sync failed after retry: {sync_message}",
        ))
    return redirect(url_for("dashboard", msg=f"Bot {state}"))

@app.route("/api/toggle-pause")
def api_toggle_pause():
    config = load_config()
    config["paused"] = not config.get("paused", False)
    save_json(CONFIG_FILE, config)
    state = "paused" if config["paused"] else "active"
    sync_ok, sync_message = _sync_pause_state(config["paused"], state)
    from notify import notify
    if config["paused"]:
        notify("Bot PAUSED - no attendance will be marked until resumed.", title="Bot Paused", priority="high", tags="pause_button")
    else:
        notify("Bot RESUMED - attendance marking is active again.", title="Bot Resumed", tags="arrow_forward")
    if not sync_ok:
        return jsonify({
            "ok": False,
            "paused": config["paused"],
            "msg": f"Bot {state} locally; GitHub pause-state sync failed after retry: {sync_message}",
        }), 502
    return jsonify({"ok": True, "paused": config["paused"], "msg": f"Bot {state}"})

@app.route("/api/action/<path:action_path>")
def api_action(action_path):
    """Run only explicitly supported no-argument actions and return their result."""
    allowed_actions = {
        "sync-from-cloud": "action_sync_from_cloud",
        "push-to-cloud": "action_push_to_cloud",
        "toggle-pause": "action_toggle_pause",
        "timein-now": "action_timein_now",
        "timeout-now": "action_timeout_now",
        "sync-portal": "action_sync_portal",
        "test-cloud-sync": "action_test_cloud_sync",
    }
    endpoint = allowed_actions.get(action_path)
    if not endpoint:
        return jsonify({"ok": False, "msg": "Action is not allowed"}), 404

    try:
        response = app.make_response(app.view_functions[endpoint]())
    except Exception:
        return jsonify({"ok": False, "msg": "Action failed"}), 500

    payload = response.get_json(silent=True)
    if isinstance(payload, dict):
        ok = bool(payload.get("ok", response.status_code < 400))
        msg = str(payload.get("msg") or payload.get("message") or "Action completed")
        return jsonify({"ok": ok, "msg": msg}), 200 if ok else 409

    location = response.headers.get("Location", "")
    messages = parse_qs(urlsplit(location).query).get("msg", [])
    msg = messages[0] if messages else f"Action returned HTTP {response.status_code}"
    failure_words = ("cannot", "failed", "fail ", "insufficient", "not found")
    ok = response.status_code < 400 and not any(word in msg.lower() for word in failure_words)
    return jsonify({"ok": ok, "msg": msg}), 200 if ok else 409

@app.route("/action/timein-now")
def action_timein_now():
    today = pk_now().strftime("%Y-%m-%d")
    status = load_json(STATUS_FILE)
    ti = status.get("timein", {})
    if ti.get("date") == today and ti.get("status") == "success":
        today_fmt = pk_now().strftime("%d-%b-%Y")
        recorded_time = ti.get("action_time") or ti.get("observed_time") or "?"
        return redirect(url_for("dashboard", msg=f"Time-In already posted today {today_fmt} at {recorded_time}"))
    # A dangling prior-day Time-Out is no longer a hard block here -
    # timein_bot.py auto-completes it before proceeding with today's Time-In.
    ok, msg, died_silently = _run_bot_now("timein")
    if died_silently:
        # The bot could not report for itself, so this page must.
        from notify import notify
        notify(msg, title="Manual Time-In Failed", priority="high", tags="rotating_light")
    return redirect(url_for("dashboard", msg=msg))

@app.route("/action/timeout-now")
def action_timeout_now():
    today = pk_now().strftime("%Y-%m-%d")
    status = load_json(STATUS_FILE)
    ti = status.get("timein", {})
    to = status.get("timeout", {})
    ti_date = ti.get("date", "")
    timed_in_today = ti.get("date") == today and ti.get("status") == "success"
    pending_prior_day = (
        ti.get("status") == "success" and ti_date and ti_date < today
        and (to.get("date") != ti_date or to.get("status") != "success")
    )
    if not timed_in_today and not pending_prior_day:
        today_fmt = pk_now().strftime("%d-%b-%Y")
        return redirect(url_for("dashboard", msg=f"Cannot Time-Out: you haven't Timed-In today {today_fmt} yet"))
    if to.get("date") == today and to.get("status") == "success":
        today_fmt = pk_now().strftime("%d-%b-%Y")
        recorded_time = to.get("action_time") or to.get("observed_time") or "?"
        return redirect(url_for("dashboard", msg=f"Time-Out already posted today {today_fmt} at {recorded_time}"))
    ok, msg, died_silently = _run_bot_now("timeout")
    if died_silently:
        from notify import notify
        notify(msg, title="Manual Time-Out Failed", priority="high", tags="rotating_light")
    return redirect(url_for("dashboard", msg=msg))

@app.route("/action/sync-portal")
def action_sync_portal():
    """Check the AKU portal for today's attendance and update records."""
    try:
        proc = subprocess.run(
            [_system_python(), str(BASE_DIR / "timein_bot.py"), "recheck"],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return redirect(url_for("dashboard", msg="Portal sync timed out"))
    except Exception as e:
        return redirect(url_for("dashboard", msg=f"Portal sync error: {e}"))
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        return redirect(url_for("dashboard", msg=f"Portal sync failed: {stderr or 'exit code ' + str(proc.returncode)}"))
    today = pk_now().strftime("%Y-%m-%d")
    status = load_json(STATUS_FILE)
    results = []
    synced = False
    for mode in ("timein", "timeout"):
        label = "Time-In" if mode == "timein" else "Time-Out"
        rec = status.get(mode, {})
        if rec.get("date") == today and rec.get("status") == "success":
            t = rec.get("action_time") or rec.get("observed_time", "?")
            results.append(f"{label}: {t}")
            synced = True
        elif rec.get("date") == today:
            results.append(f"{label}: {rec.get('status', 'unknown')}")
        else:
            results.append(f"{label}: no record yet")
    if synced:
        msg = "Portal sync: " + " | ".join(results)
    else:
        msg = "Nothing to sync - no failed attempts found today"
    return redirect(url_for("dashboard", msg=msg))


@app.route("/action/correct-attendance", methods=["POST"])
def action_correct_attendance():
    """Correct time-in and/or time-out for a given date."""
    date = request.form.get("date", "").strip()
    ti_val = request.form.get("timein", "").strip()
    to_val = request.form.get("timeout", "").strip()
    if not date:
        return redirect(url_for("dashboard", msg="Missing date for correction"))
    if not ti_val and not to_val:
        return redirect(url_for("dashboard", msg="Enter at least one time to correct"))
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return redirect(url_for("dashboard", msg="Invalid date format"))
    timeout_next_day = request.form.get("timeout_next_day") == "on"
    parts = []
    try:
        from attendance_db import record_event, record_correction_next_day
        for mode, val, label in [("timein", ti_val, "Time-In"), ("timeout", to_val, "Time-Out")]:
            if not val:
                continue
            if not _re.match(r"^\d{2}:\d{2}$", val):
                return redirect(url_for("dashboard", msg=f"Invalid {label} time format"))
            time_with_sec = val + ":00"
            if mode == "timeout" and timeout_next_day:
                record_correction_next_day(
                    date_str=date, time_str=time_with_sec,
                    message=f"{label} corrected to {val} (next-day manual entry)",
                )
                parts.append(f"{label}={val} (next day)")
            else:
                record_event(
                    date_str=date,
                    mode=mode,
                    status="success",
                    message=f"{label} corrected to {val} (manual entry)",
                    action_time=time_with_sec,
                    action_origin="bot",
                )
                parts.append(f"{label}={val}")
        try:
            from cloud_sync import sync_status, push_all
            sync_status()
            push_all()
        except Exception:
            pass
        return redirect(url_for("dashboard", msg=f"{date} corrected: {', '.join(parts)}"))
    except Exception as exc:
        return redirect(url_for("dashboard", msg=f"Correction failed: {exc}"))


@app.route("/action/update-notifications", methods=["POST"])
def action_update_notifications():
    notif_prefs = load_notif_prefs()
    prefs = notif_prefs.get("preferences", {})
    all_keys = ["timein_success", "timeout_success", "skip_day", "failure",
                "tomorrow_plan", "tomorrow_holiday", "holiday_reminder", "deadman_switch", "holiday_change"]
    for key in all_keys:
        prefs[key] = request.form.get(key) == "on"
    admin_email = request.form.get("admin_email", "").strip()
    notif_prefs["preferences"] = prefs
    notif_prefs["admin_email"] = admin_email
    save_json(NOTIF_PREFS_FILE, notif_prefs)
    _sync_notif_prefs_to_cloud()
    return redirect(url_for("dashboard", msg="Notification settings saved + synced to cloud"))

@app.route("/action/update-cloud-sync", methods=["POST"])
def action_update_cloud_sync():
    config = load_config()
    if "cloud_sync" not in config:
        config["cloud_sync"] = {}
    gh_repo = request.form.get("gh_repo", "").strip()
    gh_token = request.form.get("gh_token", "").strip()
    existing_token = config["cloud_sync"].get("github", {}).get("token", "")
    saved_token = gh_token or existing_token
    config["cloud_sync"]["github"] = {
        "repo": gh_repo,
        "token": saved_token,
        "enabled": bool(gh_repo and saved_token),
    }
    save_json(CONFIG_FILE, config)
    from notify import notify
    if gh_repo and saved_token:
        notify("Cloud sync configured: GitHub", title="Cloud Sync", tags="cloud")
    return redirect(url_for("dashboard", msg="Cloud sync settings saved"))

@app.route("/action/test-cloud-sync")
def action_test_cloud_sync():
    from cloud_sync import test_github_connection
    ok, message = test_github_connection()
    status = "OK" if ok else "FAIL"
    return redirect(url_for("dashboard", msg=f"Sync test: {status} ({message})"))

def compute_hours_worked(ti, to, timeout_next_day=False):
    """Hours between a Time-In and Time-Out clock-time. When timeout_next_day
    is True, the Time-Out was recorded the following calendar day (e.g. a
    catch-up), so 24h is added to the calculation."""
    if not ti or not to:
        return None
    ti_parts = ti.split(":")
    to_parts = to.split(":")
    ti_min = int(ti_parts[0]) * 60 + int(ti_parts[1])
    to_min = int(to_parts[0]) * 60 + int(to_parts[1])
    if timeout_next_day:
        to_min += 24 * 60
    hours = round((to_min - ti_min) / 60, 2)
    if hours <= 0 or hours > 24.5:
        return None
    return hours


@app.route("/api/status")
def api_status():
    return jsonify(load_json(STATUS_FILE))

@app.route("/api/analytics")
def api_analytics():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return jsonify({"records": []})
    history = load_json(HISTORY_FILE)
    records = history.get("records", {})
    blackout = load_json(BLACKOUT_FILE)
    bl_dates = {d["date"]: d for d in blackout.get("dates", [])}
    bl_ranges = blackout.get("ranges", [])
    working_weekends = blackout.get("working_weekends", [])
    wfh_map = {d["date"]: d for d in blackout.get("wfh", [])}
    wfh_range_list = blackout.get("wfh_ranges", [])
    holidays_data = load_json(HOLIDAYS_FILE)
    hol_map = {h["date"]: h.get("label", "Holiday") for h in holidays_data.get("holidays", []) if not h.get("disabled")}
    days = []
    d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    while d <= end_d:
        ds = d.strftime("%Y-%m-%d")
        rec = records.get(ds, {})
        ti = rec.get("timein")
        to = rec.get("timeout")
        hours = compute_hours_worked(ti, to, rec.get("timeout_next_day", False)) or 0
        is_weekend = d.weekday() >= 5
        is_working_wknd = ds in working_weekends
        hol_name = hol_map.get(ds)
        bl_entry = bl_dates.get(ds)
        bl_range = None
        for r in bl_ranges:
            if r.get("start", "") <= ds <= r.get("end", ""):
                bl_range = r
                break
        wfh_entry = wfh_map.get(ds)
        wfh_range_match = None
        if not wfh_entry:
            for wr in wfh_range_list:
                if wr.get("start", "") <= ds <= wr.get("end", ""):
                    wfh_range_match = wr
                    break
        is_wfh = wfh_entry or wfh_range_match
        if is_wfh:
            day_type = "wfh"
            label = (wfh_entry or wfh_range_match).get("reason", "Work from home")
        elif ti or to:
            day_type = "workday"
            label = ""
        elif hol_name:
            day_type = "holiday"
            label = hol_name
        elif bl_entry:
            day_type = "leave"
            lt = bl_entry.get("leave_type", "")
            reason = bl_entry.get("reason", "Leave")
            half = bl_entry.get("days", 1) == 0.5
            label = reason + (" (Half)" if half else "")
        elif bl_range:
            day_type = "leave"
            label = bl_range.get("reason", "Leave")
        elif is_weekend and not is_working_wknd:
            day_type = "weekend"
            label = "Weekend"
        elif d > datetime.now():
            day_type = "future"
            label = ""
        else:
            day_type = "missing"
            label = "No record"
        days.append({"date": ds, "day": d.strftime("%a"), "type": day_type, "label": label, "timein": ti or "-", "timeout": to or "-", "hours": hours})
        d += timedelta(days=1)
    return jsonify({"records": days})

@app.route("/download/report")
def download_report():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    history = load_json(HISTORY_FILE)
    records = history.get("records", {})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Day", "Time-In", "Time-Out", "Hours Worked", "Target (9h10m)", "Overtime/Under"])
    d = datetime.strptime(start, "%Y-%m-%d") if start else pk_now()
    end_d = datetime.strptime(end, "%Y-%m-%d") if end else pk_now()
    while d <= end_d:
        ds = d.strftime("%Y-%m-%d")
        rec = records.get(ds, {})
        ti = rec.get("timein", "")
        to = rec.get("timeout", "")
        hours = compute_hours_worked(ti, to, rec.get("timeout_next_day", False))
        diff_str = ""
        hours_str = f"{hours:.2f}" if hours is not None else ("N/A" if (ti and to) else "")
        if hours is not None:
            diff = round(hours - TARGET_MINUTES / 60, 2)
            diff_str = f"+{diff}" if diff >= 0 else str(diff)
        writer.writerow([ds, d.strftime("%A"), ti, to, hours_str, "9.17", diff_str])
        d += timedelta(days=1)
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=timein_report_{start}_to_{end}.csv"})

# --- Render helpers ---

def render_workdays(days):
    rows = []
    for d in days:
        if d.get("attendance"):
            # Actually marked in today - that outranks any calendar label.
            badge = f'<span class="badge ok">{html.escape(str(d["attendance"]))}</span>'
        elif d.get("working_weekend"):
            badge = '<span class="badge ok">Working</span>'
            badge += f' <a class="btn sm danger" style="padding:.2rem .4rem;font-size:.65rem" href="/action/remove-working-weekend/{d["date"]}">Undo</a>'
        elif d.get("wfh"):
            badge = '<span class="badge" style="background:var(--wfh-color,#2196F3);color:#fff">WFH</span>'
            badge += f' <a class="btn sm danger" style="padding:.2rem .4rem;font-size:.65rem" href="/action/cancel-wfh/{d["date"]}">Undo</a>'
        elif d["skip"]:
            badge = f'<span class="badge skip">{html.escape(str(d["skip"]))}</span>'
            if d.get("is_weekend"):
                badge += f' <a class="btn sm outline" style="padding:.2rem .4rem;font-size:.65rem" href="/action/add-working-weekend/{d["date"]}">Work</a>'
        else:
            badge = '<span class="badge ok">Active</span>'
            badge += ' <a class="btn sm outline" style="padding:.2rem .4rem;font-size:.65rem" href="#" onclick="openLeaveModal(\'' + d["date"] + '\');return false">Leave</a>'
            badge += f' <a class="btn sm outline" style="padding:.2rem .4rem;font-size:.65rem;border-color:var(--wfh-color,#2196F3);color:var(--wfh-color,#2196F3)" href="/action/quick-wfh/{d["date"]}">WFH</a>'
        day_label = "Today" if d.get("is_today") else d["day"]
        row_cls = "wd-row wd-today" if d.get("is_today") else "wd-row"
        rows.append(f'<div class="{row_cls}"><span class="wd-day">{day_label}</span><span class="wd-date">{d["label"]}</span>{badge}</div>')
    return "\n".join(rows)

def render_holidays(holidays):
    if not holidays:
        return '<p class="empty">No upcoming holidays.</p>'
    rows = []
    for h in holidays:
        d = datetime.strptime(h["date"], "%Y-%m-%d")
        disabled = h.get("disabled", False)
        moon = h.get("moon_dependent", False)
        if disabled:
            badge = '<span class="badge fail">Off</span>'
        elif h.get("confirmed", False):
            badge = '<span class="badge ok">Confirmed</span>'
        else:
            badge = '<span class="badge skip">Tentative</span>'
        moon_icon = ' <span class="moon">&#9789;</span>' if moon else ""
        dim = ' style="opacity:.45"' if disabled else ""
        toggle_label = "Enable" if disabled else "Disable"
        toggle_cls = "btn sm" if disabled else "btn sm danger"
        actions_html = '<div class="hol-actions">'
        if not disabled and not h.get("confirmed"):
            actions_html += f'<a class="btn sm" href="/action/confirm-holiday/{h["date"]}">Confirm</a>'
        actions_html += f'<a class="btn sm outline" href="/action/shift-holiday/{h["date"]}/-1">-1</a>'
        actions_html += f'<a class="btn sm outline" href="/action/shift-holiday/{h["date"]}/1">+1</a>'
        actions_html += f'<a class="{toggle_cls}" href="/action/toggle-holiday/{h["date"]}">{toggle_label}</a>'
        actions_html += '</div>'
        rows.append(
            f'<div class="hol-row"><div class="hol-info"{dim}><strong>{html.escape(str(h["label"]))}</strong>{moon_icon}<br>'
            f'<span class="hol-date">{d.strftime("%a %b %d, %Y")}</span> {badge}</div>'
            f'{actions_html}</div>')
    return "\n".join(rows)

def render_blackouts(dates, ranges, wfh_dates=None, wfh_ranges=None):
    if not dates and not ranges and not wfh_dates and not wfh_ranges:
        return '<p class="empty">No active skips or WFH days.</p>'
    rows = []
    for d in dates:
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        reason = html.escape(str(d.get("reason", "")))
        days_str = f' ({d["days"]}d)' if d.get("days") and d["days"] != 1 else ""
        cancel_url = f'/action/cancel-leave/{d["date"]}' if d.get("leave_type") else f'/action/cancel-skip/{d["date"]}'
        rows.append(
            f'<div class="bl-row"><div><strong>{dt.strftime("%a %b %d")}</strong> - {reason}{days_str}</div>'
            f'<a class="btn sm danger" href="{cancel_url}">Cancel</a></div>')
    for r in ranges:
        range_start = html.escape(str(r["start"]), quote=True)
        range_end = html.escape(str(r["end"]), quote=True)
        range_reason = html.escape(str(r.get("reason", "")))
        rows.append(
            f'<div class="bl-row"><div><strong>{range_start} to {range_end}</strong> - {range_reason}</div>'
            f'<a class="btn sm danger" href="/action/cancel-range/{range_start}/{range_end}">Cancel</a></div>')
    if wfh_dates:
        for d in wfh_dates:
            dt = datetime.strptime(d["date"], "%Y-%m-%d")
            reason = html.escape(str(d.get("reason", "Work from home")))
            rows.append(
                f'<div class="bl-row wfh-row"><div><span class="wfh-badge">WFH</span> '
                f'<strong>{dt.strftime("%a %b %d")}</strong> - {reason}</div>'
                f'<a class="btn sm danger" href="/action/cancel-wfh/{d["date"]}">Cancel</a></div>')
    if wfh_ranges:
        for r in wfh_ranges:
            rs = html.escape(str(r["start"]), quote=True)
            re_ = html.escape(str(r["end"]), quote=True)
            reason = html.escape(str(r.get("reason", "Work from home")))
            days_str = f' ({r["days"]}d)' if r.get("days") else ""
            rows.append(
                f'<div class="bl-row wfh-row"><div><span class="wfh-badge">WFH</span> '
                f'<strong>{rs} to {re_}</strong> - {reason}{days_str}</div>'
                f'<a class="btn sm danger" href="/action/cancel-wfh-range/{rs}/{re_}">Cancel</a></div>')
    return "\n".join(rows)

def render_leave_balance(lb):
    if not lb:
        return '<p class="empty">Leave balance not configured.</p>'
    rows = []
    types = [
        ("casual", "Casual", lb.get("casual", {})),
        ("sick", "Sick", lb.get("sick", {})),
        ("earned", "Earned", lb.get("earned", {})),
    ]
    for key, label, data in types:
        annual = data.get("annual", 0)
        remaining = data.get("remaining", 0)
        used = annual - remaining
        pct = int((used / annual) * 100) if annual > 0 else 0
        bar_color = "var(--green)" if pct < 60 else ("var(--orange,#e67e22)" if pct < 85 else "var(--red,#e74c3c)")
        r_fmt = int(remaining) if remaining == int(remaining) else remaining
        a_fmt = int(annual) if annual == int(annual) else annual
        rows.append(
            f'<div class="lb-row">'
            f'<div class="lb-info"><strong>{label}</strong>'
            f'<span class="lb-nums">{r_fmt}/{a_fmt}</span></div>'
            f'<div class="lb-bar"><div class="lb-fill" style="width:{pct}%;background:{bar_color}"></div></div>'
            f'</div>')
    carried = lb.get("earned", {}).get("carried_forward", 0)
    if carried > 0:
        rows.append(f'<div class="lb-note">+{carried} earned carried forward</div>')
    return "\n".join(rows)


def render_notif_prefs():
    notif_prefs = load_notif_prefs()
    prefs = notif_prefs.get("preferences", {})
    admin_email = str(notif_prefs.get("admin_email", ""))
    labels = {
        "timein_success": "Time-In Success",
        "timeout_success": "Time-Out Success",
        "skip_day": "Skip Day",
        "failure": "Failure Alert",
        "tomorrow_plan": "Tomorrow's Plan",
        "tomorrow_holiday": "Holiday Tomorrow",
        "holiday_reminder": "Holiday Reminder (3-day)",
        "deadman_switch": "Dead Man's Switch",
        "holiday_change": "Holiday Changes",
    }
    rows = []
    for key, label in labels.items():
        checked = ' checked' if prefs.get(key, True) else ''
        rows.append(f'<label class="pref-row"><input type="checkbox" name="{key}"{checked}> {label}</label>')
    return "\n".join(rows), admin_email


# --- Main dashboard ---

@app.route("/")
def dashboard():
    try:
        from manage_holidays import auto_refresh
        auto_refresh()
    except Exception:
        pass
    # Cloud pull is deliberately NOT automatic here - it's a remote-wins
    # overwrite with no real conflict detection for most files, so doing it
    # on every page load (including the 60s auto-refresh) could silently
    # discard local changes. Use the "Sync Now" button for an explicit pull.
    status = load_json(STATUS_FILE)
    config = load_config()
    upcoming = get_upcoming_holidays()
    bl_dates, bl_ranges, wfh_dates, wfh_ranges = get_active_blackouts()
    workdays = get_next_workdays(7)
    today = pk_now().strftime("%Y-%m-%d")
    now = pk_now().strftime("%H:%M")
    msg = request.args.get("msg", "")
    is_error = any(w in msg.lower() for w in ["already", "cannot"]) if msg else False
    toast_html = ""
    if msg:
        cls = "toast-error" if is_error else "toast-ok"
        toast_html = f'<div class="toast {cls}" id="toast">{html.escape(msg)}</div>'
    ti = status.get("timein", {})
    to = status.get("timeout", {})
    ti_done = ti.get("date") == today and ti.get("status") == "success"
    to_done = to.get("date") == today and to.get("status") == "success"
    notif_rows, admin_email = render_notif_prefs()
    ti_failed = ti.get("date") == today and ti.get("status") == "failed"
    to_failed = to.get("date") == today and to.get("status") == "failed"
    admin_first = admin_email.split("@")[0].split(".")[0].capitalize() if admin_email else "Admin"
    admin_email_attr = html.escape(admin_email, quote=True)
    admin_first_attr = html.escape(admin_first, quote=True)
    email_btn = ""
    if (ti_failed or to_failed) and admin_email:
        fail_mode = "Time-In" if ti_failed else "Time-Out"
        email_btn = (f'<div class="email-card"><p>{fail_mode} failed today. Use the correction form below to notify admin.</p></div>')
    email_action = ""
    if admin_email:
        default_min = random.randint(35, 59)
        default_time = f"08:{default_min:02d}"
        email_action = (
            f'<div class="card" id="corr-card">'
            f'<div class="card-title">Request Correction Email</div>'
            f'<div class="corr-form">'
            f'<div class="corr-row"><label class="corr-label">Arrival Time</label>'
            f'<input type="time" id="corr-time" value="{default_time}" class="input" style="width:auto;flex:1"></div>'
            f'<div class="corr-row"><label class="corr-label">Reason</label>'
            f'<select id="corr-reason" class="input" style="flex:1">'
            f'<option value="system">Time-In not recorded correctly</option>'
            f'<option value="forgot">Forgot to mark Time-In</option>'
            f'<option value="meeting">Was in a meeting</option>'
            f'<option value="forgot_to">Forgot to mark Time-Out</option>'
            f'</select></div>'
            f'<button class="btn full outline" onclick="composeEmail()" style="margin-top:.5rem">Compose Correction Email</button>'
            f'</div>'
            f'<input type="hidden" id="corr-email" value="{admin_email_attr}">'
            f'<input type="hidden" id="corr-name" value="{admin_first_attr}">'
            f'<input type="hidden" id="corr-date" value="{today}">'
            f'</div>')
    timeout_corr = (
        f'<div class="card" id="to-corr-card">'
        f'<div class="card-title">Correct Attendance</div>'
        f'<p style="color:var(--text2);font-size:.85rem;margin-bottom:.5rem">Fix time-in or time-out for any date. Leave a field blank to keep existing value.</p>'
        f'<div class="corr-form">'
        f'<form action="/action/correct-attendance" method="POST">'
        f'<div class="corr-row"><label class="corr-label">Date</label>'
        f'<input type="date" name="date" value="{today}" class="input" style="width:auto;flex:1"></div>'
        f'<div class="corr-row"><label class="corr-label">Correct Time-In</label>'
        f'<input type="time" name="timein" class="input" style="width:auto;flex:1"></div>'
        f'<div class="corr-row"><label class="corr-label">Correct Time-Out</label>'
        f'<input type="time" name="timeout" class="input" style="width:auto;flex:1"></div>'
        f'<div class="corr-row" style="align-items:center"><input type="checkbox" name="timeout_next_day" id="timeout_next_day" style="width:auto;margin-right:.5rem">'
        f'<label for="timeout_next_day" class="corr-label" style="font-size:.85rem">Time-Out was after midnight (next day)</label></div>'
        f'<button type="submit" class="btn full outline" style="margin-top:.5rem">Save Correction</button>'
        f'</form></div></div>')
    lb = config.get("leave_balance", {})
    leave_balance_html = render_leave_balance(lb)
    is_paused = config.get('paused', False)
    return DASHBOARD_HTML.format(
        toast_html=toast_html, today=today, now=now,
        ti_class="done" if ti_done else "none",
        ti_time=(ti.get("action_time") or ti.get("observed_time") or "-") if ti_done else "-",
        ti_meta=(today + (" (pre-existing)" if ti.get("action_origin") == "preexisting" else "")) if ti_done else "",
        to_class="done" if to_done else "none",
        to_time=(to.get("action_time") or to.get("observed_time") or "-") if to_done else "-",
        to_meta=(today + (" (pre-existing)" if to.get("action_origin") == "preexisting" else "")) if to_done else ("Pending" if ti_done else ""),
        ti_btn="disabled" if ti_done else "",
        to_btn="disabled" if to_done else "",
        workdays_html=render_workdays(workdays), holidays_html=render_holidays(upcoming),
        blackout_html=render_blackouts(bl_dates, bl_ranges, wfh_dates, wfh_ranges),
        user_id=html.escape(str(config.get("credentials", {}).get("user_id", "?")), quote=True),
        password="",
        ti_start=config['timein']['window_start'], ti_end=config['timein']['window_end'],
        to_start=config['timeout']['window_start'], to_end=config['timeout']['window_end'],
        notif_rows=notif_rows, admin_email=html.escape(admin_email, quote=True),
        email_btn=email_btn, email_action=email_action, timeout_corr=timeout_corr,
        portal_user=html.escape(str(config.get("portal", {}).get("username", "")), quote=True),
        portal_pass="",
        gh_repo=html.escape(str(config.get("cloud_sync", {}).get("github", {}).get("repo", "")), quote=True),
        gh_token="",
        paused_class="paused" if is_paused else "",
        pause_label="Resume Bot" if is_paused else "Pause Bot",
        pause_icon="&#9654;" if is_paused else "&#9208;",
        leave_balance_html=leave_balance_html,
        leave_year=lb.get('year',2026),
        lb_casual_annual=lb.get('casual',{}).get('annual',5),
        lb_casual_remaining=lb.get('casual',{}).get('remaining',5),
        lb_sick_annual=lb.get('sick',{}).get('annual',30),
        lb_sick_remaining=lb.get('sick',{}).get('remaining',30),
        lb_earned_annual=lb.get('earned',{}).get('annual',23),
        lb_earned_remaining=lb.get('earned',{}).get('remaining',23),
        lb_carry_limit=lb.get('earned',{}).get('carry_forward_limit',5),
        lb_carried=lb.get('earned',{}).get('carried_forward',0),
        paused_banner='<div class="paused-banner">BOT PAUSED &mdash; No attendance will be marked</div>' if is_paused else "",
        clock_row=_clock_row(),
    )
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Attendance Management</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/static/favicon.ico" type="image/x-icon">
<meta name="theme-color" content="#2d5f2e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--bg:#f7f7f5;--surface:#fff;--text:#1a1a18;--text2:#666;--accent:#2d5f2e;--accent-light:#e8f0e8;--border:#e5e5e0;--ok:#2d5f2e;--ok-bg:#e8f0e8;--fail:#9b2c2c;--fail-bg:#fdeaea;--skip:#7a6b2e;--skip-bg:#fdf6e8;--warn:#9b6b17;--tab-bg:#eeeee9}}
@media(prefers-color-scheme:dark){{:root{{--bg:#141413;--surface:#1e1e1c;--text:#e8e8e5;--text2:#999;--accent:#5fa960;--accent-light:#1c2e1c;--border:#333;--ok:#5fa960;--ok-bg:#1c2e1c;--fail:#d45555;--fail-bg:#2e1a1a;--skip:#c4a84a;--skip-bg:#2a2312;--warn:#d4a040;--tab-bg:#252523}}}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,system-ui,sans-serif;font-size:15px;line-height:1.5;padding:0 0 5rem}}
.header{{background:var(--accent);color:#fff;padding:1rem 1rem .7rem;text-align:center;position:relative;z-index:10}}
.header h1{{font-size:1.1rem;font-weight:700;letter-spacing:.02em;line-height:1.2}}
.header .sub{{font-size:.8rem;opacity:.8;margin-top:.15rem}}
.header-row{{display:flex;align-items:center;justify-content:center;gap:.5rem;flex-wrap:nowrap}}
.header-logo{{height:30px;border-radius:4px;flex-shrink:0}}
.container{{max-width:480px;margin:0 auto;padding:.75rem}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1rem;margin-bottom:.75rem}}
.card-title{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:.6rem}}
details.collapsible > summary {{list-style:none;}}
details.collapsible > summary::-webkit-details-marker {{display:none;}}
summary.collapsible-title {{cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between;}}
summary.collapsible-title::after {{content:"\25B6";font-size:.6rem;color:var(--text2);transition:transform .2s;}}
details.collapsible[open] > summary.collapsible-title::after {{transform:rotate(90deg);}}

.status-grid{{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}}
.status-box{{background:var(--bg);border-radius:8px;padding:.7rem;text-align:center}}
.status-box .label{{font-size:.7rem;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}}
.status-box .time{{font-size:1.6rem;font-weight:700;margin:.2rem 0}}
.status-box .meta{{font-size:.75rem;color:var(--text2)}}
.status-box.ok .time{{color:var(--ok)}}.status-box.done .time{{color:var(--ok)}}.status-box.done{{background:var(--ok-bg)}}.status-box.fail .time{{color:var(--fail)}}.status-box.skip .time{{color:var(--skip)}}.status-box.none .time{{color:var(--text2)}}
.quick-actions{{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}}
.clock-ok{{margin-top:.6rem;font-size:.7rem;color:var(--text2);text-align:center}}
.clock-warn{{margin-top:.6rem;font-size:.75rem;line-height:1.4;color:#7a2e00;background:#fff4e5;border:1px solid #ffb870;border-radius:8px;padding:.55rem .7rem}}
.btn{{display:inline-flex;align-items:center;justify-content:center;padding:.6rem .8rem;border-radius:8px;background:var(--accent);color:#fff;text-decoration:none;font-size:.85rem;font-weight:600;border:none;cursor:pointer;text-align:center}}
.btn.outline{{background:transparent;color:var(--accent);border:1.5px solid var(--accent)}}
.btn.danger{{background:var(--fail);color:#fff}}.btn.sm{{padding:.35rem .6rem;font-size:.75rem;border-radius:6px}}.btn.full{{width:100%}}.btn.disabled{{opacity:.4;pointer-events:none;cursor:default}}
.wd-row{{display:flex;align-items:center;gap:.5rem;padding:.4rem 0;border-bottom:1px solid var(--border)}}.wd-row:last-child{{border-bottom:none}}
.wd-day{{font-weight:600;width:3.2rem;font-size:.85rem}}.wd-row.wd-today{{background:var(--ok-bg);border-radius:6px;padding-left:.4rem;padding-right:.4rem}}.wd-row.wd-today .wd-day{{color:var(--accent)}}.wd-date{{flex:1;font-size:.85rem;color:var(--text2)}}
.badge{{font-size:.65rem;font-weight:700;padding:.15rem .45rem;border-radius:4px;text-transform:uppercase;letter-spacing:.04em}}
.badge.ok{{background:var(--ok-bg);color:var(--ok)}}.badge.fail{{background:var(--fail-bg);color:var(--fail)}}.badge.skip{{background:var(--skip-bg);color:var(--skip)}}
.hol-row{{display:flex;justify-content:space-between;align-items:center;padding:.6rem 0;border-bottom:1px solid var(--border);gap:.5rem}}.hol-row:last-child{{border-bottom:none}}
.hol-info strong{{font-size:.9rem}}.hol-date{{font-size:.75rem;color:var(--text2)}}.hol-actions{{display:flex;gap:.3rem;flex-shrink:0;flex-wrap:wrap}}.moon{{color:var(--warn)}}
.bl-row{{display:flex;justify-content:space-between;align-items:center;padding:.5rem 0;border-bottom:1px solid var(--border)}}.bl-row:last-child{{border-bottom:none}}.bl-row div{{font-size:.85rem}}
.wfh-row{{border-left:3px solid var(--wfh-color,#2196F3);padding-left:.5rem}}.wfh-badge{{background:var(--wfh-color,#2196F3);color:#fff;font-size:.65rem;font-weight:700;padding:.1rem .4rem;border-radius:3px;text-transform:uppercase;letter-spacing:.03em;margin-right:.3rem;vertical-align:middle}}.wfh-clock{{display:flex;gap:.5rem;margin-top:.5rem;align-items:center}}.wfh-clock .btn{{flex:1}}.wfh-clock-status{{font-size:.8rem;color:var(--text2);margin-top:.3rem}}
.empty{{color:var(--text2);font-size:.85rem;font-style:italic}}.refresh{{text-align:center;padding:.5rem;font-size:.75rem;color:var(--text2)}}
.cred-form .form-row{{margin-bottom:.5rem}}
.cred-form label{{display:block;font-size:.75rem;font-weight:600;color:var(--text2);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.04em}}
.input{{width:100%;padding:.5rem .6rem;border:1.5px solid var(--border);border-radius:6px;font-size:.9rem;background:var(--bg);color:var(--text);font-family:inherit}}.input:focus{{outline:none;border-color:var(--accent)}}
.pw-wrap{{display:flex;gap:.4rem}}.pw-wrap .input{{flex:1}}
.toast{{position:fixed;top:.75rem;left:50%;transform:translateX(-50%);padding:.6rem 1.2rem;border-radius:8px;font-size:.85rem;font-weight:600;z-index:200;box-shadow:0 4px 12px rgba(0,0,0,.15);animation:slidein .3s ease;max-width:90vw;text-align:center}}
.toast-ok{{background:var(--ok-bg);color:var(--ok);border:1.5px solid var(--ok)}}.toast-error{{background:var(--fail-bg);color:var(--fail);border:1.5px solid var(--fail)}}
@keyframes slidein{{from{{opacity:0;transform:translateX(-50%) translateY(-1rem)}}to{{opacity:1;transform:translateX(-50%) translateY(0)}}}}
.tab-bar{{display:flex;background:var(--tab-bg);border-bottom:2px solid var(--border);overflow-x:auto;-webkit-overflow-scrolling:touch}}
.tab-btn{{flex:1;padding:.7rem .2rem;text-align:center;font-size:.72rem;font-weight:600;color:var(--text2);background:none;border:none;cursor:pointer;position:relative;white-space:nowrap;letter-spacing:.02em;min-width:0}}
.tab-btn.active{{color:var(--accent)}}.tab-btn.active::after{{content:'';position:absolute;bottom:-2px;left:10%;right:10%;height:2.5px;background:var(--accent);border-radius:2px}}
.tab-panel{{display:none}}.tab-panel.active{{display:block}}
.pref-row{{display:flex;align-items:center;gap:.4rem;font-size:.85rem;padding:.3rem 0;cursor:pointer}}
.pref-row input[type=checkbox]{{width:1.1rem;height:1.1rem;accent-color:var(--accent)}}
.chart-wrap{{overflow-x:auto;padding-bottom:.3rem}}.chart-area{{display:flex;align-items:flex-end;gap:3px;min-height:140px;position:relative;padding-top:1.2rem}}
.chart-bar-col{{display:flex;flex-direction:column;align-items:center;min-width:22px;flex:1}}.chart-bar{{border-radius:3px 3px 0 0;min-width:18px;transition:height .3s}}.chart-lbl{{font-size:.55rem;color:var(--text2);margin-top:2px;white-space:nowrap}}
.chart-target{{position:absolute;left:0;right:0;border-top:2px dashed var(--fail);opacity:.5}}.chart-target-label{{position:absolute;right:0;font-size:.6rem;color:var(--fail);top:-1rem}}
.summary-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem}}.summary-box{{background:var(--bg);border-radius:8px;padding:.5rem;text-align:center}}.summary-box .val{{font-size:1.3rem;font-weight:700;color:var(--accent)}}.summary-box .lbl{{font-size:.65rem;color:var(--text2);text-transform:uppercase}}
.ana-table{{width:100%;font-size:.8rem;border-collapse:collapse}}.ana-table th{{text-align:left;font-size:.7rem;color:var(--text2);padding:.3rem .4rem;border-bottom:2px solid var(--border)}}.ana-table td{{padding:.3rem .4rem;border-bottom:1px solid var(--border)}}.ana-table .over{{color:var(--ok);font-weight:600}}.ana-table .under{{color:var(--fail);font-weight:600}}
.view-toggle{{display:inline-flex;gap:2px;background:var(--tab-bg);border-radius:6px;padding:2px;margin-top:.4rem}}.view-toggle .vbtn{{padding:.25rem .5rem;font-size:.7rem;font-weight:600;border:none;background:transparent;color:var(--text2);border-radius:4px;cursor:pointer}}.view-toggle .vbtn.active{{background:var(--accent);color:#fff}}
.email-card{{background:var(--fail-bg);border:1.5px solid var(--fail);border-radius:10px;padding:.75rem;margin-bottom:.75rem;text-align:center}}.email-card p{{font-size:.8rem;color:var(--fail);margin-bottom:.5rem;font-weight:600}}
.corr-form{{display:flex;flex-direction:column;gap:.4rem}}.corr-row{{display:flex;align-items:center;gap:.5rem}}.corr-label{{font-size:.75rem;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;min-width:5.5rem;flex-shrink:0}}
.paused-banner{{background:var(--fail);color:#fff;text-align:center;padding:.5rem;font-size:.8rem;font-weight:700;letter-spacing:.05em;animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.7}}}}
.pause-btn{{display:flex;align-items:center;justify-content:center;gap:.4rem;padding:.6rem;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;border:2px solid var(--fail);background:transparent;color:var(--fail);width:100%}}
.pause-btn.paused{{border-color:var(--ok);color:var(--ok)}}
.ajax-loading{{opacity:.6;pointer-events:none}}
.lb-row{{margin-bottom:.6rem}}
.lb-info{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:.2rem}}
.lb-info strong{{font-size:.8rem}}
.lb-nums{{font-size:.75rem;color:var(--text2)}}
.lb-bar{{height:6px;background:var(--bg2);border-radius:3px;overflow:hidden}}
.lb-fill{{height:100%;border-radius:3px;transition:width .3s}}
.lb-note{{font-size:.7rem;color:var(--text2);margin-top:.3rem}}
.modal-overlay{{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:1000;display:flex;align-items:center;justify-content:center;padding:1rem}}
.modal-box{{background:var(--bg);border-radius:12px;padding:1.2rem;width:100%;max-width:340px;box-shadow:0 8px 30px rgba(0,0,0,.3)}}
.modal-title{{font-size:1rem;font-weight:700;margin-bottom:.3rem}}
.modal-date{{font-size:.85rem;color:var(--text2);margin-bottom:.8rem}}
.modal-actions{{display:flex;flex-direction:column;gap:.4rem;margin-top:.8rem}}
</style></head><body>{toast_html}
<div class="header"><div class="header-row"><img src="/static/logo.png" alt="AKU" class="header-logo" onerror="this.src='/static/logo.svg';this.onerror=function(){{this.style.display='none'}}"><h1>Attendance Management</h1></div><div class="sub">{today} &middot; <span id="live-clock">{now}</span></div></div>
{paused_banner}
<div class="tab-bar">
  <button class="tab-btn active" data-tab="home">Home</button>
  <button class="tab-btn" data-tab="holidays">Leave / WFH</button>
  <button class="tab-btn" data-tab="analytics">Analytics</button>
  <button class="tab-btn" data-tab="settings">Settings</button>
</div><div class="container">
  <div class="tab-panel active" id="tab-home">
    <div class="card"><div class="card-title">Today's Status</div>
      <div class="status-grid">
        <div class="status-box {ti_class}"><div class="label">Time-In</div><div class="time">{ti_time}</div><div class="meta">{ti_meta}</div></div>
        <div class="status-box {to_class}"><div class="label">Time-Out</div><div class="time">{to_time}</div><div class="meta">{to_meta}</div></div>
      </div>{clock_row}</div>
    <div class="card"><div class="card-title">Today's Actions</div>
      <div class="quick-actions">
        <a class="btn full {ti_btn}" style="background:var(--ok)" href="/action/timein-now" onclick="return confirm('Run Time-In now?')">Time In Now</a>
        <a class="btn full {to_btn}" style="background:var(--warn,#b8860b)" href="/action/timeout-now" onclick="return confirm('Run Time-Out now?')">Time Out Now</a>
        <a class="btn full outline" data-no-ajax href="/action/sync-portal" onclick="return confirm('Check portal for today\'s attendance?')" style="margin-top:.5rem;font-size:.85rem">Sync from Portal</a>
      </div></div>
    {email_btn}
    {email_action}
        {timeout_corr}
    <div class="card"><div class="card-title">Tomorrow's Actions</div>
      <div class="quick-actions">
        <a class="btn full" href="#" onclick="return ajaxAction('/action/skip-tomorrow',this)">Skip Tomorrow</a>
        <a class="btn full outline" href="#" onclick="return ajaxAction('/action/cancel-skip-tomorrow',this)">Cancel Skip</a>
      </div></div>
    <div class="card">
      <button class="pause-btn {paused_class}" id="pause-toggle" onclick="togglePause()">
        <span id="pause-icon">{pause_icon}</span> <span id="pause-label">{pause_label}</span>
      </button>
    </div>
    <div class="card"><div class="card-title">Next 7 Days</div>{workdays_html}</div>
    <div class="card"><div class="card-title">Leave Balance ({leave_year})</div>{leave_balance_html}</div>
    <div class="card"><div class="card-title">Upcoming Holidays</div>{holidays_html}</div>
    <div class="card" style="display:flex;align-items:center;justify-content:space-between">
      <div><div class="card-title" style="margin-bottom:0">Quick Settings</div></div>
      <div style="display:flex;gap:.4rem;flex-wrap:wrap">
        <button class="btn sm outline" onclick="showTab('settings')">Credentials &amp; Settings</button>
        <a class="btn sm outline" href="/action/push-to-cloud" onclick="this.classList.add('ajax-loading')">Push to Cloud</a>
      </div>
    </div>
  </div>
  <div class="tab-panel" id="tab-holidays">
    <div class="card"><div class="card-title">Active Skips</div>{blackout_html}</div>
    <div class="card" id="wfh-clock-card" style="display:none">
      <div class="card-title" style="color:var(--wfh-color,#2196F3)">WFH Hours <span style="font-size:.7rem;font-weight:400;color:var(--text2)">(analytics only)</span></div>
      <div id="wfh-clock-body">
        <div style="display:flex;gap:.5rem;margin-bottom:.5rem">
          <form action="/action/wfh-record-now" method="POST" style="flex:1"><input type="hidden" name="mode" value="timein"><button type="submit" class="btn full" style="background:var(--wfh-color,#2196F3);color:#fff">Record Time In Now</button></form>
          <form action="/action/wfh-record-now" method="POST" style="flex:1"><input type="hidden" name="mode" value="timeout"><button type="submit" class="btn full outline">Record Time Out Now</button></form>
        </div>
        <form action="/action/wfh-hours" method="POST">
          <div style="display:flex;gap:.5rem;align-items:flex-end;margin-bottom:.5rem">
            <div style="flex:1"><label style="font-size:.75rem;color:var(--text2)">Or pick Time In</label><input type="time" name="wfh_ti" id="wfh-ti-time" value="09:00" class="input"></div>
            <div style="flex:1"><label style="font-size:.75rem;color:var(--text2)">Time Out</label><input type="time" name="wfh_to" id="wfh-to-time" value="18:00" class="input"></div>
          </div>
          <button type="submit" class="btn full outline" id="wfh-save-btn">Save Custom Hours</button>
        </form>
        <div class="wfh-clock-status" id="wfh-clock-status-text"></div>
      </div>
    </div>
    <div class="card"><div class="card-title" style="color:var(--wfh-color,#2196F3)">Work From Home</div>
      <form action="/action/add-wfh" method="POST" class="cred-form">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-bottom:.4rem">
          <div><label for="wfh_start">Start Date</label><input type="date" id="wfh_start" name="wfh_start" class="input" required></div>
          <div><label for="wfh_end">End Date</label><input type="date" id="wfh_end" name="wfh_end" class="input" placeholder="Same as start if blank"></div>
        </div>
        <div style="margin-bottom:.4rem">
          <label for="wfh_reason">Reason (optional)</label><input type="text" id="wfh_reason" name="wfh_reason" class="input" placeholder="Work from home">
        </div>
        <button type="submit" class="btn full" style="margin-top:.3rem;background:var(--wfh-color,#2196F3)">Mark WFH</button>
      </form>
      <p style="font-size:.75rem;color:var(--text2);margin-top:.6rem">WFH does NOT deduct from leave balance. Bot will skip attendance marking on WFH days.</p>
    </div>
    <div class="card"><div class="card-title">Add Leave</div>
      <form action="/action/add-leave" method="POST" class="cred-form">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-bottom:.4rem">
          <div><label for="leave_start">Start Date</label><input type="date" id="leave_start" name="leave_start" class="input" required></div>
          <div><label for="leave_end">End Date</label><input type="date" id="leave_end" name="leave_end" class="input" placeholder="Same as start if blank"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-bottom:.4rem">
          <div><label for="leave_type">Leave Type</label><select id="leave_type" name="leave_type" class="input"><option value="casual">Casual</option><option value="sick">Sick</option><option value="earned">Earned</option><option value="other">Other / Travel</option></select></div>
          <div><label for="leave_reason">Reason (optional)</label><input type="text" id="leave_reason" name="leave_reason" class="input" placeholder="Auto-filled from type"></div>
        </div>
        <button type="submit" class="btn full" style="margin-top:.3rem">Add Leave</button>
      </form>
      <p style="font-size:.75rem;color:var(--text2);margin-top:.6rem">Balance is deducted automatically. Weekends and holidays in a range are excluded from the count.</p></div>
    <div class="card"><div class="card-title">Add Holiday</div>
      <form action="/action/add-holiday" method="POST" class="cred-form">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-bottom:.4rem">
          <div><label for="hol_date">Date</label><input type="date" id="hol_date" name="hol_date" class="input" required></div>
          <div><label for="hol_label">Name</label><input type="text" id="hol_label" name="hol_label" class="input" placeholder="e.g. Eid Day 4" required></div>
        </div>
        <div style="display:flex;gap:.5rem;align-items:center">
          <label style="display:flex;align-items:center;gap:.3rem;font-size:.8rem;color:var(--text2);margin:0"><input type="checkbox" name="hol_moon"> Moon-dependent</label>
          <button type="submit" class="btn sm" style="margin-left:auto">Add Holiday</button>
        </div></form></div>
  </div>  <div class="tab-panel" id="tab-analytics">
    <div class="card"><div class="card-title">Date Range</div>
      <div style="display:flex;gap:.4rem;align-items:end;flex-wrap:wrap">
        <div style="flex:1;min-width:120px"><label class="cred-form" style="display:block;font-size:.7rem;font-weight:600;color:var(--text2);text-transform:uppercase;margin-bottom:.2rem">From</label><input type="date" id="ana-start" class="input"></div>
        <div style="flex:1;min-width:120px"><label class="cred-form" style="display:block;font-size:.7rem;font-weight:600;color:var(--text2);text-transform:uppercase;margin-bottom:.2rem">To</label><input type="date" id="ana-end" class="input"></div>
        <button class="btn sm" onclick="loadAnalytics()" style="margin-bottom:1px">Go</button>
      </div>
      <div style="display:flex;gap:.3rem;margin-top:.5rem;flex-wrap:wrap">
        <button class="btn sm outline" onclick="setRange(7)">7 Days</button>
        <button class="btn sm outline" onclick="setRange(30)">30 Days</button>
        <button class="btn sm outline" onclick="setRange(90)">90 Days</button>
        <button class="btn sm outline" onclick="setRange(365)">1 Year</button>
        <a class="btn sm outline" id="ana-download" href="#" style="margin-left:auto">Export CSV</a>
      </div>
      <div class="view-toggle" style="margin-top:.5rem">
        <button class="vbtn active" onclick="switchView('daily')">Daily</button>
        <button class="vbtn" onclick="switchView('weekly')">Weekly</button>
        <button class="vbtn" onclick="switchView('monthly')">Monthly</button>
      </div></div>
    <div id="ana-summary" class="card" style="display:none">
      <div class="card-title">Summary</div><div id="leave-summary" style="display:none"></div>
      <div class="summary-grid" id="ana-cards"></div></div>
    <div id="ana-chart-card" class="card" style="display:none">
      <div class="card-title">Daily Hours vs Target (9h 10m)</div>
      <div class="chart-wrap"><div class="chart-area" id="ana-chart"></div></div></div>
    <div id="ana-table-card" class="card" style="display:none">
      <div class="card-title">Detail Log</div>
      <div style="overflow-x:auto"><table class="ana-table" id="ana-table"><thead><tr><th>Date</th><th>Day</th><th>In</th><th>Out</th><th>Hours</th><th>+/-</th></tr></thead><tbody id="ana-tbody"></tbody><tfoot id="ana-tfoot" style="font-weight:700;border-top:2px solid var(--border)"></tfoot></table></div></div>
  </div>
  <div class="tab-panel" id="tab-settings">
<div class="card"><div class="card-title">Login Credentials</div>
      <form action="/action/update-credentials" method="POST" class="cred-form">
        <div class="form-row"><label for="user_id">User ID</label><input type="text" id="user_id" name="user_id" value="{user_id}" class="input"></div>
        <div class="form-row"><label for="password">Password</label>
          <div class="pw-wrap"><input type="password" id="password" name="password" value="{password}" class="input" placeholder="Enter a new password">
            <button type="button" class="btn sm outline" onclick="var p=document.getElementById('password');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'Show':'Hide'">Show</button></div></div>
        <button type="submit" class="btn full" style="margin-top:.5rem">Save Credentials</button></form>
      <p style="font-size:.75rem;color:var(--text2);margin-top:.6rem">These credentials are used by the bot to log in to the Time In / Time Out page.</p></div>
    <details class="card collapsible"><summary class="card-title collapsible-title">Portal Login (one.aku.edu)</summary>
      <form action="/action/update-portal" method="POST" class="cred-form">
        <div class="form-row"><label for="portal_user">Portal Username</label><input type="text" id="portal_user" name="portal_user" value="{portal_user}" class="input" placeholder="aly.jafferani"></div>
        <div class="form-row"><label for="portal_pass">Portal Password</label>
          <div class="pw-wrap"><input type="password" id="portal_pass" name="portal_pass" value="{portal_pass}" class="input" placeholder="Enter a new password">
            <button type="button" class="btn sm outline" onclick="var p=document.getElementById('portal_pass');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'Show':'Hide'">Show</button></div></div>
        <button type="submit" class="btn full" style="margin-top:.5rem">Save Portal Credentials</button></form>
      <p style="font-size:.75rem;color:var(--text2);margin-top:.6rem">Used to log in to one.aku.edu for remote attendance marking. Username is without @aku.edu.</p></details>
    <div class="card"><div class="card-title">Randomized Time Windows</div>
      <p style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">The bot picks a random time within each range. 85% of the time it lands in the first 75% of the window (primary zone), keeping it natural.</p>
      <form action="/action/update-windows" method="POST" class="cred-form">
        <div style="margin-bottom:.75rem"><div style="font-size:.8rem;font-weight:700;margin-bottom:.3rem">Time-In Window</div>
          <div style="display:flex;align-items:center;gap:.4rem">
            <input type="time" name="ti_start" value="{ti_start}" class="input" style="flex:1;padding:.45rem .5rem" required>
            <span style="font-size:.8rem;color:var(--text2)">to</span>
            <input type="time" name="ti_end" value="{ti_end}" class="input" style="flex:1;padding:.45rem .5rem" required></div></div>
        <div style="margin-bottom:.75rem"><div style="font-size:.8rem;font-weight:700;margin-bottom:.3rem">Time-Out Window</div>
          <div style="display:flex;align-items:center;gap:.4rem">
            <input type="time" name="to_start" value="{to_start}" class="input" style="flex:1;padding:.45rem .5rem" required>
            <span style="font-size:.8rem;color:var(--text2)">to</span>
            <input type="time" name="to_end" value="{to_end}" class="input" style="flex:1;padding:.45rem .5rem" required></div></div>
        <button type="submit" class="btn full">Save Time Windows</button></form></div>
    <div class="card"><div class="card-title">Leave Balance</div>
      <form action="/action/save-leave-balance" method="POST" class="cred-form">
        <div class="form-row"><label>Year</label><input type="number" name="lb_year" value="{leave_year}" class="input" style="width:5rem"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.4rem;margin:.5rem 0">
          <div><label style="font-size:.7rem;font-weight:600">Casual</label>
            <div style="font-size:.65rem;color:var(--text2)">Annual</div>
            <input type="number" step="0.5" name="lb_casual_annual" value="{lb_casual_annual}" class="input" style="width:100%">
            <div style="font-size:.65rem;color:var(--text2)">Remaining</div>
            <input type="number" step="0.5" name="lb_casual_remaining" value="{lb_casual_remaining}" class="input" style="width:100%"></div>
          <div><label style="font-size:.7rem;font-weight:600">Sick</label>
            <div style="font-size:.65rem;color:var(--text2)">Annual</div>
            <input type="number" step="0.5" name="lb_sick_annual" value="{lb_sick_annual}" class="input" style="width:100%">
            <div style="font-size:.65rem;color:var(--text2)">Remaining</div>
            <input type="number" step="0.5" name="lb_sick_remaining" value="{lb_sick_remaining}" class="input" style="width:100%"></div>
          <div><label style="font-size:.7rem;font-weight:600">Earned</label>
            <div style="font-size:.65rem;color:var(--text2)">Annual</div>
            <input type="number" step="0.5" name="lb_earned_annual" value="{lb_earned_annual}" class="input" style="width:100%">
            <div style="font-size:.65rem;color:var(--text2)">Remaining</div>
            <input type="number" step="0.5" name="lb_earned_remaining" value="{lb_earned_remaining}" class="input" style="width:100%"></div>
        </div>
        <div class="form-row"><label>Carry Forward Limit (Earned)</label><input type="number" name="lb_carry_limit" value="{lb_carry_limit}" class="input" style="width:4rem"></div>
        <div class="form-row"><label>Carried Forward</label><input type="number" step="0.5" name="lb_carried" value="{lb_carried}" class="input" style="width:4rem"></div>
        <button type="submit" class="btn full" style="margin-top:.5rem">Save Leave Balance</button>
      </form>
      <p style="font-size:.75rem;color:var(--text2);margin-top:.6rem">Set your current remaining leaves. The bot deducts when you mark leave days.</p></div>
    <details class="card collapsible"><summary class="card-title collapsible-title">Cloud Sync (GitHub)</summary>
      <p style="font-size:.8rem;color:var(--text2);margin-bottom:.5rem">Connect GitHub for non-secret settings, status, and fallback timing. Attendance and portal credentials remain local.</p>
      <form action="/action/update-cloud-sync" method="POST" class="cred-form">
        <div class="form-row"><label for="gh_repo">Repo (owner/name)</label><input type="text" id="gh_repo" name="gh_repo" value="{gh_repo}" class="input" placeholder="username/attendance-bot"></div>
        <div class="form-row"><label for="gh_token">Personal Access Token</label>
          <div class="pw-wrap"><input type="password" id="gh_token" name="gh_token" value="{gh_token}" class="input" placeholder="Leave blank to keep the saved token">
            <button type="button" class="btn sm outline" onclick="var p=document.getElementById('gh_token');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'Show':'Hide'">Show</button></div></div>
        <button type="submit" class="btn full" style="margin-top:.5rem">Save Cloud Sync</button>
        <a class="btn full outline" href="/action/test-cloud-sync" style="margin-top:.3rem;display:block;text-align:center">Test Sync Now</a></form></details>
    <div class="card"><div class="card-title">Notification Preferences</div>
      <p style="font-size:.8rem;color:var(--text2);margin-bottom:.5rem">Choose which notifications to receive on your phone.</p>
      <form action="/action/update-notifications" method="POST" class="cred-form">
        {notif_rows}
        <div class="form-row" style="margin-top:.75rem"><label for="admin_email">Admin Email (for correction requests)</label>
          <input type="email" id="admin_email" name="admin_email" value="{admin_email}" class="input" placeholder="admin@company.com"></div>
        <button type="submit" class="btn full" style="margin-top:.5rem">Save Notification Settings</button></form></div>
  </div>
  <div class="refresh"><a href="/">Refresh</a> &middot; Auto-refreshes every 60s</div>
<div class="modal-overlay" id="leave-modal" style="display:none" onclick="if(event.target===this)closeLeaveModal()">
  <div class="modal-box">
    <div class="modal-title">Mark Leave</div>
    <div class="modal-date" id="leave-date-label"></div>
    <input type="hidden" id="leave-date-val">
    <div class="form-row"><label>Leave Type</label>
      <select id="leave-type" class="input" onchange="toggleHalfDay()">
        <option value="casual">Casual Leave</option>
        <option value="sick">Sick Leave</option>
        <option value="earned">Earned Leave</option>
        <option value="other">Other</option>
      </select></div>
    <div class="form-row" id="half-day-row"><label class="pref-row"><input type="checkbox" id="leave-half"> Half Day (0.5)</label></div>
    <div class="modal-actions">
      <button class="btn full" onclick="confirmLeave()">Confirm Leave</button>
      <button class="btn full outline" onclick="closeLeaveModal()">Cancel</button>
    </div>
  </div>
</div>
</div><script>
  // PKT is UTC+5, no DST. Read Pakistan wall-clock even if the viewing
  // device is set to another timezone.
  function pkNow(){{var d=new Date();return new Date(d.getTime()+(d.getTimezoneOffset()+300)*60000)}}
(function(){{
  var t=document.getElementById('toast');
  if(t){{setTimeout(function(){{t.style.transition='opacity .4s';t.style.opacity='0';setTimeout(function(){{t.remove()}},400)}},3500)}}
  setInterval(function(){{
    var n=pkNow();
    var hh=String(n.getHours()).padStart(2,'0');
    var mm=String(n.getMinutes()).padStart(2,'0');
    var ss=String(n.getSeconds()).padStart(2,'0');
    var el=document.getElementById('live-clock');
    if(el)el.textContent=hh+':'+mm+':'+ss;
  }},1000);
  var saved=sessionStorage.getItem('activeTab')||'home';var params=new URLSearchParams(window.location.search);var urlTab=params.get('tab');if(urlTab)saved=urlTab;if(params.toString())history.replaceState(null,'',window.location.pathname)
  window.showTab=function(id){{
    document.querySelectorAll('.tab-panel').forEach(function(p){{p.classList.remove('active')}});
    document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active')}});
    var panel=document.getElementById('tab-'+id);
    if(panel){{panel.classList.add('active')}}
    document.querySelectorAll('.tab-btn[data-tab="'+id+'"]').forEach(function(b){{b.classList.add('active')}});
    sessionStorage.setItem('activeTab',id);
    if(id==='holidays'){{loadWfhClock();}}
  }}
  showTab(saved);
  document.querySelectorAll('.tab-btn').forEach(function(btn){{
    btn.addEventListener('click',function(){{showTab(this.getAttribute('data-tab'))}})
  }});
  if(window.history&&history.scrollRestoration)history.scrollRestoration='manual';
  var sy=sessionStorage.getItem('scrollY');
  if(sy){{window.scrollTo(0,parseInt(sy))}}
  var _formDirty=false;document.addEventListener('input',function(e){{var t=e.target;if(t.tagName==='INPUT'&&t.type!=='hidden'&&t.type!=='checkbox'||t.tagName==='TEXTAREA'){{var form=t.closest('form');if(form)_formDirty=true}}}});
  document.querySelectorAll('a[href^="/action/"]').forEach(function(a){{
    var href=a.getAttribute('href');
    var isFormAction=a.closest('form')!==null;
    var isManual=href.indexOf('timein-now')>=0||href.indexOf('timeout-now')>=0;
    if(!isFormAction&&!isManual&&!a.hasAttribute('data-no-ajax')){{
      a.addEventListener('click',function(e){{
        e.preventDefault();
        sessionStorage.setItem('scrollY',window.scrollY);
        ajaxAction(href,a);
      }});
    }} else {{
      a.addEventListener('click',function(){{sessionStorage.setItem('scrollY',window.scrollY)}});
    }}
  }});
  document.querySelectorAll('form[action^="/action/"]').forEach(function(f){{
    f.addEventListener('submit',function(){{sessionStorage.setItem('scrollY',window.scrollY)}})
  }});
  setTimeout(function(){{if(!_formDirty&&!document.querySelector('input:focus,select:focus,textarea:focus')){{sessionStorage.setItem('scrollY',window.scrollY);location.reload()}}}},60000);
  var TARGET=9.17;
  var WEEKLY_TARGET=45.83;
  var rawRecords=[];
  var currentView='daily';
  // Local (PKT) date - toISOString() is UTC and 5h behind, so before 05:00
  // it would hand back the previous day.
  function pad(d){{return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}}
  function setRange(n){{
    var end=pkNow();var start=pkNow();start.setDate(end.getDate()-n+1);
    document.getElementById('ana-start').value=pad(start);
    document.getElementById('ana-end').value=pad(end);
    loadAnalytics();
  }}
  window.setRange=setRange;
  function switchView(v){{
    currentView=v;
    document.querySelectorAll('.view-toggle .vbtn').forEach(function(b){{b.classList.remove('active')}});
    document.querySelector('.view-toggle .vbtn[onclick*="'+v+'"]').classList.add('active');
    renderAnalytics();
  }}
  window.switchView=switchView;
  function aggregateWeekly(recs){{
    var weeks={{}};
    recs.forEach(function(r){{
      var d=new Date(r.date);
      var day=d.getDay();var diff=d.getDate()-day+(day===0?-6:1);
      var mon=new Date(d);mon.setDate(diff);
      var key=pad(mon);
      if(!weeks[key])weeks[key]={{start:key,hours:0,days:0,worked:0,label:''}};
      weeks[key].days++;
      if(r.hours>0){{weeks[key].hours+=r.hours;weeks[key].worked++}}
    }});
    var result=[];
    Object.keys(weeks).sort().forEach(function(k){{
      var w=weeks[k];
      var sd=new Date(k);var ed=new Date(k);ed.setDate(ed.getDate()+6);
      w.label=sd.toLocaleDateString('en',{{month:'short',day:'numeric'}})+' - '+ed.toLocaleDateString('en',{{month:'short',day:'numeric'}});
      w.avg=w.worked>0?w.hours/w.worked:0;
      result.push(w);
    }});
    return result;
  }}
  function aggregateMonthly(recs){{
    var months={{}};
    recs.forEach(function(r){{
      var key=r.date.substring(0,7);
      if(!months[key])months[key]={{key:key,hours:0,days:0,worked:0}};
      months[key].days++;
      if(r.hours>0){{months[key].hours+=r.hours;months[key].worked++}}
    }});
    var result=[];
    Object.keys(months).sort().forEach(function(k){{
      var m=months[k];
      var d=new Date(k+'-01');
      m.label=d.toLocaleDateString('en',{{month:'long',year:'numeric'}});
      m.avg=m.worked>0?m.hours/m.worked:0;
      result.push(m);
    }});
    return result;
  }}
  function renderAnalytics(){{
    fetch('/api/leave-summary').then(function(r){{return r.json()}}).then(function(ls){{
      var el=document.getElementById('leave-summary');
      if(!el)return;
      var h='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem;margin-bottom:.8rem">';
      var types=[['casual','Casual'],['sick','Sick'],['earned','Earned']];
      types.forEach(function(t){{
        var d=ls.types[t[0]]||{{}};
        var used=d.annual-d.remaining;
        h+='<div style="text-align:center;padding:.4rem;background:var(--bg2);border-radius:6px">';
        h+='<div style="font-size:.65rem;color:var(--text2)">'+t[1]+'</div>';
        h+='<div style="font-size:1.1rem;font-weight:700">'+d.remaining+'/'+d.annual+'</div>';
        h+='<div style="font-size:.6rem;color:var(--text2)">'+used+' used</div></div>';
      }});
      h+='</div>';
      if(ls.leaves&&ls.leaves.length>0){{
        h+='<div style="font-size:.75rem;font-weight:600;margin-bottom:.3rem">Leave History</div>';
        ls.leaves.forEach(function(l){{
          var dt=l.date||l.start||'';
          h+='<div style="font-size:.7rem;padding:.2rem 0;border-bottom:1px solid var(--border)">'+dt+' — '+l.reason+(l.days==0.5?' (\xbd day)':'')+'</div>';
        }});
      }}
      el.innerHTML=h;el.style.display='';
    }}).catch(function(){{}});
    if(!rawRecords.length)return;
    var recs=rawRecords;
    if(currentView==='daily'){{renderDaily(recs);return}}
    if(currentView==='weekly'){{renderWeekly(aggregateWeekly(recs));return}}
    if(currentView==='monthly'){{renderMonthly(aggregateMonthly(recs));return}}
  }}
  function renderDaily(recs){{
    var officeWorked=recs.filter(function(r){{return r.type==='workday'&&r.hours>0}});
    var wfhWorked=recs.filter(function(r){{return r.type==='wfh'&&r.hours>0}});
    var worked=officeWorked.concat(wfhWorked);
    var wfhAll=recs.filter(function(r){{return r.type==='wfh'}}).length;
    var leaves=recs.filter(function(r){{return r.type==='leave'}}).length;
    var holidays=recs.filter(function(r){{return r.type==='holiday'}}).length;
    var missing=recs.filter(function(r){{return r.type==='missing'}}).length;
    var totalH=worked.reduce(function(a,r){{return a+r.hours}},0);
    var avgH=worked.length?totalH/worked.length:0;
    var totalOver=worked.reduce(function(a,r){{var d=r.hours-TARGET;return a+(d>0?d:0)}},0);
    var daysLabel=worked.length+(wfhWorked.length>0?' (Office: '+officeWorked.length+', WFH: '+wfhWorked.length+')':'');
    var cards='<div class="summary-box"><div class="val">'+worked.length+'</div><div class="lbl">Days Worked</div></div>'
      +'<div class="summary-box"><div class="val">'+avgH.toFixed(1)+'h</div><div class="lbl">Avg Daily</div></div>'
      +'<div class="summary-box"><div class="val">'+totalOver.toFixed(1)+'h</div><div class="lbl">Total Overtime</div></div>';
    if(wfhAll>0)cards+='<div class="summary-box"><div class="val" style="color:var(--wfh-color,#2196F3)">'+wfhAll+'</div><div class="lbl">WFH Days</div></div>';
    if(leaves>0)cards+='<div class="summary-box"><div class="val" style="color:#b45309">'+leaves+'</div><div class="lbl">Leave Days</div></div>';
    if(holidays>0)cards+='<div class="summary-box"><div class="val" style="color:var(--primary)">'+holidays+'</div><div class="lbl">Holidays</div></div>';
    if(missing>0)cards+='<div class="summary-box"><div class="val" style="color:var(--fail)">'+missing+'</div><div class="lbl">Missing</div></div>';
    document.getElementById('ana-cards').innerHTML=cards;
    document.getElementById('ana-summary').style.display='';
    var maxH=Math.max(TARGET*1.3,Math.max.apply(null,recs.map(function(r){{return r.hours||0}})));
    if(maxH<1)maxH=TARGET*1.3;
    var targetPct=(TARGET/maxH*100);
    var barsHtml='<div class="chart-target" style="bottom:'+targetPct+'%"><span class="chart-target-label">9h10m</span></div>';
    recs.forEach(function(r){{
      var tp=r.type||'workday';
      if(tp==='weekend'||tp==='future')return;
      if(tp==='holiday'){{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:3px;background:var(--primary);width:100%"></div><div class="chart-lbl" style="color:var(--primary);font-size:.45rem">H</div></div>';return}}
      if(tp==='leave'){{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:3px;background:#b45309;width:100%"></div><div class="chart-lbl" style="color:#b45309;font-size:.45rem">L</div></div>';return}}
      if(tp==='wfh'){{if(r.hours>0){{var wpct=(r.hours/maxH*100).toFixed(1);barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:'+wpct+'%;background:var(--wfh-color,#2196F3);width:100%"></div><div class="chart-lbl" style="color:var(--wfh-color,#2196F3)">W</div></div>'}}else{{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:3px;background:var(--wfh-color,#2196F3);width:100%"></div><div class="chart-lbl" style="color:var(--wfh-color,#2196F3);font-size:.45rem">W</div></div>'}}return}}
      if(r.hours<=0){{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:0;background:var(--fail);min-height:2px;width:100%"></div><div class="chart-lbl" style="color:var(--fail)">'+r.day+'</div></div>';return}}
      var pct=(r.hours/maxH*100).toFixed(1);
      var col=r.hours>=TARGET?'var(--ok)':'var(--fail)';
      barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:'+pct+'%;background:'+col+';width:100%"></div><div class="chart-lbl">'+r.day+'</div></div>';
    }});
    document.getElementById('ana-chart').innerHTML=barsHtml;
    document.getElementById('ana-chart-card').style.display='';
    var tbody='';
    recs.forEach(function(r){{
      var tp=r.type||'workday';
      if(tp==='weekend'){{
        tbody+='<tr style="color:var(--text2);opacity:.6"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-style:italic">Weekend</td><td></td></tr>';
      }}else if(tp==='holiday'){{
        tbody+='<tr style="color:var(--primary);background:rgba(59,130,246,.06)"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-weight:600">'+r.label+'</td><td></td></tr>';
      }}else if(tp==='leave'){{
        tbody+='<tr style="color:#b45309;background:rgba(234,179,8,.06)"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-weight:600">'+r.label+'</td><td></td></tr>';
      }}else if(tp==='wfh'){{
        if(r.hours>0){{
          var diff=r.hours-TARGET;var cls=diff>=0?'over':'under';var sign=diff>=0?'+':'';
          tbody+='<tr style="color:var(--wfh-color,#2196F3);background:rgba(33,150,243,.06)"><td>'+r.date+'</td><td>'+r.day+'</td><td>'+r.timein+'</td><td>'+r.timeout+'</td><td>'+r.hours.toFixed(2)+'</td><td class="'+cls+'">'+sign+diff.toFixed(2)+'h</td></tr>';
        }}else{{
          tbody+='<tr style="color:var(--wfh-color,#2196F3);background:rgba(33,150,243,.06)"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-weight:600">'+r.label+'</td><td></td></tr>';
        }}
      }}else if(tp==='future'){{
        tbody+='<tr style="color:var(--text2);opacity:.4"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-style:italic">Upcoming</td><td></td></tr>';
      }}else if(tp==='missing'){{
        tbody+='<tr style="color:var(--fail);background:rgba(239,68,68,.06)"><td>'+r.date+'</td><td>'+r.day+'</td><td colspan="3" style="text-align:center;font-weight:600">No record</td><td></td></tr>';
      }}else{{
        if(r.hours>0){{
          var diff=r.hours-TARGET;
          var cls=diff>=0?'over':'under';
          var sign=diff>=0?'+':'';
          tbody+='<tr><td>'+r.date+'</td><td>'+r.day+'</td><td>'+r.timein+'</td><td>'+r.timeout+'</td><td>'+r.hours.toFixed(2)+'</td><td class="'+cls+'">'+sign+diff.toFixed(2)+'h</td></tr>';
        }}else{{
          var partial=(r.timein!=='-'||r.timeout!=='-');
          tbody+='<tr style="'+(partial?'color:var(--warn)':'')+'"><td>'+r.date+'</td><td>'+r.day+'</td><td>'+r.timein+'</td><td>'+r.timeout+'</td><td style="font-style:italic">'+(partial?'Partial':'0.00')+'</td><td style="font-style:italic">'+(partial?'In progress':'-9.17h')+'</td></tr>';
        }}
      }}
    }});
    document.getElementById('ana-tbody').innerHTML=tbody||'<tr><td colspan="6" style="color:var(--text2);font-style:italic">No records in this range.</td></tr>';
    var totalDiff=worked.reduce(function(a,r){{return a+(r.hours-TARGET)}},0);
    var diffCls=totalDiff>=0?'over':'under';
    var diffSign=totalDiff>=0?'+':'';
    document.getElementById('ana-tfoot').innerHTML='<tr style="background:var(--bg)"><td colspan="4" style="padding:.5rem .4rem">Total ('+worked.length+' days)</td><td style="padding:.5rem .4rem">'+totalH.toFixed(1)+'h</td><td class="'+diffCls+'" style="padding:.5rem .4rem;font-size:.9rem">'+diffSign+totalDiff.toFixed(1)+'h overtime</td></tr>';
    document.getElementById('ana-table-card').style.display='';
  }}
  function renderWeekly(weeks){{
    var totalH=weeks.reduce(function(a,w){{return a+w.hours}},0);
    var totalWorked=weeks.reduce(function(a,w){{return a+w.worked}},0);
    var avgDaily=totalWorked>0?totalH/totalWorked:0;
    var totalOver=weeks.reduce(function(a,w){{var d=w.hours-WEEKLY_TARGET;return a+(d>0?d:0)}},0);
    document.getElementById('ana-cards').innerHTML='<div class="summary-box"><div class="val">'+weeks.length+'</div><div class="lbl">Weeks</div></div>'
      +'<div class="summary-box"><div class="val">'+avgDaily.toFixed(1)+'h</div><div class="lbl">Avg Daily</div></div>'
      +'<div class="summary-box"><div class="val">'+totalOver.toFixed(1)+'h</div><div class="lbl">Weekly OT</div></div>';
    document.getElementById('ana-summary').style.display='';
    var maxH=Math.max(WEEKLY_TARGET*1.3,Math.max.apply(null,weeks.map(function(w){{return w.hours||0}})));
    if(maxH<1)maxH=WEEKLY_TARGET*1.3;
    var targetPct=(WEEKLY_TARGET/maxH*100);
    var barsHtml='<div class="chart-target" style="bottom:'+targetPct+'%"><span class="chart-target-label">45h50m/wk</span></div>';
    weeks.forEach(function(w){{
      if(w.hours<=0){{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:0;background:var(--border);min-height:2px;width:100%"></div><div class="chart-lbl" style="font-size:.5rem">'+w.label+'</div></div>';return}}
      var pct=(w.hours/maxH*100).toFixed(1);
      var col=w.hours>=WEEKLY_TARGET?'var(--ok)':'var(--fail)';
      barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:'+pct+'%;background:'+col+';width:100%"></div><div class="chart-lbl" style="font-size:.5rem">'+w.label+'</div></div>';
    }});
    document.getElementById('ana-chart').innerHTML=barsHtml;
    document.getElementById('ana-chart-card').style.display='';
    var tbody='';
    weeks.forEach(function(w){{
      if(w.hours<=0)return;
      var diff=w.hours-WEEKLY_TARGET;
      var cls=diff>=0?'over':'under';
      var sign=diff>=0?'+':'';
      tbody+='<tr><td>'+w.label+'</td><td>'+w.worked+' days</td><td colspan="2">'+w.hours.toFixed(1)+'h total</td><td>'+w.avg.toFixed(2)+'h/day</td><td class="'+cls+'">'+sign+diff.toFixed(1)+'h</td></tr>';
    }});
    document.getElementById('ana-tbody').innerHTML=tbody||'<tr><td colspan="6" style="color:var(--text2);font-style:italic">No records in this range.</td></tr>';
    var totalWH=weeks.reduce(function(a,w){{return a+w.hours}},0);
    var totalWDays=weeks.reduce(function(a,w){{return a+w.worked}},0);
    var totalWDiff=totalWH-(totalWDays*TARGET);
    var wDiffCls=totalWDiff>=0?'over':'under';
    var wDiffSign=totalWDiff>=0?'+':'';
    document.getElementById('ana-tfoot').innerHTML='<tr style="background:var(--bg)"><td style="padding:.5rem .4rem">Total ('+weeks.length+' weeks)</td><td style="padding:.5rem .4rem">'+totalWDays+' days</td><td colspan="2" style="padding:.5rem .4rem">'+totalWH.toFixed(1)+'h total</td><td style="padding:.5rem .4rem">'+(totalWDays>0?(totalWH/totalWDays).toFixed(2):0)+'h/day</td><td class="'+wDiffCls+'" style="padding:.5rem .4rem;font-size:.9rem">'+wDiffSign+totalWDiff.toFixed(1)+'h overtime</td></tr>';
    document.getElementById('ana-table-card').style.display='';
  }}
  function renderMonthly(months){{
    var totalH=months.reduce(function(a,m){{return a+m.hours}},0);
    var totalWorked=months.reduce(function(a,m){{return a+m.worked}},0);
    var avgDaily=totalWorked>0?totalH/totalWorked:0;
    document.getElementById('ana-cards').innerHTML='<div class="summary-box"><div class="val">'+months.length+'</div><div class="lbl">Months</div></div>'
      +'<div class="summary-box"><div class="val">'+avgDaily.toFixed(1)+'h</div><div class="lbl">Avg Daily</div></div>'
      +'<div class="summary-box"><div class="val">'+totalH.toFixed(1)+'h</div><div class="lbl">Total Hours</div></div>';
    document.getElementById('ana-summary').style.display='';
    var maxH=Math.max(200,Math.max.apply(null,months.map(function(m){{return m.hours||0}})))*1.2;
    var barsHtml='';
    months.forEach(function(m){{
      if(m.hours<=0){{barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:0;background:var(--border);min-height:2px;width:100%"></div><div class="chart-lbl">'+m.label.split(' ')[0].substring(0,3)+'</div></div>';return}}
      var expectedH=m.worked*TARGET;
      var pct=(m.hours/maxH*100).toFixed(1);
      var col=m.hours>=expectedH?'var(--ok)':'var(--fail)';
      barsHtml+='<div class="chart-bar-col"><div class="chart-bar" style="height:'+pct+'%;background:'+col+';width:100%"></div><div class="chart-lbl">'+m.label.split(' ')[0].substring(0,3)+'</div></div>';
    }});
    document.getElementById('ana-chart').innerHTML=barsHtml;
    document.getElementById('ana-chart-card').style.display='';
    var tbody='';
    months.forEach(function(m){{
      if(m.hours<=0)return;
      var expectedH=m.worked*TARGET;
      var diff=m.hours-expectedH;
      var cls=diff>=0?'over':'under';
      var sign=diff>=0?'+':'';
      tbody+='<tr><td>'+m.label+'</td><td>'+m.worked+' days</td><td colspan="2">'+m.hours.toFixed(1)+'h total</td><td>'+m.avg.toFixed(2)+'h/day</td><td class="'+cls+'">'+sign+diff.toFixed(1)+'h</td></tr>';
    }});
    document.getElementById('ana-tbody').innerHTML=tbody||'<tr><td colspan="6" style="color:var(--text2);font-style:italic">No records in this range.</td></tr>';
    var totalMH=months.reduce(function(a,m){{return a+m.hours}},0);
    var totalMDays=months.reduce(function(a,m){{return a+m.worked}},0);
    var totalMExpected=totalMDays*TARGET;
    var totalMDiff=totalMH-totalMExpected;
    var mDiffCls=totalMDiff>=0?'over':'under';
    var mDiffSign=totalMDiff>=0?'+':'';
    document.getElementById('ana-tfoot').innerHTML='<tr style="background:var(--bg)"><td style="padding:.5rem .4rem">Total ('+months.length+' months)</td><td style="padding:.5rem .4rem">'+totalMDays+' days</td><td colspan="2" style="padding:.5rem .4rem">'+totalMH.toFixed(1)+'h total</td><td style="padding:.5rem .4rem">'+(totalMDays>0?(totalMH/totalMDays).toFixed(2):0)+'h/day</td><td class="'+mDiffCls+'" style="padding:.5rem .4rem;font-size:.9rem">'+mDiffSign+totalMDiff.toFixed(1)+'h overtime</td></tr>';
    document.getElementById('ana-table-card').style.display='';
  }}
  window.composeEmail=function(){{
    var time=document.getElementById('corr-time').value;
    var reason=document.getElementById('corr-reason').value;
    var email=document.getElementById('corr-email').value;
    var name=document.getElementById('corr-name').value;
    var date=document.getElementById('corr-date').value;
    if(!time){{var m=35+Math.floor(Math.random()*25);time='08:'+String(m).padStart(2,'0')}}
    var h=parseInt(time.split(':')[0]);var mn=time.split(':')[1];
    var ampm=h>=12?'PM':'AM';var h12=h>12?h-12:(h===0?12:h);
    var fmtTime=String(h12).padStart(2,'0')+':'+mn+' '+ampm;
    var reasons={{
      'system':'My Time-In for '+date+' was not recorded correctly by the system. I was present at the office at '+fmtTime+'.',
      'forgot':'I forgot to mark my Time-In for '+date+'. I was present at the office since '+fmtTime+'.',
      'meeting':'I was in a meeting and could not mark my Time-In for '+date+'. I arrived at the office at '+fmtTime+'.',
      'forgot_to':'I forgot to mark my Time-Out for '+date+'. I left the office at '+fmtTime+'. Could you please correct my departure time?'
    }};
    var body='Dear '+name+',%0D%0A%0D%0A'+encodeURIComponent(reasons[reason])+'%0D%0A%0D%0ACould you please update my attendance record?%0D%0A%0D%0AThank you.';
    var subject=encodeURIComponent('Attendance Correction Request - '+date);
    window.open('mailto:'+email+'?subject='+subject+'&body='+body,'_blank');
  }};
  function loadAnalytics(){{
    var s=document.getElementById('ana-start').value;
    var e=document.getElementById('ana-end').value;
    if(!s||!e)return;
    document.getElementById('ana-download').href='/download/report?start='+s+'&end='+e;
    fetch('/api/analytics?start='+s+'&end='+e).then(function(r){{return r.json()}}).then(function(data){{
      rawRecords=data.records||[];
      renderAnalytics();
    }});
  }}
  window.loadAnalytics=loadAnalytics;
  var anaStart=sessionStorage.getItem('anaStart');
  var anaEnd=sessionStorage.getItem('anaEnd');
  if(anaStart&&anaEnd){{document.getElementById('ana-start').value=anaStart;document.getElementById('ana-end').value=anaEnd;loadAnalytics()}}else{{setRange(7)}}
  document.getElementById('ana-start').addEventListener('change',function(){{sessionStorage.setItem('anaStart',this.value)}});
  document.getElementById('ana-end').addEventListener('change',function(){{sessionStorage.setItem('anaEnd',this.value)}});
  function ajaxAction(url,el){{
    if(el)el.classList.add('ajax-loading');
    fetch(url).then(function(r){{return r.text()}}).then(function(){{
      showToast('Done','ok');
      if(el)el.classList.remove('ajax-loading');
      // Only reload if nothing else on the page has unsaved input - an
      // unrelated quick action (pause, skip, holiday toggle, etc.) should
      // never wipe out something the user is mid-typing elsewhere.
      if(!_formDirty&&!document.querySelector('input:focus,select:focus,textarea:focus')){{
        setTimeout(function(){{location.reload()}},800)
      }}
    }}).catch(function(){{
      showToast('Action failed','error');
      if(el)el.classList.remove('ajax-loading');
    }});
    return false;
  }}
  window.ajaxAction=ajaxAction;
  function showToast(msg,type){{
    var old=document.getElementById('toast');if(old)old.remove();
    var t=document.createElement('div');t.id='toast';t.className='toast toast-'+(type||'ok');t.textContent=msg;
    document.body.appendChild(t);
    setTimeout(function(){{t.style.transition='opacity .4s';t.style.opacity='0';setTimeout(function(){{t.remove()}},400)}},2500);
  }}
  window.showToast=showToast;
  function openLeaveModal(dateStr){{
    var d=new Date(dateStr+'T12:00:00');
    var label=d.toLocaleDateString('en-US',{{weekday:'long',month:'short',day:'numeric',year:'numeric'}});
    document.getElementById('leave-date-label').textContent=label;
    document.getElementById('leave-date-val').value=dateStr;
    document.getElementById('leave-type').value='casual';
    document.getElementById('leave-half').checked=false;
    document.getElementById('half-day-row').style.display='';
    document.getElementById('leave-modal').style.display='flex';
  }}
  window.openLeaveModal=openLeaveModal;
  function closeLeaveModal(){{document.getElementById('leave-modal').style.display='none'}}
  window.closeLeaveModal=closeLeaveModal;
  function toggleHalfDay(){{
    var t=document.getElementById('leave-type').value;
    document.getElementById('half-day-row').style.display=(t==='casual')?'':'none';
    if(t!=='casual')document.getElementById('leave-half').checked=false;
  }}
  window.toggleHalfDay=toggleHalfDay;
  function confirmLeave(){{
    var dt=document.getElementById('leave-date-val').value;
    var lt=document.getElementById('leave-type').value;
    var half=document.getElementById('leave-half').checked;
    var days=half?0.5:1;
    var url='/action/mark-leave/'+dt+'?type='+lt+'&days='+days;
    closeLeaveModal();
    ajaxAction(url,null);
  }}
  window.confirmLeave=confirmLeave;
  function togglePause(){{
    var btn=document.getElementById('pause-toggle');
    btn.classList.add('ajax-loading');
    fetch('/api/toggle-pause').then(function(r){{return r.json()}}).then(function(data){{
      btn.classList.remove('ajax-loading');
      if(data.paused){{
        btn.classList.add('paused');
        document.getElementById('pause-icon').innerHTML='&#9654;';
        document.getElementById('pause-label').textContent='Resume Bot';
        var banner=document.createElement('div');banner.className='paused-banner';banner.id='paused-banner';
        banner.innerHTML='BOT PAUSED &mdash; No attendance will be marked';
        var tab=document.querySelector('.tab-bar');if(tab&&!document.getElementById('paused-banner'))tab.parentNode.insertBefore(banner,tab);
      }} else {{
        btn.classList.remove('paused');
        document.getElementById('pause-icon').innerHTML='&#9208;';
        document.getElementById('pause-label').textContent='Pause Bot';
        var b=document.getElementById('paused-banner');if(b)b.remove();
      }}
      showToast(data.msg || (data.paused?'Bot PAUSED':'Bot RESUMED'), data.ok?(data.paused?'error':'ok'):'error');
    }}).catch(function(){{btn.classList.remove('ajax-loading');showToast('Failed','error')}});
  }}
  window.togglePause=togglePause;
}})();
function loadWfhClock() {{
  fetch('/api/wfh-clock-status').then(function(r){{return r.json()}}).then(function(d) {{
    var card = document.getElementById('wfh-clock-card');
    if (!d.is_wfh_today) {{ card.style.display='none'; return; }}
    card.style.display='';
    var statusEl = document.getElementById('wfh-clock-status-text');
    var parts = [];
    if (d.clocked_in) {{ document.getElementById('wfh-ti-time').value=d.clocked_in.slice(0,5); parts.push('In: '+d.clocked_in.slice(0,5)); }}
    if (d.clocked_out) {{ document.getElementById('wfh-to-time').value=d.clocked_out.slice(0,5); parts.push('Out: '+d.clocked_out.slice(0,5)); }}
    if (parts.length) {{ statusEl.textContent='Saved: '+parts.join(' | '); document.getElementById('wfh-save-btn').textContent='Update WFH Hours'; }}
    else {{ statusEl.textContent=''; }}
  }});
}}
</script>
</body></html>"""


if __name__ == "__main__":
    config = load_config()
    dash = config.get("dashboard", {})
    host = dash.get("host", "0.0.0.0")
    port = dash.get("port", 5000)
    print(f"Attendance Management dashboard running at http://0.0.0.0:{port}")
    print(f"LAN access: http://{__import__('notify').get_local_ip()}:{port}")
    if dash.get("tailscale_ip"):
        print(f"Tailscale access: http://{dash['tailscale_ip']}:{port}")
    # threaded=True is Flask's default, but make it explicit: /action/timein-now
    # now blocks for up to 45s waiting on the bot, and the dashboard must stay
    # answerable on other requests while it does.
    app.run(host=host, port=port, debug=False, threaded=True)
