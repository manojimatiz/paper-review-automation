"""Creating employee/client folders on demand (spec sections 30, 38-39).

Only ever creates — never deletes, renames, or moves, matching the safety
invariant tests/test_safety.py enforces at the AST level.
"""

import pytest

from paper_automation import service


# --- name validation -----------------------------------------------------


def test_empty_name_is_refused(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "  ")
    assert result["ok"] is False
    assert "empty" in result["message"]


def test_dot_dot_is_refused(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "..")
    assert result["ok"] is False


def test_a_path_separator_is_refused(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "a/b")
    assert result["ok"] is False
    assert "separator" in result["message"]


def test_a_backslash_is_refused(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "a\\b")
    assert result["ok"] is False


def test_invalid_filename_characters_are_refused(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "bad:name")
    assert result["ok"] is False
    assert "cannot be used" in result["message"]


def test_traversal_cannot_escape_the_month_folder(cfg, storage):
    """A crafted employee name must not be able to climb out via '..'."""
    result = service.create_employee_folder(cfg, storage, "August 2026", "../../evil")
    assert result["ok"] is False


# --- employee folder creation ---------------------------------------------


def test_creates_an_employee_folder(cfg, storage):
    result = service.create_employee_folder(cfg, storage, "August 2026", "Priya")
    assert result["ok"] is True
    target = cfg.research_papers_root / "August 2026" / "Priya"
    assert target.is_dir()


def test_creating_an_employee_folder_creates_the_month_too(cfg, storage):
    assert not (cfg.research_papers_root / "August 2026").exists()
    service.create_employee_folder(cfg, storage, "August 2026", "Priya")
    assert (cfg.research_papers_root / "August 2026").is_dir()


def test_creating_an_existing_employee_folder_is_not_an_error(cfg, storage):
    """Idempotent, like the rest of the pipeline: re-running a safe action must
    not start failing just because it already happened."""
    service.create_employee_folder(cfg, storage, "August 2026", "Priya")
    result = service.create_employee_folder(cfg, storage, "August 2026", "Priya")
    assert result["ok"] is True


def test_creating_an_employee_folder_does_not_disturb_existing_papers(cfg, storage, make_client):
    """Never touches what's already there — only ever adds."""
    folder = make_client("Vani.docx", employee="Janani", client="Vani")
    before = (folder / "Vani.docx").read_bytes()
    service.create_employee_folder(cfg, storage, "August 2026", "NewPerson")
    assert (folder / "Vani.docx").read_bytes() == before


# --- client folder creation -------------------------------------------------


def test_creates_a_client_folder(cfg, storage):
    result = service.create_client_folder(cfg, storage, "August 2026", "Priya", "Acme Corp")
    assert result["ok"] is True
    target = cfg.research_papers_root / "August 2026" / "Priya" / "Acme Corp"
    assert target.is_dir()


def test_client_folder_creates_missing_employee_folder_too(cfg, storage):
    service.create_client_folder(cfg, storage, "August 2026", "Priya", "Acme Corp")
    assert (cfg.research_papers_root / "August 2026" / "Priya").is_dir()


def test_client_folder_starts_empty(cfg, storage):
    result = service.create_client_folder(cfg, storage, "August 2026", "Priya", "Acme Corp")
    target = cfg.research_papers_root / "August 2026" / "Priya" / "Acme Corp"
    assert list(target.iterdir()) == []


def test_creating_an_existing_client_folder_is_not_an_error(cfg, storage):
    service.create_client_folder(cfg, storage, "August 2026", "Priya", "Acme Corp")
    result = service.create_client_folder(cfg, storage, "August 2026", "Priya", "Acme Corp")
    assert result["ok"] is True


def test_a_bad_client_name_is_refused_even_with_a_good_employee_name(cfg, storage):
    result = service.create_client_folder(cfg, storage, "August 2026", "Priya", "../evil")
    assert result["ok"] is False


# --- run.py: create_missing_month ------------------------------------------


def test_run_once_leaves_a_missing_month_alone_by_default(cfg, storage):
    import run as run_module
    from argparse import Namespace

    cfg.research_papers_root.mkdir(parents=True, exist_ok=True)
    cfg.create_missing_month = False
    args = Namespace(month="August 2026", phase="both")
    code = run_module.run_once(cfg, storage, args)
    assert code == 0
    assert not (cfg.research_papers_root / "August 2026").exists()


def test_run_once_creates_the_month_folder_when_opted_in(cfg, storage):
    import run as run_module
    from argparse import Namespace

    cfg.research_papers_root.mkdir(parents=True, exist_ok=True)
    cfg.create_missing_month = True
    args = Namespace(month="August 2026", phase="both")
    run_module.run_once(cfg, storage, args)
    assert (cfg.research_papers_root / "August 2026").is_dir()
