"""Apply new time windows from TI_START/TI_END/TO_START/TO_END env vars.

Run by attendance.yml (action=update-windows) on the self-hosted runner.
These are plain (non-secret) workflow_dispatch inputs - just clock times,
no sensitivity - unlike credentials/portal-password. Mirrors
dashboard.py's /action/update-windows route (primary_end = start + 75%
of the window).
"""
import json
import os
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def compute_primary_end(start, end):
    s = datetime.strptime(start, "%H:%M")
    e = datetime.strptime(end, "%H:%M")
    pe = s + (e - s) * 0.75
    return pe.strftime("%H:%M")


def main():
    ti_start = os.environ.get("TI_START", "").strip()
    ti_end = os.environ.get("TI_END", "").strip()
    to_start = os.environ.get("TO_START", "").strip()
    to_end = os.environ.get("TO_END", "").strip()
    if not all([ti_start, ti_end, to_start, to_end]):
        print("Missing one or more window inputs - nothing changed")
        return

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    config["timein"]["window_start"] = ti_start
    config["timein"]["window_end"] = ti_end
    config["timein"]["primary_end"] = compute_primary_end(ti_start, ti_end)
    config["timeout"]["window_start"] = to_start
    config["timeout"]["window_end"] = to_end
    config["timeout"]["primary_end"] = compute_primary_end(to_start, to_end)

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    from notify import notify
    notify(f"Time windows updated. In: {ti_start}-{ti_end}, Out: {to_start}-{to_end}",
           title="Windows Updated", tags="clock3")
    try:
        from cloud_sync import sync_time_windows
        sync_time_windows(ti_start, to_start)
    except Exception:
        pass
    print(f"Windows updated: TI {ti_start}-{ti_end}, TO {to_start}-{to_end}")


if __name__ == "__main__":
    main()
