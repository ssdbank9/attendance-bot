"""
Daily evening scripts via Task Scheduler.
  7 PM:  Holiday popup (3 days advance) + auto-refresh holidays.
  10 PM: Tomorrow's plan notification.

Usage:
  python holiday_reminder.py              -- Run all (backward compat)
  python holiday_reminder.py holidays     -- Holiday popup + auto-refresh only (7 PM)
  python holiday_reminder.py tomorrow      -- Tomorrow's plan notification only (10 PM)
"""

import json
import sys
import tkinter as tk
from notify import notify_tomorrow, notify_tomorrow_skipped, notify_tomorrow_holiday, notify_holiday_reminder
from tkinter import messagebox
from datetime import datetime, timedelta
from pk_time import now as pk_now
from pathlib import Path

from console_guard import silence
silence(Path(__file__).parent / "timein_logs" / "holiday_reminder_stdout.log")
# pythonw.exe leaves stdout/stderr as None; see console_guard.py

BASE_DIR = Path(__file__).parent
HOLIDAYS_FILE = BASE_DIR / "holidays.json"
BLACKOUT_FILE = BASE_DIR / "blackout.json"


def load_json(path):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)





def is_holiday(date_str):
    data = load_json(HOLIDAYS_FILE)
    for h in data.get("holidays", []):
        if h["date"] == date_str and not h.get("disabled", False):
            return True, h.get("label", "Holiday")
    return False, None


def is_blacked_out(date_str):
    data = load_json(BLACKOUT_FILE)
    for d in data.get("dates", []):
        if d["date"] == date_str:
            return True, d.get("reason", "Blackout")
    for r in data.get("ranges", []):
        if r["start"] <= date_str <= r["end"]:
            return True, r.get("reason", "Blackout range")
    return False, None


def is_wfh(date_str):
    data = load_json(BLACKOUT_FILE)
    for d in data.get("wfh", []):
        if d["date"] == date_str:
            return True, d.get("reason", "Work from home")
    for r in data.get("wfh_ranges", []):
        if r["start"] <= date_str <= r["end"]:
            return True, r.get("reason", "Work from home")
    return False, None


def get_holidays_in_days(days):
    data = load_json(HOLIDAYS_FILE)
    target = (pk_now() + timedelta(days=days)).strftime("%Y-%m-%d")
    return [h for h in data.get("holidays", []) if h["date"] == target]


def show_holiday_popup(upcoming):
    data = load_json(HOLIDAYS_FILE)

    root = tk.Tk()
    root.title("Time-In Bot - Holiday Confirmation")
    root.geometry("460x350")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    target_date = datetime.strptime(upcoming[0]["date"], "%Y-%m-%d")
    date_label = target_date.strftime("%A, %B %d, %Y")

    tk.Label(root, text="Upcoming Holiday in 3 Days", font=("Segoe UI", 14, "bold"), pady=10).pack()
    tk.Label(root, text=date_label, font=("Segoe UI", 12), fg="#0078D4", pady=5).pack()

    frame = tk.Frame(root, pady=10)
    frame.pack()

    entries = []
    for h in upcoming:
        moon = h.get("moon_dependent", False)
        prefix = "\u263d VERIFY: " if moon else ""
        row = tk.Frame(frame)
        row.pack(fill="x", padx=20, pady=4)
        var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(row, text=f"{prefix}{h['label']}", variable=var,
                            font=("Segoe UI", 11), anchor="w", fg="#B33A00" if moon else "#000")
        cb.pack(side="left")
        entries.append((h, var))

    if any(h.get("moon_dependent") for h in upcoming):
        tk.Label(frame, text="\u263d Moon-dependent - may shift \u00b11 day.", font=("Segoe UI", 9, "italic"), fg="#888", pady=5).pack()

    tk.Label(root, text="Uncheck = bot WILL run.  Leave checked = bot SKIPS.", font=("Segoe UI", 9), fg="#555", pady=8).pack()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=10)

    def do_save(shift=0):
        for h, var in entries:
            if not var.get():
                # Match on label too. populate_year() emits genuine same-date
                # pairs (e.g. "Eid Milad-un-Nabi" and "12 Rabi ul-Awal"), so
                # filtering by date alone silently deleted the OTHER holiday
                # and the bot then marked attendance on a real public holiday.
                data["holidays"] = [
                    x for x in data["holidays"]
                    if not (x["date"] == h["date"] and x["label"] == h["label"])
                ]
            elif shift != 0:
                old = datetime.strptime(h["date"], "%Y-%m-%d")
                new_str = (old + timedelta(days=shift)).strftime("%Y-%m-%d")
                for x in data["holidays"]:
                    if x["date"] == h["date"] and x["label"] == h["label"]:
                        x["date"] = new_str
                        x["confirmed"] = True
            else:
                for x in data["holidays"]:
                    if x["date"] == h["date"] and x["label"] == h["label"]:
                        x["confirmed"] = True
        data["holidays"].sort(key=lambda x: x["date"])
        save_json(HOLIDAYS_FILE, data)
        messagebox.showinfo("Saved", "Holiday schedule updated!")
        root.destroy()

    tk.Button(btn_frame, text="-1 Day", command=lambda: do_save(-1), font=("Segoe UI", 10), padx=10).pack(side="left", padx=5)
    tk.Button(btn_frame, text="Confirm", command=lambda: do_save(0), font=("Segoe UI", 11, "bold"), bg="#0078D4", fg="white", padx=20, pady=3).pack(side="left", padx=5)
    tk.Button(btn_frame, text="+1 Day", command=lambda: do_save(1), font=("Segoe UI", 10), padx=10).pack(side="left", padx=5)

    root.mainloop()


def send_tomorrow_notification():
    tomorrow = pk_now() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    day_name = tomorrow.strftime("%A")

    if tomorrow.weekday() >= 5:
        bl_data = load_json(BLACKOUT_FILE)
        if tomorrow_str not in bl_data.get("working_weekends", []):
            return

    hol, hol_label = is_holiday(tomorrow_str)
    if hol:
        notify_tomorrow_holiday(day_name, hol_label, holiday_date=tomorrow_str)
        return

    blacked, bl_reason = is_blacked_out(tomorrow_str)
    if blacked:
        notify_tomorrow_skipped(day_name, bl_reason)
        return

    wfh, wfh_reason = is_wfh(tomorrow_str)
    if wfh:
        from notify import notify_tomorrow_wfh
        notify_tomorrow_wfh(day_name, wfh_reason)
        return

    notify_tomorrow(day_name, tomorrow_str)
    # No dead man's switch queued here any more. It used to be published to
    # ntfy with a Delay header the night before, so it was delivered at 09:05
    # regardless of what happened - a false alarm on every day the bot worked.
    # cloud_deadman_check.py now runs at 09:20 PKT from a GitHub-hosted runner
    # and only alerts when the synced status shows Time-In really is missing.


def run_holiday_check():
    """Holiday popup + auto-refresh. Runs at 7 PM."""
    from manage_holidays import auto_refresh
    auto_refresh()

    upcoming_3d = get_holidays_in_days(3)
    if upcoming_3d:
        # Notify FIRST. show_holiday_popup() calls root.mainloop(), which blocks
        # until someone clicks it - and this task has no ExecutionTimeLimit. With
        # the notify after the popup, an unattended desktop meant the phone alert
        # (the only channel that reaches an absent user) was never sent at all.
        for h in upcoming_3d:
            notify_holiday_reminder(h["label"], h["date"], days_until=3, moon_dependent=h.get("moon_dependent", False))
        show_holiday_popup(upcoming_3d)


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if mode == "holidays":
        run_holiday_check()
    elif mode == "tomorrow":
        send_tomorrow_notification()
    else:
        run_holiday_check()
        send_tomorrow_notification()


if __name__ == "__main__":
    main()