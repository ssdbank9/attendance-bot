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
import sys
import time
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from notify import notify, notify_status, notify_skip, notify_failure

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


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def parse_time(t_str):
    parts = t_str.split(":")
    return int(parts[0]), int(parts[1])


def write_status(mode, status, message, action_time=None):
    existing = {}
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing[mode] = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": status,
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if action_time:
        existing[mode]["action_time"] = action_time

    with open(STATUS_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    try:
        from cloud_sync import sync_status
        sync_status()
    except Exception:
        pass

    if status == "success" and action_time:
        _log_history(mode, action_time)


def _log_history(mode, action_time):
    try:
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
        else:
            data = {"records": {}}
    except Exception:
        data = {"records": {}}
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in data["records"]:
        data["records"][today] = {}
    data["records"][today][mode] = action_time
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


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
    if not STATUS_FILE.exists():
        return False, None
    try:
        with open(STATUS_FILE, "r") as f:
            status = json.load(f)
    except Exception:
        return False, None
    prev = status.get(mode, {})
    today_str = datetime.now().strftime("%Y-%m-%d")
    if prev.get("date") == today_str and prev.get("status") == "success":
        return True, prev.get("action_time", "?")
    return False, None


def timein_done_today():
    """Check if time-in was successfully completed today."""
    if not STATUS_FILE.exists():
        return False
    try:
        with open(STATUS_FILE, "r") as f:
            status = json.load(f)
    except Exception:
        return False
    ti = status.get("timein", {})
    today_str = datetime.now().strftime("%Y-%m-%d")
    return ti.get("date") == today_str and ti.get("status") == "success"


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


def run_action(mode):
    config = load_config()
    creds = config["credentials"]
    user_id = creds["user_id"]
    password = creds["password"]
    label = LABELS[mode]
    action_code = "I" if mode == "timein" else "O"

    API_URL = "https://portalservice.aku.edu/Service1.svc/json/TimeInTimeOut/"

    payload = {
        "_action": action_code,
        "_userid": user_id,
        "_password": password
    }

    log.info("[%s] Calling API %s", label, API_URL)

    try:
        import requests
        resp = requests.post(
            API_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30
        )

        if resp.status_code == 200:
            result = resp.json()
            message = result.get("TimeInTimeOutResult", "")
            log.info("API response: %s", message)

            msg_lower = message.lower() if message else ""
            if not message:
                return False, "API returned empty response"
            if "invalid" in msg_lower:
                return False, "API error: " + message
            if "error" in msg_lower and "already" not in msg_lower:
                return False, "API error: " + message

            now = datetime.now().strftime("%H:%M:%S")
            log.info("%s complete at %s (API: %s)", label, now, message)
            return True, now
        else:
            log.warning("API HTTP %d: %s", resp.status_code, resp.text[:200])
            return False, "HTTP " + str(resp.status_code)

    except Exception as e:
        log.exception("%s API call failed", label)
        return False, str(e)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "timein"
    now_flag = "--now" in sys.argv
    if mode not in BUTTON_IDS:
        print(f"Usage: python {Path(__file__).name} [timein|timeout] [--now]")
        sys.exit(1)

    label = LABELS[mode]
    config = load_config()
    retry_cfg = config["retry"]
    log.info("=== %s Bot started %s===", label, "(MANUAL) " if now_flag else "")

    done, done_time = already_done(mode)
    if done:
        today_fmt = datetime.now().strftime("%d-%b-%Y")
        msg = f"{label} already posted today {today_fmt} at {done_time}"
        log.info("=== %s - skipping ===", msg)
        write_status(mode, "skipped", msg)
        if now_flag:
            print(msg)
        return

    if mode == "timein":
        try:
            with open(STATUS_FILE, "r") as f:
                status = json.load(f)
            ti = status.get("timein", {})
            to = status.get("timeout", {})
            ti_date = ti.get("date", "")
            today_str = datetime.now().strftime("%Y-%m-%d")
            if (ti.get("status") == "success" and ti_date < today_str
                    and (to.get("date") != ti_date or to.get("status") != "success")):
                missed_fmt = datetime.strptime(ti_date, "%Y-%m-%d").strftime("%d-%b-%Y")
                msg = f"Cannot Time-In today: you haven't Timed-Out for {missed_fmt}"
                log.info("=== %s - skipping ===", msg)
                write_status(mode, "skipped", msg)
                if now_flag:
                    print(msg)
                return
        except Exception:
            pass

    if mode == "timeout" and not timein_done_today():
        today_fmt = datetime.now().strftime("%d-%b-%Y")
        msg = f"Cannot Time-Out: no Time-In recorded today {today_fmt}"
        log.info("=== %s - skipping ===", msg)
        write_status(mode, "skipped", msg)
        if now_flag:
            print(msg)
        return

    if not now_flag:
        if not should_run_today(mode):
            log.info("=== Exiting (skipped) ===")
            return

        target, sleep_secs = pick_target_time(mode)
        log.info(
            "Target time: %s (sleeping %.1f min)",
            target.strftime("%H:%M:%S"),
            sleep_secs / 60,
        )

        time.sleep(sleep_secs)
    else:
        log.info("Manual trigger - running immediately")

    max_attempts = retry_cfg["max_attempts"]
    retry_delay = retry_cfg["delay_seconds"]

    for attempt in range(1, max_attempts + 1):
        log.info("Attempt %d/%d", attempt, max_attempts)
        success, detail = run_action(mode)

        if success:
            msg = f"{label} marked at {detail}"
            log.info(msg)
            write_status(mode, "success", msg, action_time=detail)
            notify_status(mode, detail)
            log.info("=== Done ===")
            return

        log.warning("Attempt %d failed: %s", attempt, detail)
        if attempt < max_attempts:
            log.info("Retrying in %d seconds...", retry_delay)
            time.sleep(retry_delay)

    msg = f"{label} FAILED after {max_attempts} attempts"
    log.error(msg)
    write_status(mode, "failed", msg)
    notify_failure(mode, max_attempts)
    log.info("=== Done (FAILED) ===")


if __name__ == "__main__":
    main()