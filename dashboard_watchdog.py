"""
Dashboard watchdog. Relaunches dashboard.py if it isn't listening.

Runs from Task Scheduler shortly before the times the dashboard is actually
needed (ahead of the time-in and time-out windows) rather than polling all day.
Does nothing on weekends, holidays, or blackout/leave days.

  python dashboard_watchdog.py          -- check, relaunch if needed
  python dashboard_watchdog.py --force  -- ignore the calendar checks
"""

import json
import logging
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "timein_logs"
LOG_DIR.mkdir(exist_ok=True)
CONFIG_FILE = BASE_DIR / "config.json"
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "dashboard_watchdog.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("dashboard_watchdog")


def load_json(path):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def skip_reason(date_str, weekday):
    blackout = load_json(BLACKOUT_FILE)
    if weekday >= 5 and date_str not in blackout.get("working_weekends", []):
        return "weekend"
    for h in load_json(HOLIDAYS_FILE).get("holidays", []):
        if h["date"] == date_str and not h.get("disabled", False):
            return f"holiday: {h.get('label', 'Public Holiday')}"
    for d in blackout.get("dates", []):
        if d["date"] == date_str:
            return f"blackout: {d.get('reason', 'Blackout')}"
    for r in blackout.get("ranges", []):
        if r["start"] <= date_str <= r["end"]:
            return f"blackout: {r.get('reason', 'Blackout range')}"
    return None


def is_listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def interpreter():
    venv = BASE_DIR / ".venv-dashboard" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def launch(port):
    # Detached with its own log file so a crash leaves a traceback behind
    # instead of dying invisibly the way the at-logon task did.
    out = open(LOG_DIR / "dashboard_stdout.log", "a", encoding="utf-8", buffering=1)
    out.write(f"\n--- launched by watchdog {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
    subprocess.Popen(
        [interpreter(), "-u", str(BASE_DIR / "dashboard.py")],
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    log.info("Dashboard relaunched on port %s", port)


def main():
    force = "--force" in sys.argv
    now = datetime.now()
    port = load_json(CONFIG_FILE).get("dashboard", {}).get("port", 5000)

    if not force:
        reason = skip_reason(now.strftime("%Y-%m-%d"), now.weekday())
        if reason:
            log.info("Not needed today (%s) - skipping", reason)
            return

    if is_listening(port):
        log.info("Dashboard already listening on port %s - nothing to do", port)
        return

    log.warning("Dashboard not responding on port %s - relaunching", port)
    launch(port)


if __name__ == "__main__":
    main()
