"""Apply new portal (one.aku.edu) credentials from NEW_PORTAL_USER/
NEW_PORTAL_PASSWORD environment variables. The GitHub workflow deliberately
does not invoke this helper or transport credentials.
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def main():
    new_user = os.environ.get("NEW_PORTAL_USER", "").strip()
    new_pw = os.environ.get("NEW_PORTAL_PASSWORD", "").strip()
    if not new_user or not new_pw:
        print("Missing NEW_PORTAL_USER/NEW_PORTAL_PASSWORD secret - nothing changed")
        return

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    if "portal" not in config:
        config["portal"] = {"enabled": True, "url": "https://one.aku.edu/Pages/homepk.aspx"}
    config["portal"]["username"] = new_user
    config["portal"]["password"] = new_pw
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    from notify import notify
    notify("Portal credentials updated locally.", title="Portal Updated", tags="key")
    print("Portal credentials updated locally")


if __name__ == "__main__":
    main()
