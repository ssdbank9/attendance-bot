"""Apply new bot login credentials from NEW_USER_ID/NEW_PASSWORD env vars.

Run by attendance.yml (action=update-credentials) on the self-hosted
runner. The values come from GitHub Actions secrets set by the git
dashboard (encrypted, masked in logs) - config.json is local-only, so
this can't be a direct GitHub Contents API write like the synced JSON
files. Mirrors dashboard.py's /action/update-credentials route.
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
    notify(f"Credentials updated. User ID: {new_uid}", title="Credentials Changed", tags="key")
    try:
        from cloud_sync import sync_credentials
        sync_credentials(user_id=new_uid, password=new_pw)
    except Exception:
        pass
    print(f"Credentials updated for user_id={new_uid}")


if __name__ == "__main__":
    main()
