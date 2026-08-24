"""Apply new bot login credentials from NEW_USER_ID/NEW_PASSWORD env vars.

Optional local-only helper for applying credentials from environment
variables. The GitHub workflow deliberately does not invoke this helper or
transport credentials.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def main():
    new_uid = os.environ.get("NEW_USER_ID", "").strip()
    new_pw = os.environ.get("NEW_PASSWORD", "").strip()
    if not new_uid or not new_pw:
        print("Missing NEW_USER_ID/NEW_PASSWORD secret - nothing changed")
        return

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    config["credentials"]["user_id"] = new_uid
    config["credentials"]["password"] = new_pw
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    from notify import notify
    notify("Attendance credentials updated locally.", title="Credentials Changed", tags="key")
    print("Attendance credentials updated locally")


if __name__ == "__main__":
    main()
