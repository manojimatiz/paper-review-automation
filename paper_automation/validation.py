"""Final validation (spec sections 35 and 36).

The pipeline must never quietly present a fabricated or truncated manuscript as
finished. Anything suspicious here downgrades the folder to REQUIRES_HUMAN_REVIEW
rather than COMPLETED.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import docx_io

PLACEHOLDER_RE = re.compile(
    r"\[\s*(TODO|INSERT[^\]]*|ADD[^\]]*|PLACEHOLDER|FIX THIS|TBD|XXX|YOUR[^\]]*)\s*\]",
    re.IGNORECASE,
)

_METRIC_KEYWORDS = (
    "accuracy", "precision", "recall", "f1", "f-score", "fscore", "auc", "auroc",
    "roc", "sensitivity", "specificity", "rmse", "mae", "mse", "mape", "r2",
    "r-squared", "dice", "iou", "map", "bleu", "perplexity", "kappa",
    "p-value", "p value", "correlation",
)

# A number that plausibly reports a result: a percentage, or a decimal.
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%|\b\d+\.\d+\b")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n")
# Acronyms and CamelCase tokens — dataset and model names look like this.
_PROPER_RE = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][a-z]+[A-Z][A-Za-z0-9-]*)\b")

_REQUIRED_SECTIONS = (
    ("abstract",),
    ("introduction",),
    ("conclusion", "conclusions"),
    ("reference", "references", "bibliography"),
)

_MIN_LENGTH_RATIO = 0.5


@dataclass
class ValidationResult:
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        parts = [f"ISSUE: {i}" for i in self.issues]
        parts += [f"WARNING: {w}" for w in self.warnings]
        return " | ".join(parts) if parts else "All checks passed."


_KEYWORD_RES = {
    keyword: re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
    for keyword in _METRIC_KEYWORDS
}

# Numbers that look like results but are not. Without these, section numbering and
# hyperparameters get reported as fabricated metrics and every paper is flagged —
# which would make the flag worthless.
_NOT_A_RESULT = re.compile(
    r"(?:sections?|tables?|figs?(?:ure)?s?|eqs?(?:uation)?s?|appendix|chapters?|"
    r"steps?|phases?|learning[ -]rate|lr|epochs?|batch(?:[ -]size)?|seed|version|v)"
    # Allow the connector that normally sits between the cue and its value, so
    # "learning rate of 0.001" is recognised as well as "learning rate 0.001".
    r"\s*(?:\.|:|=|was|is|were|are|of|at|set\s+to)?\s*$",
    re.IGNORECASE,
)
# A gap or delta between two reported values, e.g. "a 5.1-percentage-point gap".
# Derived from numbers already in the paper, so not a new claim.
_DERIVED_SUFFIX = re.compile(r"^\s*(?:-|\s)?(?:percentage[ -]points?|pp\b|points?\b)", re.IGNORECASE)
_HEADING_OR_LIST = re.compile(r"^\s*(?:#{1,6}\s|\d+(?:\.\d+)*[.)]\s)")
# The tail of a range such as "Sections 3.2-3.3": the cue sits before the first
# number, so strip the range start and re-test against what precedes it.
_RANGE_TAIL = re.compile(r"\d+(?:\.\d+)*\s*[‐-―-]\s*$")


def _is_cross_reference(prefix: str) -> bool:
    if _NOT_A_RESULT.search(prefix):
        return True
    trimmed = _RANGE_TAIL.sub("", prefix)
    return trimmed != prefix and _NOT_A_RESULT.search(trimmed) is not None


def _normalise_number(token: str) -> str:
    return "".join(token.split()).rstrip("%")


def all_numbers(text: str) -> set[str]:
    """Every numeric token in the text, regardless of context."""
    return {_normalise_number(m.group()) for m in _NUMBER_RE.finditer(text)}


def _floats(tokens: set[str]) -> set[float]:
    values = set()
    for token in tokens:
        try:
            values.add(float(token))
        except ValueError:
            continue
    return values


def is_known_value(token: str, original: set[float]) -> bool:
    """True if the number already appears in the original, at either scale.

    Reporting an F1 of 0.912 as "91.2%" is a presentation change, not a new result,
    so the same value at a 100x scale counts as known.
    """
    try:
        value = float(token)
    except ValueError:
        return True
    return any(
        any(abs(candidate - known) <= 1e-6 * max(1.0, abs(known)) for known in original)
        for candidate in (value, value / 100.0, value * 100.0)
    )


def metric_values(text: str) -> dict[str, set[str]]:
    """Map each metric keyword to the numbers it actually reports.

    Each number is attributed to the nearest keyword before it, falling back to the
    nearest one after. Attributing every number in a sentence to every keyword in it
    would make "accuracy was 94.2% and F1 was 0.91" claim both values for both
    metrics, and any rephrasing would then look like fabrication.
    """
    found: dict[str, set[str]] = {}
    for sentence in _SENTENCE_RE.split(text):
        if _HEADING_OR_LIST.match(sentence):
            continue
        positions = [
            (match.start(), keyword)
            for keyword, pattern in _KEYWORD_RES.items()
            for match in pattern.finditer(sentence)
        ]
        if not positions:
            continue
        positions.sort()
        for number in _NUMBER_RE.finditer(sentence):
            if _is_cross_reference(sentence[: number.start()]):
                continue
            if _DERIVED_SUFFIX.match(sentence[number.end() :]):
                continue
            before = [(pos, kw) for pos, kw in positions if pos < number.start()]
            pos, keyword = before[-1] if before else positions[0]
            found.setdefault(keyword, set()).add(_normalise_number(number.group()))
    return found


def proper_nouns(text: str, min_occurrences: int = 2) -> set[str]:
    counts: dict[str, int] = {}
    for match in _PROPER_RE.finditer(text):
        counts[match.group()] = counts.get(match.group(), 0) + 1
    return {token for token, n in counts.items() if n >= min_occurrences}


def check_placeholders(text: str) -> list[str]:
    hits = {m.group().strip() for m in PLACEHOLDER_RE.finditer(text)}
    return [f"Placeholder text left in the manuscript: {', '.join(sorted(hits))}"] if hits else []


def check_sections(text: str) -> list[str]:
    lowered = text.lower()
    missing = [
        group[0]
        for group in _REQUIRED_SECTIONS
        if not any(name in lowered for name in group)
    ]
    return [f"Expected section not found in the revised manuscript: {name}" for name in missing]


def check_metrics(original: str, revised: str) -> list[str]:
    """Reported metric values must survive revision unchanged.

    A changed or newly invented number is the signature of fabrication, which is
    the failure mode this whole pipeline is built to avoid.
    """
    original_all = all_numbers(original)
    revised_all = all_numbers(revised)
    original_floats = _floats(original_all)
    before, after = metric_values(original), metric_values(revised)
    issues = []

    # A number is only suspicious if it appears nowhere in the original, at any scale.
    # Comparing per-keyword sets instead would flag ordinary rephrasing: moving "0.912"
    # from an F1 sentence into an accuracy sentence is presentation, not fabrication.
    for keyword in sorted(after):
        invented = {v for v in after[keyword] if not is_known_value(v, original_floats)}
        if invented:
            issues.append(
                f"New {keyword} value(s) not present anywhere in the original: "
                f"{', '.join(sorted(invented))}"
            )

    for keyword in sorted(before):
        vanished = {v for v in before[keyword] if v not in revised_all}
        if vanished and len(vanished) == len(before[keyword]):
            issues.append(
                f"All reported {keyword} value(s) disappeared from the revision: "
                f"{', '.join(sorted(vanished))}"
            )
    return issues


def check_named_entities(original: str, revised: str) -> list[str]:
    lost = proper_nouns(original) - proper_nouns(revised, min_occurrences=1)
    if not lost:
        return []
    return [
        "Dataset/model name(s) from the original no longer appear in the revision: "
        + ", ".join(sorted(lost)[:10])
    ]


def check_length(original: str, revised: str) -> list[str]:
    if len(original) and len(revised) / len(original) < _MIN_LENGTH_RATIO:
        return [
            f"Revision is much shorter than the original "
            f"({len(revised)} vs {len(original)} characters) — possibly truncated."
        ]
    return []


def validate_revision(
    original_markdown: str, revised_markdown: str, final_path: Path
) -> ValidationResult:
    result = ValidationResult()

    if not final_path.exists():
        result.issues.append(f"Final file was not created: {final_path.name}")
        return result
    if final_path.stat().st_size == 0:
        result.issues.append(f"Final file is empty: {final_path.name}")
        return result
    if not docx_io.is_readable(final_path):
        result.issues.append(f"Final file is not a readable .docx: {final_path.name}")
        return result
    if not revised_markdown.strip():
        result.issues.append("Revised manuscript contains no text.")
        return result

    result.issues.extend(check_placeholders(revised_markdown))
    result.issues.extend(check_metrics(original_markdown, revised_markdown))
    result.issues.extend(check_length(original_markdown, revised_markdown))
    result.warnings.extend(check_sections(revised_markdown))
    result.warnings.extend(check_named_entities(original_markdown, revised_markdown))
    return result


def validate_folder_complete(
    original: Path, review: Path, final: Path
) -> ValidationResult:
    """Spec section 35: confirm all three documents exist and open."""
    result = ValidationResult()
    for label, path in (("original", original), ("review", review), ("final", final)):
        if not path.exists():
            result.issues.append(f"Missing {label} file: {path.name}")
        elif path.stat().st_size == 0:
            result.issues.append(f"Empty {label} file: {path.name}")
        elif path.suffix.lower() == ".docx" and not docx_io.is_readable(path):
            result.issues.append(f"Unreadable {label} .docx: {path.name}")
    return result
