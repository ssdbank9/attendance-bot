"""Make the scripts safe to run under pythonw.exe.

The scheduled tasks run pythonw.exe rather than python.exe so Windows never
draws a console window (a black box popping up mid-presentation was the whole
reason). pythonw gives the process no standard streams at all: sys.stdout and
sys.stderr are None, so any print() raises AttributeError and logging's
StreamHandler silently swallows every record.

Point the missing streams somewhere real. Prefer a log file over the null
device: an uncaught exception's traceback goes to stderr, and discarding it is
how the at-logon dashboard task used to "die invisibly" - the exact problem the
watchdog's own redirect-to-a-file was added to solve.

Import and call silence() before configuring logging or printing anything.
"""

import os
import sys


def silence(log_path=None):
    """Replace any None standard stream so print()/logging cannot crash.

    log_path: append stdout and stderr here, so a crash leaves a traceback
    behind. Falls back to the null device if the file cannot be opened.
    Idempotent - a stream that already exists is left alone.
    """
    sink = None
    if log_path is not None:
        try:
            os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
            sink = open(str(log_path), "a", encoding="utf-8", buffering=1)
        except OSError:
            sink = None

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            target = sink
            if target is None:
                try:
                    target = open(os.devnull, "w", encoding="utf-8")
                except OSError:
                    continue
            setattr(sys, name, target)

    if getattr(sys, "stdin", None) is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        except OSError:
            pass
