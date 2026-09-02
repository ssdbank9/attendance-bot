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
import time
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pk_time import now as pk_now
from pathlib import Path

from console_guard import silence
silence(Path(__file__).parent / "timein_logs" / "watchdog_stdout.log")
# pythonw.exe leaves stdout/stderr as None; see console_guard.py

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


def _probe(port):
    """
    One probe. True only if the dashboard actually answers HTTP.

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


def health(port, attempts=2, gap=8):
    """Probe, and on failure wait and probe again before declaring death.

    One failed probe is not proof the dashboard is gone. This host uses Modern
    Standby (S0) and dips in and out of low-power idle many times an hour; a
    probe landing inside that transition times out against a perfectly healthy
    process. Proven on 2026-08-26: the watchdog declared the dashboard dead at
    09:06:01, three seconds after resume, and that same process went on to
    serve requests at 09:06:03 and 09:06:06. The machine was throttled hard
    enough that the watchdog could not even spawn a thread to enumerate
    sockets ("can't start new thread").

    Five of six recorded deaths sat within minutes of a standby transition, so
    a single short retry removes that whole class of needless restart. It also
    matters more now the check runs every 30 minutes rather than every 5: a
    false positive used to cost one wasted relaunch, and would now cost a
    perfectly good dashboard being replaced for no reason."""
    first_detail = None
    for attempt in range(1, attempts + 1):
        ok, detail = _probe(port)
        if ok:
            if attempt > 1:
                log.info(
                    "Dashboard healthy on probe %s - first probe said %r; "
                    "treating that as a standby transition, not a dead process",
                    attempt, first_detail,
                )
            return True, detail
        if first_detail is None:
            first_detail = detail
        if attempt < attempts:
            time.sleep(gap)
    return False, f"{first_detail} (confirmed over {attempts} probes)"


def interpreter():
    """The venv's python.exe - deliberately NOT pythonw.exe.

    Windows Firewall rules are per-executable, and this host carries
    auto-generated rules that Allow inbound python.exe but BLOCK inbound
    pythonw.exe (on Private and Public, so the Tailscale route the phone uses
    is blocked too). Serving the dashboard from pythonw.exe therefore makes it
    unreachable from the phone with no error anywhere - the server comes up
    healthy on localhost and simply never receives the connection.

    A console window is not the trade-off here: launch() passes
    CREATE_NO_WINDOW, which runs a console binary with no console attached. So
    python.exe stays firewall-allowed AND draws nothing. Only the scheduled
    task's own entry point needs pythonw.exe, and that process listens on
    nothing.
    """
    venv = BASE_DIR / ".venv-dashboard" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    # No venv (a fresh install): fall back to the console interpreter, NOT to
    # whatever launched us. Under the scheduled task that is pythonw.exe, and
    # serving from pythonw is what the firewall note above warns about.
    # CREATE_NO_WINDOW keeps python.exe windowless regardless.
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def kill_wedged(port):
    """Clear a process still holding the port, or the relaunch cannot bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
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
                           capture_output=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)


def launch(port):
    # Detached with its own log file so a crash leaves a traceback behind
    # instead of dying invisibly the way the at-logon task did.
    out = open(LOG_DIR / "dashboard_stdout.log", "a", encoding="utf-8", buffering=1)
    out.write(f"\n--- launched by watchdog {pk_now():%Y-%m-%d %H:%M:%S} ---\n")
    subprocess.Popen(
        [interpreter(), "-u", str(BASE_DIR / "dashboard.py")],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW,
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
