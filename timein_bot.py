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
from datetime import datetime, timedelta
from pathlib import Path

from notify import notify, notify_status, notify_skip, notify_failure
import attendance_db as db

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

# The direct AKU API (portalservice.aku.edu) is a private-network address and
# is unreliable/unreachable off the AKU network. Selenium (driving the saved
# portal page in a real browser) is the primary method; set this True to
# re-enable the direct API attempts before falling back to Selenium.
USE_DIRECT_API = False


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def parse_time(t_str):
    parts = t_str.split(":")
    return int(parts[0]), int(parts[1])


def write_status(mode, status, message, action_time=None, date_str=None):
    """Record an event in attendance.db (the single source of truth) and
    have it regenerate timein_status.json/timein_history.json from there -
    dashboard.py/cloud_sync.py/notify.py keep reading those files unchanged."""
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    db.record_event(day, mode, status, message, action_time)

    try:
        from cloud_sync import sync_status
        sync_status()
    except Exception:
        pass

    if status == "success" and action_time:
        check_time_diversity(mode, day, action_time)


def check_time_diversity(mode, day, action_time):
    """Alert if this marked time is identical to any of the last 14 days'
    marked time for the same mode - a sign randomization was bypassed,
    e.g. by another automation (a leftover Google Apps Script trigger,
    etc.) firing at a fixed clock time instead of our own bot."""
    try:
        recent = db.get_recent_action_times(mode, day, limit=14)
    except Exception:
        return
    dup_date = next((d for d, t in recent if t == action_time), None)
    if not dup_date:
        return
    label = LABELS[mode]
    log.warning("Anomaly: %s marked at %s is identical to %s - randomization may have been bypassed", label, action_time, dup_date)
    try:
        notify(
            f"{label} marked at {action_time} - identical to {dup_date}. "
            f"Randomization may not be working (another automation could be firing at a fixed time).",
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
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev = db.get_latest(mode, today_str)
    if prev and prev["status"] == "success":
        return True, prev.get("action_time") or "?"
    return False, None


def timein_done_today():
    """Check if time-in was successfully completed today."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    ti = db.get_latest("timein", today_str)
    return bool(ti and ti["status"] == "success")


def pending_prior_day_timein():
    """Return the date string (YYYY-MM-DD) of a prior day's Time-In that was
    never matched by a successful Time-Out, or None if nothing is pending.
    The AKU system just closes whatever session is currently open, regardless
    of which calendar day it's closed on - so a Time-Out attempt today should
    be allowed to complete a dangling Time-In from an earlier day."""
    today_str = datetime.now().strftime("%Y-%m-%d")
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

    today = datetime.now()
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

    today = datetime.now().date()
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
    sleep_secs = max(0, (target - datetime.now()).total_seconds())
    return target, sleep_secs


API_URL = "https://portalservice.aku.edu/Service1.svc/json/TimeInTimeOut/"


def parse_aku_time(message, label):
    """Extract the real HH:MM:SS AKU reports for "Time In"/"Time Out" (or
    "Timed In"/"Timed Out") from its response text, e.g. "Time In: Fri,
    Aug 21, 2026 - 8:05 AM PST" -> "08:05:00". label is "In" or "Out".
    Returns None if that field isn't present in the message. AKU's own
    timestamp is more accurate than our local clock at the moment we
    happened to check - especially when another automation (e.g. a
    leftover Google Apps Script trigger) completed the action earlier."""
    if isinstance(message, list):
        message = message[0] if message else ""
    pattern = r"Timed?\s+" + label + r":\s*\w+,\s*\w+\s+\d{1,2},\s*\d{4}\s*-\s*(\d{1,2}):(\d{2})\s*([AP]M)"
    m = re.search(pattern, message, re.IGNORECASE)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


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
        ok, message = call_aku_api(mode, creds["user_id"], creds["password"])
        if ok:
            field = "In" if mode == "timein" else "Out"
            real_time = parse_aku_time(message, field)
            now = real_time or datetime.now().strftime("%H:%M:%S")
            log.info("%s complete at %s (API: %s)", label, now, message)
            return True, now
        return False, message
    except Exception as e:
        log.exception("%s API call failed", label)
        return False, str(e)


def run_action_selenium(mode):
    """Drive the saved portal HTML page in a real browser via Selenium (the
    primary method - see USE_DIRECT_API), then verify the result via the API."""
    config = load_config()
    creds = config["credentials"]
    user_id = creds["user_id"]
    password = creds["password"]
    label = LABELS[mode]
    button_id = BUTTON_IDS[mode]

    driver = None
    try:
        log.info("[%s] (Selenium fallback) Opening %s", label, HTML_FILE.as_uri())
        options = Options()
        options.add_argument("--disable-gpu")
        driver = webdriver.Edge(options=options)
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
        button.click()
        log.info("Clicked %s", label)
        time.sleep(3)
    except Exception as e:
        log.exception("%s Selenium click failed", label)
        return False, str(e)
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
        return False, f"Clicked but could not verify: {e}"

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
                    log.info("%s already completed by another actor - confirmed via reconciliation: %s", label, other_message)
                    return True, real_time
            except Exception:
                pass
        log.warning("%s clicked but API verification says: %s", label, message)
        return False, f"Clicked but not confirmed ({message})"

    field = "In" if mode == "timein" else "Out"
    real_time = parse_aku_time(message, field)
    now = real_time or datetime.now().strftime("%H:%M:%S")
    log.info("%s complete at %s (Selenium, verified: %s)", label, now, message)
    return True, now


def attempt_action(mode, retry_cfg):
    """Run the retry loop for a mode (Selenium primary, optional direct API
    first). Returns (success, detail)."""
    max_attempts = retry_cfg["max_attempts"]
    retry_delay = retry_cfg["delay_seconds"]

    if USE_DIRECT_API:
        for attempt in range(1, max_attempts + 1):
            log.info("Attempt %d/%d (API)", attempt, max_attempts)
            success, detail = run_action(mode)
            if success:
                return True, detail
            log.warning("Attempt %d failed: %s", attempt, detail)
            if attempt < max_attempts:
                log.info("Retrying in %d seconds...", retry_delay)
                time.sleep(retry_delay)
        log.warning("API method exhausted after %d attempts - falling back to Selenium", max_attempts)

    for attempt in range(1, max_attempts + 1):
        log.info("Attempt %d/%d (Selenium)", attempt, max_attempts)
        success, detail = run_action_selenium(mode)
        if success:
            return True, detail
        log.warning("Attempt %d failed: %s", attempt, detail)
        if attempt < max_attempts:
            log.info("Retrying in %d seconds...", retry_delay)
            time.sleep(retry_delay)

    return False, f"FAILED after {max_attempts} attempts"


def run_and_record(mode, retry_cfg, catchup_date=None):
    """Run attempt_action for mode, write status, notify. Returns success."""
    label = LABELS[mode]
    success, detail = attempt_action(mode, retry_cfg)

    if success:
        if catchup_date:
            missed_fmt = datetime.strptime(catchup_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            msg = f"{label} marked at {detail} (completed pending {missed_fmt})"
        else:
            msg = f"{label} marked at {detail}"
        log.info(msg)
        write_status(mode, "success", msg, action_time=detail, date_str=catchup_date)
        notify_status(mode, detail)
        log.info("=== Done ===")
        return True

    msg = f"{label} {detail}"
    log.error(msg)
    write_status(mode, "failed", msg)
    notify_failure(mode, retry_cfg["max_attempts"])
    log.info("=== Done (FAILED) ===")
    return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "timein"
    now_flag = "--now" in sys.argv
    fallback_flag = "--fallback" in sys.argv
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
        today_fmt = datetime.now().strftime("%d-%b-%Y")
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
            today_fmt = datetime.now().strftime("%d-%b-%Y")
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

        if fallback_flag:
            # Fallback trigger (GitHub Actions self-hosted runner): runs well
            # after the local bot's own window has closed, specifically so
            # it acts second in the hierarchy - local bot first, this as
            # backup, Google Script last. No randomized wait needed since
            # we're intentionally running late; already_done() at the top
            # of main() already covers "local bot got there first".
            log.info("Fallback trigger - checking immediately, no randomized wait")
        else:
            target, sleep_secs = pick_target_time(mode)
            log.info(
                "Target time: %s (sleeping %.1f min)",
                target.strftime("%H:%M:%S"),
                sleep_secs / 60,
            )

            time.sleep(sleep_secs)

            # Re-check after the sleep in case something else (the fallback
            # trigger, or Google Script) completed it while we were waiting.
            done, done_time = already_done(mode)
            if done:
                today_fmt = datetime.now().strftime("%d-%b-%Y")
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