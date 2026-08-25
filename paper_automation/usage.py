"""Token accounting, read from the CLIs' own session logs.

Neither tool reports usage on stdout in a form worth parsing, but both write a
session log that records it exactly. After a provider call finishes, the newest
log written during that call is read and its counts extracted.

This is best-effort by design and every function returns None rather than raising:
the logs are a private format that can change with any app update, and a missing
token count must never turn a successful review into a failed one. Anything shown
in the UI from here is labelled approximate for the same reason.

Attribution works because each provider call creates its own session: `codex exec`
starts a fresh rollout, and `claude --print` starts a fresh session. Selecting the
newest log touched inside the call's time window therefore picks the right one.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_SESSIONS = Path.home() / ".claude" / "projects"

# A log is only considered if it was written during the call, with a little slack
# for clock granularity and for the app flushing after the process exits.
_START_SLACK = 5.0
_END_SLACK = 30.0


@dataclass
class Usage:
    """Tokens attributed to one provider call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    # Percentage of the subscription window consumed, when the tool reports it.
    # More meaningful than raw tokens on a subscription plan.
    limit_used_percent: float | None = None
    limit_resets_at: int | None = None
    source: str = ""

    @property
    def billable_total(self) -> int:
        """Total, falling back to a sum when the log does not provide one."""
        if self.total_tokens:
            return self.total_tokens
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.billable_total,
            "limit_used_percent": self.limit_used_percent,
            "limit_resets_at": self.limit_resets_at,
            "source": self.source,
        }


def _recent_logs(root: Path, started: float, finished: float) -> list[Path]:
    """Session logs touched during the call, newest first."""
    if not root.is_dir():
        return []
    try:
        candidates = [
            p for p in root.rglob("*.jsonl")
            if started - _START_SLACK <= p.stat().st_mtime <= finished + _END_SLACK
        ]
    except OSError:
        return []
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_lines(path: Path, limit_bytes: int = 12_000_000) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit_bytes).splitlines()
    except OSError:
        return []


def codex_usage(started: float, finished: float) -> Usage | None:
    """Totals from the last `token_count` event Codex wrote during the call."""
    for path in _recent_logs(CODEX_SESSIONS, started, finished):
        latest = None
        for line in _read_lines(path):
            if '"token_count"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload", {})
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "token_count":
                latest = payload
        if latest is None:
            continue

        totals = latest.get("info", {}).get("total_token_usage", {}) or {}
        usage = Usage(
            input_tokens=int(totals.get("input_tokens", 0) or 0),
            output_tokens=int(totals.get("output_tokens", 0) or 0),
            cached_input_tokens=int(totals.get("cached_input_tokens", 0) or 0),
            reasoning_tokens=int(totals.get("reasoning_output_tokens", 0) or 0),
            total_tokens=int(totals.get("total_tokens", 0) or 0),
            source="codex session log",
        )
        primary = (latest.get("rate_limits") or {}).get("primary") or {}
        if primary.get("used_percent") is not None:
            usage.limit_used_percent = float(primary["used_percent"])
            usage.limit_resets_at = primary.get("resets_at")
        return usage
    return None


def claude_usage(started: float, finished: float) -> Usage | None:
    """Sum of the per-message `usage` blocks Claude wrote during the call."""
    for path in _recent_logs(CLAUDE_SESSIONS, started, finished):
        usage = Usage(source="claude session log")
        found = False
        for line in _read_lines(path):
            if '"usage"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            block = _find_usage(record)
            if not block:
                continue
            found = True
            usage.input_tokens += int(block.get("input_tokens", 0) or 0)
            usage.output_tokens += int(block.get("output_tokens", 0) or 0)
            usage.cached_input_tokens += int(
                block.get("cache_read_input_tokens", 0) or 0
            )
        if found:
            return usage
    return None


def _find_usage(record) -> dict | None:
    """Locate the `usage` object, which sits at a different depth per record type."""
    if isinstance(record, dict):
        block = record.get("usage")
        if isinstance(block, dict) and "output_tokens" in block:
            return block
        for value in record.values():
            found = _find_usage(value)
            if found:
                return found
    elif isinstance(record, list):
        for item in record:
            found = _find_usage(item)
            if found:
                return found
    return None


# Codex prints a usage summary at the end of `codex exec`. It writes no session
# log in that mode — the rollout files under ~/.codex/sessions come from the
# desktop app only — so stdout is the sole source for the review stage.
_STDOUT_TOKENS = re.compile(
    r"tokens?\s+used\s*[:\-]?\s*([\d,]+)", re.IGNORECASE
)


def from_stdout(text: str) -> Usage | None:
    """Token count from a tool's own printed summary."""
    if not text:
        return None
    matches = _STDOUT_TOKENS.findall(text)
    if not matches:
        return None
    try:
        total = int(matches[-1].replace(",", ""))
    except ValueError:
        return None
    if total <= 0:
        return None
    return Usage(total_tokens=total, source="tool output")


def read(
    provider: str, started: float, finished: float, stdout: str = ""
) -> Usage | None:
    """Usage for one call: session log where there is one, stdout otherwise."""
    reader = {"codex": codex_usage, "claude": claude_usage}.get(provider)
    try:
        if reader is not None:
            found = reader(started, finished)
            if found is not None:
                return found
        return from_stdout(stdout)
    except Exception:  # a token count is never worth failing a run over
        log.debug("Could not read %s token usage", provider, exc_info=True)
        return None


def humanise(count: int) -> str:
    """Compact form for display: 50585 -> "50.6k"."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.2f}M"
