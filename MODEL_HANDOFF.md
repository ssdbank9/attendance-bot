# Model handoff: AKU Attendance Bot

## Operational context

This is a live automation repository, not a mock. Scheduled and `--now` execution can mark real AKU attendance. The primary working directory is `D:\DropBox\Self\Claude\Attendance Management`, the production branch is `main`, and attendance execution requires the AKU private network. Preserve local credentials, local SQLite history, invisibility, and PKT decision logic.

## Module map

### Attendance execution and state

- `timein_bot.py`: production orchestration. Applies calendar/pause/duplicate guards, randomized timing, wake-capable one-shot registration, prior-day Time-Out catch-up, direct AKU API, headless Selenium fallback, retry, status recording, and notification.
- `attendance_db.py`: SQLite schema and sole event insertion path. Exports `timein_status.json` and `timein_history.json` after writes. Includes the query for the latest successful unmatched Time-In.
- `migrate_to_db.py`: imports legacy status/history into SQLite.
- `pk_time.py`: canonical Pakistan wall-clock source. Returns naive PKT datetimes to stay compatible with existing scheduling calculations; also exposes the fixed UTC+5 `PKT` timezone for serialized timestamps.
- `console_guard.py`: protects `pythonw.exe` entry points from `stdout`/`stderr` being `None` and redirects them to log files.

### Calendar, leave, dashboard, and notifications

- `dashboard.py`: authenticated Flask application, local settings/credential management, attendance actions, leave/holiday/blackout management, analytics, pause sync, and API endpoints.
- `dashboard_watchdog.py`: health-checks `/login`, kills a wedged listener, and relaunches the dashboard without a console.
- `holiday_reminder.py`: 19:00 holiday reminder/confirmation and 22:00 tomorrow-plan notifications.
- `manage_holidays.py`: holiday population and CLI maintenance.
- `manage_blackout.py`: blackout/leave CLI maintenance.
- `notify.py`: ntfy delivery with Claude CLI fallback and dashboard action links.
- `mobile_dashboard.html`: locally served legacy/mobile dashboard document.
- `docs/index.html`: public GitHub Pages client for repository-backed status/settings and workflow dispatch.

### Cloud and workflows

- `cloud_sync.py`: GitHub Contents API allowlist, push/pull, workflow-cron updates, and explicit refusal to sync AKU credentials.
- `set_pause.py`: workflow-dispatch pause/resume writer for local `config.json` and timestamped remote `bot_config.json`.
- `apply_windows.py`: applies validated workflow environment time-window values locally and updates fallback cron.
- `apply_credentials.py`, `apply_portal.py`: local-only environment-based credential update helpers; the workflows do not transport these values.
- `.github/workflows/attendance.yml`: scheduled/manual fallback on the AKU-network Windows self-hosted runner.
- `.github/workflows/deadman.yml`: GitHub-hosted 09:20 PKT deadman evaluation; no AKU access and no pip install.
- `cloud_deadman_check.py`: stdlib-only evaluation and ntfy publish logic.
- `google_apps_script.js`: optional last fallback/integration, configured separately.

### Installation and operations

- `setup_new_user.py`: interactive config creation, checked dependency installation, holiday population, six persistent task registrations, watchdog start, and instructions.
- `check_quiet.ps1`: read-only task invisibility, process-window, listener, interface, and firewall verification.
- `enable_session0.ps1`: moves selected headless tasks to session 0; holiday reminder remains interactive by design.
- `repair_dashboard_startup.ps1`: repairs the dashboard startup task.
- `start_dashboard.ps1`: direct dashboard launcher with logging.

## Non-negotiable invariants

1. Real effects require authorization. Never run `timein_bot.py ... --now`, dispatch a workflow action, click action endpoints, change pause/leave state, or test against AKU unless the user explicitly intends the real effect.
2. Scheduled task entry points remain `pythonw.exe` with Hidden settings. The watchdog may spawn the firewall-compatible dashboard `python.exe`, but only with `CREATE_NO_WINDOW`.
3. Every subprocess spawn retains `CREATE_NO_WINDOW` (or the existing combined `CREATE_NO_WINDOW | DETACHED_PROCESS`). Edge remains `--headless=new` and `EdgeService(creation_flags=NO_WINDOW)`.
4. `console_guard.silence()` remains the first operational call in the scheduled Python entry points before logging or printing.
5. Every current-date/time decision uses `pk_time`; do not add `datetime.now()` or host-local date decisions. Parsing fixed date strings with `datetime.strptime` is allowed.
6. `cloud_deadman_check.py` remains standard-library-only. Do not add `requests`, Flask, dateutil, PyYAML, or another pip dependency.
7. `attendance.db` is the local source of truth. Do not make exported JSON authoritative and do not write event rows outside `attendance_db.record_event`.
8. API success is fail-closed. Only confirmed AKU success shapes may return success; unknown HTTP-200 content must retry/fail.
9. A pause suppresses the cloud deadman indefinitely while `paused` is a real boolean; `updated_at` age is advisory and surfaces as a warning annotation only. A corrupt or non-boolean value never suppresses.
10. AKU credentials, portal credentials, GitHub sync PAT, dashboard token, saved portal HTML/assets, database, and logs remain untracked/local as currently defined.
11. Do not change repository visibility, the Git remote URL, token revocation/rotation, dashboard bind address, HTTP/HTTPS policy, or firewall exposure without a new explicit authorization.

## Confirmed adversarial findings and status

1. **Workflow PowerShell injection — fixed.** `attendance.yml` declares `action` as a choice with the exact allowlist. All dispatch inputs enter PowerShell through step `env:` values. PowerShell validates action/mode/run flag and each optional/required time against strict 24-hour `HH:MM` plus `TryParseExact`. No GitHub expression is embedded in a `run:` body.
2. **Persistent broad PAT on public Pages — hardened within the accepted design.** `docs/index.html` removes/ignores legacy `localStorage` PATs, stores the token only in tab `sessionStorage`, expires it after 60 minutes, never displays token characters, adds Clear Token/Disconnect, adds no-referrer, and gives a visible warning describing classic `repo` + `workflow` authority and browser risk. GitHub App redesign and token revocation remain out of scope.
3. **409 retry clobbered concurrent content — fixed.** Pages writes now fetch current content, run a per-operation semantic reapply callback, retry with the fresh SHA, return merged content to application state, or refuse and display an error. Leave entry writes are sequenced before balance writes to avoid deducting balance when the entry itself is rejected.
4. **AKU response classifier failed open — fixed.** `classify_aku_message` accepts only the live confirmed `Successfully Timed In/Out` shape (with its optional one-line employee-name prefix), `Time In/Out already Entered for Today`, and `Time In and Time Out already entered for Today` (case-insensitive with trailing detail). Empty, non-string, maintenance, HTML, and unrecognized content fail.
5. **Skipped row hid dangling Time-In — fixed.** `attendance_db.get_latest_unmatched_successful_timein` queries the latest successful prior-date Time-In for which no successful same-date Time-Out exists. `pending_prior_day_timein` uses that query.
6. **Duplicate leave deducted balance — fixed.** `dashboard.py` serializes leave mutations with `_LEAVE_UPDATE_LOCK`, validates duplicate date and available balance before mutation, commits the blackout entry before decrementing/saving balance, then performs cloud projection sync.
7. **Pause sync false success / indefinite cloud suppression — fixed.** Both dashboard pause routes retry once, inspect `(ok, message)`, return or display failure, and include timezone-aware PKT `updated_at`. `set_pause.py` does the same for the workflow path and exits non-zero on sync failure. The stdlib-only deadman honors a valid boolean pause indefinitely - expiry was rejected because it would alert daily through a legitimate long pause - and surfaces a missing, invalid, future, or unusually old `updated_at` as a warning annotation only. A corrupt or non-boolean `paused` value never suppresses an alert. The Pages toggle waits for the dispatched run's real conclusion rather than treating HTTP 204 (queued) as success.
8. **Installer ignored pip failures — fixed.** Each pip subprocess uses `check=True`; a package-specific error aborts setup with `SystemExit(1)` before task registration. The pip subprocess also retains `CREATE_NO_WINDOW`.
9. **Missing operational docs — fixed.** `README.md` and this handoff cover real-world authorization, architecture, AKU-network prerequisites, installation, config/secret ownership, scheduled tasks, quiet verification, troubleshooting, rollback, security limitations, module map, invariants, validation, decisions, and backlog.

## Test and verification matrix

| Area | Command or method | Required result |
|---|---|---|
| Python syntax | `python -m py_compile attendance_db.py timein_bot.py dashboard.py set_pause.py cloud_deadman_check.py setup_new_user.py notify.py` | All changed Python files compile |
| Workflow YAML | Parse `.github/workflows/attendance.yml` with a real YAML parser | Well-formed mapping/list structure |
| Workflow injection | Scan every `run:` block for `${{` and inspect `env:`/allowlists | No expression in script bodies; action choice exact; time values strict |
| Pages JavaScript | Extract inline script and run `node --check -` | Syntax passes |
| API classifier | Exercise confirmed good strings plus empty, HTML/maintenance, arbitrary success-looking, and known error strings | Only allowlisted good shapes return `True` |
| Pending prior day | Temporary SQLite: successful Friday Time-In, later skipped/failed rows, no Friday success Time-Out | Friday remains pending; adding Friday success Time-Out clears it |
| Pause freshness | Temporary `bot_config.json`: fresh pause, missing timestamp, stale timestamp, future timestamp | Only fresh pause suppresses; all others are degraded |
| Installer abort | Mock first pip call to return non-zero/raise `CalledProcessError` | `SystemExit(1)` before task creation |
| Pages concurrency | Mock Contents API: initial PUT returns 409, GET returns a concurrent document, retry PUT receives merged document/fresh SHA | Concurrent fields/blackouts remain; unsafe reapply refuses |
| Invisibility source review | `rg` for `CREATE_NO_WINDOW`, `--headless=new`, `EdgeService`, `pythonw.exe`, `$s.Hidden`, and `console_guard.silence` | Invariants remain present; no changed spawn loses flags |
| PKT source review | `rg`/diff for `datetime.now` and `pk_time` | No newly introduced host-local decision logic |
| Live host verification | `powershell -ExecutionPolicy Bypass -File .\check_quiet.ps1` | `RESULT: PASS - invisible and reachable` when run with sufficient Task Scheduler/firewall permissions |

Never validate attendance by making a real API call when the day's attendance is already marked or the user did not explicitly request the real action.

## Unresolved decisions and residual risk

- The accepted public Pages design still uses a classic PAT with broad `repo` + `workflow` authority. A fine-grained PAT or GitHub App would reduce authority, but redesign was explicitly declined for this pass.
- PAT local expiry cannot revoke a token at GitHub. The owner must set GitHub-side expiry and manually revoke/rotate when needed.
- The dashboard remains intentionally bound/exposed as configured and uses HTTP. Loopback-only binding, forced HTTPS, and firewall changes were explicitly declined.
- Repository visibility was intentionally unchanged. Synced attendance, leave, and configuration data remain visible to everyone who can read the repository.
- Per-file Contents API concurrency is protected, but a leave entry and leave-balance update are still two commits. The entry is written first and failures are surfaced/reloaded; a repository-wide atomic transaction is not available in this design.
- Pause suppression is indefinite by design; expiry was rejected because it would alert daily through a legitimate multi-week pause. The failed-resume blind spot is closed by loud reporting instead: non-zero exit, urgent notification, dashboard error surfacing, and Pages polling the run conclusion.
- The AKU success allowlist is based on the confirmed strings present in the code/review history. If AKU changes wording, valid actions will fail closed until a new exact shape is verified and added.
- `timein_history.json` is tracked and consumed by the Pages dashboard, but `cloud_sync.sync_status()` currently pushes only `timein_status.json`. Decide whether history should be explicitly allowlisted/synced or remain updated through the existing repository workflow.
- `setup_new_user.py` installs three packages, while `requests` is a separately documented prerequisite. Decide whether a later scoped change should add `requests` to the installer's checked package list.
- The current environment denied read access to installed Task Scheduler definitions during this pass. Source definitions can be verified, but the live host task inventory and `check_quiet.ps1` result still require a user/elevated operational check.

## Feature backlog

1. Add committed unit tests for classifier allowlists, unmatched prior-day SQLite queries, pause freshness, and installer abort behavior.
2. Add a deterministic JavaScript test harness for Contents API 409 semantic merges and unsafe-conflict refusal.
3. Replace direct JSON writes with atomic temp-file plus `os.replace` helpers consistently across local dashboard/config modules.
4. Add an explicit reconciliation screen/job for partial multi-file cloud operations such as leave plus balance.
5. Decide and implement the intended `timein_history.json` cloud lifecycle.
6. Consolidate package requirements into a pinned, reviewed installer input without changing runtime dependencies casually.
7. Add a documented, authorized disaster-recovery exercise covering database restore, JSON regeneration, task re-registration, and deadman verification.
8. If the user later approves it, evaluate a fine-grained GitHub credential or GitHub App design for the Pages client.
