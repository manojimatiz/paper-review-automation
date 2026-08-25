"""Configuration loading: TOML file overlaid with environment variables."""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path  # noqa: E402  (imported before the default below)

DEFAULT_CONFIG_NAME = "config.toml"

# Codex's Windows sandbox refuses to grant write capability under AppData (where its
# own state lives), failing with "no writable root capability SIDs". Verified working:
# %USERPROFILE%\... and %USERPROFILE%\Documents\...  Verified failing: anything under
# AppData\Local, including AppData\Local\Temp. Staying out of OneDrive as well keeps
# the sync client from touching files mid-run.
DEFAULT_SCRATCH = Path.home() / "PaperAutomation" / "scratch"


class ConfigError(RuntimeError):
    pass


def _as_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ProviderConfig:
    binary_path: str | None = None
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800


@dataclass
class NotifyConfig:
    enabled: bool = False
    channels: list[str] = field(default_factory=list)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    slack_webhook_url: str = ""
    teams_webhook_url: str = ""


@dataclass
class Config:
    research_papers_root: Path
    timezone: str = "Asia/Kolkata"
    supported_extensions: tuple[str, ...] = (".docx",)
    state_db: Path = Path("state/processing.sqlite3")
    log_dir: Path = Path("logs")
    # Where `py run.py --backup-now` writes timestamped copies of state_db.
    # Defaults next to state_db itself if not set explicitly.
    backup_dir: Path = Path("state/backups")
    scratch_dir: Path = DEFAULT_SCRATCH
    dry_run: bool = False
    test_mode: bool = False
    # "auto" uses the mock provider in test mode and the real CLIs otherwise.
    provider_mode: str = "auto"
    # "grammar" is a cheap language-only pass for building and testing; "full" is
    # the Q1 review and scientific revision. See paper_automation/prompts.py.
    task_mode: str = "grammar"
    max_retries: int = 3
    retry_base_delay: float = 5.0
    # How many client folders a phase processes at once. 1 (the default) is exactly
    # today's sequential behaviour. Raising it runs that many Codex/Claude CLI
    # subprocesses concurrently within a single phase; review and revision phases
    # still never overlap each other (spec section 13 is unaffected).
    max_concurrent_jobs: int = 1
    # Only used by `py run.py --loop` (paper_automation.scheduler). One-shot runs
    # (the default) ignore these entirely.
    schedule_start: time = time(9, 0)
    schedule_end: time = time(18, 0)
    scan_interval_minutes: int = 15
    # SECURITY: stays 127.0.0.1 (localhost-only, no LAN access) unless an admin
    # deliberately opts in. Flipping this to "0.0.0.0" makes the web UI reachable
    # from any PC on the network — do that only once login (paper_automation.auth,
    # manage_users.py) is actually set up, since there is no other access control.
    web_host: str = "127.0.0.1"
    create_missing_month: bool = False
    # "local" covers both a plain local folder and a Google-Drive-for-Desktop
    # synced folder (it's just a local path either way). "gdrive" is reserved
    # for a future direct-API backend; paper_automation.storage.gdrive is a
    # stub today, so selecting it fails fast at load time instead of at
    # storage construction time.
    storage_backend: str = "local"
    codex: ProviderConfig = field(default_factory=ProviderConfig)
    claude: ProviderConfig = field(default_factory=ProviderConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    def review_name(self, client: str) -> str:
        return f"{sanitize(client)}_review.docx"

    def final_name(self, client: str) -> str:
        return f"Correct_{sanitize(client)}_paper.docx"


_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize(name: str) -> str:
    """Make a folder name safe to embed in a filename (spec section 32)."""
    cleaned = "".join("_" if c in _INVALID_FILENAME_CHARS else c for c in name)
    cleaned = "".join(c for c in cleaned if c.isprintable())
    return cleaned.strip().strip(".") or "unnamed"


def is_review_name(name: str) -> bool:
    return name.lower().endswith("_review.docx")


def is_final_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("correct_") and lowered.endswith("_paper.docx")


def _validated_task_mode(value, path) -> str:
    """A typo here would silently run the expensive prompts, so fail loudly."""
    from .prompts import TASK_MODES

    mode = str(value).strip().lower()
    if mode not in TASK_MODES:
        raise ConfigError(
            f"task_mode must be one of {', '.join(TASK_MODES)} (got '{value}') in {path}"
        )
    return mode


_STORAGE_BACKENDS = ("local", "gdrive")


def _validated_storage_backend(value, path) -> str:
    """A typo here would silently fall through to a wrong backend, so fail loudly."""
    backend = str(value).strip().lower()
    if backend not in _STORAGE_BACKENDS:
        raise ConfigError(
            f"storage_backend must be one of {', '.join(_STORAGE_BACKENDS)} "
            f"(got '{value}') in {path}"
        )
    if backend == "gdrive":
        raise ConfigError(
            f"storage_backend = 'gdrive' in {path} is reserved for a future release "
            "(paper_automation.storage.gdrive is not implemented yet). "
            "Use 'local' - it also covers a Google-Drive-for-Desktop synced folder."
        )
    return backend


def _validated_concurrency(value, path) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"max_concurrent_jobs must be a whole number (got {value!r}) in {path}")
    if n < 1:
        raise ConfigError(f"max_concurrent_jobs must be at least 1 (got {n}) in {path}")
    return n


def _validated_time(value, key: str, path) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be in HH:MM format (got {value!r}) in {path}")


def _validated_scan_interval(value, path) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"scan_interval_minutes must be a whole number (got {value!r}) in {path}"
        )
    if n < 1:
        raise ConfigError(f"scan_interval_minutes must be at least 1 (got {n}) in {path}")
    return n


def _provider(raw: dict) -> ProviderConfig:
    return ProviderConfig(
        binary_path=raw.get("binary_path") or None,
        model=raw.get("model") or None,
        extra_args=list(raw.get("extra_args", [])),
        timeout_seconds=int(raw.get("timeout_seconds", 1800)),
    )


def _notify(raw: dict) -> NotifyConfig:
    return NotifyConfig(
        enabled=bool(raw.get("enabled", False)),
        channels=list(raw.get("channels", [])),
        smtp_host=raw.get("smtp_host", ""),
        smtp_port=int(raw.get("smtp_port", 587)),
        smtp_user=raw.get("smtp_user", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", raw.get("smtp_password", "")),
        email_from=raw.get("email_from", ""),
        email_to=list(raw.get("email_to", [])),
        telegram_bot_token=os.environ.get(
            "TELEGRAM_BOT_TOKEN", raw.get("telegram_bot_token", "")
        ),
        telegram_chat_id=raw.get("telegram_chat_id", ""),
        slack_webhook_url=os.environ.get(
            "SLACK_WEBHOOK_URL", raw.get("slack_webhook_url", "")
        ),
        teams_webhook_url=os.environ.get(
            "TEAMS_WEBHOOK_URL", raw.get("teams_webhook_url", "")
        ),
    )


def _default_base_dir() -> Path:
    """Where to look for config.toml when the caller didn't say.

    A frozen (PyInstaller) build has no `.py` files on disk to resolve
    against — `__file__` would point into the temporary extraction
    directory. The installer keeps config.toml, state/, and logs/ under the
    user's home folder instead (see packaging/installer.iss), matching
    DEFAULT_SCRATCH's existing home-folder placement above.
    """
    if getattr(sys, "frozen", False):
        return Path.home() / "PaperReviewAutomation"
    return Path(__file__).resolve().parent.parent


def load(path: Path | None = None, base_dir: Path | None = None) -> Config:
    """Load configuration from TOML, then apply environment overrides.

    Relative paths in the config are resolved against the config file's directory
    so a scheduled run does not depend on the working directory it inherits.
    """
    base_dir = base_dir or _default_base_dir()
    path = path or base_dir / DEFAULT_CONFIG_NAME
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Copy config.example.toml to {path.name} and set research_papers_root."
        )
    # utf-8-sig, not utf-8: Notepad and PowerShell's Out-File both write a BOM,
    # which tomllib reports only as "Invalid statement at line 1, column 1".
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    root = raw.get("research_papers_root", "").strip()
    if not root:
        raise ConfigError(
            f"'research_papers_root' is not set in {path}. "
            "Point it at the folder that contains the '<Month Year>' directories."
        )

    def resolve(value: str, default: str) -> Path:
        p = Path(value or default).expanduser()
        return p if p.is_absolute() else (base_dir / p)

    exts = raw.get("supported_extensions", [".docx"])
    cfg = Config(
        research_papers_root=Path(root).expanduser(),
        timezone=raw.get("timezone", "Asia/Kolkata"),
        supported_extensions=tuple(e.lower() for e in exts),
        state_db=resolve(raw.get("state_db", ""), "state/processing.sqlite3"),
        log_dir=resolve(raw.get("log_dir", ""), "logs"),
        backup_dir=resolve(raw.get("backup_dir", ""), "state/backups"),
        scratch_dir=(
            resolve(raw["scratch_dir"], "") if raw.get("scratch_dir") else DEFAULT_SCRATCH
        ),
        dry_run=bool(raw.get("dry_run", False)),
        test_mode=bool(raw.get("test_mode", False)),
        task_mode=_validated_task_mode(raw.get("task_mode", "grammar"), path),
        max_retries=int(raw.get("max_retries", 3)),
        retry_base_delay=float(raw.get("retry_base_delay", 5.0)),
        max_concurrent_jobs=_validated_concurrency(
            raw.get("max_concurrent_jobs", 1), path
        ),
        schedule_start=_validated_time(raw.get("schedule_start", "09:00"), "schedule_start", path),
        schedule_end=_validated_time(raw.get("schedule_end", "18:00"), "schedule_end", path),
        scan_interval_minutes=_validated_scan_interval(
            raw.get("scan_interval_minutes", 15), path
        ),
        web_host=raw.get("web_host", "127.0.0.1").strip() or "127.0.0.1",
        create_missing_month=bool(raw.get("create_missing_month", False)),
        storage_backend=_validated_storage_backend(
            raw.get("storage_backend", "local"), path
        ),
        codex=_provider(raw.get("providers", {}).get("codex", {})),
        claude=_provider(raw.get("providers", {}).get("claude", {})),
        notify=_notify(raw.get("notify", {})),
    )
    if cfg.schedule_start >= cfg.schedule_end:
        raise ConfigError(
            f"schedule_start ({cfg.schedule_start:%H:%M}) must be before "
            f"schedule_end ({cfg.schedule_end:%H:%M}) in {path}"
        )

    if "DRY_RUN" in os.environ:
        cfg.dry_run = _as_bool(os.environ["DRY_RUN"])
    if "TEST_MODE" in os.environ:
        cfg.test_mode = _as_bool(os.environ["TEST_MODE"])
    if os.environ.get("RESEARCH_PAPERS_ROOT"):
        cfg.research_papers_root = Path(os.environ["RESEARCH_PAPERS_ROOT"]).expanduser()

    return cfg
