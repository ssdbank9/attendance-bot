"""
Manage blackout dates (sick days, leave, custom skips) for the attendance bot.

Usage:
    python manage_blackout.py skip tomorrow "Sick day"
    python manage_blackout.py skip 2026-08-20 "Doctor appointment"
    python manage_blackout.py range 2026-09-01 2026-09-05 "Annual leave"
    python manage_blackout.py cancel 2026-08-20
    python manage_blackout.py cancel-range 2026-09-01 2026-09-05
    python manage_blackout.py list
    python manage_blackout.py clear-past
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BLACKOUT_FILE = Path(__file__).parent / "blackout.json"


def load():
    if BLACKOUT_FILE.exists():
        with open(BLACKOUT_FILE, "r") as f:
            return json.load(f)
    return {"dates": [], "ranges": []}


def save(data):
    with open(BLACKOUT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def resolve_date(s):
    s = s.lower().strip()
    today = datetime.now().date()
    if s == "today":
        return today.strftime("%Y-%m-%d")
    if s == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if s in day_names:
        target = day_names.index(s)
        diff = (target - today.weekday()) % 7
        if diff == 0:
            diff = 7
        return (today + timedelta(days=diff)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        print(f"ERROR: Cannot parse date '{s}'. Use YYYY-MM-DD, 'today', 'tomorrow', or a day name.")
        sys.exit(1)


def is_blacked_out(date_str, data):
    for d in data["dates"]:
        if d["date"] == date_str:
            return True, d.get("reason", "")
    for r in data["ranges"]:
        if r["start"] <= date_str <= r["end"]:
            return True, r.get("reason", "")
    return False, None


def skip_date(date_input, reason=""):
    date_str = resolve_date(date_input)
    data = load()
    already, _ = is_blacked_out(date_str, data)
    if already:
        print(f"Already skipped: {date_str}")
        return
    data["dates"].append({
        "date": date_str,
        "reason": reason,
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    data["dates"].sort(key=lambda d: d["date"])
    save(data)
    day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    print(f"SKIPPED: {date_str} ({day_name}) - {reason}")


def skip_range(start_input, end_input, reason=""):
    start = resolve_date(start_input)
    end = resolve_date(end_input)
    if start > end:
        start, end = end, start
    data = load()
    for r in data["ranges"]:
        if r["start"] == start and r["end"] == end:
            print(f"Range already exists: {start} to {end}")
            return
    data["ranges"].append({
        "start": start,
        "end": end,
        "reason": reason,
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    data["ranges"].sort(key=lambda r: r["start"])
    save(data)
    s_day = datetime.strptime(start, "%Y-%m-%d").strftime("%A")
    e_day = datetime.strptime(end, "%Y-%m-%d").strftime("%A")
    num_days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
    print(f"SKIPPED RANGE: {start} ({s_day}) to {end} ({e_day}) [{num_days} days] - {reason}")


def cancel_date(date_input):
    date_str = resolve_date(date_input)
    data = load()
    before = len(data["dates"])
    data["dates"] = [d for d in data["dates"] if d["date"] != date_str]
    if len(data["dates"]) < before:
        save(data)
        print(f"CANCELLED skip for {date_str} - bot WILL run")
    else:
        print(f"No skip found for {date_str}")


def cancel_range(start_input, end_input):
    start = resolve_date(start_input)
    end = resolve_date(end_input)
    data = load()
    before = len(data["ranges"])
    data["ranges"] = [r for r in data["ranges"] if not (r["start"] == start and r["end"] == end)]
    if len(data["ranges"]) < before:
        save(data)
        print(f"CANCELLED range {start} to {end} - bot WILL run those days")
    else:
        print(f"No range found matching {start} to {end}")


def list_blackouts():
    data = load()
    today = datetime.now().date()
    has_any = False

    active_dates = [d for d in data["dates"] if datetime.strptime(d["date"], "%Y-%m-%d").date() >= today]
    active_ranges = [r for r in data["ranges"] if datetime.strptime(r["end"], "%Y-%m-%d").date() >= today]

    if active_dates:
        has_any = True
        print("Single-day skips:")
        for d in active_dates:
            dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
            diff = (dt - today).days
            day_name = dt.strftime("%A")
            when = "TODAY" if diff == 0 else f"in {diff}d" if diff > 0 else "past"
            print(f"  {d['date']} ({day_name}) [{when}] - {d.get('reason', '')}")

    if active_ranges:
        has_any = True
        print("Date ranges:")
        for r in active_ranges:
            s = datetime.strptime(r["start"], "%Y-%m-%d").date()
            e = datetime.strptime(r["end"], "%Y-%m-%d").date()
            num = (e - s).days + 1
            print(f"  {r['start']} to {r['end']} [{num} days] - {r.get('reason', '')}")

    if not has_any:
        print("No active blackouts. Bot will run every weekday (except holidays).")

    print(f"\nNext 5 working days:")
    count = 0
    for i in range(0, 14):
        day = today + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        blacked, reason = is_blacked_out(day.strftime("%Y-%m-%d"), data)
        status = f"SKIPPED ({reason})" if blacked else "will run"
        marker = " << TODAY" if i == 0 else ""
        print(f"  {day.strftime('%Y-%m-%d')} ({day.strftime('%A')}) - {status}{marker}")
        count += 1
        if count >= 5:
            break


def clear_past():
    data = load()
    today = datetime.now().strftime("%Y-%m-%d")
    before_d = len(data["dates"])
    before_r = len(data["ranges"])
    data["dates"] = [d for d in data["dates"] if d["date"] >= today]
    data["ranges"] = [r for r in data["ranges"] if r["end"] >= today]
    removed = (before_d - len(data["dates"])) + (before_r - len(data["ranges"]))
    save(data)
    print(f"Cleared {removed} past entries.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "skip" and len(sys.argv) >= 3:
        reason = sys.argv[3] if len(sys.argv) > 3 else ""
        skip_date(sys.argv[2], reason)
    elif cmd == "range" and len(sys.argv) >= 4:
        reason = sys.argv[4] if len(sys.argv) > 4 else ""
        skip_range(sys.argv[2], sys.argv[3], reason)
    elif cmd == "cancel" and len(sys.argv) >= 3:
        cancel_date(sys.argv[2])
    elif cmd == "cancel-range" and len(sys.argv) >= 4:
        cancel_range(sys.argv[2], sys.argv[3])
    elif cmd == "list":
        list_blackouts()
    elif cmd == "clear-past":
        clear_past()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()