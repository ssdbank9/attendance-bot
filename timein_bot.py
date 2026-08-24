"""
Automated attendance bot for the mock Time In / Time Out page.

Modes:
  python timein_bot.py timein   -- Morning (config-driven windows)
  python timein_bot.py timeout  -- Evening (config-driven windows)

Reads config.json for time windows and weights.
Skips weekends, public holidays (holidays.json), and blackout dates (blackout.json).
Retries on failure. Phone notification via Claude Code push.
"""

import json
import random
import re
import sys
import time
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pk_time import now as pk_now
from pathlib import Path

from notify import notify, notify_status, notify_skip, notify_failure
import attendance_db as db

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from console_guard import silence
silence(Path(__file__).parent / "timein_logs" / "timein_stdout.log")
# pythonw.exe leaves stdout/stderr as None; see console_guard.py

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "timein_logs"
LOG_DIR.mkdir(exist_ok=True)
STATUS_FILE = BASE_DIR / "timein_status.json"
CONFIG_FILE = BASE_DIR / "config.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"
HISTORY_FILE = BASE_DIR / "timein_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "timein.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("attendance_bot")

HTML_FILE = BASE_DIR / "timein_page.html"

BUTTON_IDS = {
    "timein": "AKU_TL_DRIVED04_BUTTON",
    "timeout": "AKU_TL_DRIVED04_BUTTON1",
}
LABELS = {
    "timein": "Time-In",
    "timeout": "Time-Out",
}

# The direct AKU API (portalservice.aku.edu) is the primary method: it draws
# nothing on screen, which matters because a visible Edge window announced the
# bot mid-presentation. This is not a new dependency - the Selenium path always
# called the same endpoint to verify its click, so a run that could not reach
# the API never succeeded anyway. Off the AKU network the API is unreachable;
# the attempts then fail and Selenium (now headless) still runs as the fallback.
USE_DIRECT_API = True

# Windows shows a console window for every python.exe/child process a task
# spawns. CREATE_NO_WINDOW on each spawn plus pythonw.exe for the scheduled
# entry points keeps the bot invisible.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def gui_executable():
    """sys.executable's console-free twin (pythonw.exe) when it exists, so a
    process we register or spawn cannot flash a black console window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        quiet = exe.with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return sys.executable


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def parse_time(t_str):
    parts = t_str.split(":")
    return int(parts[0]), int(parts[1])


def write_status(mode, status, message, action_time=None, date_str=None,
                 action_origin="bot", observed_time=None):
    """Record an event in attendance.db (the single source of truth) and
    have it regenerate timein_status.json/timein_history.json from there -
    dashboard.py/cloud_sync.py/notify.py keep reading those files unchanged."""
    day = date_str or pk_now().strftime("%Y-%m-%d")
    db.record_event(
        day, mode, status, message, action_time,
        action_origin=action_origin, observed_time=observed_time,
    )

    try:
        from cloud_sync import sync_status
        sync_status()
    except Exception:
        pass

    if status == "success":
        check_time_diversity(
            mode, day, action_time,
            action_origin=action_origin, observed_time=observed_time,
        )


def check_time_diversity(mode, day, action_time, action_origin="bot",
                         observed_time=None):
    """Distinguish a repeated bot-owned time from a pre-existing portal entry."""
    label = LABELS[mode]
    if action_origin == "preexisting":
        portal_time = observed_time or "an unknown time"
        log.warning(
            "Anomaly: %s was already present in the portal at %s before the "
            "bot acted - pre-empted by another actor; bot randomization was "
            "not bypassed",
            label, portal_time,
        )
        try:
            notify(
                f"{label} was already present in the portal at {portal_time} "
                "before this bot acted. Another actor pre-empted the bot; "
                "the bot's randomized time was not recorded as the action time.",
                title="Attendance Pre-empted", priority="high", tags="warning",
            )
        except Exception:
            pass
        return

    if not action_time:
        return
    try:
        recent = db.get_recent_action_times(mode, day, limit=14)
    except Exception:
        return
    dup_date = next((d for d, t in recent if t == action_time), None)
    if not dup_date:
        return
    log.warning(
        "Anomaly: bot-owned %s action at %s is identical to %s - the bot's "
        "randomization may have been bypassed",
        label, action_time, dup_date,
    )
    try:
        notify(
            f"The bot's own {label} action at {action_time} is identical to "
            f"{dup_date}. The bot's randomization may have been bypassed.",
            title="Time Anomaly Detected", priority="high", tags="warning",
        )
    except Exception:
        pass


def is_holiday(today_str):
    if not HOLIDAYS_FILE.exists():
        return False, None
    with open(HOLIDAYS_FILE, "r") as f:
        data = json.load(f)
    for h in data.get("holidays", []):
        if h["date"] == today_str and not h.get("disabled", False):
            return True, h.get("label", "Public Holiday")
    return False, None


def is_blacked_out(today_str):
    if not BLACKOUT_FILE.exists():
        return False, None
    with open(BLACKOUT_FILE, "r") as f:
        data = json.load(f)
    for d in data.get("dates", []):
        if d["date"] == today_str:
            return True, d.get("reason", "Blackout")
    for r in data.get("ranges", []):
        if r["start"] <= today_str <= r["end"]:
            return True, r.get("reason", "Blackout range")
    return False, None


def is_working_weekend(date_str):
    if not BLACKOUT_FILE.exists():
        return False
    with open(BLACKOUT_FILE, "r") as f:
        data = json.load(f)
    return date_str in data.get("working_weekends", [])


def already_done(mode):
    """Check if this mode was already completed today."""
    today_str = pk_now().strftime("%Y-%m-%d")
    prev = db.get_latest(mode, today_str)
    if prev and prev["status"] == "success":
        return True, prev.get("action_time") or prev.get("observed_time") or "?"
    return False, None


def timein_done_today():
    """Check if time-in was successfully completed today."""
    today_str = pk_now().strftime("%Y-%m-%d")
    ti = db.get_latest("timein", today_str)
    return bool(ti and ti["status"] == "success")


def pending_prior_day_timein():
    """Return the date string (YYYY-MM-DD) of a prior day's Time-In that was
    never matched by a successful Time-Out, or None if nothing is pending.
    The AKU system just closes whatever session is currently open, regardless
    of which calendar day it's closed on - so a Time-Out attempt today should
    be allowed to complete a dangling Time-In from an earlier day."""
    today_str = pk_now().strftime("%Y-%m-%d")
    ti = db.get_latest("timein")
    to = db.get_latest("timeout")
    ti_date = (ti or {}).get("date", "")
    if (ti and ti["status"] == "success" and ti_date and ti_date < today_str
            and (not to or to.get("date") != ti_date or to.get("status") != "success")):
        return ti_date
    return None


def should_run_today(mode):
    config = load_config()
    if config.get("paused", False):
        log.info("Bot is PAUSED globally - skipping")
        write_status(mode, "skipped", "Bot paused")
        return False

    today = pk_now()
    today_str = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")

    if today.weekday() >= 5:
        if not is_working_weekend(today_str):
            log.info("Skipping - %s is a weekend", day_name)
            write_status(mode, "skipped", f"Weekend ({day_name})")
            return False
        log.info("%s is a working weekend - proceeding", day_name)

    holiday, label = is_holiday(today_str)
    if holiday:
        log.info("Skipping - holiday: %s", label)
        write_status(mode, "skipped", f"Holiday: {label}")
        return False

    blacked, reason = is_blacked_out(today_str)
    if blacked:
        log.info("Skipping - blackout: %s", reason)
        write_status(mode, "skipped", f"Blackout: {reason}")
        notify_skip(mode, reason)
        return False

    return True


def pick_target_time(mode):
    config = load_config()
    mc = config[mode]

    today = pk_now().date()
    sh, sm = parse_time(mc["window_start"])
    ph, pm = parse_time(mc["primary_end"])
    eh, em = parse_time(mc["window_end"])

    start = datetime(today.year, today.month, today.day, sh, sm)
    primary_end = datetime(today.year, today.month, today.day, ph, pm)
    end = datetime(today.year, today.month, today.day, eh, em)

    primary_secs = int((primary_end - start).total_seconds())
    secondary_secs = int((end - primary_end).total_seconds())
    weight = mc["primary_weight"]

    if random.random() < weight:
        offset = random.randint(0, primary_secs)
    else:
        offset = primary_secs + random.randint(0, secondary_secs)

    target = start + timedelta(seconds=offset)
    sleep_secs = max(0, (target - pk_now()).total_seconds())
    return target, sleep_secs


def pick_fallback_target_time(mode, buffer_minutes=10):
    """Pick a random time within [window_end, window_end + buffer_minutes]
    for the GitHub Actions fallback trigger - a short randomized buffer
    right after the local bot's own window closes, so the fallback isn't
    firing at a suspiciously exact instant every day."""
    config = load_config()
    mc = config[mode]
    today = pk_now().date()
    eh, em = parse_time(mc["window_end"])
    start = datetime(today.year, today.month, today.day, eh, em)
    offset = random.randint(0, buffer_minutes * 60)
    target = start + timedelta(seconds=offset)
    sleep_secs = max(0, (target - pk_now()).total_seconds())
    return target, sleep_secs


API_URL = "https://portalservice.aku.edu/Service1.svc/json/TimeInTimeOut/"


def _aku_message_text(message):
    if isinstance(message, list):
        message = message[0] if message else ""
    return message or ""


def parse_aku_datetime(message, label):
    """Parse a portal-reported local date/time for the requested field.

    The service labels these values ``PST``, but its clock matches Pakistan
    local time used by the scheduler; no Pacific-time conversion is applied.
    The portal only reports minute precision, so this timestamp is evidence
    about an existing portal record, not a replacement for a bot-owned action
    time with seconds.
    """
    message = _aku_message_text(message)
    pattern = (
        r"Timed?\s+" + label
        + r":\s*\w+,\s*(\w+\s+\d{1,2},\s*\d{4})\s*-\s*"
          r"(\d{1,2}:\d{2}\s*[AP]M)(?:\s+[A-Z]{2,5})?"
    )
    m = re.search(pattern, message, re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %I:%M %p")
    except ValueError:
        return None


def parse_aku_time(message, label):
    """Return a portal-reported time as HH:MM:00, or None."""
    portal_dt = parse_aku_datetime(message, label)
    return portal_dt.strftime("%H:%M:%S") if portal_dt else None


def portal_entry_predates_attempt(message, label, attempted_at):
    """True when a minute-precision portal record clearly predates this bot."""
    portal_dt = parse_aku_datetime(message, label)
    if not portal_dt or not attempted_at:
        return False
    attempted_minute = attempted_at.replace(second=0, microsecond=0)
    return portal_dt < attempted_minute


def classify_aku_message(message):
    """Given the AKU API's TimeInTimeOutResult text, decide whether it means
    the action succeeded (or was already done) vs a real error. Returns
    (ok: bool, message: str)."""
    if isinstance(message, list):
        message = message[0] if message else ""
    if not message:
        return False, "API returned empty response"
    msg_lower = message.lower()
    if "invalid" in msg_lower or "does not exist" in msg_lower or "no time in information" in msg_lower:
        return False, "API error: " + message
    if "error" in msg_lower and "already" not in msg_lower:
        return False, "API error: " + message
    return True, message


def call_aku_api(mode, user_id, password):
    """Call the AKU TimeInTimeOut API directly. Returns (ok, message)."""
    action_code = "I" if mode == "timein" else "O"
    payload = {"_action": action_code, "_userid": user_id, "_password": password}

    import requests
    resp = requests.post(
        API_URL, json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    if resp.status_code != 200:
        return False, "HTTP " + str(resp.status_code)

    result = resp.json()
    message = result.get("TimeInTimeOutResult", "")
    log.info("API response: %s", message)
    return classify_aku_message(message)


def run_action(mode):
    config = load_config()
    creds = config["credentials"]
    label = LABELS[mode]

    log.info("[%s] Calling API %s", label, API_URL)
    try:
        attempted_at = pk_now()
        ok, message = call_aku_api(mode, creds["user_id"], creds["password"])
        if ok:
            field = "In" if mode == "timein" else "Out"
            portal_time = parse_aku_time(message, field)
            if portal_time and portal_entry_predates_attempt(message, field, attempted_at):
                log.warning(
                    "%s portal entry at %s predates this API attempt; treating "
                    "it as pre-existing",
                    label, portal_time,
                )
                return True, portal_time, "preexisting"
            now = pk_now().strftime("%H:%M:%S")
            log.info("%s complete at %s (API: %s)", label, now, message)
            return True, now, "bot"
        return False, message, "unknown"
    except Exception as e:
        log.exception("%s API call failed", label)
        return False, str(e), "unknown"


def run_action_selenium(mode):
    """Drive the saved portal HTML page in a headless browser via Selenium (the
    fallback behind the direct API - see USE_DIRECT_API), then verify the result
    via the API."""
    config = load_config()
    creds = config["credentials"]
    user_id = creds["user_id"]
    password = creds["password"]
    label = LABELS[mode]
    button_id = BUTTON_IDS[mode]

    driver = None
    attempted_at = None
    try:
        log.info("[%s] (Selenium fallback) Opening %s", label, HTML_FILE.as_uri())
        options = Options()
        options.add_argument("--disable-gpu")
        # Headless because a visible Edge window opening itself and typing
        # credentials announces the bot to anyone watching the screen. It also
        # becomes mandatory if the tasks are moved to session 0
        # (enable_session0.ps1), which has no desktop to draw on at all.
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--log-level=3")
        # Without this msedgedriver.exe opens its own console window.
        service = EdgeService(creation_flags=NO_WINDOW)
        driver = webdriver.Edge(options=options, service=service)
        driver.get(HTML_FILE.as_uri())

        wait = WebDriverWait(driver, 20)
        user_field = wait.until(EC.presence_of_element_located((By.ID, "AKU_TL_DRIVED04_OPRID")))
        user_field.clear()
        user_field.send_keys(user_id)
        log.info("Entered User ID")

        pw_field = driver.find_element(By.ID, "AKU_TL_DRIVED04_OPERPSWD")
        pw_field.clear()
        pw_field.send_keys(password)
        log.info("Entered Password")

        button = wait.until(EC.element_to_be_clickable((By.ID, button_id)))
        attempted_at = pk_now()
        button.click()
        log.info("Clicked %s", label)
        time.sleep(3)
    except Exception as e:
        log.exception("%s Selenium click failed", label)
        return False, str(e), "unknown"
    finally:
        if driver:
            driver.quit()

    # The click alone doesn't confirm the portal actually accepted the
    # action (no fixed success indicator was found in the saved page).
    # Verify with the same API call used by the direct-API path - this is
    # safe to call again even if the click already succeeded, since AKU's
    # API is idempotent and just reports "already Entered" once the action
    # has landed; it also self-heals if the click silently failed by
    # performing the real action here instead.
    try:
        ok, message = call_aku_api(mode, user_id, password)
    except Exception as e:
        log.exception("%s post-click API verification failed", label)
        return False, f"Clicked but could not verify: {e}", "unknown"

    if not ok:
        # Can happen when another automation (e.g. a leftover Google Apps
        # Script trigger) already completed the action before we got here.
        # Safe to cross-check via the OTHER action's status ONLY for
        # timeout - main() already confirmed today's Time-In succeeded
        # before ever attempting a Time-Out, so calling action=I here is
        # a safe idempotent check, not a fresh action.
        if mode == "timeout":
            try:
                _, other_message = call_aku_api("timein", user_id, password)
                real_time = parse_aku_time(other_message, "Out")
                if real_time:
                    if portal_entry_predates_attempt(other_message, "Out", attempted_at):
                        log.info("%s already completed by another actor - confirmed via reconciliation: %s", label, other_message)
                        return True, real_time, "preexisting"
                    now = pk_now().strftime("%H:%M:%S")
                    log.info("%s completed by this Selenium attempt - confirmed via reconciliation: %s", label, other_message)
                    return True, now, "bot"
            except Exception:
                pass
        log.warning("%s clicked but API verification says: %s", label, message)
        return False, f"Clicked but not confirmed ({message})", "unknown"

    field = "In" if mode == "timein" else "Out"
    portal_time = parse_aku_time(message, field)
    if portal_time and portal_entry_predates_attempt(message, field, attempted_at):
        log.warning(
            "%s portal entry at %s predates the Selenium click; treating it "
            "as pre-existing",
            label, portal_time,
        )
        return True, portal_time, "preexisting"
    now = pk_now().strftime("%H:%M:%S")
    log.info("%s complete at %s (Selenium, verified: %s)", label, now, message)
    return True, now, "bot"


def attempt_action(mode, retry_cfg):
    """Run the retry loop for a mode (Selenium primary, optional direct API
    first). Returns (success, detail, action_origin)."""
    max_attempts = retry_cfg["max_attempts"]
    retry_delay = retry_cfg["delay_seconds"]

    if USE_DIRECT_API:
        for attempt in range(1, max_attempts + 1):
            log.info("Attempt %d/%d (API)", attempt, max_attempts)
            success, detail, action_origin = run_action(mode)
            if success:
                return True, detail, action_origin
            log.warning("Attempt %d failed: %s", attempt, detail)
            if attempt < max_attempts:
                log.info("Retrying in %d seconds...", retry_delay)
                time.sleep(retry_delay)
        log.warning("API method exhausted after %d attempts - falling back to Selenium", max_attempts)

    for attempt in range(1, max_attempts + 1):
        log.info("Attempt %d/%d (Selenium)", attempt, max_attempts)
        success, detail, action_origin = run_action_selenium(mode)
        if success:
            return True, detail, action_origin
        log.warning("Attempt %d failed: %s", attempt, detail)
        if attempt < max_attempts:
            log.info("Retrying in %d seconds...", retry_delay)
            time.sleep(retry_delay)

    return False, f"FAILED after {max_attempts} attempts", "unknown"


def run_and_record(mode, retry_cfg, catchup_date=None):
    """Run attempt_action for mode, write status, notify. Returns success."""
    label = LABELS[mode]
    success, detail, action_origin = attempt_action(mode, retry_cfg)

    if success:
        if action_origin == "preexisting":
            msg = f"{label} was already present before this bot acted (portal reported {detail})"
        elif catchup_date:
            missed_fmt = datetime.strptime(catchup_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            msg = f"{label} marked at {detail} (completed pending {missed_fmt})"
        else:
            msg = f"{label} marked at {detail}"
        log.info(msg)
        if action_origin == "preexisting":
            write_status(
                mode, "success", msg, action_time=None, date_str=catchup_date,
                action_origin="preexisting", observed_time=detail,
            )
        else:
            write_status(
                mode, "success", msg, action_time=detail, date_str=catchup_date,
                action_origin="bot",
            )
            notify_status(mode, detail)
        log.info("=== Done ===")
        return True

    msg = f"{label} {detail}"
    log.error(msg)
    write_status(mode, "failed", msg)
    notify_failure(mode, retry_cfg["max_attempts"])
    log.info("=== Done (FAILED) ===")
    return False


# A long time.sleep() is the reason attendance kept landing late. WakeToRun on
# the 08:45/20:00 tasks only guarantees the host is awake when the task STARTS;
# the randomized target can be an hour later. On battery this host suspends
# after 5 idle minutes and a sleeping process is not activity, so the target
# passed mid-suspend and the bot acted whenever the lid was next opened
# (20:32 target -> acted 20:58). Handing the wait to a one-shot task with
# WakeToRun makes Windows wake the machine at the randomized instant itself.
ONESHOT_PREFIX = "TimeInBot_OneShot_"
INLINE_WAIT_LIMIT = 120  # seconds; below this, suspend is not a real risk


def schedule_oneshot(mode, target):
    """Register a wake-capable one-time task at `target`. True if it took."""
    task = f"{ONESHOT_PREFIX}{mode}"
    script = str(Path(__file__).resolve())
    exe = gui_executable()
    # Expires shortly after the target so a stale task cannot re-fire, but with
    # enough slack that a host resuming late still runs it rather than skipping.
    end = target + timedelta(hours=4)
    ps = f"""$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{script}" {mode} --scheduled' -WorkingDirectory '{BASE_DIR}'
$t = New-ScheduledTaskTrigger -Once -At '{target:%Y-%m-%dT%H:%M:%S}'
$t.EndBoundary = '{end:%Y-%m-%dT%H:%M:%S}'
$s = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -DeleteExpiredTaskAfter (New-TimeSpan -Minutes 5)
$s.Priority = 4
$s.Hidden = $true
# S4U + Hidden runs the wake-up in session 0, where no window can be drawn at
# all. Switching a task's principal needs elevation, so when this shell is not
# elevated fall back to the default interactive principal - pythonw.exe plus
# Hidden already means nothing appears on screen; only the session differs.
# The fallback matters: a failed registration would drop the caller into an
# hours-long inline sleep, which is exactly the late-attendance bug that the
# one-shot handoff exists to prevent.
$p = New-ScheduledTaskPrincipal -UserId ("$env:USERDOMAIN" + [char]92 + "$env:USERNAME") -LogonType S4U -RunLevel Limited
try {{
  Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
  Write-Output 'principal=S4U'
}} catch {{
  Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
  Write-Output 'principal=interactive'
}}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=90,
            creationflags=NO_WINDOW,
        )
    except Exception as e:
        log.warning("Could not register %s (%s) - falling back to inline wait", task, e)
        return False
    if r.returncode != 0:
        log.warning(
            "Could not register %s - falling back to inline wait: %s",
            task, (r.stderr or r.stdout).strip()[:300],
        )
        return False
    log.info("Scheduled wake-capable %s for %s (%s)", task,
             target.strftime("%H:%M:%S"), (r.stdout or "").strip() or "principal=?")
    return True


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "timein"
    now_flag = "--now" in sys.argv
    fallback_flag = "--fallback" in sys.argv
    # Fired by the one-shot task at the randomized instant: act now, but
    # keep every guard the inline path would have re-run after waking.
    scheduled_flag = "--scheduled" in sys.argv
    if mode not in BUTTON_IDS:
        print(f"Usage: python {Path(__file__).name} [timein|timeout] [--now|--fallback]")
        sys.exit(1)

    label = LABELS[mode]
    config = load_config()
    retry_cfg = config["retry"]
    run_kind = "(MANUAL) " if now_flag else ("(FALLBACK) " if fallback_flag else "")
    log.info("=== %s Bot started %s===", label, run_kind)

    done, done_time = already_done(mode)
    if done:
        today_fmt = pk_now().strftime("%d-%b-%Y")
        msg = f"{label} already posted today {today_fmt} at {done_time}"
        log.info("=== %s - skipping ===", msg)
        # Do NOT write_status here - it would overwrite the existing
        # "success" record with "skipped", corrupting timein_done_today()/
        # pending_prior_day_timein() for every check that follows.
        if now_flag:
            print(msg)
        return

    catchup_date = None
    if mode == "timeout":
        catchup_date = pending_prior_day_timein()
        if not timein_done_today() and not catchup_date:
            today_fmt = pk_now().strftime("%d-%b-%Y")
            msg = f"Cannot Time-Out: no Time-In recorded today {today_fmt}"
            log.info("=== %s - skipping ===", msg)
            write_status(mode, "skipped", msg)
            if now_flag:
                print(msg)
            return
        if catchup_date:
            missed_fmt = datetime.strptime(catchup_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            log.info("Completing pending Time-Out for %s before proceeding", missed_fmt)

    if not now_flag:
        if not should_run_today(mode):
            log.info("=== Exiting (skipped) ===")
            return

        if not scheduled_flag:
            if fallback_flag:
                # Fallback trigger (GitHub Actions self-hosted runner): runs
                # right as the local bot's own window closes, with a short
                # randomized buffer of its own (not firing at a suspiciously
                # exact instant every day) - the backup behind the local bot.
                target, sleep_secs = pick_fallback_target_time(mode)
                kind = "Fallback target"
            else:
                target, sleep_secs = pick_target_time(mode)
                kind = "Target"

            if sleep_secs > INLINE_WAIT_LIMIT and schedule_oneshot(mode, target):
                log.info("=== Handed off to one-shot task at %s ===",
                         target.strftime("%H:%M:%S"))
                return

            log.info(
                "%s time: %s (sleeping %.1f min)",
                kind, target.strftime("%H:%M:%S"), sleep_secs / 60,
            )
            time.sleep(sleep_secs)

        # Re-check after the sleep in case something else (the other
        # trigger, or Google Script) completed it while we were waiting.
        done, done_time = already_done(mode)
        if done:
            today_fmt = pk_now().strftime("%d-%b-%Y")
            msg = f"{label} already posted today {today_fmt} at {done_time} (completed by another trigger while waiting)"
            log.info("=== %s - skipping ===", msg)
            # Not write_status here either - same reason as above.
            return
    else:
        log.info("Manual trigger - running immediately")

    if mode == "timein":
        pending_date = pending_prior_day_timein()
        if pending_date:
            missed_fmt = datetime.strptime(pending_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            log.info("Pending Time-Out for %s detected - auto-completing before Time-In", missed_fmt)
            if not run_and_record("timeout", retry_cfg, catchup_date=pending_date):
                msg = f"Cannot Time-In: failed to auto-complete pending Time-Out for {missed_fmt}"
                log.error(msg)
                write_status(mode, "failed", msg)
                return

    run_and_record(mode, retry_cfg)


if __name__ == "__main__":
    main()
