"""
Dashboard watchdog. Relaunches dashboard.py if it isn't serving requests.

Runs from Task Scheduler every few minutes, all day, every day. The dashboard
is a thing the user opens from their phone at arbitrary times - including
weekends and leave days, when checking or booking leave is exactly what they
want to do - so availability is not gated on the attendance calendar.

  python dashboard_watchdog.py           -- check, relaunch if not healthy
  python dashboard_watchdog.py --status  -- report health, never relaunch
"""

import json
import logging
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pk_time import now as pk_now
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "timein_logs"
LOG_DIR.mkdir(exist_ok=True)
CONFIG_FILE = BASE_DIR / "config.json"

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


def health(port):
    """
    True only if the dashboard actually answers HTTP.

    An open socket is not enough: a wedged process keeps the port bound while
    serving nothing, which looks identical to 'down' from the phone. /login is
    the one route that answers without a session, so it is the health probe.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return False, "port not listening"
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/login", timeout=8
        ) as resp:
            if resp.status == 200:
                return True, "healthy"
            return False, f"/login returned HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # Answering at all means the app is alive and routing.
        return True, f"healthy (/login returned HTTP {e.code})"
    except Exception as e:
        return False, f"port open but not serving ({type(e).__name__})"


def interpreter():
    venv = BASE_DIR / ".venv-dashboard" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def kill_wedged(port):
    """Clear a process still holding the port, or the relaunch cannot bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception as e:
        log.warning("Could not enumerate sockets to free port %s: %s", port, e)
        return
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") \
                and parts[3] == "LISTENING":
            pid = parts[4]
            log.warning("Killing wedged process %s holding port %s", pid, port)
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=20)


def launch(port):
    # Detached with its own log file so a crash leaves a traceback behind
    # instead of dying invisibly the way the at-logon task did.
    out = open(LOG_DIR / "dashboard_stdout.log", "a", encoding="utf-8", buffering=1)
    out.write(f"\n--- launched by watchdog {pk_now():%Y-%m-%d %H:%M:%S} ---\n")
    subprocess.Popen(
        [interpreter(), "-u", str(BASE_DIR / "dashboard.py")],
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    log.info("Dashboard relaunched on port %s", port)


def main():
    port = load_json(CONFIG_FILE).get("dashboard", {}).get("port", 5000)
    ok, detail = health(port)

    if "--status" in sys.argv:
        print(f"port {port}: {detail}")
        return 0 if ok else 1

    if ok:
        # Quiet on the happy path - this runs every few minutes.
        log.debug("Dashboard %s on port %s", detail, port)
        return 0

    log.warning("Dashboard unhealthy on port %s (%s) - relaunching", port, detail)
    kill_wedged(port)
    launch(port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
