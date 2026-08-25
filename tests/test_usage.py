"""Token accounting.

The fixtures below are trimmed from real session logs. Parsing a private format
means these tests are the only thing that will tell us when an app update breaks
it — and a broken parse must degrade to "no number shown", never to a failed run.
"""

import json
import time

import pytest

from paper_automation import usage


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in records),
        encoding="utf-8",
    )
    return path


CODEX_TOKEN_EVENT = {
    "timestamp": "2026-08-10T06:02:05.415Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 285921,
                "cached_input_tokens": 240640,
                "output_tokens": 4804,
                "reasoning_output_tokens": 521,
                "total_tokens": 290725,
            },
            "last_token_usage": {"total_tokens": 56063},
        },
        "rate_limits": {
            "primary": {
                "used_percent": 82.0,
                "window_minutes": 43200,
                "resets_at": 1788002342,
            }
        },
    },
}


# --- codex --------------------------------------------------------------------


def test_codex_totals_are_read_from_the_session_log(tmp_path, monkeypatch):
    log = _write(tmp_path / "sessions" / "run.jsonl", [CODEX_TOKEN_EVENT])
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")

    result = usage.codex_usage(log.stat().st_mtime - 1, log.stat().st_mtime + 1)

    assert result.total_tokens == 290725
    assert result.input_tokens == 285921
    assert result.output_tokens == 4804
    assert result.cached_input_tokens == 240640
    assert result.reasoning_tokens == 521


def test_codex_reports_the_subscription_percentage(tmp_path, monkeypatch):
    """More meaningful than raw tokens on a subscription plan."""
    log = _write(tmp_path / "sessions" / "run.jsonl", [CODEX_TOKEN_EVENT])
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")

    result = usage.codex_usage(log.stat().st_mtime - 1, log.stat().st_mtime + 1)
    assert result.limit_used_percent == 82.0
    assert result.limit_resets_at == 1788002342


def test_the_last_token_event_wins(tmp_path, monkeypatch):
    """Codex emits a cumulative event per turn; only the final one is the total."""
    later = json.loads(json.dumps(CODEX_TOKEN_EVENT))
    later["payload"]["info"]["total_token_usage"]["total_tokens"] = 999999
    log = _write(tmp_path / "sessions" / "run.jsonl", [CODEX_TOKEN_EVENT, later])
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")

    result = usage.codex_usage(log.stat().st_mtime - 1, log.stat().st_mtime + 1)
    assert result.total_tokens == 999999


# --- claude -------------------------------------------------------------------


def test_claude_usage_is_summed_across_messages(tmp_path, monkeypatch):
    records = [
        {"message": {"usage": {"input_tokens": 2, "output_tokens": 769,
                               "cache_read_input_tokens": 448840}}},
        {"message": {"usage": {"input_tokens": 5, "output_tokens": 231,
                               "cache_read_input_tokens": 1000}}},
    ]
    log = _write(tmp_path / "projects" / "p" / "s.jsonl", records)
    monkeypatch.setattr(usage, "CLAUDE_SESSIONS", tmp_path / "projects")

    result = usage.claude_usage(log.stat().st_mtime - 1, log.stat().st_mtime + 1)

    assert result.input_tokens == 7
    assert result.output_tokens == 1000
    assert result.cached_input_tokens == 449840
    assert result.billable_total == 1007  # no explicit total, so input + output


# --- attribution --------------------------------------------------------------


def test_logs_written_before_the_call_are_ignored(tmp_path, monkeypatch):
    """Otherwise a previous run's tokens get charged to this paper."""
    import os

    log = _write(tmp_path / "sessions" / "old.jsonl", [CODEX_TOKEN_EVENT])
    old = time.time() - 4000
    os.utime(log, (old, old))
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")

    assert usage.codex_usage(time.time() - 10, time.time()) is None


def test_missing_log_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "nope")
    assert usage.codex_usage(0, time.time()) is None


def test_a_corrupt_log_line_does_not_raise(tmp_path, monkeypatch):
    log = _write(tmp_path / "sessions" / "run.jsonl",
                 ["{ not json at all", json.dumps(CODEX_TOKEN_EVENT)])
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")

    result = usage.codex_usage(log.stat().st_mtime - 1, log.stat().st_mtime + 1)
    assert result.total_tokens == 290725


def test_read_never_raises_for_an_unknown_provider():
    assert usage.read("gemini", 0, time.time()) is None


def test_read_swallows_unexpected_failures(monkeypatch):
    def boom(*_args):
        raise RuntimeError("log format changed")

    monkeypatch.setattr(usage, "codex_usage", boom)
    assert usage.read("codex", 0, time.time()) is None


# --- display ------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [(0, "0"), (999, "999"), (1000, "1k"), (50585, "50.6k"), (1_250_000, "1.25M")],
)
def test_humanise(count, expected):
    assert usage.humanise(count) == expected


# --- stdout fallback ----------------------------------------------------------
#
# `codex exec` writes no session log — the rollouts under ~/.codex/sessions come
# from the desktop app only — so its token count has to come from stdout.


def test_tokens_are_parsed_from_the_printed_summary():
    result = usage.from_stdout("Done.\n\ntokens used\n50,585\n")
    assert result.total_tokens == 50585
    assert result.source == "tool output"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("tokens used: 1,234", 1234),
        ("Tokens Used  9876", 9876),
        ("token used - 42", 42),
    ],
)
def test_summary_wording_variants(text, expected):
    assert usage.from_stdout(text).total_tokens == expected


def test_the_last_printed_count_wins():
    """A retried call prints more than once; the final figure is the total."""
    assert usage.from_stdout("tokens used 100\n...\ntokens used 250").total_tokens == 250


def test_no_summary_means_no_number_rather_than_zero():
    assert usage.from_stdout("all finished") is None
    assert usage.from_stdout("") is None


def test_read_falls_back_to_stdout_when_no_log_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "absent")
    result = usage.read("codex", 0, time.time(), "tokens used\n7,500\n")
    assert result.total_tokens == 7500


def test_session_log_is_preferred_over_stdout(tmp_path, monkeypatch):
    log = _write(tmp_path / "sessions" / "run.jsonl", [CODEX_TOKEN_EVENT])
    monkeypatch.setattr(usage, "CODEX_SESSIONS", tmp_path / "sessions")
    result = usage.read(
        "codex", log.stat().st_mtime - 1, log.stat().st_mtime + 1, "tokens used 5"
    )
    assert result.total_tokens == 290725
