"""Provider discovery, argument construction, and failure classification."""

from pathlib import Path

import pytest

from paper_automation.config import ProviderConfig
from paper_automation.models import FailureKind, ProviderError
from paper_automation.providers import build_providers, discovery
from paper_automation.providers.base import MockProvider
from paper_automation.providers.claude_code import ClaudeCodeProvider
from paper_automation.providers.codex import CodexProvider
from paper_automation.prompts import OUTPUT_FILE
from paper_automation.providers.subprocess_provider import classify


# --- failure classification ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("You have hit your usage limit for this week", FailureKind.USAGE_LIMIT),
        ("HTTP 429 Too Many Requests", FailureKind.USAGE_LIMIT),
        ("insufficient_quota", FailureKind.USAGE_LIMIT),
        ("Error: not logged in. Please run `codex login`", FailureKind.AUTH_REQUIRED),
        ("401 unauthorized", FailureKind.AUTH_REQUIRED),
        ("Invalid API key provided", FailureKind.AUTH_REQUIRED),
        ("ECONNRESET while contacting the server", FailureKind.TRANSIENT),
        ("503 service unavailable", FailureKind.TRANSIENT),
        ("something else entirely", FailureKind.UNKNOWN),
    ],
)
def test_output_is_classified(text, expected):
    assert classify(text) is expected


def test_usage_limit_wins_over_auth_mention():
    """Both words appear in real quota messages; the quota reading is correct."""
    assert (
        classify("Your account has reached its usage limit; please log in to upgrade")
        is FailureKind.USAGE_LIMIT
    )


def test_retryable_only_for_transient_and_timeout():
    assert ProviderError(FailureKind.TRANSIENT, "x").retryable
    assert ProviderError(FailureKind.TIMEOUT, "x").retryable
    assert not ProviderError(FailureKind.USAGE_LIMIT, "x").retryable
    assert not ProviderError(FailureKind.AUTH_REQUIRED, "x").retryable


# --- argument construction ----------------------------------------------------


def test_codex_args_confine_writes_to_the_scratch_directory(tmp_path):
    provider = CodexProvider(ProviderConfig())
    args = provider._build_args(Path("codex.exe"), tmp_path, "do the thing")

    assert args[1] == "exec"
    assert "--skip-git-repo-check" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("--cd") + 1] == str(tmp_path)
    assert args[-1] == "do the thing"


def test_claude_args_allow_edits_but_restrict_tools(tmp_path):
    provider = ClaudeCodeProvider(ProviderConfig())
    args = provider._build_args(Path("claude.exe"), tmp_path, "revise it")

    assert "--print" in args
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert "Bash" not in args
    assert args[args.index("--add-dir") + 1] == str(tmp_path)


def test_claude_sends_the_prompt_on_stdin_not_as_an_argument(tmp_path):
    """--allowedTools is variadic and would otherwise swallow a trailing prompt."""
    provider = ClaudeCodeProvider(ProviderConfig())
    args = provider._build_args(Path("claude.exe"), tmp_path, "revise it")

    assert provider.uses_stdin
    assert "revise it" not in args
    # One comma-separated value, so nothing after it can be absorbed.
    assert args[args.index("--allowedTools") + 1] == "Read,Write,Edit,Glob,Grep"


def test_codex_passes_the_prompt_as_an_argument(tmp_path):
    provider = CodexProvider(ProviderConfig())
    assert not provider.uses_stdin


def test_model_and_extra_args_are_passed_through(tmp_path):
    config = ProviderConfig(model="gpt-5", extra_args=["--json"])
    args = CodexProvider(config)._build_args(Path("codex.exe"), tmp_path, "p")

    assert args[args.index("--model") + 1] == "gpt-5"
    assert "--json" in args


# --- console-window suppression -------------------------------------------------
# codex.exe/claude.exe are console-subsystem programs invoked from the windowless
# (pythonw.exe-run) web UI process; without creationflags Windows would flash a
# visible console for every call.


def test_preflight_suppresses_the_console_window(tmp_path, monkeypatch):
    import paper_automation.providers.subprocess_provider as sp

    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return type("R", (), {"returncode": 0, "stdout": "v1.0", "stderr": ""})()

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    provider = CodexProvider(ProviderConfig())
    provider._binary = tmp_path / "codex.exe"
    provider.preflight()
    assert calls[0].get("creationflags") == sp._CREATE_NO_WINDOW


def test_version_suppresses_the_console_window(tmp_path, monkeypatch):
    import paper_automation.providers.subprocess_provider as sp

    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return type("R", (), {"returncode": 0, "stdout": "v1.0", "stderr": ""})()

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    provider = CodexProvider(ProviderConfig())
    provider._binary = tmp_path / "codex.exe"
    provider.version()
    assert calls[0].get("creationflags") == sp._CREATE_NO_WINDOW


def test_invoke_suppresses_the_console_window(tmp_path, monkeypatch):
    import paper_automation.providers.subprocess_provider as sp

    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    provider = CodexProvider(ProviderConfig())
    provider._binary = tmp_path / "codex.exe"
    provider._invoke(tmp_path, "do the thing")
    assert calls[0].get("creationflags") == sp._CREATE_NO_WINDOW


# --- discovery ----------------------------------------------------------------


def test_pinned_binary_path_wins(tmp_path):
    pinned = tmp_path / "codex.exe"
    pinned.write_text("", encoding="utf-8")
    assert discovery.find_codex(str(pinned)) == pinned


def test_pinned_but_missing_path_returns_none(tmp_path):
    assert discovery.find_codex(str(tmp_path / "absent.exe")) is None


def test_newest_versioned_directory_wins(tmp_path, monkeypatch):
    """The version segment changes when the app updates, so pick the newest."""
    import os

    old = tmp_path / "bin" / "aaa" / "codex.exe"
    new = tmp_path / "bin" / "zzz" / "codex.exe"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    monkeypatch.setattr(discovery, "CODEX_PATTERNS", (tmp_path / "bin" / "*" / "codex.exe",))
    monkeypatch.setattr(discovery.shutil, "which", lambda _c: None)

    assert discovery.find_codex() == new


def test_missing_binary_raises_a_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "CODEX_PATTERNS", (tmp_path / "nope" / "codex.exe",))
    monkeypatch.setattr(discovery.shutil, "which", lambda _c: None)
    provider = CodexProvider(ProviderConfig())

    with pytest.raises(ProviderError) as excinfo:
        _ = provider.binary

    assert excinfo.value.kind is FailureKind.BINARY_MISSING
    assert "binary_path" in str(excinfo.value)


# --- selection ----------------------------------------------------------------


def test_mock_mode_selects_mock_providers(cfg):
    cfg.provider_mode = "mock"
    assert all(isinstance(p, MockProvider) for p in build_providers(cfg))


def test_test_mode_implies_mock(cfg):
    cfg.provider_mode = "auto"
    cfg.test_mode = True
    assert all(isinstance(p, MockProvider) for p in build_providers(cfg))


def test_real_mode_selects_the_two_clis(cfg):
    cfg.provider_mode = "real"
    review, revise = build_providers(cfg)
    assert isinstance(review, CodexProvider)
    assert isinstance(revise, ClaudeCodeProvider)


# --- output contract ----------------------------------------------------------


def test_missing_output_file_is_an_error(tmp_path):
    class Silent(MockProvider):
        def _invoke(self, workdir, prompt):
            return "did nothing"

    with pytest.raises(ProviderError) as excinfo:
        Silent().generate(tmp_path, "p")
    assert excinfo.value.kind is FailureKind.EMPTY_OUTPUT


def test_blank_output_file_is_an_error(tmp_path):
    class Blank(MockProvider):
        def _invoke(self, workdir, prompt):
            (workdir / "output.md").write_text("   \n", encoding="utf-8")
            return ""

    with pytest.raises(ProviderError) as excinfo:
        Blank().generate(tmp_path, "p")
    assert excinfo.value.kind is FailureKind.EMPTY_OUTPUT


# --- failure classification (regressions from a real run) ---------------------
#
# A live run discarded a completed 50k-token review and halted the batch because
# the review's own prose matched a usage-limit keyword. Two rules now hold: a
# zero-exit run that produced output is never re-judged by its text, and no
# failure needle is a bare number.


def test_a_successful_run_is_never_condemned_by_its_own_text(tmp_path):
    """The exact shape that broke a live run: real output, alarming words."""
    from paper_automation.providers.base import CliProvider

    review = (
        "## 6. Limitations\n"
        "The dataset of 54,429 images was collected under a 401(k)-funded grant.\n"
        "Sample 503 was excluded. The authors note the limit reached in Table 2.\n"
        "## 13. Final Reviewer Recommendation\nReject.\ntokens used\n50,585\n"
    )

    class FakeProvider(CliProvider):
        name = "fake"

        def _invoke(self, workdir, prompt):
            (workdir / OUTPUT_FILE).write_text(review, encoding="utf-8")
            return review

    result = FakeProvider().generate(tmp_path, "go")
    assert "Final Reviewer Recommendation" in result


def test_numbers_in_prose_are_not_status_codes():
    assert classify("we evaluated 54,429 images") is FailureKind.UNKNOWN
    assert classify("accuracy on sample 401 was high") is FailureKind.UNKNOWN
    assert classify("see page 503 of the appendix") is FailureKind.UNKNOWN


def test_status_codes_with_context_are_still_caught():
    assert classify("HTTP 429 Too Many Requests") is FailureKind.USAGE_LIMIT
    assert classify("request failed with status 401") is FailureKind.AUTH_REQUIRED
    assert classify("error 503 from upstream") is FailureKind.TRANSIENT


def test_real_usage_limit_messages_are_caught():
    for message in (
        "You've reached your usage limit. Resets at 9pm.",
        "Rate limit exceeded, try again later",
        "You are out of credits — upgrade your plan",
        "monthly limit reached for this account",
    ):
        assert classify(message) is FailureKind.USAGE_LIMIT, message


def test_missing_output_with_a_usage_limit_stops_the_phase(tmp_path):
    """No file produced AND a limit message: this one must halt the batch."""
    from paper_automation.providers.base import CliProvider

    class LimitedProvider(CliProvider):
        name = "limited"

        def _invoke(self, workdir, prompt):
            return "You've reached your usage limit. Resets at 9pm."

    with pytest.raises(ProviderError) as exc:
        LimitedProvider().generate(tmp_path, "go")
    assert exc.value.kind is FailureKind.USAGE_LIMIT


def test_missing_output_without_a_limit_is_only_an_empty_result(tmp_path):
    from paper_automation.providers.base import CliProvider

    class SilentProvider(CliProvider):
        name = "silent"

        def _invoke(self, workdir, prompt):
            return "I could not complete that request."

    with pytest.raises(ProviderError) as exc:
        SilentProvider().generate(tmp_path, "go")
    assert exc.value.kind is FailureKind.EMPTY_OUTPUT


def test_provider_capacity_errors_are_retryable():
    """"Selected model is at capacity" clears by itself; giving up wastes a paper."""
    from paper_automation.models import ProviderError

    kind = classify("ERROR: Selected model is at capacity. Please try a different model.")
    assert kind is FailureKind.TRANSIENT
    assert ProviderError(kind, "x").retryable


def test_failure_summary_drops_the_echoed_prompt():
    """Codex echoes the prompt on stdout; a raw tail is our own instructions."""
    from paper_automation.providers.failures import summarise_failure

    output = (
        "Begin `output.md` with this header:\n"
        "# LANGUAGE REVIEW REPORT\n"
        "Finish with `## Summary` giving the total count.\n"
        "ERROR: Selected model is at capacity. Please try a different model.\n"
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    summary = summarise_failure(output)

    assert summary == "ERROR: Selected model is at capacity. Please try a different model."
    assert "LANGUAGE REVIEW REPORT" not in summary


def test_failure_summary_falls_back_when_nothing_is_marked():
    from paper_automation.providers.failures import summarise_failure

    assert summarise_failure("something went wrong") == "something went wrong"
