"""State store: disk is authoritative, so a crashed run cannot wedge a folder."""

from paper_automation.models import FolderState, Phase, ProcessingState
from paper_automation.state import StateStore

MONTH = "August 2026"


def folder(tmp_path, *, originals=0, reviews=0, finals=0) -> FolderState:
    state = FolderState(employee="Manoj Paper", client="Vani", folder=tmp_path)
    state.originals = [tmp_path / f"o{i}.docx" for i in range(originals)]
    state.reviews = [tmp_path / f"r{i}_review.docx" for i in range(reviews)]
    state.finals = [tmp_path / f"Correct_f{i}_paper.docx" for i in range(finals)]
    return state


def test_state_round_trips(cfg, tmp_path):
    with StateStore(cfg.state_db) as store:
        store.set_state(MONTH, "Manoj Paper", "Vani", ProcessingState.REVIEW_COMPLETED)
        row = store.get(MONTH, "Manoj Paper", "Vani")
    assert row["state"] == "REVIEW_COMPLETED"


def test_attempts_increment_only_when_asked(cfg):
    with StateStore(cfg.state_db) as store:
        store.set_state(MONTH, "E", "C", ProcessingState.FAILED, bump_attempts=True)
        store.set_state(MONTH, "E", "C", ProcessingState.FAILED, bump_attempts=True)
        store.set_state(MONTH, "E", "C", ProcessingState.FAILED)
        assert store.get(MONTH, "E", "C")["attempts"] == 2


def test_reconcile_derives_state_from_disk(cfg, tmp_path):
    with StateStore(cfg.state_db) as store:
        assert (
            store.reconcile(MONTH, folder(tmp_path, originals=1))
            is ProcessingState.PENDING_REVIEW
        )
        assert (
            store.reconcile(MONTH, folder(tmp_path, originals=1, reviews=1))
            is ProcessingState.PENDING_REVISION
        )
        assert (
            store.reconcile(MONTH, folder(tmp_path, originals=1, reviews=1, finals=1))
            is ProcessingState.COMPLETED
        )


def test_reconcile_clears_a_stale_in_progress_row(cfg, tmp_path):
    """A crash mid-review leaves REVIEW_IN_PROGRESS; disk says otherwise."""
    with StateStore(cfg.state_db) as store:
        store.set_state(MONTH, "Manoj Paper", "Vani", ProcessingState.REVIEW_IN_PROGRESS)
        derived = store.reconcile(MONTH, folder(tmp_path, originals=1))
        assert derived is ProcessingState.PENDING_REVIEW
        assert store.get(MONTH, "Manoj Paper", "Vani")["state"] == "PENDING_REVIEW"


def test_reconcile_preserves_human_review_flag(cfg, tmp_path):
    """A validation failure must not be silently cleared by a later scan."""
    with StateStore(cfg.state_db) as store:
        store.set_state(
            MONTH, "Manoj Paper", "Vani", ProcessingState.REQUIRES_HUMAN_REVIEW
        )
        derived = store.reconcile(MONTH, folder(tmp_path, originals=1, reviews=1))
        assert derived is ProcessingState.REQUIRES_HUMAN_REVIEW


def test_audit_rows_are_appended(cfg, tmp_path):
    with StateStore(cfg.state_db) as store:
        target = folder(tmp_path, originals=1)
        store.record(
            MONTH,
            target,
            ProcessingState.REVIEW_COMPLETED,
            phase=Phase.REVIEW,
            model="codex",
            file_path=tmp_path / "Vani_review.docx",
            message="ok",
        )
        store.record(MONTH, target, ProcessingState.FAILED, phase=Phase.REVISE)
        rows = store._conn.execute(
            "SELECT phase, model, state FROM audit ORDER BY id"
        ).fetchall()

    assert [r["state"] for r in rows] == ["REVIEW_COMPLETED", "FAILED"]
    assert rows[0]["model"] == "codex"


def test_an_existing_database_gains_the_tokens_column(tmp_path):
    """Adding a column must not force users to delete their audit history."""
    import sqlite3

    db = tmp_path / "processing.sqlite3"
    # A database created before tokens existed.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            month TEXT NOT NULL, employee TEXT NOT NULL, client TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, file_path TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO audit (timestamp, month, employee, client, state)
        VALUES ('2026-08-01', 'August 2026', 'E', 'C', 'COMPLETED');
        """
    )
    conn.commit()
    conn.close()

    from paper_automation.state import StateStore

    with StateStore(db) as store:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(audit)")}
        rows = store._conn.execute("SELECT client, tokens FROM audit").fetchall()

    assert "tokens" in columns
    assert rows[0]["client"] == "C"   # the old row survived
    assert rows[0]["tokens"] == 0


def test_an_existing_user_gains_the_approval_columns_already_approved(tmp_path):
    """A pre-existing account (created before this migration) must keep
    working exactly as it did — nobody currently able to log in should be
    locked out by the approved/must_change_password columns arriving."""
    import sqlite3

    db = tmp_path / "processing.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE user (
            username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
            role TEXT NOT NULL, employee TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO user (username, password_hash, role, created_at)
        VALUES ('alice', 'somehash', 'ADMIN', '2026-08-01');
        """
    )
    conn.commit()
    conn.close()

    from paper_automation.state import StateStore

    with StateStore(db) as store:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(user)")}
        row = store.get_user("alice")

    assert {"approved", "must_change_password", "claim_requested_at"} <= columns
    assert row["approved"] == 1
    assert row["must_change_password"] == 0
    assert row["claim_requested_at"] == ""


def test_record_version_assigns_version_one_to_the_first_hash(cfg):
    from paper_automation.fingerprint import Fingerprint

    fp = Fingerprint(sha256="a" * 64, size=100, modified_time=1.0)
    with StateStore(cfg.state_db) as store:
        assert store.record_version(MONTH, "E", "C", "original", fp) == 1


def test_record_version_is_idempotent_for_the_same_hash(cfg):
    from paper_automation.fingerprint import Fingerprint

    fp = Fingerprint(sha256="a" * 64, size=100, modified_time=1.0)
    with StateStore(cfg.state_db) as store:
        store.record_version(MONTH, "E", "C", "original", fp)
        store.record_version(MONTH, "E", "C", "original", fp)
        rows = store._conn.execute(
            "SELECT COUNT(*) AS n FROM paper_version WHERE month=? AND employee=? AND client=?",
            (MONTH, "E", "C"),
        ).fetchone()
    assert rows["n"] == 1


def test_record_version_bumps_on_a_new_hash(cfg):
    from paper_automation.fingerprint import Fingerprint

    fp1 = Fingerprint(sha256="a" * 64, size=100, modified_time=1.0)
    fp2 = Fingerprint(sha256="b" * 64, size=200, modified_time=2.0)
    with StateStore(cfg.state_db) as store:
        assert store.record_version(MONTH, "E", "C", "original", fp1) == 1
        assert store.record_version(MONTH, "E", "C", "original", fp2) == 2


def test_latest_version_returns_the_highest_version_number(cfg):
    from paper_automation.fingerprint import Fingerprint

    fp1 = Fingerprint(sha256="a" * 64, size=100, modified_time=1.0)
    fp2 = Fingerprint(sha256="b" * 64, size=200, modified_time=2.0)
    with StateStore(cfg.state_db) as store:
        store.record_version(MONTH, "E", "C", "original", fp1)
        store.record_version(MONTH, "E", "C", "original", fp2)
        latest = store.latest_version(MONTH, "E", "C", "original")
    assert latest["sha256"] == "b" * 64
    assert latest["version_number"] == 2


def test_latest_version_is_none_when_nothing_recorded(cfg):
    with StateStore(cfg.state_db) as store:
        assert store.latest_version(MONTH, "E", "C", "original") is None


def test_enqueue_job_creates_a_queued_row(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        row = store.get_job(job_id)
    assert row["status"] == "QUEUED"
    assert row["month"] == MONTH and row["employee"] == "E" and row["client"] == "C"
    assert row["phase"] == "review"
    assert row["priority"] == "NORMAL"
    assert job_id.startswith("JOB-")


def test_enqueue_job_is_idempotent_while_queued(cfg):
    with StateStore(cfg.state_db) as store:
        first = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        second = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
    assert first == second


def test_enqueue_job_is_idempotent_while_in_progress(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        store.start_job(job_id)
        again = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
    assert again == job_id


def test_enqueue_job_after_finish_creates_a_new_job(cfg):
    with StateStore(cfg.state_db) as store:
        first = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        store.start_job(first)
        store.finish_job(first, ProcessingState.REVIEW_COMPLETED)
        second = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
    assert second != first


def test_start_job_sets_in_progress_status_and_bumps_attempts(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVISE)
        store.start_job(job_id)
        row = store.get_job(job_id)
    assert row["status"] == "REVISION_IN_PROGRESS"
    assert row["attempts"] == 1
    assert row["started_at"] != ""


def test_finish_job_records_status_and_reason(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        store.start_job(job_id)
        store.finish_job(job_id, ProcessingState.FAILED, "provider timed out")
        row = store.get_job(job_id)
    assert row["status"] == "FAILED"
    assert row["reason"] == "provider timed out"
    assert row["finished_at"] != ""


def test_cancel_job_only_succeeds_while_queued(cfg):
    with StateStore(cfg.state_db) as store:
        queued = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        assert store.cancel_job(queued) is True
        assert store.get_job(queued)["status"] == "CANCELLED"

        running = store.enqueue_job(MONTH, "E", "D", Phase.REVIEW)
        store.start_job(running)
        assert store.cancel_job(running) is False
        assert store.get_job(running)["status"] == "REVIEW_IN_PROGRESS"


def test_queued_jobs_orders_high_priority_first(cfg):
    from paper_automation.models import Priority

    with StateStore(cfg.state_db) as store:
        low = store.enqueue_job(MONTH, "E", "Low", Phase.REVIEW, Priority.NORMAL)
        high = store.enqueue_job(MONTH, "E", "High", Phase.REVIEW, Priority.HIGH)
        ids = [row["job_id"] for row in store.queued_jobs()]
    assert ids == [high, low]


def test_requeue_stale_processing_jobs_resets_them_to_queued(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        store.start_job(job_id)
        count = store.requeue_stale_processing_jobs()
        row = store.get_job(job_id)
    assert count == 1
    assert row["status"] == "QUEUED"
    assert "interrupted" in row["reason"]


def test_requeue_stale_processing_jobs_leaves_finished_jobs_alone(cfg):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, "E", "C", Phase.REVIEW)
        store.start_job(job_id)
        store.finish_job(job_id, ProcessingState.REVIEW_COMPLETED)
        count = store.requeue_stale_processing_jobs()
        row = store.get_job(job_id)
    assert count == 0
    assert row["status"] == "REVIEW_COMPLETED"


def test_tokens_are_recorded_against_a_client(tmp_path):
    from paper_automation.models import FolderState, Phase, ProcessingState
    from paper_automation.state import StateStore

    folder = FolderState(employee="E", client="Vani", folder=tmp_path)
    with StateStore(tmp_path / "s.sqlite3") as store:
        store.record("August 2026", folder, ProcessingState.COMPLETED,
                     phase=Phase.REVIEW, tokens=50585)
        row = store._conn.execute("SELECT tokens FROM audit").fetchone()

    assert row["tokens"] == 50585
