"""SQLite processing state and append-only audit trail (spec sections 37 and 40).

The filesystem remains the source of truth for what exists; this database records
*how* each folder got there, plus attempt counts and error reasons. When the two
disagree the filesystem wins and the row is corrected, which is what makes a
crashed run safe to repeat (spec section 23).
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .fingerprint import Fingerprint
from .models import FolderState, Phase, Priority, ProcessingState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_state (
    month     TEXT NOT NULL,
    employee  TEXT NOT NULL,
    client    TEXT NOT NULL,
    state     TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    attempts  INTEGER NOT NULL DEFAULT 0,
    updated   TEXT NOT NULL,
    PRIMARY KEY (month, employee, client)
);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    month     TEXT NOT NULL,
    employee  TEXT NOT NULL,
    client    TEXT NOT NULL,
    phase     TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',
    state     TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    message   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS audit_lookup ON audit (month, employee, client);

-- Every distinct content hash seen for a role (currently only "original" is
-- recorded), in the order first observed. Lets a later run notice the writer
-- edited the source after it was captured for processing (spec section 14),
-- without needing a full job/queue system to do it.
CREATE TABLE IF NOT EXISTS paper_version (
    month          TEXT NOT NULL,
    employee       TEXT NOT NULL,
    client         TEXT NOT NULL,
    role           TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    file_size      INTEGER NOT NULL,
    modified_time  REAL NOT NULL,
    version_number INTEGER NOT NULL,
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (month, employee, client, role, sha256)
);

CREATE INDEX IF NOT EXISTS paper_version_lookup
    ON paper_version (month, employee, client, role);

-- A persistent, inspectable record of each processing attempt (spec sections
-- 17-18), layered alongside client_state/audit rather than replacing them —
-- the folder-eligibility scan on disk remains the actual gate for what runs;
-- this table exists for visibility, crash recovery, and cancellation of work
-- that has not started yet. Sequential execution today (one job at a time);
-- priority is recorded now so a future worker pool has something to sort on.
CREATE TABLE IF NOT EXISTS job (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL UNIQUE,
    month       TEXT NOT NULL,
    employee    TEXT NOT NULL,
    client      TEXT NOT NULL,
    phase       TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'NORMAL',
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS job_lookup ON job (month, employee, client, phase);
CREATE INDEX IF NOT EXISTS job_status ON job (status);

-- Web UI logins (spec section 40). No self-registration: rows are created by
-- an admin via manage_users.py. Never deleted, only disabled, so a removed
-- login still shows up in an audit of who had access and when — same spirit
-- as "disable employee" rather than delete elsewhere in the spec.
CREATE TABLE IF NOT EXISTS user (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    employee      TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0
);

-- Admin-edited prompt bodies, every version retained (spec section 25).
-- Keyed by (phase, task_mode): the review phase is always Codex and the
-- revision phase always Claude, so the phase already identifies the provider,
-- and a row cannot describe an impossible pairing. Nothing is ever updated in
-- place — editing writes a new version, so an earlier one can always be
-- restored and the audit trail stays honest about what a given run used.
CREATE TABLE IF NOT EXISTS prompt (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phase      TEXT NOT NULL,
    task_mode  TEXT NOT NULL,
    body       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    active     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS prompt_lookup ON prompt (phase, task_mode);
"""

_IN_PROGRESS_STATUS = {
    Phase.REVIEW: ProcessingState.REVIEW_IN_PROGRESS,
    Phase.REVISE: ProcessingState.REVISION_IN_PROGRESS,
}
_IN_PROGRESS_VALUES = tuple(s.value for s in _IN_PROGRESS_STATUS.values())

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so each is applied only when absent, which keeps an existing database usable
# instead of forcing it to be deleted.
_ADDED_COLUMNS = (
    ("audit", "tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("audit", "seconds", "REAL NOT NULL DEFAULT 0"),
    # Recorded so an estimate is drawn from comparable runs: a grammar pass and
    # a full review differ by an order of magnitude in both time and tokens.
    ("audit", "task_mode", "TEXT NOT NULL DEFAULT ''"),
    # Self-service sign-up + forced-first-password-change (spec section 40).
    # Defaulting "approved" to 1 and "must_change_password" to 0 means every
    # account that already existed before this migration keeps working
    # exactly as it did — nobody currently able to log in gets locked out.
    ("user", "approved", "INTEGER NOT NULL DEFAULT 1"),
    ("user", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("user", "claim_requested_at", "TEXT NOT NULL DEFAULT ''"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # phases.py serializes every call through its own lock even when a phase
        # runs multiple client folders concurrently (max_concurrent_jobs > 1), so
        # a single connection used from whichever thread currently holds that
        # lock is safe; check_same_thread=False just lifts sqlite3's own (more
        # conservative) same-thread restriction to allow that.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with closing(self._conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
            for table, column, spec in _ADDED_COLUMNS:
                existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get(self, month: str, employee: str, client: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM client_state WHERE month=? AND employee=? AND client=?",
            (month, employee, client),
        )
        return cur.fetchone()

    def set_state(
        self,
        month: str,
        employee: str,
        client: str,
        state: ProcessingState,
        reason: str = "",
        bump_attempts: bool = False,
    ) -> None:
        existing = self.get(month, employee, client)
        attempts = (existing["attempts"] if existing else 0) + (1 if bump_attempts else 0)
        self._conn.execute(
            """
            INSERT INTO client_state (month, employee, client, state, reason, attempts, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (month, employee, client) DO UPDATE SET
                state=excluded.state,
                reason=excluded.reason,
                attempts=excluded.attempts,
                updated=excluded.updated
            """,
            (month, employee, client, state.value, reason, attempts, _now()),
        )
        self._conn.commit()

    def record(
        self,
        month: str,
        folder: FolderState,
        state: ProcessingState,
        phase: Phase | None = None,
        model: str = "",
        file_path: Path | None = None,
        message: str = "",
        tokens: int = 0,
        seconds: float = 0.0,
        task_mode: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO audit
                (timestamp, month, employee, client, phase, model, state, file_path,
                 message, tokens, seconds, task_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now(),
                month,
                folder.employee,
                folder.client,
                phase.value if phase else "",
                model,
                state.value,
                str(file_path) if file_path else "",
                message,
                int(tokens or 0),
                float(seconds or 0.0),
                task_mode,
            ),
        )
        self._conn.commit()

    def record_version(
        self, month: str, employee: str, client: str, role: str, fp: Fingerprint
    ) -> int:
        """Record a content hash if new; return its version number either way.

        Reprocessing the same content is idempotent (the primary key is the hash
        itself), so calling this every run never inflates the version count.
        """
        row = self._conn.execute(
            """
            SELECT version_number FROM paper_version
            WHERE month=? AND employee=? AND client=? AND role=? AND sha256=?
            """,
            (month, employee, client, role, fp.sha256),
        ).fetchone()
        if row:
            return row["version_number"]

        existing_max = self._conn.execute(
            """
            SELECT MAX(version_number) AS m FROM paper_version
            WHERE month=? AND employee=? AND client=? AND role=?
            """,
            (month, employee, client, role),
        ).fetchone()["m"]
        version_number = (existing_max or 0) + 1
        self._conn.execute(
            """
            INSERT INTO paper_version
                (month, employee, client, role, sha256, file_size, modified_time,
                 version_number, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                month, employee, client, role, fp.sha256, fp.size,
                fp.modified_time, version_number, _now(),
            ),
        )
        self._conn.commit()
        return version_number

    def latest_version(
        self, month: str, employee: str, client: str, role: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM paper_version
            WHERE month=? AND employee=? AND client=? AND role=?
            ORDER BY version_number DESC LIMIT 1
            """,
            (month, employee, client, role),
        ).fetchone()

    def enqueue_job(
        self,
        month: str,
        employee: str,
        client: str,
        phase: Phase,
        priority: Priority = Priority.NORMAL,
    ) -> str:
        """Create a job, or return the id of one already queued/in progress.

        Idempotent per (month, employee, client, phase) so a rescan before this
        job has been picked up never produces a duplicate entry in the queue.
        """
        existing = self._conn.execute(
            """
            SELECT job_id FROM job
            WHERE month=? AND employee=? AND client=? AND phase=?
                  AND status IN (?, ?)
            ORDER BY seq DESC LIMIT 1
            """,
            (
                month, employee, client, phase.value,
                ProcessingState.QUEUED.value, _IN_PROGRESS_STATUS[phase].value,
            ),
        ).fetchone()
        if existing:
            return existing["job_id"]

        next_seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM job"
        ).fetchone()["n"]
        job_id = f"JOB-{datetime.now(timezone.utc):%Y%m%d}-{next_seq:06d}"
        now = _now()
        self._conn.execute(
            """
            INSERT INTO job
                (seq, job_id, month, employee, client, phase, priority, status,
                 attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                next_seq, job_id, month, employee, client, phase.value,
                priority.value, ProcessingState.QUEUED.value, now, now,
            ),
        )
        self._conn.commit()
        return job_id

    def start_job(self, job_id: str) -> None:
        row = self.get_job(job_id)
        if row is None:
            raise KeyError(f"No such job: {job_id}")
        status = _IN_PROGRESS_STATUS[Phase(row["phase"])]
        now = _now()
        self._conn.execute(
            """
            UPDATE job SET status=?, attempts=attempts+1, started_at=?, updated_at=?
            WHERE job_id=?
            """,
            (status.value, now, now, job_id),
        )
        self._conn.commit()

    def finish_job(self, job_id: str, status: ProcessingState, reason: str = "") -> None:
        now = _now()
        self._conn.execute(
            "UPDATE job SET status=?, reason=?, finished_at=?, updated_at=? WHERE job_id=?",
            (status.value, reason, now, now, job_id),
        )
        self._conn.commit()

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job that has not started yet. Running jobs are left alone
        (spec section 11: never kill work already in progress)."""
        row = self.get_job(job_id)
        if row is None or row["status"] != ProcessingState.QUEUED.value:
            return False
        self._conn.execute(
            "UPDATE job SET status=?, updated_at=? WHERE job_id=?",
            (ProcessingState.CANCELLED.value, _now(), job_id),
        )
        self._conn.commit()
        return True

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM job WHERE job_id=?", (job_id,)
        ).fetchone()

    def list_jobs(
        self,
        status: ProcessingState | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[sqlite3.Row]:
        order = "seq DESC" if newest_first else "seq"
        sql = "SELECT * FROM job"
        params: list = []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status.value)
        sql += f" ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._conn.execute(sql, tuple(params)).fetchall()

    def job_status_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM job GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def queued_jobs(self) -> list[sqlite3.Row]:
        """Queued jobs in the order a worker should take them: HIGH first, then FIFO."""
        return self._conn.execute(
            """
            SELECT * FROM job WHERE status=?
            ORDER BY CASE priority WHEN 'HIGH' THEN 0 ELSE 1 END, seq
            """,
            (ProcessingState.QUEUED.value,),
        ).fetchall()

    def requeue_stale_processing_jobs(self) -> int:
        """Recover jobs left mid-flight by a process that crashed or was killed.

        Disk remains the actual source of truth for whether work is still needed
        (scan_month + decide() re-derive that fresh every run), so this only
        restores queue visibility — it never marks anything COMPLETED itself.
        """
        rows = self._conn.execute(
            f"SELECT job_id FROM job WHERE status IN ({','.join('?' * len(_IN_PROGRESS_VALUES))})",
            _IN_PROGRESS_VALUES,
        ).fetchall()
        now = _now()
        for row in rows:
            self._conn.execute(
                "UPDATE job SET status=?, reason=?, updated_at=? WHERE job_id=?",
                (
                    ProcessingState.QUEUED.value,
                    "Requeued after an interrupted run.",
                    now, row["job_id"],
                ),
            )
        self._conn.commit()
        return len(rows)

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        employee: str = "",
        approved: bool = True,
        must_change_password: bool = False,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO user
                (username, password_hash, role, employee, created_at,
                 approved, must_change_password)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username, password_hash, role, employee, _now(),
                1 if approved else 0, 1 if must_change_password else 0,
            ),
        )
        self._conn.commit()

    def get_user(self, username: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM user WHERE username=?", (username,)
        ).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM user ORDER BY username").fetchall()

    def set_password(self, username: str, password_hash: str) -> None:
        self._conn.execute(
            "UPDATE user SET password_hash=? WHERE username=?",
            (password_hash, username),
        )
        self._conn.commit()

    def set_user_disabled(self, username: str, disabled: bool) -> None:
        self._conn.execute(
            "UPDATE user SET disabled=? WHERE username=?",
            (1 if disabled else 0, username),
        )
        self._conn.commit()

    def set_user_approved(self, username: str, approved: bool) -> None:
        self._conn.execute(
            "UPDATE user SET approved=? WHERE username=?",
            (1 if approved else 0, username),
        )
        self._conn.commit()

    def set_must_change_password(self, username: str, value: bool) -> None:
        self._conn.execute(
            "UPDATE user SET must_change_password=? WHERE username=?",
            (1 if value else 0, username),
        )
        self._conn.commit()

    def record_claim_request(self, username: str) -> None:
        self._conn.execute(
            "UPDATE user SET claim_requested_at=? WHERE username=?",
            (_now(), username),
        )
        self._conn.commit()

    def rename_user(self, old_username: str, new_username: str) -> None:
        self._conn.execute(
            "UPDATE user SET username=? WHERE username=?",
            (new_username, old_username),
        )
        self._conn.commit()

    def count_active_admins(self) -> int:
        """Approved, non-disabled ADMIN accounts — used to stop the last one
        from being deleted and locking everyone out of /users."""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM user WHERE role='ADMIN' AND approved=1 AND disabled=0"
        ).fetchone()["n"]

    def delete_user(self, username: str) -> None:
        self._conn.execute("DELETE FROM user WHERE username=?", (username,))
        self._conn.commit()

    def save_prompt(
        self, phase: Phase, task_mode: str, body: str, created_by: str = ""
    ) -> int:
        """Store a new prompt version and make it the active one.

        Returns the version number. Previous versions are kept, only
        deactivated, so any of them can be restored later.
        """
        highest = self._conn.execute(
            "SELECT MAX(version) AS m FROM prompt WHERE phase=? AND task_mode=?",
            (phase.value, task_mode),
        ).fetchone()["m"]
        version = (highest or 0) + 1
        self._conn.execute(
            "UPDATE prompt SET active=0 WHERE phase=? AND task_mode=?",
            (phase.value, task_mode),
        )
        self._conn.execute(
            """
            INSERT INTO prompt
                (phase, task_mode, body, version, active, created_at, created_by)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (phase.value, task_mode, body, version, _now(), created_by),
        )
        self._conn.commit()
        return version

    def active_prompt(self, phase: Phase, task_mode: str) -> sqlite3.Row | None:
        """The prompt in force, or None when the built-in one is being used."""
        return self._conn.execute(
            """
            SELECT * FROM prompt
            WHERE phase=? AND task_mode=? AND active=1
            ORDER BY version DESC LIMIT 1
            """,
            (phase.value, task_mode),
        ).fetchone()

    def prompt_versions(self, phase: Phase, task_mode: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM prompt WHERE phase=? AND task_mode=?
            ORDER BY version DESC
            """,
            (phase.value, task_mode),
        ).fetchall()

    def activate_prompt_version(self, phase: Phase, task_mode: str, version: int) -> bool:
        """Restore an earlier version. False when that version does not exist."""
        row = self._conn.execute(
            "SELECT id FROM prompt WHERE phase=? AND task_mode=? AND version=?",
            (phase.value, task_mode, version),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute(
            "UPDATE prompt SET active=0 WHERE phase=? AND task_mode=?",
            (phase.value, task_mode),
        )
        self._conn.execute("UPDATE prompt SET active=1 WHERE id=?", (row["id"],))
        self._conn.commit()
        return True

    def clear_active_prompt(self, phase: Phase, task_mode: str) -> None:
        """Fall back to the built-in prompt without discarding any version."""
        self._conn.execute(
            "UPDATE prompt SET active=0 WHERE phase=? AND task_mode=?",
            (phase.value, task_mode),
        )
        self._conn.commit()

    def reconcile(self, month: str, folder: FolderState) -> ProcessingState:
        """Correct the stored state to match what is actually on disk.

        Disk is authoritative, so a database row claiming work is in progress after
        a crash cannot wedge a folder permanently.
        """
        if folder.is_complete:
            derived = ProcessingState.COMPLETED
        elif folder.reviews and folder.originals:
            derived = ProcessingState.PENDING_REVISION
        elif folder.originals:
            derived = ProcessingState.PENDING_REVIEW
        else:
            derived = ProcessingState.SKIPPED

        row = self.get(month, folder.employee, folder.client)
        stored = row["state"] if row else None
        # Terminal states that disk cannot contradict are left alone.
        protected = {
            ProcessingState.VALIDATION_FAILED.value,
            ProcessingState.REQUIRES_HUMAN_REVIEW.value,
        }
        if stored in protected and derived is not ProcessingState.COMPLETED:
            return ProcessingState(stored)
        if stored != derived.value:
            self.set_state(month, folder.employee, folder.client, derived)
        return derived
