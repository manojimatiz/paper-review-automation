"""File-count and role rules — the gates that protect the user's papers."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from paper_automation import scanner
from paper_automation.models import Decision, Phase


def classify(cfg, storage, folder: Path):
    return scanner.classify_folder(
        cfg, storage, folder.parent.name, folder.name, folder
    )


# --- review phase gate (spec section 6) ---------------------------------------


def test_exactly_one_paper_is_processed(cfg, storage, make_client):
    folder = make_client("Vani.docx")
    state = classify(cfg, storage, folder)
    assert state.decide(Phase.REVIEW).decision is Decision.PROCESS


def test_zero_files_is_skipped(cfg, storage, make_client):
    folder = make_client()
    outcome = classify(cfg, storage, folder).decide(Phase.REVIEW)
    assert outcome.decision is Decision.SKIP
    assert "expected exactly 1" in outcome.reason


def test_two_papers_is_skipped_without_guessing(cfg, storage, make_client):
    folder = make_client("Vani.docx", "another.docx")
    outcome = classify(cfg, storage, folder).decide(Phase.REVIEW)
    assert outcome.decision is Decision.SKIP
    assert "Refusing to guess" in outcome.reason


# --- duplicate content among candidate originals (spec section 16) -----------


def test_two_different_papers_are_not_flagged_as_duplicates(cfg, storage, make_client):
    folder = make_client("Vani.docx")
    (folder / "another.docx").write_bytes(b"genuinely different content")
    state = classify(cfg, storage, folder)
    assert state.duplicate_originals is False


def test_byte_identical_originals_are_flagged_as_duplicates(cfg, storage, make_client):
    folder = make_client("Vani.docx")
    (folder / "Vani - Copy.docx").write_bytes((folder / "Vani.docx").read_bytes())
    state = classify(cfg, storage, folder)
    assert len(state.originals) == 2
    assert state.duplicate_originals is True
    outcome = state.decide(Phase.REVIEW)
    assert "exact duplicates" in outcome.reason


def test_single_original_is_never_flagged_as_duplicate(cfg, storage, make_client):
    folder = make_client("Vani.docx")
    assert classify(cfg, storage, folder).duplicate_originals is False


# --- what counts as a file (spec section 47) ----------------------------------


def test_office_lock_file_is_not_counted(cfg, storage, make_client):
    folder = make_client("Vani.docx", "~$Vani.docx")
    state = classify(cfg, storage, folder)
    assert len(state.originals) == 1
    assert state.decide(Phase.REVIEW).decision is Decision.PROCESS


def test_subdirectory_is_not_counted(cfg, storage, make_client):
    folder = make_client("Vani.docx", subdirs=("drafts", "figures"))
    state = classify(cfg, storage, folder)
    assert len(state.originals) == 1
    assert state.decide(Phase.REVIEW).decision is Decision.PROCESS


def test_unsupported_extension_is_ignored_not_treated_as_paper(cfg, storage, make_client):
    folder = make_client("Vani.docx", "notes.txt", "data.xlsx")
    state = classify(cfg, storage, folder)
    assert len(state.originals) == 1
    assert {p.name for p in state.ignored} == {"notes.txt", "data.xlsx"}


def test_hidden_file_is_ignored(cfg, storage, make_client):
    folder = make_client("Vani.docx", ".DS_Store")
    assert len(classify(cfg, storage, folder).originals) == 1


def test_pdf_counts_only_when_configured(cfg, storage, make_client):
    folder = make_client("Vani.pdf")
    assert classify(cfg, storage, folder).originals == []

    cfg.supported_extensions = (".docx", ".pdf")
    assert len(classify(cfg, storage, folder).originals) == 1


# --- idempotency (spec sections 22 and 23) ------------------------------------


def test_existing_review_is_not_regenerated(cfg, storage, make_client):
    folder = make_client("Vani.docx", "Vani_review.docx")
    outcome = classify(cfg, storage, folder).decide(Phase.REVIEW)
    assert outcome.decision is Decision.SKIP
    assert "already exists" in outcome.reason


def test_complete_folder_is_completed_in_both_phases(cfg, storage, make_client):
    folder = make_client("Vani.docx", "Vani_review.docx", "Correct_Vani_paper.docx")
    state = classify(cfg, storage, folder)
    assert state.is_complete
    assert state.decide(Phase.REVIEW).decision is Decision.COMPLETED
    assert state.decide(Phase.REVISE).decision is Decision.COMPLETED


def test_roles_are_assigned_by_name(cfg, storage, make_client):
    folder = make_client("Vani.docx", "Vani_review.docx", "Correct_Vani_paper.docx")
    state = classify(cfg, storage, folder)
    assert state.original.name == "Vani.docx"
    assert state.review.name == "Vani_review.docx"
    assert state.finals[0].name == "Correct_Vani_paper.docx"


# --- revision phase gate (spec sections 15, 16 and 48) ------------------------


def test_revision_requires_one_original_and_one_review(cfg, storage, make_client):
    folder = make_client("Vani.docx", "Vani_review.docx")
    assert classify(cfg, storage, folder).decide(Phase.REVISE).decision is Decision.PROCESS


def test_revision_skips_when_review_missing(cfg, storage, make_client):
    folder = make_client("Vani.docx")
    outcome = classify(cfg, storage, folder).decide(Phase.REVISE)
    assert outcome.decision is Decision.SKIP
    assert "not generated" in outcome.reason


def test_revision_rejects_an_arbitrary_pair_of_files(cfg, storage, make_client):
    """Two files is not enough — they must be an original and a review."""
    folder = make_client("paper1.docx", "paper2.docx")
    state = classify(cfg, storage, folder)
    assert state.counted == 2
    assert state.decide(Phase.REVISE).decision is Decision.SKIP


def test_revision_skips_when_zero_files(cfg, storage, make_client):
    folder = make_client()
    assert classify(cfg, storage, folder).decide(Phase.REVISE).decision is Decision.SKIP


def test_revision_skips_when_three_unexpected_files(cfg, storage, make_client):
    folder = make_client("a.docx", "b.docx", "c.docx")
    assert classify(cfg, storage, folder).decide(Phase.REVISE).decision is Decision.SKIP


# --- month detection (spec sections 3, 29 and 30) -----------------------------


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-08-01", "August 2026"),
        ("2026-08-08", "August 2026"),
        ("2026-08-31", "August 2026"),
        ("2026-09-01", "September 2026"),
    ],
)
def test_month_is_derived_from_date(cfg, iso, expected):
    now = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(cfg.timezone))
    assert scanner.current_month(cfg, now) == expected


def test_month_uses_configured_timezone_not_machine(cfg):
    """23:30 on 31 Aug in Kolkata is still August, though UTC has rolled over."""
    cfg.timezone = "Asia/Kolkata"
    late = datetime(2026, 8, 31, 23, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert scanner.current_month(cfg, late) == "August 2026"


def test_missing_month_folder_returns_none(cfg, storage):
    cfg.research_papers_root.mkdir(parents=True)
    assert scanner.month_folder(cfg, storage, "August 2026") is None


# --- traversal ----------------------------------------------------------------


def test_scan_walks_all_employees_and_clients(cfg, storage, make_client):
    make_client("Vani.docx", employee="Manoj Paper", client="Vani")
    make_client("Ashok.docx", employee="Manoj Paper", client="R Ashok")
    make_client("A.docx", employee="Janani Paper", client="Client A")
    month_dir = cfg.research_papers_root / "August 2026"

    states = scanner.scan_month(cfg, storage, month_dir)

    assert [(s.employee, s.client) for s in states] == [
        ("Janani Paper", "Client A"),
        ("Manoj Paper", "R Ashok"),
        ("Manoj Paper", "Vani"),
    ]


def test_test_mode_limits_to_one_employee_and_client(cfg, storage, make_client):
    make_client("Vani.docx", employee="Manoj Paper", client="Vani")
    make_client("Ashok.docx", employee="Manoj Paper", client="R Ashok")
    make_client("A.docx", employee="Janani Paper", client="Client A")
    cfg.test_mode = True
    month_dir = cfg.research_papers_root / "August 2026"

    states = scanner.scan_month(cfg, storage, month_dir)

    assert len(states) == 1


# --- empty employee folders must not stop the run (spec section 4) ------------


def test_employee_folder_with_no_clients_is_skipped_not_fatal(cfg, storage, make_client):
    """An employee folder containing nothing at all contributes no work."""
    make_client("Vani.docx", employee="B Normal")
    (cfg.research_papers_root / "August 2026" / "A Empty").mkdir(parents=True)

    month_dir = cfg.research_papers_root / "August 2026"
    states = scanner.scan_month(cfg, storage, month_dir)

    assert [s.employee for s in states] == ["B Normal"]
    assert states[0].decide(Phase.REVIEW).decision is Decision.PROCESS


def test_an_empty_employee_does_not_hide_later_employees(cfg, storage, make_client):
    """Alphabetically first and empty is the case that would mask a bug."""
    for name in ("A Empty", "C Also Empty"):
        (cfg.research_papers_root / "August 2026" / name).mkdir(parents=True)
    make_client("Vani.docx", employee="B Normal")
    make_client("Ashok.docx", employee="D Normal", client="Ashok")

    states = scanner.scan_month(cfg, storage, cfg.research_papers_root / "August 2026")

    assert [s.employee for s in states] == ["B Normal", "D Normal"]
    assert all(s.decide(Phase.REVIEW).decision is Decision.PROCESS for s in states)


def test_employee_with_only_empty_clients_yields_skips(cfg, storage):
    employee = cfg.research_papers_root / "August 2026" / "C Only Empty"
    (employee / "Client X").mkdir(parents=True)
    (employee / "Client Y").mkdir(parents=True)

    states = scanner.scan_month(cfg, storage, cfg.research_papers_root / "August 2026")

    assert len(states) == 2
    for state in states:
        assert state.decide(Phase.REVIEW).decision is Decision.SKIP


def test_a_paper_loose_in_an_employee_folder_is_not_processed(cfg, storage):
    """Papers must sit in a client folder; a stray one is ignored, not guessed at."""
    employee = cfg.research_papers_root / "August 2026" / "D Loose"
    employee.mkdir(parents=True)
    (employee / "Stray.docx").write_text("content", encoding="utf-8")

    states = scanner.scan_month(cfg, storage, cfg.research_papers_root / "August 2026")

    assert states == []
