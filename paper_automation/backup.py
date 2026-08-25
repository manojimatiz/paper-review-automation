"""Backups of the SQLite state database (spec section 44).

The state database holds more than processing history now: job records, saved
prompt versions, and web-UI accounts all live in the same file, so it is worth
protecting on its own rather than trusting only whatever syncs the papers
themselves.

Only ever CREATES a new timestamped copy — this module never deletes an old
backup. That is a deliberate consequence of the project's no-delete invariant
(tests/test_safety.py enforces it at the AST level across all of
paper_automation/), not an oversight: pruning old backups automatically would
need a delete call this package is not allowed to make. Clearing old backups
by hand is the same trade-off phases.py already makes for old scratch
directories (see `_scratch_for`'s docstring).
"""

import shutil
from datetime import datetime, timezone
from pathlib import Path


def backup_state_db(state_db: Path, backup_dir: Path) -> Path | None:
    """Copy state_db to a timestamped file in backup_dir.

    Returns the new path, or None when there is nothing to back up yet (a
    fresh install with no database is not an error).
    """
    state_db = Path(state_db)
    if not state_db.exists():
        return None

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{state_db.stem}-{stamp}{state_db.suffix}"
    shutil.copy2(state_db, target)
    return target


def latest_backup(backup_dir: Path) -> Path | None:
    """The most recent backup, or None if none exist yet."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return None
    backups = sorted(backup_dir.glob("*.sqlite3"))
    return backups[-1] if backups else None
