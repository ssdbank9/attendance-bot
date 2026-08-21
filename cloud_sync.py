"""
Cloud sync module - syncs credentials and settings to GitHub Actions and Google Apps Script.
Called automatically when settings change in the dashboard.
"""
import json
import base64
import logging
from pathlib import Path

log = logging.getLogger("cloud_sync")
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def get_sync_config():
    config = load_config()
    return config.get("cloud_sync", {})


# ---- GitHub Actions ----

def _gh_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _encrypt_secret(public_key_b64, secret_value):
    from nacl import encoding, public as nacl_public
    pk_bytes = base64.b64decode(public_key_b64)
    sealed = nacl_public.SealedBox(nacl_public.PublicKey(pk_bytes))
    encrypted = sealed.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def sync_github_secret(repo, token, secret_name, secret_value):
    import requests
    headers = _gh_headers(token)
    key_url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
    resp = requests.get(key_url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return False, f"Public key fetch failed: HTTP {resp.status_code}"

    key_data = resp.json()
    encrypted = _encrypt_secret(key_data["key"], secret_value)

    secret_url = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
    payload = {"encrypted_value": encrypted, "key_id": key_data["key_id"]}
    resp = requests.put(secret_url, json=payload, headers=headers, timeout=15)
    if resp.status_code in (201, 204):
        return True, "OK"
    return False, f"HTTP {resp.status_code}"


def sync_github_credentials(user_id=None, password=None):
    sync = get_sync_config().get("github", {})
    repo = sync.get("repo", "").strip()
    token = sync.get("token", "").strip()
    if not repo or not token:
        return False, "GitHub not configured"

    results = []
    ok_all = True
    if user_id:
        ok, msg = sync_github_secret(repo, token, "AKU_USER_ID", user_id)
        results.append(f"User ID: {msg}")
        ok_all = ok_all and ok
    if password:
        ok, msg = sync_github_secret(repo, token, "AKU_PASSWORD", password)
        results.append(f"Password: {msg}")
        ok_all = ok_all and ok
    return ok_all, "; ".join(results)


def sync_github_workflow_timing(timein_start, timeout_start):
    """Update the cron schedule in the GitHub Actions workflow file."""
    sync = get_sync_config().get("github", {})
    repo = sync.get("repo", "").strip()
    token = sync.get("token", "").strip()
    if not repo or not token:
        return False, "GitHub not configured"

    import requests
    headers = _gh_headers(token)

    ti_h, ti_m = timein_start.split(":")
    to_h, to_m = timeout_start.split(":")
    ti_utc_h = (int(ti_h) - 5) % 24
    to_utc_h = (int(to_h) - 5) % 24

    file_path = ".github/workflows/attendance.yml"
    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return False, f"Workflow file not found: HTTP {resp.status_code}"

    file_data = resp.json()
    content = base64.b64decode(file_data["content"]).decode("utf-8")
    sha = file_data["sha"]

    import re
    lines = content.split("\n")
    new_lines = []
    for i, line in enumerate(lines):
        if "cron:" in line and i > 0:
            prev = lines[i-1] if i > 0 else ""
            if "Time-In" in prev or "8:45" in prev or "3:45" in prev:
                line = f"    - cron: '{int(ti_m)} {ti_utc_h} * * 1-5'"
            elif "Time-Out" in prev or "8:00 PM" in prev or "3:00 PM" in prev or "15:" in prev:
                line = f"    - cron: '{int(to_m)} {to_utc_h} * * 1-5'"
        new_lines.append(line)

    new_content = "\n".join(new_lines)
    if new_content == content:
        return True, "No timing change needed"

    encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": f"Update attendance timing: TI={timein_start} TO={timeout_start} PKT",
        "content": encoded,
        "sha": sha,
    }
    resp = requests.put(url, json=payload, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True, f"Cron updated: TI={ti_utc_h}:{ti_m} UTC, TO={to_utc_h}:{to_m} UTC"
    return False, f"Update failed: HTTP {resp.status_code}"




def _gh_config():
    """Get GitHub repo and token from config, or empty strings."""
    sync = get_sync_config().get("github", {})
    return sync.get("repo", "").strip(), sync.get("token", "").strip()


def sync_github_file(file_name, content_str, commit_msg="Auto-sync from dashboard"):
    """Push a file to the GitHub repo."""
    import requests
    repo, token = _gh_config()
    if not repo or not token:
        return False, "GitHub not configured"

    headers = _gh_headers(token)
    url = f"https://api.github.com/repos/{repo}/contents/{file_name}"

    # Get current file SHA (needed for updates, not for new files)
    sha = None
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 200:
        sha = resp.json().get("sha")

    encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    payload = {"message": commit_msg, "content": encoded}
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, json=payload, headers=headers, timeout=15)
    if resp.status_code in (200, 201):
        return True, "OK"
    return False, f"HTTP {resp.status_code}"


def sync_holidays():
    """Push holidays.json to GitHub repo."""
    hol_path = BASE_DIR / "holidays.json"
    if not hol_path.exists():
        return False, "holidays.json not found"
    with open(hol_path, "r", encoding="utf-8") as f:
        content = f.read()
    return sync_github_file("holidays.json", content, "Sync holidays from dashboard")


def sync_blackout():
    """Push blackout.json to GitHub repo."""
    bl_path = BASE_DIR / "blackout.json"
    if not bl_path.exists():
        return False, "blackout.json not found"
    with open(bl_path, "r", encoding="utf-8") as f:
        content = f.read()
    return sync_github_file("blackout.json", content, "Sync blackout/leave from dashboard")



def pull_github_file(file_name):
    """Pull a file from GitHub repo to local."""
    import requests
    repo, token = _gh_config()
    if not repo or not token:
        return False, "GitHub not configured", None
    headers = _gh_headers(token)
    url = f"https://api.github.com/repos/{repo}/contents/{file_name}"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        return False, "File not found on GitHub", None
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}", None
    data = resp.json()
    decoded = base64.b64decode(data["content"]).decode("utf-8")
    return True, "OK", decoded


def sync_leave_balance():
    """Push leave_balance.json to GitHub repo."""
    lb_path = BASE_DIR / "leave_balance.json"
    if not lb_path.exists():
        return False, "leave_balance.json not found"
    with open(lb_path, "r", encoding="utf-8") as f:
        content = f.read()
    return sync_github_file("leave_balance.json", content, "Sync leave balance from desktop")


def sync_status():
    """Push timein_status.json to GitHub repo."""
    st_path = BASE_DIR / "timein_status.json"
    if not st_path.exists():
        return False, "timein_status.json not found"
    with open(st_path, "r", encoding="utf-8") as f:
        content = f.read()
    return sync_github_file("timein_status.json", content, "Sync status from desktop")


def sync_notification_prefs():
    """Push notification_prefs.json to GitHub repo."""
    np_path = BASE_DIR / "notification_prefs.json"
    if not np_path.exists():
        return False, "notification_prefs.json not found"
    with open(np_path, "r", encoding="utf-8") as f:
        content = f.read()
    return sync_github_file("notification_prefs.json", content, "Sync notification prefs from desktop")


def pull_from_cloud():
    """Pull blackout.json, leave_balance.json and notification_prefs.json from
    GitHub, merge with local (blackout.json gets a real merge; the others are
    a straight remote-wins overwrite)."""
    results = {}
    for fname in ("blackout.json", "leave_balance.json", "notification_prefs.json"):
        ok, msg, pulled = pull_github_file(fname)
        if ok and pulled:
            local_path = BASE_DIR / fname
            try:
                remote = json.loads(pulled)
                if local_path.exists():
                    with open(local_path, "r", encoding="utf-8") as f:
                        local = json.load(f)
                else:
                    local = {}
                if fname == "blackout.json":
                    local_dates = {d["date"]: d for d in local.get("dates", [])}
                    remote_dates = {d["date"]: d for d in remote.get("dates", [])}
                    merged = {}
                    for k in set(list(local_dates.keys()) + list(remote_dates.keys())):
                        ld = local_dates.get(k)
                        rd = remote_dates.get(k)
                        if ld and rd:
                            merged[k] = rd if (rd.get("added", "") >= ld.get("added", "")) else ld
                        else:
                            merged[k] = rd or ld
                    remote["dates"] = sorted(merged.values(), key=lambda d: d["date"])
                    for key in ("ranges", "working_weekends"):
                        local_vals = local.get(key, [])
                        remote_vals = remote.get(key, [])
                        if local_vals and not remote_vals:
                            remote[key] = local_vals
                with open(local_path, "w", encoding="utf-8") as f:
                    json.dump(remote, f, indent=2)
                results[fname] = {"ok": True, "message": "Pulled"}
                if fname == "leave_balance.json":
                    config_path = BASE_DIR / "config.json"
                    if config_path.exists():
                        with open(config_path, "r", encoding="utf-8") as f:
                            config = json.load(f)
                        config["leave_balance"] = remote
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(config, f, indent=2)
            except Exception as e:
                results[fname] = {"ok": False, "message": str(e)}
        else:
            results[fname] = {"ok": ok, "message": msg}
    log.info("Pull from cloud: %s", results)
    return results


def push_all():
    """Push all syncable files to GitHub."""
    results = {}
    r1 = sync_blackout()
    results["blackout"] = {"ok": r1[0], "message": r1[1]}
    r2 = sync_holidays()
    results["holidays"] = {"ok": r2[0], "message": r2[1]}
    r3 = sync_leave_balance()
    results["leave_balance"] = {"ok": r3[0], "message": r3[1]}
    r4 = sync_status()
    results["status"] = {"ok": r4[0], "message": r4[1]}
    r5 = sync_notification_prefs()
    results["notification_prefs"] = {"ok": r5[0], "message": r5[1]}
    log.info("Push all: %s", results)
    return results

# ---- Google Apps Script ----

def sync_google_script(user_id=None, password=None):
    sync = get_sync_config().get("google_script", {})
    url = sync.get("web_app_url", "").strip()
    if not url:
        return False, "Google Script not configured"

    import requests
    payload = {}
    if user_id:
        payload["user_id"] = user_id
    if password:
        payload["password"] = password
    if not payload:
        return True, "Nothing to sync"

    try:
        resp = requests.post(url, json=payload, timeout=15,
                             headers={"Content-Type": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("message", "Updated")
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


# ---- Combined sync ----

def sync_credentials(user_id=None, password=None):
    """Sync credentials to all configured cloud services."""
    results = {}

    gh_ok, gh_msg = sync_github_credentials(user_id, password)
    results["github"] = {"ok": gh_ok, "message": gh_msg}

    gs_ok, gs_msg = sync_google_script(user_id, password)
    results["google"] = {"ok": gs_ok, "message": gs_msg}

    log.info("Cloud sync credentials: GitHub=%s, Google=%s", gh_msg, gs_msg)
    return results


def sync_time_windows(timein_start, timeout_start):
    """Sync time windows to GitHub Actions."""
    gh_ok, gh_msg = sync_github_workflow_timing(timein_start, timeout_start)
    log.info("Cloud sync timing: GitHub=%s", gh_msg)
    return {"github": {"ok": gh_ok, "message": gh_msg}}