"""UI-agnostic operations layer.

Everything a front end needs, expressed as plain dicts and dataclasses. No Flask
import belongs in this module and no HTML belongs in it either: the web UI is one
consumer, and a future desktop app, REST API, or scheduled report is another.

Storage is always reached through StorageBackend, so when papers move to Drive this
layer keeps working unchanged. The one local-only capability — asking the operating
system to open a file — is reported through `supports_open_file` rather than
assumed, so a front end can hide the button when it does not apply.
"""

import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_module
from . import model_registry, phases, scanner, usage
from .config import Config
from .models import Decision, Phase, ProcessingState
from .state import StateStore
from .storage import LocalStorage
from .storage.base import StorageBackend

log = logging.getLogger(__name__)

# Every subprocess call in this module shells out to a console-subsystem
# program (schtasks.exe, powershell.exe) from a windowless (pythonw.exe-run)
# app. Without this flag, Windows briefly opens and closes a visible console
# for each call — this suppresses that, without changing what's captured.
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SCHEDULED_TASK_NAME = "ResearchPaperAutomation"
MAX_LOG_LINES = 4000


# --------------------------------------------------------------------- scanning


@dataclass
class FileRow:
    name: str
    role: str
    path: str


@dataclass
class ClientRow:
    """One client folder, flattened for display."""

    employee: str
    client: str
    folder: str
    files: list[FileRow] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    originals: int = 0
    reviews: int = 0
    finals: int = 0
    status: str = ""
    status_label: str = ""
    action: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        return f"{self.employee}/{self.client}"


# Display status, derived from the same rules the pipeline uses so the UI can never
# disagree with what a run would actually do.
_STATUS_LABELS = {
    "COMPLETE": "Done",
    "READY_REVIEW": "Waiting to be checked",
    "READY_REVISE": "Waiting for corrections",
    "BLOCKED": "Needs your attention",
    "EMPTY": "No paper yet",
}

# Plain-English replacements for the wording the pipeline uses internally. The
# person reading the dashboard did not write this system and should not have to
# know what a "phase" or an "original" is.
_PLAIN_REASONS = (
    ("No supported research paper found; expected exactly 1.",
     "This folder is empty. Put the paper in here and it will be picked up."),
    ("No original paper found; expected exactly 1.",
     "This folder is empty. Put the paper in here and it will be picked up."),
    ("Refusing to guess which is the original.",
     "so nothing was touched."),
    ("candidate papers found; expected exactly 1.",
     "papers in this folder. Leave just one and the rest will be handled."),
    ("Review already exists; nothing to do in the review phase.",
     "Already checked. A corrected copy is next."),
    ("Review was not generated; expected exactly 1 original + 1 review.",
     "Not checked yet, so there is nothing to correct from."),
    ("Original, review and final all present.",
     "All three files are here, so this one is finished."),
    ("Exactly 1 original paper and no review.",
     "One paper, ready to be checked."),
    ("Exactly 1 original paper and 1 review.",
     "Checked already; ready for a corrected copy."),
)


def plain(text: str) -> str:
    """Rewrite an internal reason into something anyone can read."""
    if not text:
        return ""
    for jargon, friendly in _PLAIN_REASONS:
        if jargon in text:
            text = text.replace(jargon, friendly)
    return (
        text.replace("original(s)", "papers")
        .replace("review(s)", "checked copies")
        .replace("final file(s)", "corrected copies")
        .replace("Unexpected state:", "Something unusual here:")
        .replace("Ambiguous folder:", "Too many files here:")
        .strip()
    )


def _derive_status(state) -> tuple[str, str, str]:
    """Return (status, action, reason) for a folder, using the pipeline's own rules."""
    review = state.decide(Phase.REVIEW)
    revise = state.decide(Phase.REVISE)

    if review.decision is Decision.COMPLETED:
        return "COMPLETE", "Nothing left to do", plain(review.reason)
    if review.decision is Decision.PROCESS:
        return "READY_REVIEW", "Will be checked for mistakes", plain(review.reason)
    if revise.decision is Decision.PROCESS:
        return "READY_REVISE", "A corrected copy will be made", plain(revise.reason)
    if not state.originals and not state.reviews and not state.finals:
        return "EMPTY", "Waiting for a paper", plain(review.reason)
    return "BLOCKED", "Will be left alone", plain(review.reason)


def scan(
    cfg: Config,
    storage: StorageBackend,
    month: str | None = None,
    employee_filter: str | None = None,
) -> dict:
    """Everything the dashboard needs about the current month.

    `employee_filter`, when set, restricts the result to one employee's client
    folders — how a WRITER login's view is scoped to their own papers. Counts
    are derived from the already-filtered rows, so they stay consistent
    automatically.
    """
    month = month or scanner.current_month(cfg)
    root_exists = storage.find_folder(cfg.research_papers_root) is not None
    month_dir = scanner.month_folder(cfg, storage, month) if root_exists else None

    rows: list[ClientRow] = []
    if month_dir is not None:
        for state in scanner.scan_month(cfg, storage, month_dir):
            if employee_filter and state.employee != employee_filter:
                continue
            status, action, reason = _derive_status(state)
            row = ClientRow(
                employee=state.employee,
                client=state.client,
                folder=str(state.folder),
                originals=len(state.originals),
                reviews=len(state.reviews),
                finals=len(state.finals),
                status=status,
                status_label=_STATUS_LABELS[status],
                action=action,
                reason=reason,
                ignored=[p.name for p in state.ignored],
            )
            for role, paths in (
                ("original", state.originals),
                ("review", state.reviews),
                ("final", state.finals),
            ):
                row.files.extend(
                    FileRow(name=p.name, role=role, path=str(p)) for p in paths
                )
            rows.append(row)

    counts = {key: 0 for key in _STATUS_LABELS}
    for row in rows:
        counts[row.status] += 1

    return {
        "month": month,
        "task_mode": cfg.task_mode,
        "root": str(cfg.research_papers_root),
        "root_exists": root_exists,
        "month_exists": month_dir is not None,
        "rows": [asdict(r) for r in rows],
        "counts": counts,
        "employees": sorted({r.employee for r in rows}),
        "months": available_months(cfg, storage),
    }


def available_months(cfg: Config, storage: StorageBackend) -> list[str]:
    if storage.find_folder(cfg.research_papers_root) is None:
        return []
    return [p.name for p in storage.list_folders(cfg.research_papers_root)]


# --------------------------------------------------------- folder management


def _safe_segment(name: str) -> str:
    """One path segment, safe to create as a folder name.

    Rejects a bad name outright rather than silently mangling it with
    config.sanitize() — a name that needed rewriting to become safe was
    probably a typo or a paste-in mistake, not something to accept quietly.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Name cannot be empty.")
    if name in (".", ".."):
        raise ValueError("Name cannot be '.' or '..'.")
    if "/" in name or "\\" in name:
        raise ValueError("Name cannot contain a path separator.")
    if config_module.sanitize(name) != name:
        raise ValueError(
            f"{name!r} contains a character that cannot be used in a folder name."
        )
    return name


def create_employee_folder(
    cfg: Config, storage: StorageBackend, month: str, employee: str
) -> dict:
    """Create <root>/<month>/<employee>, and the month folder too if needed.

    Idempotent like the rest of the pipeline: creating a folder that already
    exists is success, not an error (spec section 38).
    """
    try:
        month = _safe_segment(month)
        employee = _safe_segment(employee)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    target = cfg.research_papers_root / month / employee
    storage.create_folder(target)
    return {
        "ok": True,
        "message": f"Created a folder for {employee} in {month}.",
        "path": str(target),
    }


def create_client_folder(
    cfg: Config, storage: StorageBackend, month: str, employee: str, client: str
) -> dict:
    """Create <root>/<month>/<employee>/<client>, creating any missing parent
    folder along the way (spec section 38)."""
    try:
        month = _safe_segment(month)
        employee = _safe_segment(employee)
        client = _safe_segment(client)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    target = cfg.research_papers_root / month / employee / client
    storage.create_folder(target)
    return {
        "ok": True,
        "message": f"Created a folder for {client} under {employee}.",
        "path": str(target),
    }


# ------------------------------------------------------------------- durations

# Used only until this installation has measured its own papers. A grammar pass
# and a full review differ enormously, so the fallback is per mode.
FALLBACK_SECONDS = {"grammar": 60.0, "full": 180.0}
_MIN_SAMPLES = 2


def format_duration(seconds: float) -> str:
    """Readable elapsed time: "45 seconds", "3 min 20 sec", "1 hr 12 min"."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs} sec" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min" if minutes else f"{hours} hr"


def average_seconds(cfg: Config) -> dict:
    """Mean time per paper per phase, measured from this installation's own runs.

    Falls back to a built-in figure until enough samples exist. Only successful
    rows count: a folder that was skipped in a fraction of a second would drag the
    estimate down and make every prediction far too optimistic.
    """
    default = FALLBACK_SECONDS.get(cfg.task_mode, 180.0)
    result = {"review": default, "revise": default, "measured": False, "samples": 0}
    if not Path(cfg.state_db).exists():
        return result

    try:
        with StateStore(Path(cfg.state_db)) as store:
            rows = store._conn.execute(
                """
                SELECT phase, AVG(seconds) AS mean, COUNT(*) AS n FROM audit
                 WHERE seconds > 0 AND task_mode = ?
                   AND state IN ('COMPLETED', 'REVIEW_COMPLETED', 'REVISION_COMPLETED',
                                 'REQUIRES_HUMAN_REVIEW')
                 GROUP BY phase
                """,
                (cfg.task_mode,),
            ).fetchall()
    except Exception:  # an estimate is never worth an error page
        return result

    total = 0
    for row in rows:
        if row["phase"] in ("review", "revise") and row["n"] >= _MIN_SAMPLES:
            result[row["phase"]] = float(row["mean"])
            result["measured"] = True
            total += row["n"]
    result["samples"] = total
    return result


# ---------------------------------------------------------------------- history


def history(
    cfg: Config,
    limit: int = 200,
    month: str | None = None,
    employee_filter: str | None = None,
) -> list[dict]:
    """Recent audit rows, newest first.

    `employee_filter` scopes this to one employee, the same restriction `scan`
    applies for a WRITER login.
    """
    if not Path(cfg.state_db).exists():
        return []
    with StateStore(Path(cfg.state_db)) as store:
        sql = (
            "SELECT timestamp, month, employee, client, phase, model, state, "
            "file_path, message, tokens, seconds FROM audit"
        )
        conditions = []
        params: list = []
        if month:
            conditions.append("month = ?")
            params.append(month)
        if employee_filter:
            conditions.append("employee = ?")
            params.append(employee_filter)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = store._conn.execute(sql, tuple(params)).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        _, label = _OUTCOME_VIEW.get(item.get("state", ""), ("skipped", "Left alone"))
        item["friendly"] = label
        item["message"] = plain(item.get("message", ""))
        item["tokens_label"] = (
            usage.humanise(item["tokens"]) if item.get("tokens") else ""
        )
        item["duration_label"] = (
            format_duration(item["seconds"]) if item.get("seconds") else ""
        )
        out.append(item)
    return out


def log_files(cfg: Config, limit: int = 30) -> list[dict]:
    log_dir = Path(cfg.log_dir)
    if not log_dir.is_dir():
        return []
    files = sorted(log_dir.glob("run-*.log"), reverse=True)[:limit]
    return [
        {"name": f.name, "path": str(f), "size": f.stat().st_size}
        for f in files
    ]


# ----------------------------------------------------------------------- config

# Only these may be edited from a front end. Credentials and provider binary paths
# are deliberately excluded: they belong in the file or the environment, not in a
# form that is one click away from a browser.
EDITABLE = {
    "research_papers_root": "path",
    "timezone": "str",
    "task_mode": "str",
    "supported_extensions": "list",
    "create_missing_month": "bool",
    "scratch_dir": "path",
    "max_retries": "int",
    "retry_base_delay": "float",
}


def _format_toml_value(value, kind: str) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("int",):
        return str(int(value))
    if kind == "float":
        return str(float(value))
    if kind == "list":
        items = value if isinstance(value, list) else [
            v.strip() for v in str(value).split(",") if v.strip()
        ]
        inner = ", ".join('"%s"' % str(i).strip().strip('"') for i in items)
        return f"[{inner}]"
    text = str(value).replace("\\", "/").strip().strip('"')
    return f'"{text}"'


def update_config_file(path: Path, updates: dict) -> list[str]:
    """Rewrite scalar keys in place, preserving comments and layout.

    A full parse-and-dump would strip every comment out of config.toml, and those
    comments are the only documentation someone editing the file by hand will see.
    So this edits the specific lines instead, and appends a key only when it is
    genuinely absent.
    """
    if not updates:
        return []

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    applied: list[str] = []

    for key, raw in updates.items():
        kind = EDITABLE.get(key)
        if kind is None:
            continue
        rendered = f"{key} = {_format_toml_value(raw, kind)}"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i, line in enumerate(lines):
            # Stop at the first table header: these keys all live at top level, and
            # a same-named key under [providers.codex] must not be clobbered.
            if line.lstrip().startswith("["):
                break
            if pattern.match(line):
                if lines[i] != rendered:
                    lines[i] = rendered
                    applied.append(key)
                break
        else:
            insert_at = next(
                (i for i, line in enumerate(lines) if line.lstrip().startswith("[")),
                len(lines),
            )
            lines.insert(insert_at, rendered)
            applied.append(key)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return applied


def config_values(cfg: Config) -> dict:
    return {
        "research_papers_root": str(cfg.research_papers_root),
        "timezone": cfg.timezone,
        "task_mode": cfg.task_mode,
        "supported_extensions": ", ".join(cfg.supported_extensions),
        "create_missing_month": cfg.create_missing_month,
        "scratch_dir": str(cfg.scratch_dir),
        "max_retries": cfg.max_retries,
        "retry_base_delay": cfg.retry_base_delay,
    }


# ------------------------------------------------------------------- model info


def registry_path(base_dir: Path) -> Path:
    return base_dir / model_registry.REGISTRY_FILENAME


# Asking a CLI for its version means starting the whole app: `claude --version`
# alone takes about a second. The answer only changes when the app updates, but
# it was being paid on every page load. Cached across requests, with a TTL so a
# version bump is still noticed without restarting the server.
_VERSION_TTL = 600.0
_version_cache: dict[str, tuple[float, str]] = {}


def cli_version(provider_name: str, provider_cfg, force: bool = False) -> str:
    """The CLI's version string, cached. Returns "" when the app is unavailable."""
    key = f"{provider_name}:{provider_cfg.binary_path or ''}"
    cached = _version_cache.get(key)
    if cached and not force and (time.monotonic() - cached[0]) < _VERSION_TTL:
        return cached[1]

    try:
        version = _provider_instance(provider_name, provider_cfg).version()
    except Exception:  # display only; a missing app must not break the page
        version = ""
    _version_cache[key] = (time.monotonic(), version)
    return version


def clear_version_cache() -> None:
    _version_cache.clear()


def model_status(cfg: Config, base_dir: Path) -> dict:
    """What each stage will use, plus the CLI version, for display.

    Never raises. A missing app shows as unavailable rather than breaking the page.
    """
    registry = model_registry.load(registry_path(base_dir))
    stages = []

    for stage, provider_name, provider_cfg, role in (
        ("Review", "codex", cfg.codex, "Codex / ChatGPT"),
        ("Revision", "claude", cfg.claude, "Claude"),
    ):
        active = model_registry.active_model(provider_name, provider_cfg.model)
        version = cli_version(provider_name, provider_cfg)
        available = bool(version)

        stages.append({
            "stage": stage,
            "provider": provider_name,
            "role": role,
            "available": available,
            "version": version,
            "options": model_registry.as_dicts(registry.for_provider(provider_name)),
            **active,
        })
    return {"stages": stages}


def _provider_instance(provider_name: str, provider_cfg):
    from .providers.claude_code import ClaudeCodeProvider
    from .providers.codex import CodexProvider

    cls = CodexProvider if provider_name == "codex" else ClaudeCodeProvider
    return cls(provider_cfg)


def update_provider_model(config_path: Path, provider: str, model_id: str) -> bool:
    """Set providers.<name>.model, which lives inside a TOML table."""
    if provider not in ("codex", "claude"):
        return False

    model_id = (model_id or "").strip()
    text = config_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header = f"[providers.{provider}]"
    rendered = f'model = "{model_id}"'

    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        lines += ["", header, rendered]
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            break  # next table; the key is absent from this one
        if re.match(r"^\s*model\s*=", lines[i]):
            if lines[i] == rendered:
                return False
            lines[i] = rendered
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    else:
        i = len(lines)

    lines.insert(i, rendered)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------- schedule


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, timeout=20,
        creationflags=_CREATE_NO_WINDOW,
    )


DEFAULT_SCHEDULE_TIME = "09:00"

_START_BOUNDARY = re.compile(r"<StartBoundary>\s*\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})")
# "Next Run Time: 12/08/2026 09:00:00" — the fallback when the XML is unreadable.
_NEXT_RUN_TIME = re.compile(r"(\d{1,2}):(\d{2}):\d{2}\s*(AM|PM)?", re.IGNORECASE)


def scheduled_time() -> str:
    """The hour the task is actually registered for, as HH:MM.

    Read from the task rather than assumed, so the settings field shows the real
    time instead of a hard-coded default that quietly lies after any change.
    """
    try:
        result = _schtasks("/Query", "/TN", SCHEDULED_TASK_NAME, "/XML", "ONE")
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode == 0 and result.stdout:
        # schtasks emits UTF-16 here, which can arrive with interleaved NULs.
        match = _START_BOUNDARY.search(result.stdout.replace("\x00", ""))
        if match:
            return match.group(1)

    try:
        listing = _schtasks("/Query", "/TN", SCHEDULED_TASK_NAME, "/FO", "LIST")
    except (OSError, subprocess.SubprocessError):
        return ""
    if listing.returncode != 0:
        return ""
    for line in listing.stdout.splitlines():
        if line.lower().startswith("next run time"):
            match = _NEXT_RUN_TIME.search(line)
            if not match:
                continue
            hour, minute, meridiem = int(match.group(1)), match.group(2), match.group(3)
            if meridiem:
                upper = meridiem.upper()
                if upper == "PM" and hour != 12:
                    hour += 12
                elif upper == "AM" and hour == 12:
                    hour = 0
            return f"{hour:02d}:{minute}"
    return ""


def schedule_status() -> dict:
    """Whether the daily task exists, when it next runs, and at what time."""
    if sys.platform != "win32":
        return {
            "supported": False, "enabled": False, "detail": "Windows only.",
            "time": DEFAULT_SCHEDULE_TIME,
        }
    try:
        result = _schtasks("/Query", "/TN", SCHEDULED_TASK_NAME, "/FO", "LIST")
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": True, "enabled": False, "detail": str(exc),
            "time": DEFAULT_SCHEDULE_TIME,
        }

    if result.returncode != 0:
        return {
            "supported": True, "enabled": False, "detail": "Not scheduled.",
            "time": DEFAULT_SCHEDULE_TIME,
        }

    detail = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            detail[key.strip()] = value.strip()
    return {
        "supported": True,
        "enabled": detail.get("Status", "") != "Disabled",
        "next_run": detail.get("Next Run Time", ""),
        "status": detail.get("Status", ""),
        "time": scheduled_time() or DEFAULT_SCHEDULE_TIME,
        "detail": "",
    }


def set_schedule(enabled: bool, base_dir: Path, at: str = "09:00") -> dict:
    """Register or remove the daily task by delegating to the existing script."""
    if sys.platform != "win32":
        return {"ok": False, "message": "Scheduling is Windows only."}

    script = base_dir / "scripts" / "register_task.ps1"
    if not script.exists():
        return {"ok": False, "message": f"Missing {script}"}

    args = [
        "powershell", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-TaskName", SCHEDULED_TASK_NAME,
    ]
    if enabled:
        args += ["-At", at]
    else:
        args += ["-Remove"]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=90,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": str(exc)}

    output = (result.stdout + result.stderr).strip()
    return {"ok": result.returncode == 0, "message": output[-500:]}


# ------------------------------------------------------------------ file access


def supports_open_file(storage: StorageBackend) -> bool:
    """Only a local backend can be handed to the desktop shell."""
    return isinstance(storage, LocalStorage) and sys.platform == "win32"


def open_in_explorer(target: Path, cfg: Config) -> dict:
    """Open a file or folder with the OS default handler.

    Confined to the configured tree so a crafted path cannot turn the local server
    into a way to open arbitrary files.
    """
    target = Path(target).resolve()
    allowed = [Path(cfg.research_papers_root).resolve(), Path(cfg.log_dir).resolve()]
    if not any(_is_within(target, root) for root in allowed):
        return {"ok": False, "message": "Path is outside the configured folders."}
    if not target.exists():
        return {"ok": False, "message": "That file no longer exists."}
    try:
        import os

        os.startfile(str(target))  # noqa: S606  (Windows shell open, local UI only)
        return {"ok": True, "message": f"Opened {target.name}"}
    except OSError as exc:
        return {"ok": False, "message": str(exc)}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def path_belongs_to_employee(path: Path, cfg: Config, employee: str) -> bool:
    """True when `path` sits under <root>/<month>/<employee>/.

    Used to stop a WRITER login acting on another employee's files. Resolved
    first, so a path containing ".." cannot climb out of their folder and back
    into someone else's.
    """
    root = Path(cfg.research_papers_root).resolve()
    try:
        parts = Path(path).resolve().relative_to(root).parts
    except (ValueError, OSError):
        return False
    # <month>/<employee>/... — the employee segment is the second one.
    return len(parts) >= 2 and parts[1] == employee


# ------------------------------------------------------------------------- jobs

_JOB_STATUS_LABELS = {
    "QUEUED": "Waiting",
    "REVIEW_IN_PROGRESS": "Checking now",
    "REVISION_IN_PROGRESS": "Correcting now",
    "REVIEW_COMPLETED": "Checked",
    "COMPLETED": "Done",
    "REQUIRES_HUMAN_REVIEW": "Needs a look",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}

_JOB_PHASE_LABELS = {"review": "Checking for mistakes", "revise": "Making corrections"}


def _job_view(row) -> dict:
    item = dict(row)
    item["status_label"] = _JOB_STATUS_LABELS.get(item["status"], item["status"])
    item["phase_label"] = _JOB_PHASE_LABELS.get(item["phase"], item["phase"])
    item["reason"] = plain(item.get("reason", ""))
    item["provider"] = _PHASE_PROVIDER.get(Phase(item["phase"]), "")
    return item


def job_overview(cfg: Config, status: str | None = None, limit: int = 200) -> dict:
    """Everything the Jobs page needs: recent job records plus system status.

    Read-only. There is deliberately no retry-a-single-job action here yet —
    that needs the worker pool to actually consume a persistent queue between
    runs, which Phase 4 stopped short of on purpose (see ROADMAP.md).
    """
    jobs: list[dict] = []
    counts: dict[str, int] = {}
    if Path(cfg.state_db).exists():
        with StateStore(Path(cfg.state_db)) as store:
            filter_status = ProcessingState(status) if status else None
            jobs = [
                _job_view(r)
                for r in store.list_jobs(filter_status, limit=limit, newest_first=True)
            ]
            counts = store.job_status_counts()

    return {
        "jobs": jobs,
        "counts": counts,
        "total": sum(counts.values()),
        "filter": status or "",
    }


def _humanise_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def system_health(cfg: Config, storage: StorageBackend, run_status: dict) -> dict:
    """A one-glance status panel: storage, automation, schedule, concurrency,
    and enough about disk usage that a human notices before it becomes a
    problem, rather than the pipeline trying to clean up after itself
    (spec section 44 — this project never deletes a file on its own)."""
    from . import backup as backup_module

    storage_ok = storage.find_folder(cfg.research_papers_root) is not None
    sched = schedule_status()

    log_dir = Path(cfg.log_dir)
    log_count = len(list(log_dir.glob("run-*.log"))) if log_dir.is_dir() else 0

    db_path = Path(cfg.state_db)
    db_size_label = _humanise_bytes(db_path.stat().st_size) if db_path.exists() else "—"

    latest = backup_module.latest_backup(cfg.backup_dir)
    last_backup_label = latest.stem.rsplit("-", 1)[-1] if latest else "never"

    return {
        "storage_ok": storage_ok,
        "storage_root": str(cfg.research_papers_root),
        "automation_running": run_status.get("running", False),
        "scheduler_enabled": sched.get("enabled", False),
        "scheduler_next_run": sched.get("next_run", ""),
        "max_concurrent_jobs": cfg.max_concurrent_jobs,
        "task_mode": cfg.task_mode,
        "web_host": cfg.web_host,
        "lan_reachable": cfg.web_host not in ("127.0.0.1", "localhost", "::1"),
        "log_file_count": log_count,
        "state_db_size_label": db_size_label,
        "last_backup_label": last_backup_label,
    }


def backup_now(cfg: Config) -> dict:
    from . import backup as backup_module

    target = backup_module.backup_state_db(cfg.state_db, cfg.backup_dir)
    if target is None:
        return {"ok": False, "message": "Nothing to back up yet."}
    return {"ok": True, "message": f"Backed up to {target.name}.", "path": str(target)}


# ----------------------------------------------------------------------- prompts

# Which model runs each phase. Fixed by the pipeline, shown so the person editing
# a prompt knows who will read it.
_PHASE_PROVIDER = {Phase.REVIEW: "Codex", Phase.REVISE: "Claude"}
_PHASE_TITLE = {
    Phase.REVIEW: "Finding the mistakes",
    Phase.REVISE: "Writing the corrections",
}


def prompt_overview(cfg: Config) -> list[dict]:
    """Both prompts for the current task mode, with their version history."""
    from . import prompts as prompts_module

    out = []
    for phase in (Phase.REVIEW, Phase.REVISE):
        default = prompts_module.default_body(phase, cfg.task_mode)
        active, versions = None, []
        if Path(cfg.state_db).exists():
            with StateStore(Path(cfg.state_db)) as store:
                row = store.active_prompt(phase, cfg.task_mode)
                active = dict(row) if row else None
                versions = [dict(v) for v in store.prompt_versions(phase, cfg.task_mode)]
        out.append({
            "phase": phase.value,
            "provider": _PHASE_PROVIDER[phase],
            "title": _PHASE_TITLE[phase],
            "placeholders": list(prompts_module.PLACEHOLDERS[phase]),
            "default_body": default,
            "body": active["body"] if active else default,
            "customised": active is not None,
            "active_version": active["version"] if active else None,
            "versions": versions,
        })
    return out


def save_prompt(cfg: Config, phase: str, body: str, username: str = "") -> dict:
    """Validate and store a new prompt version, making it active."""
    from . import prompts as prompts_module

    target = Phase(phase)
    try:
        prompts_module.validate_custom(body, target)
    except prompts_module.PromptError as exc:
        return {"ok": False, "message": str(exc)}

    with StateStore(Path(cfg.state_db)) as store:
        version = store.save_prompt(target, cfg.task_mode, body, username)
    return {"ok": True, "message": f"Saved as version {version}.", "version": version}


def activate_prompt(cfg: Config, phase: str, version: int) -> dict:
    with StateStore(Path(cfg.state_db)) as store:
        if not store.activate_prompt_version(Phase(phase), cfg.task_mode, version):
            return {"ok": False, "message": f"No version {version} to restore."}
    return {"ok": True, "message": f"Version {version} is now in use."}


def reset_prompt(cfg: Config, phase: str) -> dict:
    """Go back to the built-in prompt. Saved versions are kept, not deleted."""
    with StateStore(Path(cfg.state_db)) as store:
        store.clear_active_prompt(Phase(phase), cfg.task_mode)
    return {"ok": True, "message": "Using the built-in instructions again."}


# ------------------------------------------------------------------- run manager


PHASE_LABELS = {
    "review": "Step 1 of 2 — checking papers for mistakes",
    "revise": "Step 2 of 2 — writing the corrected copies",
}

# How each internal outcome reads to someone who did not build this.
_OUTCOME_VIEW = {
    "COMPLETED": ("done", "Finished"),
    "REVIEW_COMPLETED": ("done", "Checked"),
    "REVISION_COMPLETED": ("done", "Corrected copy made"),
    "REQUIRES_HUMAN_REVIEW": ("attention", "Done — please read it before sending"),
    "VALIDATION_FAILED": ("attention", "Done — please read it before sending"),
    "FAILED": ("failed", "Could not be completed"),
    "SKIPPED": ("skipped", "Left alone"),
}


def _result_item(event: dict) -> dict:
    """One finished client, described for a non-technical reader."""
    tone, label = _OUTCOME_VIEW.get(event.get("state", ""), ("skipped", "Left alone"))
    created = event.get("created", "")
    return {
        "client": event.get("client", ""),
        "employee": event.get("employee", ""),
        "tone": tone,
        "label": label,
        "detail": plain(event.get("reason", "")),
        "seconds": float(event.get("seconds", 0) or 0),
        "duration_label": (
            format_duration(event["seconds"]) if event.get("seconds") else ""
        ),
        "tokens": int(event.get("tokens", 0) or 0),
        "tokens_label": (
            usage.humanise(int(event["tokens"])) if event.get("tokens") else ""
        ),
        "created": created,
        "created_kind": (
            "corrected copy" if created.lower().startswith("correct_")
            else "checked copy" if created else ""
        ),
    }


def _latest_per_paper(events: list[dict]) -> list[dict]:
    """One entry per paper, not one per phase.

    A paper passes through both phases, so it emits two events. Listing it twice
    reads as two different papers to anyone who does not know the pipeline has
    phases. The later event is the more advanced state, so it wins.
    """
    by_paper: dict[tuple, dict] = {}
    for event in events:
        by_paper[(event.get("employee", ""), event.get("client", ""))] = event
    return list(by_paper.values())


def _outcome_counts(events: list[dict]) -> dict:
    counts = {"done": 0, "attention": 0, "failed": 0, "skipped": 0}
    for event in _latest_per_paper(events):
        tone, _ = _OUTCOME_VIEW.get(event.get("state", ""), ("skipped", ""))
        counts[tone] += 1
    return counts


class _LogCollector(logging.Handler):
    """Mirrors pipeline log records into a bounded buffer for the UI."""

    def __init__(self, sink: deque):
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(
                {"level": record.levelname, "text": self.format(record)}
            )
        except Exception:  # a logging failure must never break a run
            pass


class RunManager:
    """Runs the pipeline on a background thread, one run at a time.

    The UI polls `status()`. Events and log lines accumulate in bounded buffers so
    a long run cannot grow without limit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log: deque = deque(maxlen=MAX_LOG_LINES)
        self._events: list[dict] = []
        self._reset_state()

    def _reset_state(self) -> None:
        self._running = False
        self._started_at = ""
        self._finished_at = ""
        self._summary = ""
        self._error = ""
        self._current = ""
        self._phase = ""
        self._options = {}
        self._done = 0
        self._total = 0
        self._papers_total = 0
        self._tokens = 0
        self._limit_percent = None
        self._started_monotonic = 0.0
        self._finished_monotonic = 0.0
        self._per_paper = {}

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict:
        return {
            "running": self._running,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "phase": self._phase,
            "step_label": PHASE_LABELS.get(self._phase, ""),
            "current": self._current,
            # Papers, not work items: a person counts documents, not phases.
            "done": len(_latest_per_paper(self._events)),
            "total": self._papers_total,
            # The bar still tracks work items, so it advances smoothly through both
            # phases rather than jumping.
            "percent": round(self._done / self._total * 100) if self._total else 0,
            "headline": self._headline(),
            "results": [_result_item(e) for e in _latest_per_paper(self._events)],
            "outcomes": _outcome_counts(self._events),
            "elapsed_seconds": round(self._elapsed(), 1),
            "elapsed_label": format_duration(self._elapsed()) if self._started_at else "",
            "remaining_label": self._remaining_label(),
            "tokens": self._tokens,
            "tokens_label": usage.humanise(self._tokens) if self._tokens else "",
            "limit_used_percent": self._limit_percent,
            "summary": self._summary,
            "error": self._error,
            "options": self._options,
            "events": list(self._events),
            "log": list(self._log),
        }

    def _elapsed(self) -> float:
        """Wall time for the run, frozen once it ends."""
        if not self._started_monotonic:
            return 0.0
        end = self._finished_monotonic or time.monotonic()
        return end - self._started_monotonic

    def _remaining_label(self) -> str:
        """Rough time left, from the measured average and what is still to do."""
        if not self._running or not self._total:
            return ""
        remaining_steps = max(0, self._total - self._done)
        if not remaining_steps:
            return ""
        phase_avg = self._per_paper.get(self._phase or "review")
        if not phase_avg:
            return ""
        return format_duration(remaining_steps * phase_avg)

    def _headline(self) -> str:
        """One sentence describing what is happening, for someone just watching."""
        if self._error:
            return "The run stopped early."
        if self._running:
            if self._current:
                verb = "Writing corrections for" if self._phase == "revise" else "Checking"
                return f"{verb} {self._current.split('/')[-1].strip()}…"
            return "Getting started…"
        if not self._started_at:
            return ""
        counts = _outcome_counts(self._events)
        if counts["failed"]:
            return f"Finished, but {counts['failed']} could not be completed."
        if counts["attention"]:
            return f"Finished. {counts['attention']} need{'s' if counts['attention'] == 1 else ''} a look from you."
        if counts["done"]:
            return f"All done — {counts['done']} finished successfully."
        return "Finished. There was nothing to do."

    def start(self, cfg: Config, storage: StorageBackend, options: dict) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "message": "A run is already in progress."}
            self._log.clear()
            self._events.clear()
            self._reset_state()
            self._running = True
            self._options = dict(options)
            self._started_at = datetime.now(ZoneInfo(cfg.timezone)).strftime(
                "%d-%b-%Y %H:%M:%S"
            )
            self._started_monotonic = time.monotonic()
            self._per_paper = average_seconds(cfg)
            self._thread = threading.Thread(
                target=self._execute, args=(cfg, storage, options), daemon=True
            )
            self._thread.start()
        return {"ok": True, "message": "Run started."}

    def _on_event(self, kind: str, payload: dict) -> None:
        if kind == "phase_start":
            self._phase = payload.get("phase", "")
            clients = int(payload.get("clients", 0))
            self._total += clients
            # Both phases see the same papers, so the paper count is the larger of
            # the two, never their sum.
            self._papers_total = max(self._papers_total, clients)
        elif kind == "client_start":
            self._current = f"{payload.get('employee')} / {payload.get('client')}"
        elif kind == "client_done":
            self._done += 1
            self._current = ""
            self._tokens += int(payload.get("tokens", 0) or 0)
            if payload.get("limit_percent") is not None:
                self._limit_percent = float(payload["limit_percent"])
            self._events.append({"kind": kind, **payload})
        elif kind == "finished":
            self._summary = payload.get("summary", "")

    def _execute(self, cfg: Config, storage: StorageBackend, options: dict) -> None:
        handler = _LogCollector(self._log)
        root_logger = logging.getLogger()
        # The root logger defaults to WARNING, which drops every INFO record before
        # it reaches a handler — leaving the UI's live log pane empty. Lower it for
        # the duration of the run and put it back afterwards.
        previous_level = root_logger.level
        if previous_level > logging.INFO or previous_level == logging.NOTSET:
            root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        try:
            month = options.get("month") or scanner.current_month(cfg)
            month_dir = scanner.month_folder(cfg, storage, month)
            if month_dir is None:
                self._error = f"Month folder not found: {month}"
                log.warning(self._error)
                return

            states = scanner.scan_month(cfg, storage, month_dir)
            phases.run(
                cfg, storage, month, month_dir, states,
                options.get("phase", "both"), self._on_event,
            )
        except Exception as exc:  # a UI must never be left with a silent dead thread
            self._error = f"{type(exc).__name__}: {exc}"
            log.exception("Run failed")
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_level)
            self._finished_at = datetime.now(ZoneInfo(cfg.timezone)).strftime(
                "%d-%b-%Y %H:%M:%S"
            )
            self._finished_monotonic = time.monotonic()
            self._current = ""
            self._running = False


def dry_run_preview(
    cfg: Config,
    storage: StorageBackend,
    month: str | None = None,
    employee_filter: str | None = None,
) -> dict:
    """What a run would do, without touching anything."""
    data = scan(cfg, storage, month, employee_filter)
    would_review = [r for r in data["rows"] if r["status"] == "READY_REVIEW"]
    would_revise = [r for r in data["rows"] if r["status"] == "READY_REVISE"]
    per_phase = average_seconds(cfg)
    # A paper waiting to be checked also gets corrected in the same run, so it
    # costs both phases; one already checked costs only the correction.
    seconds = (
        len(would_review) * (per_phase["review"] + per_phase["revise"])
        + len(would_revise) * per_phase["revise"]
    )
    return {
        **data,
        "would_review": would_review,
        "would_revise": would_revise,
        "estimate_seconds": round(seconds),
        "estimate_label": format_duration(seconds),
        "estimate_measured": per_phase["measured"],
        "estimate_samples": per_phase["samples"],
        "estimate_minutes": round(seconds / 60, 1),
    }


def load_config(path: Path | None = None) -> Config:
    return config_module.load(path)
