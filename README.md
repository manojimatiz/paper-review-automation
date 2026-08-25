# Research Paper Review & Revision Automation

Drop a `.docx` into a client folder during the day. At 09:00 the next morning the
folder contains three files:

```
Vani/
├── Vani.docx                    <- your original, never modified
├── Vani_review.docx             <- Q1-standard review, written by Codex
└── Correct_Vani_paper.docx      <- revised manuscript, written by Claude
```

Nobody opens a chat window, uploads a file, or pastes a document. The pipeline reads
and writes files on disk.

---

## How it works

```
09:00  Windows Task Scheduler
   |
   +-> determine the current month in the configured timezone -> "August 2026"
   +-> walk every employee folder, then every client folder
   +-> apply the file-count rules to decide what is eligible
   |
   +-- REVIEW PHASE ------------------------------------------
   |     paper.docx -> markdown -> Codex -> <Client>_review.docx
   |     (every eligible client is reviewed before the next phase starts)
   |
   +-- REVISION PHASE ---------------------------------------
   |     re-scan from disk
   |     paper + review -> Claude -> Correct_<Client>_paper.docx
   |     validate; flag anything suspicious for human review
   |
   +-> write the log, print the daily summary, optionally notify
```

The models are reached through the **Codex** and **Claude Code** command-line
binaries that ship with the Codex and Claude desktop apps. Those authenticate with
your ChatGPT and Claude *subscriptions* — there are no API keys anywhere in this
system.

### What the models can and cannot touch

A model never sees your client folders. For each paper the pipeline copies the text
into a scratch directory as `manuscript.md`, runs the CLI confined to that
directory, and reads back the `output.md` it produces. The pipeline itself — not the
model — decides the output filename and writes the `.docx`.

This system contains **no** delete, move, or rename operation of any kind. A test
enforces that (`tests/test_safety.py`). Writes refuse to overwrite an existing file.

---

## Setup

### 1. Install the dependency

```bash
py -m pip install -r requirements.txt
```

`py` matters: the `python` on PATH is a Microsoft Store stub that does not work.

### 2. Confirm the two CLIs are present

Both ship with their desktop apps and are found automatically.

```bash
py -c "from paper_automation.providers import discovery; print(discovery.find_codex()); print(discovery.find_claude())"
```

If either prints `None`, install the corresponding desktop app, or set
`binary_path` under `[providers.codex]` / `[providers.claude]` in `config.toml`.

### 3. Log in once

Each CLI needs one interactive login. This is the only manual step in the system,
and the session persists across every future scheduled run.

```bash
codex login
claude
```

(Use the full paths printed in step 2 if the commands are not on your PATH.)

### 4. Configure

```bash
copy config.example.toml config.toml
```

Then set `research_papers_root` to the folder that **contains** your
`<Month Year>` directories. This is the "give it access to the folder" step.

```toml
research_papers_root = "C:/Users/you/OneDrive/Desktop/Research Papers"
timezone = "Asia/Kolkata"
```

Leave `scratch_dir` empty. It defaults to `%USERPROFILE%\PaperAutomation\scratch`,
and that location matters: Codex's Windows sandbox refuses to grant write access
anywhere under `AppData` — where its own state lives — and fails with *"no writable
root capability SIDs"*. If you override it, keep it outside `AppData` and outside
OneDrive.

### 5. Try it safely

Nothing below this line modifies a file until you run step 5c.

**a. Dry run** — reports what it *would* do, touches nothing:

```bash
py run.py --dry-run
```

```
[Manoj Paper]
  Vani           1 original / 0 review / 0 final   -> would send to Codex for review
  R Ashok        2 original / 0 review / 0 final   -> skip — 2 candidate papers found...
  Jyoti R        0 original / 0 review / 0 final   -> skip — No supported research paper found...
  Client D       1 original / 1 review / 0 final   -> would send to Claude for revision
```

**b. Mock run** — exercises the whole pipeline with a fake model, so you can check
the file naming and folder handling without spending any usage:

```bash
py run.py --test-mode
```

**c. One real paper** — point `research_papers_root` at a copy of a single client
folder first, then:

```bash
py run.py --provider real
```

### 6. Schedule it

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

**Timezone caveat:** Task Scheduler fires in your *machine's* local time, while the
month/date logic uses the `timezone` in `config.toml`. If the machine is not in
`Asia/Kolkata`, pass the equivalent local time:

```bash
powershell -File scripts\register_task.ps1 -At 06:30
```

Because the CLIs rely on your saved logins, the task runs as your interactive user.
The machine must be on and logged in at the scheduled time. Remove it with
`-Remove`.

---

## The rules that decide what gets processed

Files are grouped by **role**, not by raw count, so an arbitrary pair of documents
can never be mistaken for a paper-plus-review:

| Name pattern | Role |
|---|---|
| `Correct_*_paper.docx` | final |
| `*_review.docx` | review |
| any other supported file | original candidate |

Not counted at all: subdirectories, hidden/system files, Office lock files
(`~$paper.docx` — present whenever you have the document open), and any extension
not in `supported_extensions`.

**Review phase** runs only with exactly 1 original, no review, no final.
**Revision phase** runs only with exactly 1 original *and* exactly 1 review, no final.
All three present means done — it is skipped.
Anything else is skipped with the reason recorded.

Nothing is ever deleted to resolve an ambiguous folder. It is skipped and logged.

### Safe to re-run

Eligibility is recomputed from disk every time, so the job is idempotent. If it
crashes halfway, the next run picks up where it left off. It will never produce
`Vani_review_review.docx` or `Correct_Correct_Vani_paper.docx`.

---

## Scientific integrity

Both prompts forbid inventing results, datasets, citations, metrics, statistical
tests, or experiments, and require the model to say *"Missing information"* or
*"Requires author clarification"* rather than guess.

That is enforced, not just requested. After each revision the pipeline compares the
revised manuscript against the original and **fails** it for:

- a reported metric whose value changed (an inflated accuracy is the signature
  failure this guards against)
- a metric that appears in the revision but not the original
- leftover placeholder text (`[TODO]`, `[INSERT RESULT]`, `[ADD CITATION]`, …)
- a revision less than half the length of the original — likely truncated

It **warns**, without failing, when a dataset or model name disappears or an
expected section is missing.

A flagged paper is recorded `REQUIRES_HUMAN_REVIEW`, never `COMPLETED`. The file is
still written so you can inspect it. The summary lists every flagged paper and why.

---

## Failure handling

One bad paper never stops the batch: the failure is logged and the run continues.

Retries use exponential backoff, up to `max_retries`, and only for failures a retry
could fix (network errors, timeouts). Two conditions are *not* retried and instead
stop that phase immediately, because every remaining paper would fail the same way:

- **usage limit reached** — remaining papers are deferred to the next run
- **login required** — reported with an actionable message

---

## Sharing the panel on your network

By default the control panel is reachable only from the PC it runs on. To let
colleagues open it from their own machines:

**1. Bootstrap the first admin account.** This one step needs the CLI, since
there's no admin yet to approve a sign-up through the web UI:

```bash
py manage_users.py add yourname --role ADMIN
```

Sign in with `yourname` and the default password `iMatiz` — you'll be asked
to set your own password immediately.

**2. Let colleagues request their own accounts.** From then on, the everyday
way someone gets access is the web UI's own **Sign up** page (linked from the
sign-in screen): they enter their name, pick Admin or User, and — for a User
account — the exact employee folder name their papers live under (must match
the folder under `<Month Year>/` exactly, since that's what restricts them to
their own papers). That creates a *pending request* with no password
attached yet; you approve or reject it from the **Users** page. Approving is
what first gives the account the default password `iMatiz`, and their first
login immediately forces them to replace it with one of their own.

Creating an account directly yourself (via `py manage_users.py add` or the
Users page's "Add a user directly" form) is still available and skips the
request queue entirely, since you're vouching for it yourself — useful for
provisioning someone before they've had a chance to sign up.

**3. Open it to the network.** In `config.toml`:

```toml
web_host = "0.0.0.0"
```

**4. Allow the port through Windows Firewall** — for the **Private** profile
only, never Public. Colleagues then browse to
`http://<this-pc-name-or-ip>:5000/`.

The app refuses to start if `web_host` is opened up while no accounts exist,
rather than serving an unprotected panel to the network.

### What each role can do

| | Admin | User |
|---|---|---|
| See papers | every employee | their own employee only |
| Start a run, change settings, change the schedule, edit prompts | yes | no |
| See the Users page, approve/reject sign-up requests | yes | no |
| Open a file on the server | any paper | their own only |

### Security notes

- Passwords are stored only as salted hashes, never in plain text, and are
  never written to a log.
- Every restriction is enforced on the server. Hiding a button is cosmetic;
  a User calling the API directly is refused with a 403.
- Disabling — or rejecting a sign-up request — revokes access on the next
  request without erasing the record. **Delete** (on `/users`, or
  `py manage_users.py delete NAME`) is the one genuinely permanent action in
  the account system — it's refused for the last remaining Admin account, so
  it can't be used to lock everyone out.
- A pending sign-up request has **no usable password at all** until an admin
  approves it: `/login` shows "waiting for an administrator" for any password
  typed against a pending account, without ever checking whether it was
  correct. The fixed default password `iMatiz` only becomes real the moment
  an admin approves (or creates) the account, and the very next login on it
  forces a real password to be set before anything else is reachable.
- `/login` is rate-limited per source IP (in-process, resets on restart) —
  meaningfully slows down password guessing, is not a hardened auth server.
- **Traffic is plain HTTP**, so passwords cross the network unencrypted. That
  is an acceptable trade-off on a trusted office LAN and nowhere else — do not
  expose this to the internet or to a network you do not control.
- Sessions end when the app restarts, unless you set a fixed
  `PAPER_AUTOMATION_SECRET_KEY` environment variable.

### Always-on access: desktop icon + system tray

`config.toml` ships with `web_host = "0.0.0.0"` by default now (see above),
and the tray tools below make it always reachable rather than only while
you've manually run `py ui.py`:

**One-time setup**, both PowerShell scripts run from the project folder:

```bash
powershell -ExecutionPolicy Bypass -File scripts\install_desktop_icon.ps1
powershell -ExecutionPolicy Bypass -File scripts\register_tray_autostart.ps1
```

- The first puts a **"Paper Review Automation" icon on your Desktop**.
  Clicking it opens the dashboard in your browser — starting the server
  first if it isn't already running.
- The second makes the **system tray icon start automatically when you log
  in**, the same way `register_task.ps1` already schedules the daily run —
  just with a logon trigger instead of a daily one. No console window, no
  browser tab pops up on its own at login; it just quietly starts the server
  and sits in the notification area.

**The tray icon menu**: Open dashboard · Start/Stop service (a toggle,
reflecting whatever's actually running) · the LAN URL to share with
colleagues, once running · Exit (stops the server and removes the icon).

Both scripts accept `-Remove` to undo themselves. `py ui.py` (the original,
console-window path) still works exactly as before — the tray is an
addition, not a replacement, for the times you just want a quick local check
without any of this.

**One more reminder on the Firewall step**, since it's easy to do everything
above and still not be reachable: the port still needs to be allowed through
Windows Firewall for the **Private** profile (never Public) before another
PC on the network can actually reach it — see step 3 under "Sharing the
panel on your network" above.

---

## Command reference

```bash
py run.py                          # normal scheduled run
py run.py --dry-run                # report decisions, change nothing
py run.py --test-mode              # first employee/client only, mock model
py run.py --provider mock|real     # force the model choice
py run.py --phase review|revise    # run a single phase
py run.py --month "July 2026"      # re-run a specific month
py run.py --loop                   # stay resident, rescan on a schedule
py run.py --backup-now             # back up state.sqlite3, then exit
py run.py --verbose                # include the CLI command lines
```

```bash
py manage_users.py list                              # show accounts, incl. pending requests
py manage_users.py add NAME --role ADMIN             # create an account, active immediately
py manage_users.py add NAME --role WRITER --employee "Folder"
py manage_users.py approve NAME                      # activate a pending sign-up request
py manage_users.py reject NAME                       # decline a pending sign-up request
py manage_users.py passwd NAME                       # set a specific password by hand
py manage_users.py reset-password NAME               # reset to the default (iMatiz)
py manage_users.py disable NAME                      # revoke access
py manage_users.py enable NAME                       # restore access
py manage_users.py delete NAME                       # permanently remove (last-admin guarded)
```

```bash
py -m pytest tests/ -q             # 523 tests, no login or network needed
```

---

## Where things are

| Path | What |
|---|---|
| `config.toml` | your settings (not in version control) |
| `logs/run-YYYY-MM-DD.log` | per-run log, with credentials redacted |
| `state/processing.sqlite3` | processing state and the audit trail |
| `%USERPROFILE%\PaperAutomation\scratch` | per-paper working files |

The audit trail records, for every paper: timestamp, employee, client, phase, which
model ran, the resulting state, the file produced, and any error. Query it directly:

```sql
SELECT timestamp, client, phase, model, state, message FROM audit ORDER BY id DESC;
```

Scratch directories are never deleted automatically, since this pipeline does not
delete anything. Clear old ones by hand when you want the space back.

---

## Building a portable installer

`installer/` holds a PyInstaller + Inno Setup build that packages the app as a
single Windows installer — for a PC that just needs the finished tool, not a
Python dev setup. Five executables share one runtime under one install:
`PaperReviewAutomation.exe` (tray), `PaperReviewAutomationService.exe` (web
server, spawned by the tray), `paper-review-run.exe` (Task Scheduler target),
`paper-review-users.exe` (admin CLI), and `FirstRunSetup.exe` (one-time wizard
that writes `config.toml` and creates the first admin account).

```bash
py -m pip install pyinstaller
py -m PyInstaller installer\paper_review_automation.spec --distpath dist --workpath build --noconfirm
```

That produces `dist\PaperReviewAutomation\` (~90 MB, all five exes). To wrap
it into an actual `.exe` installer, install [Inno Setup](https://jrsoftware.org/isinfo.php)
and run:

```bash
ISCC.exe installer\installer.iss
```

The installer is per-user (no admin rights needed): program files go under
`%LOCALAPPDATA%\Programs\PaperReviewAutomation`, while `config.toml`,
`state\`, and `logs\` live separately under `%USERPROFILE%\PaperReviewAutomation`
— kept apart deliberately, so re-running a newer installer over an existing
one replaces the program files and never touches accounts, job history, or
settings. Uninstalling only removes the program-files half, for the same
reason. Codex and Claude Code themselves are not bundled — they're separate
subscription-authenticated desktop apps; the installer sets up the
automation and dashboard around them, not model access itself. The build is
not code-signed, so Windows SmartScreen may warn on first run.

---

## Adding Google Drive later

The storage layer is abstracted behind `paper_automation/storage/base.py`. If you
use Drive for Desktop, no work is needed — point `research_papers_root` at the
synced local folder. `storage/gdrive.py` documents what a direct API backend would
need if you ever want one.

---

## Known constraints

- **Usage limits** cap how many papers one night can handle. The pipeline defers the
  rest rather than failing them permanently, so a large backlog drains over several
  nights.
- **The machine must be on and logged in** at the scheduled time, because the CLIs
  use your saved interactive sessions.
- **CLI flags change between versions.** They are config values
  (`extra_args`, `binary_path`) and a `--version` probe runs before any paper is
  processed, so a breaking update fails fast with a clear message.
- **Review quality depends on the CLIs' default models.** Set `model` under
  `[providers.*]` to pin a specific one.
