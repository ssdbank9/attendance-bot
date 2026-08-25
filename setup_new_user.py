"""
One-click setup for a new TimeIn Bot user.
Run: python setup_new_user.py

Creates config, installs dependencies, sets up scheduled tasks,
and generates a unique ntfy topic for push notifications.
"""

import json
import os
import random
import string
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def generate_ntfy_topic(user_id):
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"timeinbot-{user_id}-{suffix}"


def get_input(prompt, default=None):
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    while True:
        val = input(f"{prompt}: ").strip()
        if val:
            return val
        print("  This field is required.")


def setup_config():
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        print("\n[!] config.json already exists.")
        overwrite = input("    Overwrite? (y/N): ").strip().lower()
        if overwrite != 'y':
            print("    Keeping existing config.")
            with open(config_path) as f:
                return json.load(f)

    print("\n--- Credentials ---")
    user_id = get_input("  Employee/User ID")
    password = get_input("  Password")

    print("\n--- Portal Login (one.aku.edu) ---")
    print("  Drives the saved portal page for the Selenium fallback.")
    portal_user = get_input("  Portal username (e.g. firstname.lastname)")
    portal_pass = get_input("  Portal password")

    print("\n--- Time Windows (press Enter for defaults) ---")
    ti_start = get_input("  Time-In window start (HH:MM)", "08:45")
    ti_end = get_input("  Time-In window end (HH:MM)", "09:05")
    to_start = get_input("  Time-Out window start (HH:MM)", "20:00")
    to_end = get_input("  Time-Out window end (HH:MM)", "21:30")

    ntfy_topic = generate_ntfy_topic(user_id)

    config = {
        "credentials": {
            "user_id": user_id,
            "password": password,
        },
        "timein": {
            "window_start": ti_start,
            "primary_end": "09:00",
            "window_end": ti_end,
            "primary_weight": 0.85,
        },
        "timeout": {
            "window_start": to_start,
            "primary_end": "21:00",
            "window_end": to_end,
            "primary_weight": 0.85,
        },
        "retry": {
            "max_attempts": 3,
            "delay_seconds": 30,
        },
        "notifications": {
            "ntfy_topic": ntfy_topic,
            "ntfy_server": "https://ntfy.sh",
        },
        "dashboard": {
            "port": 5000,
            "host": "0.0.0.0",
        },
        "portal": {
            "enabled": True,
            "url": "https://one.aku.edu/Pages/homepk.aspx",
            "username": portal_user,
            "password": portal_pass,
        },
        "paused": False,
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    print(f"\n  Config saved! Your ntfy topic: {ntfy_topic}")
    print(f"  Subscribe in the ntfy app to: {ntfy_topic}")
    return config


def setup_json_files():
    holidays_path = BASE_DIR / "holidays.json"
    if not holidays_path.exists():
        with open(holidays_path, "w") as f:
            json.dump({"holidays": []}, f, indent=2)
        print("  Created holidays.json")

    blackout_path = BASE_DIR / "blackout.json"
    if not blackout_path.exists():
        with open(blackout_path, "w") as f:
            json.dump({"dates": [], "ranges": []}, f, indent=2)
        print("  Created blackout.json")

    status_path = BASE_DIR / "timein_status.json"
    if not status_path.exists():
        with open(status_path, "w") as f:
            json.dump({}, f, indent=2)
        print("  Created timein_status.json")

    logs_dir = BASE_DIR / "timein_logs"
    logs_dir.mkdir(exist_ok=True)
    print("  Log directory ready")


def install_dependencies():
    print("\n--- Installing Python Dependencies ---")
    packages = ["selenium", "flask", "hijri-converter"]
    for pkg in packages:
        print(f"  Installing {pkg}...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True,
                text=True,
                check=True,
                creationflags=NO_WINDOW,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "pip returned a non-zero exit code").strip()
            print(f"  [!] Failed to install {pkg}: {detail[:500]}")
            print("  Setup aborted. No scheduled tasks were registered.")
            raise SystemExit(1)
    print("  All dependencies installed.")


def populate_holidays():
    print("\n--- Populating Holidays ---")
    from pk_time import now as pk_now
    year = pk_now().year
    try:
        subprocess.run(
            [sys.executable, str(BASE_DIR / "manage_holidays.py"), "populate", str(year)],
            check=True,
            creationflags=NO_WINDOW,
        )
        print(f"  Holidays populated for {year}.")
    except Exception as e:
        print(f"  Warning: Could not populate holidays: {e}")
        print(f"  Run manually: python manage_holidays.py populate {year}")


def quiet_interpreter():
    """pythonw.exe beside the running interpreter, or None if absent.

    Scheduled tasks must not run python.exe. It is a console binary, so Windows
    draws a black console window every time a task fires - several times a day,
    including mid-presentation. pythonw.exe has no console to show.
    """
    quiet = Path(sys.executable).with_name("pythonw.exe")
    return str(quiet) if quiet.exists() else None


def _task_ps(name, exe, bot_dir, script, args, trigger, limit_min,
             wake=False, extra=""):
    """One Register-ScheduledTask block. Hidden + pythonw is what keeps the bot
    off screen; the battery and StartWhenAvailable flags stop a laptop that was
    asleep or unplugged from silently skipping a day."""
    wake_flag = " -WakeToRun" if wake else ""
    return f"""
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{bot_dir}\\{script}"{args}' -WorkingDirectory '{bot_dir}'
{trigger}
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable{wake_flag} -ExecutionTimeLimit (New-TimeSpan -Minutes {limit_min}) -MultipleInstances IgnoreNew
$s.Hidden = $true
{extra}Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
Write-Output '  created {name}'"""


def create_scheduled_tasks():
    print("\n--- Setting Up Scheduled Tasks ---")
    bot_dir = str(BASE_DIR)
    exe = quiet_interpreter()
    if not exe:
        print("  [!] pythonw.exe not found beside this interpreter.")
        print("      Setup aborted before task registration; install a Python build with pythonw.exe.")
        raise SystemExit(1)
    print(f"  Using {exe} (no console window)")

    blocks = ["$ErrorActionPreference = 'Stop'"]

    # WakeToRun only on the attendance pair: they hand off to a one-shot task at
    # a randomized instant and the host has to be awake for it. The reminders
    # can wait for the next time the machine is up.
    for name, at, script, args, wake in (
        ("TimeInBot", "08:45", "timein_bot.py", " timein", True),
        ("TimeInBot_TimeOut", "20:00", "timein_bot.py", " timeout", True),
        ("TimeInBot_HolidayReminder", "19:00", "holiday_reminder.py", " holidays", False),
        ("TimeInBot_TomorrowPlan", "22:00", "holiday_reminder.py", " tomorrow", False),
    ):
        blocks.append(_task_ps(
            name, exe, bot_dir, script, args,
            f"$t = New-ScheduledTaskTrigger -Daily -At '{at}'",
            25, wake=wake,
        ))

    # The watchdog owns the dashboard. It is the single launch path: it restarts
    # the dashboard whenever it stops serving, and the at-logon task points at
    # the watchdog rather than dashboard.py so two paths cannot disagree about
    # how the dashboard is started.
    repeat = (
        "$t = New-ScheduledTaskTrigger -Daily -At '00:05'\n"
        "$t.Repetition = (New-ScheduledTaskTrigger -Once -At '00:05' "
        "-RepetitionInterval (New-TimeSpan -Minutes 5) "
        "-RepetitionDuration (New-TimeSpan -Days 1)).Repetition\n"
        "$logon = New-ScheduledTaskTrigger -AtLogOn"
    )
    blocks.append(_task_ps(
        "TimeInBot_DashboardWatchdog", exe, bot_dir, "dashboard_watchdog.py", "",
        repeat, 10, extra="$t = @($t, $logon)\n",
    ))
    blocks.append(_task_ps(
        "TimeInBot_Dashboard", exe, bot_dir, "dashboard_watchdog.py", "",
        "$t = New-ScheduledTaskTrigger -AtLogOn", 10,
    ))

    script_text = "\n".join(blocks)
    if "--dry-run" in sys.argv:
        print("\n  DRY RUN - PowerShell that would run:\n")
        print(script_text)
        return script_text

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script_text],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    for line in (result.stdout or "").splitlines():
        print(line)
    if result.returncode != 0:
        print("  [!] Task creation failed:")
        print("     ", (result.stderr or "").strip()[:400])
        print("      Re-run this script from an Administrator PowerShell.")
    return script_text


def start_dashboard():
    print("\n--- Starting Dashboard ---")
    # Through the watchdog, so the very first launch takes the same path every
    # later restart will, instead of a one-off that behaves differently.
    subprocess.Popen(
        [sys.executable, str(BASE_DIR / "dashboard_watchdog.py")],
        creationflags=0x08000000,
    )
    print("  Dashboard starting on http://localhost:5000")


def print_ntfy_instructions(config):
    topic = config["notifications"]["ntfy_topic"]
    print("\n" + "=" * 55)
    print("  SETUP COMPLETE!")
    print("=" * 55)
    print(f"""
  Next steps:

  1. PHONE NOTIFICATIONS
     Install the ntfy app on your phone:
       Android: play.google.com/store/apps/details?id=io.heckel.ntfy
       iOS:     apps.apple.com/app/ntfy/id1625396347
     Open the app and subscribe to: {topic}

  2. DASHBOARD
     Open http://localhost:5000 on this PC
     (For phone access on same WiFi, use your PC's local IP)

  3. OPTIONAL: TAILSCALE (phone access from any network)
     Install Tailscale on PC + phone
     Then run: python setup_new_user.py --set-tailscale <IP>

  4. CHECK IT IS SILENT AND REACHABLE
     powershell -ExecutionPolicy Bypass -File check_quiet.ps1
     Expect "RESULT: PASS". This confirms no task draws a console window
     and the dashboard is actually serving.

  5. TEST IT  (careful - these mark REAL attendance immediately)
     python timein_bot.py timein --now
     python timein_bot.py timeout --now

  REQUIREMENTS
     * You must be on the AKU network. portalservice.aku.edu is a private
       address; there is no path that works off-network.
     * This PC must be awake around your Time-In window. The bot registers a
       wake-capable one-shot task for its randomized time, so sleep is fine,
       but the machine cannot be shut down.

  NOT SET UP (optional, needs your own GitHub repo)
     cloud_sync + the 09:20 deadman alert are not configured. Without them
     everything still works; you just get no warning on a morning this PC
     never came on. To enable: add a cloud_sync.github block to config.json
     and set the NTFY_TOPIC repository secret.

  Your ntfy topic (keep private - anyone with it can read and send
  your notifications): {topic}
""")


def set_tailscale_ip(ip):
    config_path = BASE_DIR / "config.json"
    with open(config_path) as f:
        config = json.load(f)
    config["dashboard"]["tailscale_ip"] = ip
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"Tailscale IP set to {ip}")
    print(f"Dashboard accessible at: http://{ip}:5000")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--set-tailscale":
        if len(sys.argv) < 3:
            print("Usage: python setup_new_user.py --set-tailscale <IP>")
            return
        set_tailscale_ip(sys.argv[2])
        return

    print("=" * 55)
    print("  TimeIn Bot - New User Setup")
    print("=" * 55)
    print(f"\n  Install directory: {BASE_DIR}")
    print("  This will set up everything you need.\n")

    config = setup_config()
    setup_json_files()
    install_dependencies()
    populate_holidays()
    create_scheduled_tasks()
    start_dashboard()
    print_ntfy_instructions(config)


if __name__ == "__main__":
    main()
