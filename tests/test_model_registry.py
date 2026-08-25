"""Model selection and reporting.

The requirement driving these: the list must not go stale when a new model ships,
and what the dashboard reports must be what a run actually uses.
"""

import json

import pytest

from paper_automation import model_registry, service
from paper_automation.config import ProviderConfig
from paper_automation.providers.claude_code import ClaudeCodeProvider
from paper_automation.providers.codex import CodexProvider


# --- staying current ----------------------------------------------------------


def test_aliases_are_offered_for_claude():
    """An alias tracks the newest model, so the list never needs updating."""
    options = model_registry.load().claude
    aliases = {o.id for o in options if o.alias}
    assert {"opus", "sonnet", "haiku"} <= aliases


def test_an_alias_is_reported_as_tracking_the_latest():
    active = model_registry.active_model("claude", "opus")
    assert active["pinned"] is True
    assert "latest" in active["effective"]


def test_a_pinned_model_is_reported_verbatim():
    active = model_registry.active_model("claude", "claude-opus-5")
    assert active["effective"] == "claude-opus-5"
    assert "latest" not in active["effective"]


def test_no_codex_model_names_are_invented():
    """Guessing OpenAI names would put options in the UI that fail when selected."""
    ids = [o.id for o in model_registry.load().codex if o.source == "built-in"]
    assert ids == [""]


def test_a_new_model_can_be_used_without_touching_the_code(tmp_path):
    registry_file = tmp_path / "models.json"
    assert model_registry.remember(registry_file, "claude", "claude-opus-6")

    ids = [o.id for o in model_registry.load(registry_file).claude]
    assert "claude-opus-6" in ids


def test_remembering_is_idempotent(tmp_path):
    registry_file = tmp_path / "models.json"
    assert model_registry.remember(registry_file, "codex", "gpt-6")
    assert not model_registry.remember(registry_file, "codex", "gpt-6")

    entries = json.loads(registry_file.read_text(encoding="utf-8"))["codex"]
    assert len(entries) == 1


def test_built_in_models_are_not_duplicated_into_the_file(tmp_path):
    registry_file = tmp_path / "models.json"
    assert not model_registry.remember(registry_file, "claude", "opus")
    assert not registry_file.exists()


def test_a_corrupt_registry_file_does_not_break_the_list(tmp_path):
    registry_file = tmp_path / "models.json"
    registry_file.write_text("{ this is not json", encoding="utf-8")

    options = model_registry.load(registry_file).claude
    assert any(o.id == "opus" for o in options)


# --- detection ----------------------------------------------------------------


def test_detection_reads_the_model_from_a_session_log(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "run.jsonl").write_text(
        '{"type":"turn_context","payload":{"model":"gpt-9-turbo","cwd":"x"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(
        model_registry._SESSION_LOGS, "codex", (sessions, "*.jsonl")
    )
    assert model_registry.detect_recent_models("codex") == ["gpt-9-turbo"]


def test_detection_survives_a_missing_log_directory(tmp_path, monkeypatch):
    monkeypatch.setitem(
        model_registry._SESSION_LOGS, "codex", (tmp_path / "nope", "*.jsonl")
    )
    assert model_registry.detect_recent_models("codex") == []


def test_unconfigured_model_reports_what_was_last_used(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "a.jsonl").write_text('{"model":"gpt-9-turbo"}\n', encoding="utf-8")
    monkeypatch.setitem(
        model_registry._SESSION_LOGS, "codex", (sessions, "*.jsonl")
    )

    active = model_registry.active_model("codex", "")
    assert active["pinned"] is False
    assert active["effective"] == "gpt-9-turbo"


# --- writing the choice into config -------------------------------------------


def test_model_is_written_into_the_right_provider_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'research_papers_root = "C:/x"\n\n'
        '[providers.codex]\nmodel = ""\n\n'
        '[providers.claude]\nmodel = ""\n',
        encoding="utf-8",
    )
    service.update_provider_model(path, "claude", "opus")
    text = path.read_text(encoding="utf-8")

    codex_block = text.split("[providers.codex]")[1].split("[providers.claude]")[0]
    claude_block = text.split("[providers.claude]")[1]
    assert 'model = ""' in codex_block          # untouched
    assert 'model = "opus"' in claude_block


def test_model_key_is_added_when_the_table_lacks_it(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'research_papers_root = "C:/x"\n\n[providers.claude]\ntimeout_seconds = 60\n',
        encoding="utf-8",
    )
    assert service.update_provider_model(path, "claude", "sonnet")
    assert 'model = "sonnet"' in path.read_text(encoding="utf-8")


def test_clearing_the_model_returns_to_the_app_default(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[providers.claude]\nmodel = "opus"\n', encoding="utf-8")
    service.update_provider_model(path, "claude", "")
    assert 'model = ""' in path.read_text(encoding="utf-8")


def test_an_unknown_provider_is_refused(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[providers.claude]\nmodel = ""\n', encoding="utf-8")
    assert service.update_provider_model(path, "gemini", "x") is False


# --- the audit trail records the model ----------------------------------------


def test_audit_label_includes_the_chosen_model():
    provider = ClaudeCodeProvider(ProviderConfig(model="opus"))
    assert provider.model_label == "claude-code:opus"


def test_audit_label_without_a_model_names_the_tool():
    assert CodexProvider(ProviderConfig()).model_label == "codex"


def test_model_label_is_not_shadowed_by_a_class_attribute():
    """Subclasses set label_base; a plain model_label attribute would hide the property."""
    provider = CodexProvider(ProviderConfig(model="gpt-5"))
    assert provider.model_label == "codex:gpt-5"


# --- service-level status -----------------------------------------------------


def test_model_status_never_raises_when_the_apps_are_absent(cfg, tmp_path, monkeypatch):
    from paper_automation.providers import discovery

    monkeypatch.setattr(discovery, "CODEX_PATTERNS", (tmp_path / "nope.exe",))
    monkeypatch.setattr(discovery, "CLAUDE_PATTERNS", (tmp_path / "nope.exe",))
    monkeypatch.setattr(discovery.shutil, "which", lambda _c: None)

    status = service.model_status(cfg, tmp_path)
    assert len(status["stages"]) == 2
    for stage in status["stages"]:
        assert stage["available"] is False
        assert stage["options"]


def test_model_status_covers_both_stages(cfg, tmp_path):
    stages = service.model_status(cfg, tmp_path)["stages"]
    assert [s["provider"] for s in stages] == ["codex", "claude"]
    assert [s["stage"] for s in stages] == ["Review", "Revision"]
