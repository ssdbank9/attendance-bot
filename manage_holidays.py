"""
Manage public holidays for the Time-In bot.
Usage:
    python manage_holidays.py add 2026-12-25 "Christmas Day"
    python manage_holidays.py add 2026-12-25 "Christmas Day" --moon
    python manage_holidays.py remove 2026-12-25
    python manage_holidays.py list
    python manage_holidays.py upcoming
    python manage_holidays.py shift 2026-08-25 +1
    python manage_holidays.py shift 2026-08-25 -1
    python manage_holidays.py populate 2028
"""

import json
import sys
from datetime import datetime, timedelta
from pk_time import now as pk_now
from pathlib import Path

HOLIDAYS_FILE = Path(__file__).parent / "holidays.json"


def load():
    if HOLIDAYS_FILE.exists():
        with open(HOLIDAYS_FILE, "r") as f:
            return json.load(f)
    return {"holidays": []}


def save(data):
    with open(HOLIDAYS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_holiday(date_str, label="", moon=False):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date format '{date_str}'. Use YYYY-MM-DD.")
        return

    data = load()
    for h in data["holidays"]:
        if h["date"] == date_str:
            print(f"Already exists: {date_str} ({h.get('label', '')})")
            return

    entry = {"date": date_str, "label": label, "confirmed": False}
    if moon:
        entry["moon_dependent"] = True
    data["holidays"].append(entry)
    data["holidays"].sort(key=lambda h: h["date"])
    save(data)
    print(f"Added: {date_str} - {label}")


def remove_holiday(date_str):
    data = load()
    before = len(data["holidays"])
    data["holidays"] = [h for h in data["holidays"] if h["date"] != date_str]
    if len(data["holidays"]) < before:
        save(data)
        print(f"Removed: {date_str}")
    else:
        print(f"Not found: {date_str}")


def shift_holiday(date_str, offset):
    data = load()
    for h in data["holidays"]:
        if h["date"] == date_str:
            old = datetime.strptime(date_str, "%Y-%m-%d")
            new = old + timedelta(days=offset)
            new_str = new.strftime("%Y-%m-%d")
            h["date"] = new_str
            h["confirmed"] = True
            data["holidays"].sort(key=lambda x: x["date"])
            save(data)
            print(f"Shifted: {date_str} -> {new_str} ({h.get('label', '')})")
            return
    print(f"Not found: {date_str}")


def list_holidays():
    data = load()
    if not data["holidays"]:
        print("No holidays configured.")
        return
    today = pk_now().date()
    print(f"{'Date':<14} {'Day':<10} {'Status':<12} Label")
    print("-" * 65)
    for h in data["holidays"]:
        d = datetime.strptime(h["date"], "%Y-%m-%d").date()
        day_name = d.strftime("%a")
        moon = " \u263d" if h.get("moon_dependent") else ""
        confirmed = "confirmed" if h.get("confirmed") else "tentative"
        marker = ""
        if d < today:
            marker = " (past)"
        elif d == today:
            marker = " << TODAY"
        print(f"{h['date']:<14} {day_name:<10} {confirmed:<12} {h.get('label', '')}{moon}{marker}")


def upcoming():
    data = load()
    today = pk_now().date()
    print(f"\nUpcoming holidays (next 30 days):")
    found = False
    for h in data["holidays"]:
        d = datetime.strptime(h["date"], "%Y-%m-%d").date()
        diff = (d - today).days
        if 0 <= diff <= 30:
            moon = " [MOON SIGHTING]" if h.get("moon_dependent") else ""
            confirmed = "CONFIRMED" if h.get("confirmed") else "TENTATIVE"
            print(f"  {h['date']} ({d.strftime('%A')}) in {diff}d - {h.get('label', '')} [{confirmed}]{moon}")
            found = True
    if not found:
        print("  None in the next 30 days.")

    print(f"\nNext 7 working days:")
    for i in range(1, 10):
        day = today + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        is_hol = any(
            datetime.strptime(x["date"], "%Y-%m-%d").date() == day
            for x in data["holidays"]
        )
        status = "HOLIDAY - bot will SKIP" if is_hol else "bot will RUN"
        print(f"  {day.strftime('%Y-%m-%d')} ({day.strftime('%A')}) - {status}")


PAKISTAN_FIXED_HOLIDAYS = [
    ("01-01", "New Year's Day"),
    ("02-05", "Kashmir Day"),
    ("03-23", "Pakistan Day"),
    ("05-01", "Labour Day"),
    ("08-14", "Independence Day"),
    ("11-09", "Iqbal Day"),
    ("12-25", "Quaid-e-Azam Day"),
]

ISLAMIC_HOLIDAYS_HIJRI = [
    ("Shab-e-Meraj", 7, 27),
    ("Shab-e-Barat", 8, 15),
    ("Shab-e-Qadr", 9, 27),
    ("Eid ul-Fitr (Day 1)", 10, 1),
    ("Eid ul-Fitr (Day 2)", 10, 2),
    ("Eid ul-Fitr (Day 3)", 10, 3),
    ("Eid ul-Adha (Day 1)", 12, 10),
    ("Eid ul-Adha (Day 2)", 12, 11),
    ("Eid ul-Adha (Day 3)", 12, 12),
    ("Eid Milad-un-Nabi", 3, 12),
    ("12 Rabi ul-Awal", 3, 12),
    ("1st Muharram", 1, 1),
    ("9th Muharram", 1, 9),
    ("10th Muharram (Ashura)", 1, 10),
]


def estimate_islamic_dates(year):
    """Estimate Gregorian dates for Islamic holidays in a given year using Hijri calendar."""
    try:
        from hijri_converter import Gregorian as HijriGregorian, Hijri
    except ImportError:
        print("WARNING: hijri-converter not installed. Using placeholders.")
        return {}

    mid_year = HijriGregorian(year, 7, 1).to_hijri()
    hijri_year = mid_year.year

    results = {}
    seen_labels = set()
    for label, month, day in ISLAMIC_HOLIDAYS_HIJRI:
        if label in seen_labels:
            continue
        seen_labels.add(label)
        for hy in [hijri_year - 1, hijri_year, hijri_year + 1]:
            try:
                greg = Hijri(hy, month, day).to_gregorian()
                if greg.year == year:
                    results[label] = greg.strftime("%Y-%m-%d")
                    break
            except (ValueError, OverflowError):
                continue
    return results


def populate_year(year):
    data = load()
    existing_labels = {h["label"] for h in data["holidays"] if h["date"].startswith(str(year))}
    added = 0

    for mm_dd, label in PAKISTAN_FIXED_HOLIDAYS:
        date_str = f"{year}-{mm_dd}"
        if label not in existing_labels:
            entry = {"date": date_str, "label": label, "confirmed": True}
            data["holidays"].append(entry)
            added += 1
            print(f"  Added fixed: {date_str} - {label}")

    estimates = estimate_islamic_dates(year)
    print(f"\n  Estimating moon-dependent holidays via Hijri calendar:")
    for label, month, day in ISLAMIC_HOLIDAYS_HIJRI:
        if label in existing_labels:
            continue
        est_date = estimates.get(label)
        if est_date:
            entry = {"date": est_date, "label": label, "confirmed": False, "moon_dependent": True}
            data["holidays"].append(entry)
            added += 1
            print(f"  Added tentative: {est_date} - {label} (estimated, confirm from dashboard)")
        else:
            placeholder = f"{year}-01-01"
            entry = {"date": placeholder, "label": f"[SET DATE] {label}", "confirmed": False, "moon_dependent": True}
            data["holidays"].append(entry)
            added += 1
            print(f"  No estimate for {label} - added placeholder")

    data["holidays"].sort(key=lambda h: h["date"])
    save(data)
    print(f"\nPopulated {added} holidays for {year}.")
    print("Tentative dates are estimated from the Hijri calendar.")
    print("Actual dates may differ by 1-2 days based on moon sighting.")
    print("Confirm or shift each holiday from the dashboard or notification.")


def auto_refresh():
    """Check if all holidays have passed and auto-populate next year."""
    data = load()
    today = pk_now().strftime("%Y-%m-%d")
    current_year = pk_now().year
    next_year = current_year + 1

    future = [h for h in data.get("holidays", []) if h["date"] >= today and not h.get("disabled", False)]
    has_next_year = any(h["date"].startswith(str(next_year)) for h in data.get("holidays", []))

    if not future and not has_next_year:
        print(f"All holidays have passed. Auto-populating {next_year}...")
        populate_year(next_year)
        return True

    if len(future) <= 3 and not has_next_year:
        print(f"Only {len(future)} holidays remaining. Pre-populating {next_year}...")
        populate_year(next_year)
        return True

    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "add" and len(sys.argv) >= 3:
        label = sys.argv[3] if len(sys.argv) > 3 else ""
        moon = "--moon" in sys.argv
        add_holiday(sys.argv[2], label, moon)
    elif cmd == "remove" and len(sys.argv) >= 3:
        remove_holiday(sys.argv[2])
    elif cmd == "shift" and len(sys.argv) >= 4:
        shift_holiday(sys.argv[2], int(sys.argv[3]))
    elif cmd == "list":
        list_holidays()
    elif cmd == "upcoming":
        upcoming()
    elif cmd == "populate" and len(sys.argv) >= 3:
        try:
            year = int(sys.argv[2])
            populate_year(year)
        except ValueError:
            print("ERROR: Year must be a number, e.g. 2028")
    elif cmd == "auto-refresh":
        auto_refresh()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()