"""Set the bot's paused state. Usage: python set_pause.py [true|false]

Used by attendance.yml's workflow_dispatch (action=pause/resume) so the
git-hosted dashboard can pause/resume the bot - config.json is local-only,
so this runs on the self-hosted runner rather than being editable via the
GitHub Contents API like the other synced JSON files.
"""
import json
import sys
from pathlib import Path

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

    try:
        from cloud_sync import sync_github_file
        sync_github_file("bot_config.json", json.dumps({"paused": paused}, indent=2),
                          f"Bot {'paused' if paused else 'resumed'} from git dashboard")
    except Exception:
        pass

    from notify import notify
    if paused:
        notify("Bot PAUSED - no attendance will be marked until resumed.",
                title="Bot Paused", priority="high", tags="pause_button")
    else:
        notify("Bot RESUMED - attendance marking is active again.",
                title="Bot Resumed", tags="arrow_forward")
    print(f"paused={paused}")


if __name__ == "__main__":
    main()
