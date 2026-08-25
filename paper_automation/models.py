"""Core data types and the folder-eligibility rules."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ProcessingState(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    REVIEW_IN_PROGRESS = "REVIEW_IN_PROGRESS"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    PENDING_REVISION = "PENDING_REVISION"
    REVISION_IN_PROGRESS = "REVISION_IN_PROGRESS"
    REVISION_COMPLETED = "REVISION_COMPLETED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    COMPLETED = "COMPLETED"
    # Job-queue-only states (paper_automation.state's job table); a folder's own
    # client_state row never holds these two.
    QUEUED = "QUEUED"
    CANCELLED = "CANCELLED"


class Priority(str, Enum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class FileRole(str, Enum):
    ORIGINAL = "original"
    REVIEW = "review"
    FINAL = "final"


class Decision(str, Enum):
    """What a phase should do with a client folder."""

    PROCESS = "PROCESS"
    SKIP = "SKIP"
    COMPLETED = "COMPLETED"


class Phase(str, Enum):
    REVIEW = "review"
    REVISE = "revise"


class FailureKind(str, Enum):
    """Why a provider call failed. Drives retry policy and the daily report."""

    TRANSIENT = "TRANSIENT"
    TIMEOUT = "TIMEOUT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    USAGE_LIMIT = "USAGE_LIMIT"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    BINARY_MISSING = "BINARY_MISSING"
    UNKNOWN = "UNKNOWN"


class ProviderError(RuntimeError):
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in (FailureKind.TRANSIENT, FailureKind.TIMEOUT)


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    reason: str


@dataclass
class FolderState:
    """The classified contents of one client folder.

    Files are grouped by *role*, never by raw count, so an arbitrary pair of
    documents can never be mistaken for an original-plus-review pair.
    """

    employee: str
    client: str
    folder: Path
    originals: list[Path] = field(default_factory=list)
    reviews: list[Path] = field(default_factory=list)
    finals: list[Path] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)
    # True when >1 original candidates were found and at least two are byte-identical
    # (spec section 16). Set by scanner.classify_folder, which has file access; this
    # dataclass stays otherwise IO-free so it can be built and tested in memory.
    duplicate_originals: bool = False

    @property
    def original(self) -> Path:
        return self.originals[0]

    @property
    def review(self) -> Path:
        return self.reviews[0]

    @property
    def counted(self) -> int:
        return len(self.originals) + len(self.reviews) + len(self.finals)

    @property
    def is_complete(self) -> bool:
        return (
            len(self.originals) == 1
            and len(self.reviews) == 1
            and len(self.finals) == 1
        )

    def decide(self, phase: Phase) -> Outcome:
        return (
            self._decide_review() if phase is Phase.REVIEW else self._decide_revise()
        )

    def _decide_review(self) -> Outcome:
        if self.is_complete:
            return Outcome(Decision.COMPLETED, "Original, review and final all present.")
        if self.finals:
            return Outcome(
                Decision.SKIP,
                f"Unexpected state: {len(self.finals)} final file(s) present with "
                f"{len(self.originals)} original(s) and {len(self.reviews)} review(s).",
            )
        if self.reviews:
            return Outcome(
                Decision.SKIP,
                "Review already exists; nothing to do in the review phase.",
            )
        if not self.originals:
            return Outcome(
                Decision.SKIP, "No supported research paper found; expected exactly 1."
            )
        if len(self.originals) > 1:
            reason = (
                f"{len(self.originals)} candidate papers found; expected exactly 1. "
                "Refusing to guess which is the original."
            )
            if self.duplicate_originals:
                reason += " At least two appear to be exact duplicates (identical content)."
            return Outcome(Decision.SKIP, reason)
        return Outcome(Decision.PROCESS, "Exactly 1 original paper and no review.")

    def _decide_revise(self) -> Outcome:
        if self.is_complete:
            return Outcome(Decision.COMPLETED, "Original, review and final all present.")
        if self.finals:
            return Outcome(
                Decision.SKIP,
                f"Unexpected state: {len(self.finals)} final file(s) already present.",
            )
        if not self.originals:
            return Outcome(Decision.SKIP, "No original paper found; expected exactly 1.")
        if not self.reviews:
            return Outcome(
                Decision.SKIP,
                "Review was not generated; expected exactly 1 original + 1 review.",
            )
        if len(self.originals) > 1 or len(self.reviews) > 1:
            reason = (
                f"Ambiguous folder: {len(self.originals)} original(s) and "
                f"{len(self.reviews)} review(s); expected exactly 1 of each."
            )
            if self.duplicate_originals:
                reason += " At least two originals appear to be exact duplicates (identical content)."
            return Outcome(Decision.SKIP, reason)
        return Outcome(Decision.PROCESS, "Exactly 1 original paper and 1 review.")


@dataclass
class ClientResult:
    """The recorded outcome of one client in one phase."""

    employee: str
    client: str
    phase: Phase
    state: ProcessingState
    reason: str = ""
    created: Path | None = None
    tokens: int = 0
    seconds: float = 0.0


@dataclass
class RunReport:
    month: str
    started: str = ""
    finished: str = ""
    employees: int = 0
    clients: int = 0
    tokens: int = 0
    seconds: float = 0.0
    # Percentage of the subscription window consumed, when a tool reports it.
    limit_used_percent: float | None = None
    results: list[ClientResult] = field(default_factory=list)

    def for_phase(self, phase: Phase) -> list[ClientResult]:
        return [r for r in self.results if r.phase is phase]

    def count(self, phase: Phase, *states: ProcessingState) -> int:
        return sum(1 for r in self.for_phase(phase) if r.state in states)
