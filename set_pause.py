"""Set the bot's paused state. Usage: python set_pause.py [true|false]

Used by attendance.yml's workflow_dispatch (action=pause/resume) so the
git-hosted dashboard can pause/resume the bot - config.json is local-only,
so this runs on the self-hosted runner rather than being editable via the
GitHub Contents API like the other synced JSON files.
"""
import json
import sys
from pathlib import Path
from pk_time import now as pk_now, PKT

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("true", "false"):
        print("Usage: python set_pause.py [true|false]")
        sys.exit(1)
    paused = sys.argv[1] == "true"

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["paused"] = paused
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    from cloud_sync import sync_github_file
    pause_state = {
        "paused": paused,
        "updated_at": pk_now().replace(tzinfo=PKT).isoformat(timespec="seconds"),
    }
    sync_ok = False
    sync_message = "unknown sync failure"
    for _attempt in range(2):
        try:
            sync_ok, sync_message = sync_github_file(
                "bot_config.json",
                json.dumps(pause_state, indent=2),
                f"Bot {'paused' if paused else 'resumed'} from git dashboard",
            )
        except Exception as exc:
            sync_ok, sync_message = False, str(exc)
        if sync_ok:
            break

    from notify import notify
    if paused:
        notify("Bot PAUSED - no attendance will be marked until resumed.",
                title="Bot Paused", priority="high", tags="pause_button")
    else:
        notify("Bot RESUMED - attendance marking is active again.",
                title="Bot Resumed", tags="arrow_forward")
    print(f"paused={paused}")
    if not sync_ok:
        print(f"GitHub pause-state sync failed after retry: {sync_message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
