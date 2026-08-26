# AKU Attendance Bot

This repository runs a real attendance automation for an authorized AKU user. It is not a simulator. A scheduled or manual run can create a real Time-In or Time-Out record in the AKU attendance system, and leave, holiday, pause, and time-window changes can determine whether future attendance is marked.

Only install or operate it for an account you own or are expressly authorized to manage. Review institutional policy before enabling automation. Commands containing `--now`, the dashboard's Time-In/Time-Out buttons, and the GitHub Pages action buttons can act immediately. Do not use them merely as connectivity tests.

## Architecture and data flow

The desktop on the AKU network is the authority for attendance execution:

1. Windows Task Scheduler starts `timein_bot.py` with `pythonw.exe` from a Hidden task.
2. `timein_bot.py` uses Pakistan Standard Time from `pk_time.py`, then checks pause state, duplicate success, weekends, working weekends, holidays, blackouts, leave, and pending unmatched prior-day Time-Ins.
3. For a normal scheduled run it chooses a randomized time and registers a wake-capable `TimeInBot_OneShot_*` task. Short waits may remain inline.
4. At the target time it calls the AKU private API. If the direct path fails, it uses the saved portal page through headless Edge and verifies the result through the API.
5. Only allowlisted AKU success response shapes are accepted. An empty, malformed, maintenance, HTML, or otherwise unknown response fails closed and is retried.
6. Successful, failed, and skipped events are recorded in local SQLite (`attendance.db`). `attendance_db.py` regenerates `timein_status.json` and `timein_history.json` from that database.
7. `cloud_sync.py` can push allowlisted, non-credential JSON files to GitHub. The self-hosted attendance workflow provides a fallback execution path; the GitHub-hosted deadman workflow reads synced state and alerts when a legal working day has no Time-In.

The local Flask dashboard (`dashboard.py`) manages credentials, time windows, holidays, blackouts, leave, notification settings, and pause state. `dashboard_watchdog.py` keeps it serving. The static GitHub Pages dashboard in `docs/index.html` talks directly to the GitHub Contents and Actions APIs; it never receives the AKU credentials.

The intended execution hierarchy is:

1. Local Windows scheduled task.
2. `.github/workflows/attendance.yml` on the AKU-network self-hosted runner as fallback or for an authorized manual dispatch.
3. The optional `google_apps_script.js` integration as the last external fallback, if separately configured and authorized.

`.github/workflows/deadman.yml` is different: it runs on a GitHub-hosted runner at 09:20 PKT and never contacts AKU. It only evaluates repository state and sends an ntfy alert when appropriate.

## Prerequisites

- Windows with Task Scheduler and PowerShell.
- Python 3 with both `python.exe` and `pythonw.exe` in the same installation. Scheduled task entry points must use `pythonw.exe` and Hidden settings.
- Microsoft Edge and a Selenium-compatible Edge driver. Selenium Manager may obtain the driver where policy and network access permit.
- A locally saved `timein_page.html` and its required assets for the Selenium fallback. These files are deliberately gitignored.
- Access to the AKU network when attendance is executed. `portalservice.aku.edu` is private and is not reachable from a normal GitHub-hosted runner or an off-network desktop.
- The attendance computer must be powered on or sleeping in a state from which Task Scheduler can wake it. It cannot execute while shut down.
- Python packages used by the code: `requests`, `selenium`, `flask`, and `hijri-converter`.
- Administrator rights when initially registering or repairing scheduled tasks.
- Optional: an ntfy subscription, a GitHub repository/token for cloud sync, a self-hosted GitHub Actions runner on the AKU desktop, and Tailscale for remote dashboard access.

## Installation

Run installation on the authorized AKU attendance computer from this repository directory.

```powershell
Set-Location 'D:\DropBox\Self\Claude\Attendance Management'
python -m pip install requests
python setup_new_user.py
```

`setup_new_user.py` collects local credentials, creates the initial JSON files, installs `selenium`, `flask`, and `hijri-converter`, populates holidays, registers the scheduled tasks, starts the dashboard watchdog, and prints ntfy instructions. Dependency installation is fail-fast: if any pip command fails, setup exits before registering tasks.

During setup:

- Enter the AKU employee/user ID and attendance password only on the local computer.
- Enter the portal fields if that local dashboard workflow is used. The current attendance execution path reads `credentials`; the separate `portal` block is retained for dashboard-managed portal configuration.
- Confirm time windows in 24-hour `HH:MM` format.
- Keep the generated ntfy topic private.

After setup, ensure the local Selenium fallback page exists, then verify the installation without marking attendance:

```powershell
powershell -ExecutionPolicy Bypass -File .\check_quiet.ps1
```

Do not run `python timein_bot.py timein --now` or `timeout --now` as an installation test unless you deliberately intend to create that real attendance action.

### Optional cloud configuration

The authenticated local dashboard can save the `cloud_sync.github` repository and PAT into local `config.json`. The token remains local to the desktop. The GitHub Pages dashboard requires a classic PAT with only the scopes its current design needs: `repo` for the private repository contents and `workflow` for workflow actions. Use the shortest practical GitHub expiration and no additional scopes.

The public Pages application stores that PAT only in the current tab's `sessionStorage`, applies a local 60-minute expiry, deletes legacy `localStorage` tokens, never displays token characters, and provides Clear Token and Disconnect controls. This reduces persistence but does not make a browser-held PAT safe from malicious page script, browser extensions, or a compromised/shared browser profile.

For the deadman workflow, configure repository Actions secrets as needed:

- `NTFY_TOPIC` (required for a usable alert channel)
- `NTFY_SERVER` (optional; defaults to `https://ntfy.sh`)
- `DASHBOARD_URL` (optional; adds dashboard action links)

## Handing this to another user

This system is single-user per installation. Everything identifying a person is
global: one `config.json`, one `attendance.db`, one dashboard on port 5000, one
ntfy topic, and one set of `TimeInBot*` scheduled task names. There is no user
table. A second person therefore needs their own independent installation on
their own computer, not an account on yours.

Two hard requirements before starting. The new user must be on the **AKU
network** — `portalservice.aku.edu` is a private address and no execution path
works off it — and their computer must be **awake** near their Time-In window.
Sleep is fine, because the bot registers a wake-capable one-shot task for its
randomized time, but a powered-off machine cannot mark attendance.

### 1. Clone, and do not copy your own installation

```powershell
git clone https://github.com/ssdbank9/attendance-bot.git
Set-Location attendance-bot
```

Clone rather than copying a working directory. `config.json`,
`.dashboard_auth_token`, and `attendance.db` are gitignored, so a clone
correctly excludes your credentials, your dashboard login token, and your
attendance database. Copying a folder would carry all three.

### 2. Reset the state a clone still carries

Several state files **are** tracked, so a clone arrives holding the previous
user's records. `setup_new_user.py` does not clear them: it creates those files
only when they are missing, and in a clone they already exist.

```powershell
Remove-Item timein_status.json, timein_history.json -ErrorAction SilentlyContinue
python -c "import json; json.dump({'dates': [], 'ranges': [], 'working_weekends': []}, open('blackout.json', 'w', encoding='utf-8'), indent=2)"
python -c "import json; json.dump({'paused': False}, open('bot_config.json', 'w', encoding='utf-8'), indent=2)"
```

Write these with Python, not `Out-File -Encoding utf8` or `>`. Windows
PowerShell 5.1 emits UTF-8 **with a byte-order mark**, and the loaders here do
not tolerate one. `timein_bot.is_blacked_out` opens `blackout.json` with no
explicit encoding and no exception handling, so a BOM raises
`JSONDecodeError` and aborts the normal scheduled/fallback run (a manual
`--now` run bypasses that guard, so it is the guarded path that breaks).
`cloud_deadman_check.load_json` instead swallows the error and returns `{}`,
which reads as "no leave booked" and produces a false missing-attendance alert
on a leave day - it cannot mark attendance itself. Both failures are silent at
the point of editing.

`attendance.db` is the source of truth and regenerates both exports on the
first recorded event, so deleting them is safe. Because the database starts
empty while those JSON files do not, skipping this step leaves the dashboard
displaying the previous user's attendance until their own first run.

Then personalize two files by hand:

- `notification_prefs.json` — replace `admin_email` with the new user's own
  attendance administrator. The failure notification builds a pre-filled
  correction request to that address.
- `leave_balance.json` — replace the year and entitlements with theirs.

Keep `holidays.json`. Pakistan public holidays are not user-specific, and
setup re-populates the current year regardless.

### 3. Run setup

```powershell
python -m pip install requests
python setup_new_user.py
```

Setup prompts for the employee ID and attendance password, the portal
username and password, and the four time windows. It generates a **new random
ntfy topic**, installs dependencies fail-fast, populates holidays, and
registers the six scheduled tasks under `pythonw.exe` with `Hidden` set so
none of them draws a console window.

### 4. Verify without marking attendance

```powershell
powershell -ExecutionPolicy Bypass -File .\check_quiet.ps1
```

Expect `RESULT: PASS`. Do not use `timein --now` as an installation test; that
creates a real attendance action.

### 5. Their phone

1. Install ntfy — Android `io.heckel.ntfy`, iOS `apps.apple.com/app/ntfy/id1625396347`.
2. Subscribe to the topic setup printed, in the form `timeinbot-<their-id>-xxxxx`.
   Never reuse an existing topic: both users would receive each other's
   notifications, and either could publish to the other. The topic is the only
   access control ntfy applies, so it functions as a password.
3. Dashboard at `http://<their-PC-IP>:5000` on the same Wi-Fi. The login token
   is in `.dashboard_auth_token` on their computer.
4. For access from other networks, install Tailscale on both devices, then
   `python setup_new_user.py --set-tailscale <their-tailscale-IP>`.

### What a fresh installation does not include

`cloud_sync` and the 09:20 deadman alert are unconfigured. Everything else
works without them; the new user simply receives no warning on a morning their
computer never came on. Enabling it requires their own GitHub repository, a
`cloud_sync.github` block in `config.json`, and the Actions secrets listed
under Optional cloud configuration.

## Configuration and secret ownership

`config.json` is local-only and gitignored. Its current schema is:

| Key | Purpose | Secret owner / exposure |
|---|---|---|
| `credentials.user_id`, `credentials.password` | Direct AKU attendance API and saved-page fields | AKU user; local desktop only |
| `timein.window_start`, `primary_end`, `window_end`, `primary_weight` | Time-In randomized window | Non-secret local configuration |
| `timeout.window_start`, `primary_end`, `window_end`, `primary_weight` | Time-Out randomized window | Non-secret local configuration |
| `retry.max_attempts`, `retry.delay_seconds` | Attendance retry policy | Non-secret local configuration |
| `notifications.ntfy_topic`, `ntfy_server` | Push notification destination | Topic is bearer-like and should be private; local desktop, with topic/server optionally duplicated as Actions secrets |
| `dashboard.host`, `port`, `tailscale_ip` | Dashboard listener and advertised remote address | Non-secret; host currently remains `0.0.0.0` by design |
| `portal.enabled`, `url`, `username`, `password` | Dashboard-managed portal configuration | AKU user; local desktop only |
| `cloud_sync.github.enabled`, `repo`, `token` | GitHub Contents/Actions sync | Repository owner; PAT local desktop only |
| `cloud_sync.hierarchy` | Descriptive sync ordering | Non-secret |
| `paused` | Local execution pause flag | Non-secret local control state |
| `leave_balance.*` | Annual and remaining casual/sick/earned leave | Personal data; local and optionally repository-synced |

Other state files:

| File | Role |
|---|---|
| `attendance.db` | Local SQLite source of truth; gitignored |
| `timein_status.json` | Latest exported Time-In/Time-Out status; repository-synced for Pages/deadman |
| `timein_history.json` | Exported successful bot-owned action history; tracked and read by dashboards |
| `blackout.json` | Skip dates, ranges, leave entries, and working weekends |
| `holidays.json` | Holiday calendar, confirmation, moon-dependent, and disabled state |
| `leave_balance.json` | Repository-facing leave balance projection |
| `notification_prefs.json` | Notification switches and optional admin email |
| `bot_config.json` | Repository-facing `paused` plus PKT `updated_at`; the deadman honors a valid boolean pause indefinitely, and reports an unusually old one as a warning rather than alerting |
| `.dashboard_auth_token` | Generated local dashboard login secret; gitignored |

`cloud_sync.py` has an explicit repository-file allowlist and refuses credential sync. AKU and portal credentials must not be added to tracked JSON, workflow inputs, Actions logs, or the Pages application.

## Windows scheduled tasks

There are six persistent task definitions plus the dynamic one-shot role used during a normal attendance run. This is the seven-role schedule; the one-shot family can briefly produce separate Time-In and Time-Out task names.

| Task or task family | Trigger | Action |
|---|---|---|
| `TimeInBot` | Daily 08:45 | Starts `timein_bot.py timein`; wake enabled |
| `TimeInBot_TimeOut` | Daily 20:00 | Starts `timein_bot.py timeout`; wake enabled |
| `TimeInBot_HolidayReminder` | Daily 19:00 | Runs `holiday_reminder.py holidays` |
| `TimeInBot_TomorrowPlan` | Daily 22:00 | Runs `holiday_reminder.py tomorrow` |
| `TimeInBot_DashboardWatchdog` | At logon and every five minutes from 00:05 | Health-checks and relaunches the dashboard |
| `TimeInBot_Dashboard` | At logon | Starts the same watchdog path for compatibility/startup reliability |
| `TimeInBot_OneShot_timein` / `TimeInBot_OneShot_timeout` | Dynamically registered at the randomized target | Wake-capable real attendance action with `--scheduled`; expires after execution |

The scheduled task entry points use `pythonw.exe` and `$s.Hidden = $true`. Child processes retain `CREATE_NO_WINDOW`. The watchdog deliberately serves Flask from `python.exe` because this host's inbound firewall rules allow that executable; it still launches with `CREATE_NO_WINDOW`, so no console is attached. Selenium remains `--headless=new` with `EdgeService(creation_flags=...)`.

## Verification

Run the non-mutating quiet/reachability check:

```powershell
powershell -ExecutionPolicy Bypass -File .\check_quiet.ps1
```

Expected final line:

```text
RESULT: PASS - invisible and reachable
```

The script checks:

- every `TimeInBot*` scheduled task is Hidden and does not use `python.exe` as its task entry point;
- the dashboard is listening on port 5000;
- the dashboard is reachable through loopback, Wi-Fi, and Tailscale interfaces that exist;
- relevant inbound Python firewall rules;
- no Python, Pythonw, or EdgeDriver process owns a visible window.

Useful read-only checks:

```powershell
python dashboard_watchdog.py --status
Get-ScheduledTask | Where-Object TaskName -Like 'TimeInBot*' | Sort-Object TaskName
Get-Content .\timein_status.json
```

The deadman can be evaluated without sending an alert:

```powershell
python cloud_deadman_check.py --dry-run
```

## Troubleshooting

### `check_quiet.ps1` reports a task using `python.exe`

Confirm `pythonw.exe` exists beside the selected interpreter, then rerun setup from an Administrator PowerShell. Do not accept a visible-console fallback for scheduled task entry points.

### Dashboard is not listening or the phone cannot connect

Run:

```powershell
python dashboard_watchdog.py --status
powershell -ExecutionPolicy Bypass -File .\check_quiet.ps1
```

Review `timein_logs/dashboard_stdout.log` and `timein_logs/dashboard_watchdog.log`. `repair_dashboard_startup.ps1` repairs the at-logon dashboard task. The current host/firewall exposure is intentional; do not change bind address, HTTPS, or firewall policy without a separately approved design change.

### Attendance API fails or returns an unrecognized response

Confirm the desktop is on the AKU network and the local credentials are current. Review `timein_logs/timein.log`. Unknown HTTP-200 content now fails closed; do not add a broad substring or default-success rule. Add a new accepted response shape only after confirming an authentic AKU good-path response.

### A prior Time-In is still open

The bot queries SQLite for the latest successful prior-day Time-In with no successful Time-Out on the same date. It can close that dangling session before a new Time-In. Inspect `attendance.db` and `timein_status.json`; do not "fix" this by inserting a skipped row or editing only the exported JSON.

### Pause/resume reports a GitHub sync failure

The local state has changed, but the dashboard now reports the remote failure instead of claiming success. Retry after checking `cloud_sync.github` access. A remote paused state suppresses the cloud deadman for as long as it is set, so an unsynced resume must be retried; the deadman surfaces an old `updated_at`; stale or missing timestamps are degraded and cannot suppress a missing-attendance alert indefinitely.

### Setup stops during pip installation

Read the package-specific error, correct network/proxy/Python permissions, and rerun setup. No tasks are registered after a dependency failure. Because `requests` is currently a separately installed prerequisite, also confirm:

```powershell
python -c "import requests, selenium, flask, hijri_converter; print('dependencies OK')"
```

### GitHub Pages asks for the PAT again

That is expected after 60 minutes, tab/session closure, explicit Clear Token/Disconnect, or migration from the older persistent-storage version. Enter a new short-lived classic PAT with only `repo` and `workflow` scope. Never paste the PAT into logs, issues, source files, or chat.

### Deadman Check emailed "all jobs have failed"

Open the run and read its annotation before assuming attendance was missed. A
failure means an alert was genuinely due and could **not** be delivered, so the
annotation names both facts, for example:

`Time-In is MISSING for Wednesday 2026-08-26 AND this deadman could not notify you: NTFY_TOPIC is not set.`

Two distinct causes, with different responses:

- **`NTFY_TOPIC` is not set.** The alert channel does not exist, so a real
  missing-attendance day cannot reach the phone. Add the secret under Settings
  > Secrets and variables > Actions. Until then the check still evaluates
  correctly, but it has no way to tell anyone.
- **The ntfy send failed.** The channel is configured but unreachable; check the
  topic value and `NTFY_SERVER`.

A run that decided no alert was needed - holiday, weekend, paused, or
attendance already recorded - stays green even with an unusable channel, and
reports it as a warning annotation instead. Earlier behaviour failed those runs
too, which generated a failure email every weekday, including on public
holidays. If a green run carries an "Alert channel unusable" warning, the check
worked and simply had nothing to send.

## Rollback and recovery

Before a code rollback, preserve local state:

```powershell
Copy-Item .\config.json .\config.json.rollback-backup
Copy-Item .\attendance.db .\attendance.db.rollback-backup
Copy-Item .\blackout.json .\blackout.json.rollback-backup
```

Use a normal Git revert for a pushed code change so history remains auditable:

```powershell
git log --oneline -10
git revert <commit-id>
```

Then rerun compilation/verification and, only if task definitions changed, rerun setup from an Administrator PowerShell. Do not use `git reset --hard` on this live working directory. Do not restore `timein_status.json` or `timein_history.json` as the source of truth when `attendance.db` is available; regenerate exports through `attendance_db.py`/the normal event path.

To halt future execution while investigating, use the authenticated dashboard pause control and confirm both the local result and GitHub sync result. Disabling scheduled tasks is a stronger manual containment option, but record which tasks were disabled so they can be restored deliberately.

## Security limitations

- `config.json`, `.dashboard_auth_token`, the saved portal page, and local logs/database are plaintext local files protected by the Windows account and filesystem permissions, not an encrypted secret store.
- The local dashboard intentionally remains network-exposed on its configured host and uses HTTP. Token authentication, HttpOnly/SameSite cookies, no-store responses, and security headers reduce risk but do not provide transport encryption.
- The public GitHub Pages client still handles a broad classic PAT in browser memory/session storage. The one-hour expiry is local hardening, not revocation; GitHub-side expiry and manual revocation remain the owner's responsibility.
- Repository readers can see tracked attendance status, holidays, blackouts/leave, notification preferences, leave balance, and pause state. Repository visibility is not changed by this project.
- GitHub JSON updates use per-file optimistic concurrency. Conflict retries now reapply semantic operations or refuse safely, but related changes across two files (for example, a leave entry and leave balance) are not a repository-wide transaction; the UI reports partial failure and reloads authoritative state.
- A pause suppresses the cloud deadman indefinitely, deliberately: expiring it would alert daily through a legitimate long pause. The failed-resume blind spot is instead closed by reporting sync failure loudly - `set_pause.py` exits non-zero and notifies at urgent priority, both dashboard routes surface the error, and the Pages toggle waits for the run's real conclusion. A corrupt or non-boolean `paused` value never suppresses an alert.
- The deadman validates notification configuration locally but does not probe ntfy on every quiet day. A wrong but well-formed topic or an ntfy outage may only be discovered when an alert is attempted; workflow failure email is the backup signal.
- Randomized timing, retries, Selenium, self-hosted workflows, and wake settings reduce operational gaps but cannot guarantee attendance when the PC is off, the AKU network/service is unavailable, credentials expire, Windows does not wake, or GitHub/ntfy is unavailable.

See `MODEL_HANDOFF.md` for the module map, invariants, finding status, validation matrix, unresolved decisions, and backlog.
