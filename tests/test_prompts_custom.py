"""Admin-edited prompts: validation, versioning, and the rules they cannot remove."""

import pytest

from paper_automation import prompts, service
from paper_automation.models import Phase
from paper_automation.state import StateStore


# --- validation --------------------------------------------------------------


def test_a_valid_body_passes():
    prompts.validate_custom("Check {client}'s {original_filename}.", Phase.REVISE)


def test_an_empty_body_is_refused():
    with pytest.raises(prompts.PromptError, match="cannot be empty"):
        prompts.validate_custom("   ", Phase.REVIEW)


def test_an_unknown_placeholder_is_refused():
    """Caught when saved, not when a run explodes on it at 9am."""
    with pytest.raises(prompts.PromptError, match="Unknown placeholder"):
        prompts.validate_custom("Hello {nonsense}", Phase.REVIEW)


def test_review_date_is_not_available_to_the_revision_prompt():
    with pytest.raises(prompts.PromptError, match="Unknown placeholder"):
        prompts.validate_custom("On {review_date}", Phase.REVISE)


def test_unbalanced_braces_are_refused():
    with pytest.raises(prompts.PromptError):
        prompts.validate_custom("Check {client", Phase.REVIEW)


def test_the_builtin_body_is_itself_valid():
    """"Start from the current one" must give the admin something that works."""
    for phase in (Phase.REVIEW, Phase.REVISE):
        for mode in ("grammar", "full"):
            prompts.validate_custom(prompts.default_body(phase, mode), phase)


# --- rendering ---------------------------------------------------------------


def test_placeholders_are_substituted():
    out = prompts.render_custom(
        "Paper {original_filename} from {client} on {review_date}.",
        Phase.REVIEW, "Dr Vani", "paper.docx", "01-Aug-2026",
    )
    assert "Paper paper.docx from Dr Vani on 01-Aug-2026." in out


def test_integrity_rules_are_always_appended():
    """An admin must not be able to edit away the no-fabrication rules."""
    out = prompts.render_custom("Do whatever you like.", Phase.REVIEW, "C", "p.docx", "d")
    assert "SCIENTIFIC INTEGRITY" in out
    assert "Never invent experimental results" in out


def test_the_file_contract_is_always_appended():
    """The pipeline owns where output goes, not the prompt."""
    for phase in (Phase.REVIEW, Phase.REVISE):
        out = prompts.render_custom("Anything.", phase, "C", "p.docx", "d")
        assert prompts.OUTPUT_FILE in out
        assert prompts.MANUSCRIPT_FILE in out


def test_the_revision_contract_protects_the_review_file_too():
    out = prompts.render_custom("Anything.", Phase.REVISE, "C", "p.docx")
    assert prompts.REVIEW_FILE in out


# --- storage and versioning --------------------------------------------------


def test_saving_creates_version_one(cfg):
    with StateStore(cfg.state_db) as store:
        assert store.save_prompt(Phase.REVIEW, "grammar", "body one") == 1


def test_each_save_makes_a_new_version(cfg):
    with StateStore(cfg.state_db) as store:
        assert store.save_prompt(Phase.REVIEW, "grammar", "one") == 1
        assert store.save_prompt(Phase.REVIEW, "grammar", "two") == 2


def test_the_newest_save_becomes_active(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "one")
        store.save_prompt(Phase.REVIEW, "grammar", "two")
        assert store.active_prompt(Phase.REVIEW, "grammar")["body"] == "two"


def test_no_active_prompt_on_a_fresh_database(cfg):
    with StateStore(cfg.state_db) as store:
        assert store.active_prompt(Phase.REVIEW, "grammar") is None


def test_phases_and_modes_are_kept_separate(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "review-grammar")
        store.save_prompt(Phase.REVISE, "grammar", "revise-grammar")
        store.save_prompt(Phase.REVIEW, "full", "review-full")

        assert store.active_prompt(Phase.REVIEW, "grammar")["body"] == "review-grammar"
        assert store.active_prompt(Phase.REVISE, "grammar")["body"] == "revise-grammar"
        assert store.active_prompt(Phase.REVIEW, "full")["body"] == "review-full"
        assert store.active_prompt(Phase.REVISE, "full") is None


def test_an_earlier_version_can_be_restored(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "one")
        store.save_prompt(Phase.REVIEW, "grammar", "two")
        assert store.activate_prompt_version(Phase.REVIEW, "grammar", 1) is True
        assert store.active_prompt(Phase.REVIEW, "grammar")["body"] == "one"


def test_restoring_a_version_that_does_not_exist_fails_cleanly(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "one")
        assert store.activate_prompt_version(Phase.REVIEW, "grammar", 99) is False


def test_resetting_falls_back_to_the_builtin_without_losing_history(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "one")
        store.clear_active_prompt(Phase.REVIEW, "grammar")
        assert store.active_prompt(Phase.REVIEW, "grammar") is None
        assert len(store.prompt_versions(Phase.REVIEW, "grammar")) == 1


def test_every_version_is_retained(cfg):
    with StateStore(cfg.state_db) as store:
        for n in range(4):
            store.save_prompt(Phase.REVIEW, "grammar", f"body {n}")
        versions = store.prompt_versions(Phase.REVIEW, "grammar")
    assert [v["version"] for v in versions] == [4, 3, 2, 1]


def test_the_author_is_recorded(cfg):
    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, "grammar", "one", created_by="alice")
        assert store.active_prompt(Phase.REVIEW, "grammar")["created_by"] == "alice"


# --- service layer -----------------------------------------------------------


def test_overview_reports_the_builtin_until_edited(cfg):
    rows = service.prompt_overview(cfg)
    assert [r["phase"] for r in rows] == ["review", "revise"]
    assert all(r["customised"] is False for r in rows)
    assert all(r["body"] for r in rows)


def test_overview_reports_a_customised_prompt(cfg):
    service.save_prompt(cfg, "review", "My own {client} instructions.", "alice")
    review = service.prompt_overview(cfg)[0]
    assert review["customised"] is True
    assert review["active_version"] == 1
    assert review["body"] == "My own {client} instructions."


def test_saving_an_invalid_prompt_is_reported_not_stored(cfg):
    result = service.save_prompt(cfg, "review", "Bad {oops}", "alice")
    assert result["ok"] is False
    assert "Unknown placeholder" in result["message"]
    assert service.prompt_overview(cfg)[0]["customised"] is False


def test_service_restore_and_reset(cfg):
    service.save_prompt(cfg, "review", "first {client}", "alice")
    service.save_prompt(cfg, "review", "second {client}", "alice")
    assert service.activate_prompt(cfg, "review", 1)["ok"] is True
    assert service.prompt_overview(cfg)[0]["body"] == "first {client}"

    service.reset_prompt(cfg, "review")
    assert service.prompt_overview(cfg)[0]["customised"] is False


def test_service_restore_of_a_missing_version_fails_cleanly(cfg):
    service.save_prompt(cfg, "review", "first {client}", "alice")
    assert service.activate_prompt(cfg, "review", 42)["ok"] is False


# --- what a run actually sends -----------------------------------------------


def test_a_run_uses_the_builtin_when_nothing_is_saved(cfg):
    from paper_automation import phases

    with StateStore(cfg.state_db) as store:
        built = phases.build_prompt(store, cfg, Phase.REVIEW, "Vani", "p.docx", "01-Aug")
    assert built == prompts.grammar_review_prompt("Vani", "p.docx", "01-Aug")


def test_a_run_uses_the_saved_prompt_once_one_is_active(cfg):
    from paper_automation import phases

    service.save_prompt(cfg, "review", "MARKER for {client}", "alice")
    with StateStore(cfg.state_db) as store:
        built = phases.build_prompt(store, cfg, Phase.REVIEW, "Vani", "p.docx", "01-Aug")
    assert "MARKER for Vani" in built
    assert "SCIENTIFIC INTEGRITY" in built


def test_a_broken_stored_prompt_falls_back_rather_than_failing_the_paper(cfg):
    """Validation should stop this ever being stored; if it happens anyway, a
    paper must not be lost over it."""
    from paper_automation import phases

    with StateStore(cfg.state_db) as store:
        store.save_prompt(Phase.REVIEW, cfg.task_mode, "Broken {unknown_placeholder}")
        built = phases.build_prompt(store, cfg, Phase.REVIEW, "Vani", "p.docx", "01-Aug")
    assert built == prompts.grammar_review_prompt("Vani", "p.docx", "01-Aug")
