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
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True,
        )
    print("  All dependencies installed.")


def populate_holidays():
    print("\n--- Populating Holidays ---")
    from datetime import datetime
    year = datetime.now().year
    try:
        subprocess.run(
            [sys.executable, str(BASE_DIR / "manage_holidays.py"), "populate", str(year)],
            check=True,
        )
        print(f"  Holidays populated for {year}.")
    except Exception as e:
        print(f"  Warning: Could not populate holidays: {e}")
        print(f"  Run manually: python manage_holidays.py populate {year}")


def create_scheduled_tasks():
    print("\n--- Setting Up Scheduled Tasks ---")
    python_exe = sys.executable
    bot_dir = str(BASE_DIR)

    tasks = [
        {
            "name": "TimeInBot",
            "time": "08:45",
            "args": f'"{python_exe}" "{bot_dir}\\timein_bot.py" timein',
            "desc": "Daily time-in at 8:45 AM",
        },
        {
            "name": "TimeInBot_TimeOut",
            "time": "20:00",
            "args": f'"{python_exe}" "{bot_dir}\\timein_bot.py" timeout',
            "desc": "Daily time-out at 8:00 PM",
        },
        {
            "name": "TimeInBot_HolidayReminder",
            "time": "19:00",
            "args": f'"{python_exe}" "{bot_dir}\\holiday_reminder.py" holidays',
            "desc": "Holiday check at 7:00 PM",
        },
        {
            "name": "TimeInBot_TomorrowPlan",
            "time": "22:00",
            "args": f'"{python_exe}" "{bot_dir}\\holiday_reminder.py" tomorrow',
            "desc": "Tomorrow plan at 10:00 PM",
        },
    ]

    for task in tasks:
        print(f"\n  Creating: {task['name']} ({task['desc']})")
        cmd = (
            f'schtasks /Create /TN "{task["name"]}" '
            f'/TR "{task["args"]}" '
            f'/SC DAILY /ST {task["time"]} '
            f'/F /RL HIGHEST'
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"    OK")
        else:
            print(f"    Failed: {result.stderr.strip()}")
            print(f"    Run manually as admin if needed.")

    print(f"\n  Creating: TimeInBot_Dashboard (at logon)")
    launcher = BASE_DIR / "start_dashboard.ps1"
    dash_action = (
        "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden "
        f'-ExecutionPolicy Bypass -File "{launcher}"'
    )
    result = subprocess.run(
        [
            "schtasks", "/Create",
            "/TN", "TimeInBot_Dashboard",
            "/TR", dash_action,
            "/SC", "ONLOGON",
            "/F",
            "/RL", "LIMITED",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"    OK")
    else:
        print(f"    Failed (may need admin): {result.stderr.strip()}")


def start_dashboard():
    print("\n--- Starting Dashboard ---")
    python_exe = sys.executable
    subprocess.Popen(
        [python_exe, str(BASE_DIR / "dashboard.py")],
        creationflags=0x08000000,
    )
    print("  Dashboard started on http://localhost:5000")


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

  4. TEST IT
     python timein_bot.py timein --now   (test time-in)
     python timein_bot.py timeout --now  (test time-out)

  Your ntfy topic (keep private): {topic}
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
