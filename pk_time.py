"""
Pakistan Standard Time helpers.

Every date and time decision in this project is a Pakistan wall-clock decision:
the attendance windows, which day a holiday or leave entry falls on, and which
day "today" is. Those were all read from datetime.now(), which follows whatever
timezone the host happens to be set to - correct only for as long as the host
stays on PKT.

now() returns a NAIVE datetime carrying PKT wall-clock time, so it is a drop-in
replacement for datetime.now(). Naive is deliberate: the rest of the codebase
builds naive datetimes (datetime(y, m, d, h, m)) and compares them directly, and
mixing aware and naive values raises TypeError at runtime.

Pakistan has not observed DST since 2009, so the offset is a flat UTC+5 with no
seasonal cases to handle.

Not covered: Windows Task Scheduler fires its triggers on host wall-clock, so
the 08:45/20:00 start times still follow the machine. This module governs what
the code decides once it is running, not when Windows starts it.
"""

from datetime import datetime, timedelta, timezone

PKT = timezone(timedelta(hours=5), "PKT")


def now():
    """Current PKT wall-clock time, naive (drop-in for datetime.now())."""
    return datetime.now(PKT).replace(tzinfo=None)


def today_str():
    """Today's date in PKT as YYYY-MM-DD."""
    return now().strftime("%Y-%m-%d")
