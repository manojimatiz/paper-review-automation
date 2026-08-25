# Company-wide platform roadmap

All 10 phases from the original spec are implemented, several in a
deliberately reduced scope — see each entry below for exactly what was left
out and why.

## Done

- **Phase 1** — Baseline verification.
- **Phase 2** — Storage backend selection (`storage_backend` config key,
  `paper_automation/storage/factory.py`). `GoogleDriveStorage` stays stubbed;
  Drive-for-Desktop + `LocalStorage` already covers Google Drive.
- **Phase 3** — File fingerprinting, version detection, duplicate detection
  (`paper_automation/fingerprint.py`, `state.py`'s `paper_version` table).
- **Phase 4** — Persistent job records (`state.py`'s `job` table: job IDs,
  status, priority, attempts, crash-recovery requeue), still sequential.
- **Phase 4b** — Concurrent worker pool (`max_concurrent_jobs` config,
  `ThreadPoolExecutor` in `phases.py`, per-worker provider instances).
- **Phase 5** — In-app scheduler (`py run.py --loop`, `paper_automation/scheduler.py`,
  `schedule_start`/`schedule_end`/`scan_interval_minutes` config).
- **Phase 8** — Multi-user LAN access: per-person accounts with ADMIN/User
  (WRITER) roles (`paper_automation/auth.py`, `manage_users.py`, `user`
  table), Flask-Login sessions, waitress, user-scoped dashboard, and the
  `web_host` config key. `webui/app.py`'s `main()` refuses to start when
  bound to a non-loopback host with no accounts configured. **Extended**:
  self-service `/signup` is now the primary way an account is requested —
  it creates a pending record with *no usable password at all* until an
  admin approves it on the new `/users` page (which also supports the
  optional, immediately-active admin-direct "Add user" path,
  rename/disable/enable/reset-password). Approval is the moment the fixed
  default password (`iMatiz`) first becomes real; the very next login on it
  is forced through a "set your own password" screen
  (`_require_password_change` before_request hook in `webui/app.py`) before
  anything else is reachable. The MANAGER role was dropped — it was never
  actually used — leaving just ADMIN and User. **Further extended**: a real,
  permanent `delete_user()` was added (a deliberate reversal of the original
  disable-only design), guarded against removing the last active Admin —
  available as a Delete button on `/users` and `py manage_users.py delete`.
- **Phase 6** — Per-provider prompt editing with versioning: an "Instructions"
  page (admin/manager only) lets the review (Codex) and revision (Claude)
  prompts be edited independently, per task mode. Every save creates a new
  version (`state.py`'s `prompt` table); nothing is overwritten, so any
  version can be restored. Scientific-integrity rules and the output-file
  contract (`paper_automation/prompts.py`'s `_contract()`) are appended
  automatically and cannot be edited away.

- **Phase 7** — Admin dashboard: a `/jobs` page (admin/manager only) built on
  the Phase 4 job table — system status (storage, automation, schedule,
  worker count, LAN exposure), job counts by status as filter chips, and a
  table of every job attempt with plain-English status/phase labels
  (`service.job_overview()`, `service.system_health()`). **Scoped down from
  the doc's full list**: no separate Queue/Employees/Clients/Storage/Providers
  pages (Employees/Clients folder-browsing already lives on the dashboard;
  Storage/Providers status already lives on Settings) and, notably, **no retry
  action** — a per-job retry button needs the worker pool to actually consume
  a persistent queue across runs, which Phase 4 deliberately stopped short of.
  Revisit if/when that's built.
- **Phase 9** — Automatic folder management: `create_missing_month` now
  actually creates the month folder (`run.py`'s `run_once()`) instead of being
  a dead config flag; an "Add a folder" panel on the dashboard lets an
  admin/manager create an employee or client folder on demand
  (`service.create_employee_folder()`/`create_client_folder()`,
  `POST /api/create-folder`), name-validated against path traversal and
  idempotent like the rest of the pipeline.

- **Phase 10** — Production hardening, scoped:
  - **Backups**: `py run.py --backup-now` copies `state.sqlite3` (job history,
    prompts, and accounts all live in that one file) to a timestamped file in
    `backup_dir` (`paper_automation/backup.py`). Register it as its own
    separate Task Scheduler entry for a regular cadence — there is no internal
    scheduler for this, deliberately, since Windows Task Scheduler already
    fills that role for the main pipeline. A "Back up now" button on `/jobs`
    does the same on demand.
  - **Login throttling**: `/login` is rate-limited per source IP
    (`webui/app.py`'s `_LoginRateLimiter`, 8 failures / 5 minutes,
    in-process). Meaningfully slows down password guessing; not a hardened
    auth server.
  - **Disk-usage visibility**: `/jobs`' System status panel now shows log
    file count and the state-database size, so growth is visible to a human
    rather than the pipeline trying to manage it.
  - **Explicitly NOT done — flagging the tradeoff**: "log rotation" and
    "backup pruning" in the traditional sense both mean *deleting* old files.
    This project's no-delete invariant
    (`tests/test_safety.py`'s AST check over all of `paper_automation/`) is
    load-bearing everywhere else — the CLAUDE.md rule exists specifically so
    a bug can never delete a client's paper. Building an exception into a
    "housekeeping" feature would be exactly the kind of quiet erosion that
    rule exists to prevent. Old logs and old backups are left for a human to
    clear by hand, same as `phases.py`'s scratch directories already are.
    Revisit only if this becomes a real disk-space problem in practice, and
    treat it as a deliberate, reviewed exception, not a default.
  - **Not done**: further crash-recovery work beyond what Phases 3-4 already
    built (`reconcile()`, `requeue_stale_processing_jobs()`) — no gap was
    found worth closing beyond documenting reliance on SQLite's own journaling
    and a UPS for the host PC, per the original spec's hardware note.

## Beyond the original 10 phases

- **Desktop icon + system tray + always-on LAN access** — genuinely new
  scope, not one of the original spec's phases. `tray_app.py` is a
  `pystray`-based controller that spawns `py ui.py` as a child process
  (`ServerController`, subprocess-based Start/Stop, not in-process
  threading — a killed subprocess is recovered the same way an unexpected
  crash already is, via Phase 4's `requeue_stale_processing_jobs()`).
  `scripts/register_tray_autostart.ps1` starts it at login (a logon trigger,
  mirroring `register_task.ps1`'s daily trigger for the main pipeline);
  `scripts/install_desktop_icon.ps1` creates a Desktop shortcut running
  `tray_app.py --open` — a one-shot "open the dashboard, starting it first
  only if nothing's listening yet" action, deliberately not a second tray
  icon, so a double-click can never bind a second server to the same port.
  `config.example.toml` (and this project's own live `config.toml`) now ship
  `web_host = "0.0.0.0"` by default — the point of this feature is LAN
  reachability, so the account-existence gate (Phase 8) is what carries the
  safety weight now, not the loopback default that used to.

Each phase should get its own exploration + scoping pass before implementation,
the same way Phases 1-5 were each scoped individually.
