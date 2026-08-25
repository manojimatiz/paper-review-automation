"""Month detection, folder traversal, and file classification."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, is_final_name, is_review_name
from .fingerprint import Fingerprint
from .models import FolderState
from .storage.base import StorageBackend


def current_month(cfg: Config, now: datetime | None = None) -> str:
    """The month folder name for today, in the configured timezone.

    Derived every run, never hard-coded (spec sections 3 and 29).
    """
    now = now or datetime.now(ZoneInfo(cfg.timezone))
    return now.strftime("%B %Y")


def month_folder(cfg: Config, storage: StorageBackend, month: str) -> Path | None:
    return storage.find_folder(cfg.research_papers_root / month)


def is_ignorable(path: Path) -> bool:
    """Files that are never research papers regardless of extension.

    Office writes a `~$name.docx` lock file whenever a document is open; counting
    one would break the exactly-one-file rule for a folder the user is editing
    (spec section 47).
    """
    name = path.name
    return name.startswith("~$") or name.startswith(".") or name.lower() == "thumbs.db"


def classify_folder(
    cfg: Config, storage: StorageBackend, employee: str, client: str, folder: Path
) -> FolderState:
    """Group a client folder's files by role.

    Only immediate files with a supported extension are counted. Subdirectories,
    hidden/system files and Office lock files are recorded as ignored so the log
    can explain a skip, but they never affect eligibility.
    """
    state = FolderState(employee=employee, client=client, folder=folder)
    for path in storage.list_files(folder):
        if is_ignorable(path) or path.suffix.lower() not in cfg.supported_extensions:
            state.ignored.append(path)
        elif is_final_name(path.name):
            state.finals.append(path)
        elif is_review_name(path.name):
            state.reviews.append(path)
        else:
            state.originals.append(path)
    if len(state.originals) > 1:
        state.duplicate_originals = _has_duplicate_content(state.originals)
    return state


def _has_duplicate_content(paths: list[Path]) -> bool:
    """Best-effort: unreadable files just mean "can't tell", not "duplicate"."""
    hashes = []
    for path in paths:
        try:
            hashes.append(Fingerprint.of(path).sha256)
        except OSError:
            return False
    return len(set(hashes)) < len(hashes)


def scan_month(
    cfg: Config, storage: StorageBackend, month_dir: Path
) -> list[FolderState]:
    """Every client folder under a month, in deterministic alphabetical order.

    Employee folders are all immediate subdirectories of the month (spec section 4),
    and client folders are all immediate subdirectories of an employee.
    """
    states: list[FolderState] = []
    employees = storage.list_folders(month_dir)
    if cfg.test_mode:
        employees = employees[:1]
    for employee_dir in employees:
        clients = storage.list_folders(employee_dir)
        if cfg.test_mode:
            clients = clients[:1]
        for client_dir in clients:
            states.append(
                classify_folder(
                    cfg, storage, employee_dir.name, client_dir.name, client_dir
                )
            )
    return states
