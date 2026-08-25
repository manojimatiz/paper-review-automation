"""Classification of tool output into failure kinds.

Shared by the provider base class and the subprocess plumbing, so both agree on
what a usage limit looks like without importing each other.

Two rules govern everything here, both learned the hard way:

1. Only ever classify output from a run that actually went wrong — a non-zero exit,
   or a run that produced no output file. The models emit the review or the revised
   manuscript on stdout, and scanning that prose for failure keywords means the
   paper's own content decides whether the run "failed".

2. No bare numeric needles. A substring search for "429" matches a dataset of
   54,429 images; "401" matches a sample count; "503" matches a page number. HTTP
   status codes must be matched with their surrounding context or not at all.
"""

import re

from ..models import FailureKind

# Checked in order: a usage-limit message often also mentions the account, so it is
# tested before auth. Each needle must be specific enough that it cannot plausibly
# occur in the body of a research paper or a review of one.
_SIGNALS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (
        FailureKind.USAGE_LIMIT,
        (
            "usage limit", "rate limit", "rate-limit", "quota exceeded",
            "exceeded your quota", "insufficient_quota", "too many requests",
            "limit reached", "out of credit", "out of credits",
            "upgrade your plan", "usage cap", "monthly limit", "weekly limit",
            "try again later", "resets at",
        ),
    ),
    (
        FailureKind.AUTH_REQUIRED,
        (
            "not logged in", "please log in", "please login", "run `codex login`",
            "codex login", "claude login", "unauthorized", "authentication failed",
            "invalid api key", "no credentials", "expired token",
            "re-authenticate", "oauth token", "session expired",
        ),
    ),
    (
        FailureKind.TRANSIENT,
        (
            "econnreset", "etimedout", "enotfound", "socket hang up",
            "network error", "temporarily unavailable", "service unavailable",
            "overloaded", "connection reset", "connection refused",
            "bad gateway", "gateway timeout",
            # Provider-side capacity. Worth retrying: it clears on its own, and
            # treating it as unknown means giving up on a paper for no reason.
            "at capacity", "is busy", "try a different model", "server_error",
            "please try again", "model_overloaded",
        ),
    ),
)

# HTTP status codes need context. "429" alone is a number that appears in papers;
# "status 429" or "HTTP 429" is a diagnostic.
_STATUS_CONTEXT = r"(?:http|https|status|code|error|response)\W{0,3}"
_STATUS_CODES: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (FailureKind.USAGE_LIMIT, ("429",)),
    (FailureKind.AUTH_REQUIRED, ("401", "403")),
    (FailureKind.TRANSIENT, ("502", "503", "504")),
)
_STATUS_RES = tuple(
    (kind, re.compile(_STATUS_CONTEXT + code, re.IGNORECASE))
    for kind, codes in _STATUS_CODES
    for code in codes
)


def classify(output: str) -> FailureKind:
    """Best guess at why a run failed. Only call this on output from a failed run."""
    lowered = output.lower()
    for kind, needles in _SIGNALS:
        if any(needle in lowered for needle in needles):
            return kind
    for kind, pattern in _STATUS_RES:
        if pattern.search(output):
            return kind
    return FailureKind.UNKNOWN


# Codex echoes the whole prompt back on stdout, so a raw tail of the combined
# output is mostly our own instructions. The lines that actually diagnose the
# failure are the ones the tool marks as errors.
_ERROR_LINE = re.compile(r"^\s*(?:ERROR|Error|error|FATAL|panic)\b[: ]|error:", re.MULTILINE)


def summarise_failure(output: str, limit: int = 400) -> str:
    """The part of a failed run's output worth showing a human."""
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and _ERROR_LINE.match(line)
    ]
    # Preserve order, drop the duplicates these tools tend to print.
    seen, unique = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    text = " | ".join(unique[-4:]) if unique else output.strip()
    return text[-limit:]
