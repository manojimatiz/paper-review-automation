# Paper Review Automation — Claude Code context

**The project is called "Paper Review Automation."** Not "Paper Helper" or any
other shortening — the name appears in the UI header, the window title, and the
docs, and it has been renamed by mistake before.

## What this project does

Fully automated research-paper review and revision pipeline. A `.docx` dropped
into a client folder is processed overnight:

```
Research Papers/<Month Year>/<Employee>/<Client>/
    paper.docx                    <- original, never touched
    <Client>_review.docx          <- written by Codex (grammar check or Q1 review)
    Correct_<Client>_paper.docx   <- written by Claude Code (corrected manuscript)
```

`09:00 Asia/Kolkata` — Windows Task Scheduler fires `py run.py`. No human
interaction, no API keys. Models are reached via the **Codex** and **Claude Code**
desktop-app CLIs, authenticated by subscription login.

## Key conventions

- **Python launcher**: always `py`, never `python` or `python3` (Microsoft Store stubs).
- **Two strictly separate phases**: all reviews complete before any revision starts.
- **No destructive operations**: the codebase contains zero delete/rename/move calls
  against client folders. `tests/test_safety.py` enforces this with an AST check.
- **Idempotency**: eligibility re-derived from disk every run; completed folders skipped.
- **Safe writes only**: `safe_write()` refuses to overwrite an existing file.
- **Scratch dir must be outside AppData**: Codex's sandbox rejects writes there.
  Default: `%USERPROFILE%\PaperAutomation\scratch`.

## Project structure

```
Paper review automation/
├── run.py                          # CLI entrypoint; --dry-run, --test-mode, --phase, --loop
├── ui.py                           # Flask web UI launcher (exits code 2 if Flask missing)
├── manage_users.py                 # bootstrap/approve/reset/delete web logins (self-signup is the everyday path)
├── tray_app.py                     # system tray Start/Stop control; spawns ui.py as a child process
├── Start UI.bat                    # double-click launcher for the web control panel
├── config.toml                     # live config (not in version control)
├── config.example.toml             # documented template for config.toml
├── models.json                     # user-editable model registry
├── scripts/
│   ├── register_task.ps1           # registers the Task Scheduler job (daily 09:00)
│   ├── register_tray_autostart.ps1 # registers the tray icon to start at logon
│   └── install_desktop_icon.ps1    # creates the Desktop shortcut (tray_app.py --open)
├── installer/                      # portable-installer build (PyInstaller + Inno Setup)
│   ├── paper_review_automation.spec
│   ├── first_run_wizard.py         # Tkinter setup wizard, frozen to FirstRunSetup.exe
│   └── installer.iss               # Inno Setup script (per-user install, no admin)
├── paper_automation/
│   ├── config.py                   # TOML loader; reads utf-8-sig; raises ConfigError on failure
│   ├── auth.py                     # accounts, password hashing, roles (no Flask import)
│   ├── backup.py                   # timestamped state.sqlite3 copies; never deletes
│   ├── scheduler.py                # --loop processing window + scan interval
│   ├── fingerprint.py              # SHA-256 file identity for version/duplicate detection
│   ├── models.py                   # dataclasses + ProcessingState enum
│   ├── scanner.py                  # month detection, folder traversal, role classification
│   ├── phases.py                   # phase_review(), phase_revise() — strictly separated
│   ├── validation.py               # numeric/fabrication checks after revision
│   ├── state.py                    # SQLite audit trail; additive column migrations
│   ├── prompts.py                  # review + revision prompts; grammar vs full mode
│   ├── docx_io.py                  # .docx ↔ markdown
│   ├── usage.py                    # token accounting from CLI session logs / stdout
│   ├── model_registry.py           # model list; detect_recent_models(); alias resolution
│   ├── service.py                  # UI-agnostic business logic layer (~800 lines)
│   ├── notify.py                   # email/Telegram/Slack/Teams (all optional)
│   ├── storage/
│   │   ├── base.py                 # StorageBackend ABC
│   │   ├── local.py                # LocalStorage — full implementation
│   │   └── gdrive.py               # documented stub for future Google Drive
│   └── providers/
│       ├── base.py                 # CliProvider ABC + MockProvider
│       ├── discovery.py            # globs for codex.exe / claude.exe across version dirs
│       ├── codex.py                # `codex exec` driver — review phase
│       ├── claude_code.py          # `claude --print` driver — revision phase
│       ├── subprocess_provider.py  # shared subprocess logic
│       └── failures.py             # failure classification (USAGE_LIMIT, AUTH, TRANSIENT)
├── webui/
│   ├── app.py                      # Flask routes
│   ├── templates/                  # base.html, dashboard.html, history.html, settings.html
│   └── static/                     # style.css (CSS custom-property theme tokens), app.js
├── tests/                          # pytest; 523 tests; no login or network needed
├── logs/                           # per-run log files (run-YYYY-MM-DD.log)
├── state/                          # SQLite DB (processing.sqlite3)
└── docs/                           # Paper_Review_Automation_Documentation.docx
```

## Task modes

| Mode | What it does | Cost |
|---|---|---|
| `grammar` | grammar-only check + corrected paper | cheap |
| `full` | Q1 journal review + full scientific revision | expensive |

Set in `config.toml` → `task_mode`. Also overrideable per run.

**`grammar` is a temporary testing state, not the destination.** The project was
switched to grammar-only deliberately, to conserve subscription usage while the
system is still being built. `full` is the intended production mode. Do not treat
the two as equal permanent options, and do not remove the `full` prompts.

The grammar revision prompt forbids changing anything but language — no number,
metric, percentage, dataset name, model name, or citation may be altered.

## File-role rules (folder eligibility)

| Name pattern | Role |
|---|---|
| `Correct_*_paper.docx` | final |
| `*_review.docx` | review |
| any other supported extension | original candidate |

Not counted: subdirectories, hidden files, Office lock files (`~$*`), unsupported extensions.

- **Review**: exactly 1 original, 0 review, 0 final → send to Codex
- **Revision**: exactly 1 original, 1 review, 0 final → send to Claude
- **Done**: all 3 present → skip
- **Anything else**: skip with reason recorded

## Provider design

Both CLIs are invoked non-interactively against a scratch directory containing
`manuscript.md`. They write `output.md`. The pipeline—not the model—owns all
filename decisions and `.docx` writing. Models never see or touch client folders.

- Review (Codex): `codex exec --cd <scratch> --skip-git-repo-check "<prompt>"`
- Revision (Claude): `claude --print "<prompt>" --add-dir <scratch>`

`--allowedTools` is comma-separated (not space-separated); trailing prompt must not
follow a variadic flag — use `input=prompt` via stdin instead.

### Subclasses set `label_base`, never `model_label`

`model_label` is a **property** on `SubprocessProvider` that composes
`label_base` + the configured model. A subclass that assigns `model_label = "..."`
as a class attribute shadows the property, and the model name silently vanishes
from the audit trail — no error, no test failure, just a missing column value.

### Token accounting differs per provider

`codex exec` writes **no session log**. The rollout files under `~/.codex/sessions`
come from the Codex desktop app only, so the review stage's token count can be
read *only* by parsing stdout (`tokens used\n50,585`). Claude Code *does* write a
session log under `~/.claude/projects`, and that is the source for the revision
stage. `usage.py` handles both; every function returns `None` rather than raising,
because usage reporting must never fail a run that otherwise succeeded.

### Models

`opus` / `sonnet` / `haiku` are **aliases that always resolve to the newest model**
in their family. This is the mechanism that keeps the model list from going stale,
and is why nothing needs updating when a new model ships. Prefer aliases over
pinned IDs.

The Codex dropdown deliberately offers **only "Default"**. The Codex CLI provides
no way to enumerate available models, and inventing plausible-looking OpenAI model
IDs would produce options that fail at run time. Do not "fix" the short list by
guessing names.

## Version caching

`service.cli_version()` caches CLI version strings for 10 minutes across requests
(`_VERSION_TTL = 600.0`). A missing CLI is cached as `""` to avoid repeated
subprocess launches. Call `service.clear_version_cache()` after changing binary paths.

## Failure classification (`providers/failures.py`)

**Critical rules** — do not violate:
1. Only classify output from a run that actually failed (non-zero exit or no output
   file). Never scan successful stdout — paper prose triggers false positives.
2. No bare numeric needles. `"429"` matches `54,429 images`; require HTTP context.

## Validation (`validation.py`)

After revision: checks numeric values, dataset names, model names vs original.
Any metric that changed value → `VALIDATION_FAILED`. False-positive guards:
- Numbers that appear in cross-references, hyperparameters, or range tails are excluded.
- Percentage-point deltas derived from original numbers are allowed.
- Values at either scale (raw / ×100) are compared.

## SQLite audit trail

```sql
SELECT timestamp, client, phase, model, state, message FROM audit ORDER BY id DESC;
```

`state.py` uses additive-only `ALTER TABLE ... ADD COLUMN` migrations. Do not
remove columns; add new ones with defaults.

## Web UI

- Served by waitress (falls back to the Flask dev server if it is missing).
  Bound to `config.web_host`, `127.0.0.1` by default — see "Logins and network
  exposure" below before changing it.
- Theme: CSS custom-property tokens; `data-theme` attribute; `prefers-color-scheme`
  fallback. `.no-transition` class + double `requestAnimationFrame` prevents colour
  flicker on theme switch.
- `service.py` is the UI-agnostic layer — routes call it; tests mock it.

### Backups and log rotation only ever create, never delete

`py run.py --backup-now` (`paper_automation/backup.py`) copies `state.sqlite3`
to a timestamped file in `backup_dir` and exits. It is a separate,
manually-scheduled Task Scheduler entry, not folded into the main run — the
main pipeline already owns one daily trigger, and giving backups their own
means the two schedules genuinely don't interact.

There is deliberately **no pruning of old backups or old logs**. The no-delete
invariant (`tests/test_safety.py`'s AST check over all of `paper_automation/`)
exists specifically so a bug can never delete a client's file; carving out an
exception for "housekeeping" would undermine the exact guarantee that rule is
for. `/jobs`' System status panel shows log-file count and database size
instead, so growth is visible to a human who can clear old files by hand — the
same trade-off `phases.py`'s `_scratch_for` already makes for old run
directories. Do not add automatic deletion here without discussing it first;
it is a deliberate, reviewed line, not an oversight.

### Login is rate-limited, not hardened

`webui/app.py`'s `_LoginRateLimiter` blocks an IP after 8 failed `/login`
attempts in 5 minutes, in-process, resetting on restart. This is meant to
meaningfully slow down password guessing on a LAN tool, not to be a
production-grade auth server — do not remove it, but also don't mistake it for
more protection than it is (see the module-level SECURITY MODEL docstring in
`webui/app.py`).

### The Jobs page has no retry button, on purpose

`/jobs` (admin/manager only) reads the Phase 4 `job` table
(`service.job_overview()`) and shows every processing attempt with its
status, phase, attempts, and reason — but it is read-only. A "Retry" action
was deliberately left out: it would need a worker pool that actually consumes
a *persistent* queue across separate runs, and Phase 4 stopped short of that
by design (see ROADMAP.md's Phase 4 note). Don't wire up a retry button
without that underlying change, since a retry that just re-triggers today's
disk-driven scan is not the same thing.

### Folder creation is additive-only, same as everything else

`create_missing_month` (`config.toml`) now actually does something:
`run.py`'s `run_once()` creates the month folder via `storage.create_folder()`
when it's missing and this is `true`, instead of only ever skipping the run.
Default stays `false` — existing installs see no behavior change.

Separately, an admin/manager can create an employee or client folder on demand
from the dashboard's "Add a folder" panel (`service.create_employee_folder()`
/ `create_client_folder()`, `POST /api/create-folder`). Both reject empty
names, `.`/`..`, and any path separator via `service._safe_segment()` — a
crafted name cannot climb out of the configured root. Both are idempotent
(creating an existing folder is success, not an error) and only ever call
`storage.create_folder()`, never anything that could delete, rename, or move —
`tests/test_safety.py`'s AST check covers this file too.

### Admin-edited prompts never bypass the safety rules

An admin can rewrite what the review/revision prompt asks for, via the
"Instructions" page (`/prompts`, admin/manager only) — but never *how* it must
be structured. `paper_automation/prompts.py`'s `render_custom()` always
appends `_contract()` (the file-naming contract) and `INTEGRITY_RULES` (the
no-fabrication rules) after the admin's text, and `validate_custom()` rejects
any placeholder outside `{client}`/`{original_filename}`/`{review_date}` at
save time — before it can fail a real run. Do not let an edit-prompt feature
grow a way to skip either of those.

Every save creates a new row in `state.py`'s `prompt` table rather than
updating one in place, so a bad edit can always be reverted with
`activate_prompt_version()`; nothing is ever deleted.

### Logins and network exposure

`Config.web_host`'s code default is still `127.0.0.1` (the safe fallback if
the key is ever absent), but `config.example.toml` — and this project's own
live `config.toml` — now ship `"0.0.0.0"`, since the tray/autostart tools
(below) exist specifically to make the panel always LAN-reachable. The
account-existence gate is what carries the safety weight now, not the
loopback default; do not remove or weaken it just because the shipped
example looks more open than it used to.

Two modes, picked automatically by whether any accounts exist
(`auth.any_users_exist()`):

- **No accounts** — the original local single-user mode, every request treated
  as an implicit admin. Safe only because nothing off the machine can reach it.
- **Accounts exist** — every page and API route requires a login. `WRITER`
  ("User" in every user-facing string) sees only their own employee folder and
  cannot start runs or change settings; `ADMIN` sees everything. **Only these
  two roles exist** — MANAGER was removed since no MANAGER account was ever
  created in practice; do not reintroduce a third role without a real need.

`webui/app.py`'s `main()` **refuses to start** (exit 2) when `web_host` is
non-loopback and no accounts exist. Do not remove that gate — it is the only
thing standing between an unauthenticated control panel and the whole LAN.

Enforcement lives in the `@protected` / `@controller_only` decorators, not in
the templates. Hiding a control in a template is presentation only; a writer
calling the API directly must still get a 403. Both matter — hide the control
*and* check the role — but only the decorator is security.

### Account lifecycle: self-signup + approval, or admin-direct

Two ways an account comes into existence — both documented in full in
`paper_automation/auth.py`'s module docstring, which is the source of truth if
this section and the code ever disagree:

- **`/signup` (public, primary path)** → `auth.request_signup()` creates a
  **pending** account with **no usable password at all**: the stored hash is
  of a random, unrecoverable token, not the default password. `authenticate()`
  checks `approved` *before* it ever looks at the submitted password — a
  pending account raises `PendingApprovalError` for literally any password
  typed against it. This is deliberate, not a shortcut: there is no real
  credential to compare against yet, so there is nothing to leak by refusing
  early.
- **`/users` "Add user" or `manage_users.py add` (optional, admin-direct)** →
  `auth.create_user()` is immediately active — no queue — because the admin
  creating it is vouching for it directly.
- **`auth.approve_user()`** is the one place a pending account's password
  first becomes real: it sets the fixed default password (`DEFAULT_PASSWORD =
  "iMatiz"`) and `must_change_password = True`.
- **The forced password-change gate** — `webui/app.py`'s
  `_require_password_change` `before_request` hook — redirects every route
  except `/account` and `/logout` to `/account` whenever
  `must_change_password` is set, for API routes too (403 JSON, not a redirect,
  since a fetch() call following a redirect would just receive HTML). This is
  what makes the default password unusable for anything beyond the one login
  that triggers the forced change.
- **Two distinct password-change functions, not one** —
  `complete_first_login()` (no current-password check; the fresh session
  *is* the proof) only works while `must_change_password` is set, and
  `change_own_password()` (does check the current password) is the ongoing
  voluntary path on `/account` afterwards. Do not merge these: collapsing them
  would either add an unnecessary current-password prompt to the forced first
  change, or — worse — remove the current-password check from the voluntary
  one.
- Accounts are created with `py manage_users.py` or the web UI's `/users`
  page. Disabling (or rejecting a still-pending request, the same
  `disabled=True`) is the normal way to revoke access. **`auth.delete_user()`
  is a real, permanent delete** — the one genuinely destructive operation in
  this module, added deliberately (not the original design) and guarded:
  it refuses to remove the last remaining approved, non-disabled Admin
  account, since that would either drop the system back to implicit-local-
  admin mode or leave User accounts that can log in with nobody able to
  reach `/users` to fix it. Both `/users`' Delete button and
  `manage_users.py delete` go through this same guard — never bypass it with
  a raw SQL statement from application code.

Traffic is plain HTTP by deliberate choice (trusted office LAN); that is
documented in the README and should not be quietly changed in either
direction.

### The tray/autostart tools are a thin shell around `ui.py`, not a new server

`tray_app.py` does not run the Flask/waitress app in-process — it spawns
`py ui.py --no-browser` as a **child process** (`ServerController` in
`tray_app.py`) and Stop is `Popen.terminate()`. This is a deliberate
simplicity trade-off: an interrupted run is recovered the same way an
unexpected crash already is, by `requeue_stale_processing_jobs()` (Phase 4)
on the next start — so there is no graceful-shutdown plumbing to maintain,
at the cost of an in-flight review/revision being cut off rather than
finishing first when someone clicks Stop. Do not "fix" this by adding
in-process threading without discussing it first; it was chosen on purpose.

`tray_app.py --open` (what the desktop shortcut runs) and plain `tray_app.py`
(what login autostart runs) are deliberately different entry points — see
the module docstring. `--open` checks whether something is already
listening on the port before ever spawning anything, specifically so a
double-click on the desktop icon can never create a second server bound to
the same port, or a second tray icon, when autostart already started one.
`ServerController.running` also treats "something else is already listening
on this port" as running, for the same reason.

### The UI is for non-technical readers

This is a hard design constraint, not a preference. The intended user has no
technical knowledge and should never see machine vocabulary:

- Statuses are plain English — "Waiting to be checked", not `READY_REVIEW`.
  `_STATUS_LABELS` and `plain()` in `service.py` do this translation; route
  handlers and templates must go through them rather than printing raw enum values.
- Progress belongs in the UI, not in a console-style dump.
- Phases read "Step 1 of 2 — checking papers for mistakes", not `phase_review`.

### Two traps in the progress/logging path

**Progress counts papers, not work items.** Each paper produces one event per phase,
so a naive count shows "8 of 4". `_latest_per_paper()` collapses them. Skipped and
already-complete folders must still emit `client_done`, or a fully-complete run
reports "0 of 4".

**The live log pane depends on the root logger's level.** It defaults to WARNING,
which drops the INFO lines the pane exists to show. `RunManager._execute` lowers
the level for the duration of a run and restores it afterwards. Removing that
leaves the pane silently empty — no error, just nothing.

### ETA

Estimates come from this system's own measured history in the audit trail, filtered
by `task_mode` and counting only successful states. Fewer than `_MIN_SAMPLES` (2)
falls back to `FALLBACK_SECONDS` — 60s grammar, 180s full. A paper not yet reviewed
costs both phases, so its estimate is the sum.

CLI version strings are cached for 10 minutes (`_VERSION_TTL`) because
`claude --version` takes ~1.1s — it starts the whole app — and was previously paid
on every page load.

## Running tests

```bash
py -m pytest tests/ -q
```

523 tests, no network, no CLI login needed. Includes safety test (AST-based,
checks for any delete/rename/move operations in source files).

## Open decision: running when the machine is off

Currently the machine **must be on and logged in** at 09:00, because the CLIs
depend on the user's saved interactive sessions. Two options were discussed and
neither is implemented — this is a deliberate hold, not an oversight:

- **A.** A Task Scheduler wake timer that brings the machine out of sleep.
- **B.** Catch-up on power-on — run the missed job when the machine next starts.

`StartWhenAvailable` is already set in `register_task.ps1`, which gets part of B.
Do not implement either without asking.

## Where the spec came from

README, `scripts/`, and several modules cite section numbers (§13, §35, §48, …)
from a 50-section master specification. **That document is not in the repo** — it
existed only in the original conversation. The citations are historical markers
showing which requirement a piece of code satisfies; they cannot be looked up.
Do not go hunting for the file, and do not add new `§` citations.

## Common CLI commands

```bash
py run.py --dry-run                # report decisions, change nothing
py run.py --test-mode              # first employee/client only, mock model
py run.py --phase review           # review phase only
py run.py --month "August 2026"    # re-run a specific month
py ui.py                           # start the web control panel
```
