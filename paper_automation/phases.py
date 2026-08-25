"""The two processing phases.

Every review finishes before any revision starts (spec section 13). The phases are
separate functions and the revision phase re-scans the month from disk, so it sees
the reviews the first phase just wrote.
"""

import hashlib
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import docx_io, prompts, report, scanner, usage, validation
from .config import Config
from .fingerprint import Fingerprint
from .models import (
    ClientResult,
    Decision,
    FailureKind,
    FolderState,
    Phase,
    ProcessingState,
    ProviderError,
    RunReport,
)
from .providers import build_providers
from .providers.base import CliProvider
from .state import StateStore
from .storage.base import StorageBackend, TargetExistsError

log = logging.getLogger(__name__)

# Failures that make every remaining client in the phase fail the same way.
_FATAL_KINDS = (FailureKind.AUTH_REQUIRED, FailureKind.USAGE_LIMIT, FailureKind.BINARY_MISSING)


class PhaseAborted(RuntimeError):
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind


def _scratch_for(cfg: Config, run_id: str, folder: FolderState, phase: Phase) -> Path:
    """A fresh working directory per client per phase.

    Kept short on purpose: Codex's Windows sandbox refuses to grant write capability
    to deep paths, so the folder name is a truncated client name plus a hash rather
    than the full employee/client pair.

    Never reused and never cleaned up automatically, because this pipeline does not
    delete files. Old run directories can be cleared by hand.
    """
    key = f"{folder.employee}/{folder.client}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    stem = "".join(c for c in folder.client if c.isalnum())[:12] or "client"
    path = cfg.scratch_dir / run_id / phase.value[:3] / f"{stem}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generate_with_retries(
    cfg: Config, provider: CliProvider, workdir: Path, prompt: str
) -> str:
    """Call the provider, retrying only failures that a retry could fix.

    Token usage for the call is recorded on the provider, where the caller can
    pick it up. Reading it here keeps the timing window tight, which is what makes
    the session log attributable to this call rather than a neighbouring one.
    """
    last: ProviderError | None = None
    for attempt in range(1, cfg.max_retries + 1):
        started = time.time()
        try:
            result = provider.generate(workdir, prompt)
            provider.last_usage = usage.read(
                provider.name, started, time.time(), provider.last_stdout
            )
            return result
        except ProviderError as exc:
            # A failed attempt still burns tokens; attribute them anyway.
            provider.last_usage = usage.read(
                provider.name, started, time.time(), provider.last_stdout
            )
            last = exc
            if exc.kind in _FATAL_KINDS:
                raise PhaseAborted(exc.kind, str(exc)) from exc
            if not exc.retryable or attempt == cfg.max_retries:
                raise
            delay = cfg.retry_base_delay * (2 ** (attempt - 1))
            log.warning(
                "Attempt %d/%d failed (%s); retrying in %.0fs",
                attempt, cfg.max_retries, exc.kind.value, delay,
            )
            time.sleep(delay)
    raise last  # unreachable; retained so the type is obvious


def build_prompt(
    store: StateStore,
    cfg: Config,
    phase: Phase,
    client: str,
    original_filename: str,
    review_date: str = "",
) -> str:
    """The admin's prompt when one is active, otherwise the built-in one.

    A stored prompt that somehow fails to render falls back to the built-in
    rather than failing the paper — validated at save time, so reaching the
    fallback means something unexpected, worth a warning but not a lost run.
    """
    row = store.active_prompt(phase, cfg.task_mode)
    if row is not None:
        try:
            return prompts.render_custom(
                row["body"], phase, client, original_filename, review_date
            )
        except (KeyError, IndexError, ValueError):
            log.warning(
                "Custom %s prompt v%s could not be rendered; using the built-in one",
                phase.value, row["version"],
            )

    review_fn, revision_fn = prompts.for_mode(cfg.task_mode)
    if phase is Phase.REVIEW:
        return review_fn(client, original_filename, review_date)
    return revision_fn(client, original_filename)


def _review_one(
    cfg: Config,
    storage: StorageBackend,
    store: StateStore,
    provider: CliProvider,
    run_id: str,
    review_date: str,
    folder: FolderState,
) -> Path:
    workdir = _scratch_for(cfg, run_id, folder, Phase.REVIEW)
    original_md = docx_io.extract(folder.original)
    (workdir / prompts.MANUSCRIPT_FILE).write_text(original_md, encoding="utf-8")

    prompt = build_prompt(
        store, cfg, Phase.REVIEW, folder.client, folder.original.name, review_date
    )
    review_md = _generate_with_retries(cfg, provider, workdir, prompt)

    staged = workdir / cfg.review_name(folder.client)
    docx_io.build(review_md, staged, title=None)
    return storage.upload_file(staged, folder.folder / cfg.review_name(folder.client))


def _revise_one(
    cfg: Config,
    storage: StorageBackend,
    store: StateStore,
    provider: CliProvider,
    run_id: str,
    folder: FolderState,
) -> tuple[Path, validation.ValidationResult]:
    workdir = _scratch_for(cfg, run_id, folder, Phase.REVISE)
    original_md = docx_io.extract(folder.original)
    review_md = docx_io.extract(folder.review)
    (workdir / prompts.MANUSCRIPT_FILE).write_text(original_md, encoding="utf-8")
    (workdir / prompts.REVIEW_FILE).write_text(review_md, encoding="utf-8")

    prompt = build_prompt(
        store, cfg, Phase.REVISE, folder.client, folder.original.name
    )
    revised_md = _generate_with_retries(cfg, provider, workdir, prompt)

    staged = workdir / cfg.final_name(folder.client)
    docx_io.build(revised_md, staged, title=None)

    result = validation.validate_revision(original_md, revised_md, staged)
    final = storage.upload_file(staged, folder.folder / cfg.final_name(folder.client))
    result.issues.extend(
        validation.validate_folder_complete(folder.original, folder.review, final).issues
    )
    return final, result


def _noop_event(kind: str, payload: dict) -> None:
    """Default progress sink. The CLI ignores events; the web UI subscribes."""


class _PhaseContext:
    """Shared, lock-guarded state for one phase's worker pool.

    Only bookkeeping (state store writes, run-report mutation, progress events)
    goes through `lock`. The slow part — the actual CLI subprocess call — runs
    outside it, which is the entire point of running folders concurrently.
    """

    def __init__(self, cfg, storage, store, run, phase, run_id, review_date, on_event):
        self.cfg = cfg
        self.storage = storage
        self.store = store
        self.run = run
        self.phase = phase
        self.run_id = run_id
        self.review_date = review_date
        self.on_event = on_event
        self.lock = threading.Lock()
        self.aborted: PhaseAborted | None = None


def _build_provider_pool(
    cfg: Config, phase: Phase, primary: CliProvider, size: int
) -> "queue.Queue[CliProvider]":
    """One independent provider instance per worker slot.

    A CliProvider stashes `last_usage`/`last_stdout` on itself after each call,
    so two workers sharing one instance would clobber each other's results.
    `primary` (already preflighted) fills the first slot; the rest are built
    fresh from the same config, which is cheap — these are thin CLI wrappers
    with no I/O in their constructor.
    """
    pool: "queue.Queue[CliProvider]" = queue.Queue()
    pool.put(primary)
    for _ in range(max(0, size - 1)):
        review_provider, revision_provider = build_providers(cfg)
        pool.put(review_provider if phase is Phase.REVIEW else revision_provider)
    return pool


def _process_folder(
    ctx: _PhaseContext, provider_pool: "queue.Queue[CliProvider]", folder: FolderState
) -> None:
    cfg, storage, store, run, phase = ctx.cfg, ctx.storage, ctx.store, ctx.run, ctx.phase
    label = "REVIEW" if phase is Phase.REVIEW else "REVISION"

    with ctx.lock:
        store.reconcile(run.month, folder)
        outcome = folder.decide(phase)

        if ctx.aborted is not None:
            reason = f"Phase stopped early: {ctx.aborted.kind.value}."
            run.results.append(ClientResult(folder.employee, folder.client, phase,
                                             ProcessingState.SKIPPED, reason))
            ctx.on_event("client_done", {"employee": folder.employee, "client": folder.client,
                                         "phase": phase.value,
                                         "state": ProcessingState.SKIPPED.value,
                                         "reason": reason, "created": ""})
            return

        if outcome.decision is Decision.COMPLETED:
            store.set_state(
                run.month, folder.employee, folder.client, ProcessingState.COMPLETED
            )
            run.results.append(ClientResult(folder.employee, folder.client, phase,
                                             ProcessingState.COMPLETED, outcome.reason))
            ctx.on_event("client_done", {"employee": folder.employee, "client": folder.client,
                                         "phase": phase.value,
                                         "state": ProcessingState.COMPLETED.value,
                                         "reason": outcome.reason, "created": ""})
            log.info("%s / %s: already complete", folder.employee, folder.client)
            return

        if outcome.decision is Decision.SKIP:
            store.record(run.month, folder, ProcessingState.SKIPPED, phase=phase,
                         message=outcome.reason)
            run.results.append(ClientResult(folder.employee, folder.client, phase,
                                             ProcessingState.SKIPPED, outcome.reason))
            ctx.on_event("client_done", {"employee": folder.employee, "client": folder.client,
                                         "phase": phase.value,
                                         "state": ProcessingState.SKIPPED.value,
                                         "reason": outcome.reason, "created": ""})
            log.info("%s / %s: SKIPPED — %s", folder.employee, folder.client, outcome.reason)
            return

        in_progress = (
            ProcessingState.REVIEW_IN_PROGRESS
            if phase is Phase.REVIEW
            else ProcessingState.REVISION_IN_PROGRESS
        )
        store.set_state(run.month, folder.employee, folder.client, in_progress,
                        bump_attempts=True)
        job_id = store.enqueue_job(run.month, folder.employee, folder.client, phase)
        store.start_job(job_id)
        ctx.on_event("client_start", {"employee": folder.employee, "client": folder.client,
                                      "phase": phase.value})

    # --- outside the lock: the part worth running concurrently ----------------
    provider = provider_pool.get()
    try:
        provider.last_usage = None
        provider.last_stdout = ""
        client_started = time.monotonic()
        log.info("%s / %s: processing with %s", folder.employee, folder.client, provider.name)

        try:
            before_fp = None
            try:
                before_fp = Fingerprint.of(folder.original)
                with ctx.lock:
                    store.record_version(
                        run.month, folder.employee, folder.client, "original", before_fp
                    )
            except OSError:
                log.warning(
                    "%s / %s: could not fingerprint the original before processing",
                    folder.employee, folder.client,
                )

            if phase is Phase.REVIEW:
                created = _review_one(
                    cfg, storage, store, provider, ctx.run_id, ctx.review_date, folder
                )
                state, reason = ProcessingState.REVIEW_COMPLETED, ""
            else:
                created, result = _revise_one(
                    cfg, storage, store, provider, ctx.run_id, folder
                )
                if result.ok:
                    state, reason = ProcessingState.COMPLETED, result.summary()
                else:
                    state = ProcessingState.REQUIRES_HUMAN_REVIEW
                    reason = result.summary()
                    log.warning(
                        "%s / %s: validation flagged — %s",
                        folder.employee, folder.client, reason,
                    )
                if result.warnings and result.ok:
                    log.info("%s / %s: %s", folder.employee, folder.client, result.summary())

            if before_fp is not None:
                try:
                    after_fp = Fingerprint.of(folder.original)
                    if after_fp.sha256 != before_fp.sha256:
                        with ctx.lock:
                            store.record_version(
                                run.month, folder.employee, folder.client,
                                "original", after_fp,
                            )
                        note = (
                            "Original paper was modified during processing; this output "
                            "reflects the version captured at the start. The updated "
                            "version will be picked up on the next eligible run."
                        )
                        reason = f"{reason} {note}".strip()
                        log.warning("%s / %s: %s", folder.employee, folder.client, note)
                except OSError:
                    pass

        except PhaseAborted as exc:
            with ctx.lock:
                if ctx.aborted is None:
                    ctx.aborted = exc
            state, reason, created = ProcessingState.FAILED, str(exc), None
            log.error(
                "%s / %s: %s — stopping the %s phase early",
                folder.employee, folder.client, exc.kind.value, label.lower(),
            )
        except TargetExistsError as exc:
            state, reason, created = ProcessingState.SKIPPED, str(exc), None
            log.warning("%s / %s: %s", folder.employee, folder.client, exc)
        except (ProviderError, docx_io.DocxError) as exc:
            state, reason, created = ProcessingState.FAILED, str(exc), None
            log.error("%s / %s: FAILED — %s", folder.employee, folder.client, exc)
        except Exception as exc:  # one bad client must not end the run (spec section 24)
            state, reason, created = ProcessingState.FAILED, repr(exc), None
            log.exception("%s / %s: unexpected failure", folder.employee, folder.client)

        elapsed = time.monotonic() - client_started
        spent = provider.last_usage
        tokens = spent.billable_total if spent else 0
        if spent:
            log.info(
                "%s / %s: used ~%s tokens", folder.employee, folder.client,
                usage.humanise(tokens),
            )
    finally:
        provider_pool.put(provider)

    with ctx.lock:
        if spent and spent.limit_used_percent is not None:
            run.limit_used_percent = spent.limit_used_percent
        run.tokens += tokens
        run.seconds += elapsed

        store.set_state(run.month, folder.employee, folder.client, state, reason)
        store.record(run.month, folder, state, phase=phase, model=provider.model_label,
                     file_path=created, message=reason, tokens=tokens,
                     seconds=elapsed, task_mode=cfg.task_mode)
        store.finish_job(job_id, state, reason)
        run.results.append(
            ClientResult(folder.employee, folder.client, phase, state, reason,
                         created, tokens, elapsed)
        )
        ctx.on_event("client_done", {"employee": folder.employee, "client": folder.client,
                                     "phase": phase.value, "state": state.value,
                                     "reason": reason, "tokens": tokens,
                                     "seconds": elapsed,
                                     "limit_percent": (
                                         spent.limit_used_percent if spent else None
                                     ),
                                     "created": created.name if created else ""})
        if created:
            log.info("%s / %s: created %s", folder.employee, folder.client, created.name)


def _run_phase(
    cfg: Config,
    storage: StorageBackend,
    store: StateStore,
    provider: CliProvider,
    run: RunReport,
    phase: Phase,
    states: list[FolderState],
    run_id: str,
    on_event=_noop_event,
) -> None:
    label = "REVIEW" if phase is Phase.REVIEW else "REVISION"
    log.info("=== %s PHASE (%s) ===", label, provider.name)
    on_event("phase_start", {"phase": phase.value, "provider": provider.name,
                             "clients": len(states)})

    review_date = datetime.now(ZoneInfo(cfg.timezone)).strftime("%d-%b-%Y")
    ctx = _PhaseContext(cfg, storage, store, run, phase, run_id, review_date, on_event)

    max_workers = max(1, cfg.max_concurrent_jobs)
    provider_pool = _build_provider_pool(cfg, phase, provider, min(max_workers, len(states) or 1))

    # A ThreadPoolExecutor context manager waits for every submitted task before
    # returning, so review folders are always fully finished before run() moves
    # on to build the revision phase's fresh scan (spec section 13), regardless
    # of how many workers ran concurrently within this phase.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_process_folder, ctx, provider_pool, folder)
            for folder in states
        ]
        for future in futures:
            future.result()


def run(
    cfg: Config,
    storage: StorageBackend,
    month: str,
    month_dir: Path,
    states: list[FolderState],
    phase_arg: str,
    on_event=_noop_event,
) -> int:
    now = datetime.now(ZoneInfo(cfg.timezone))
    run_id = now.strftime("%Y%m%d-%H%M%S")
    run_report = RunReport(
        month=month,
        started=now.strftime("%d-%b-%Y %H:%M:%S"),
        employees=len({s.employee for s in states}),
        clients=len(states),
    )

    review_provider, revision_provider = build_providers(cfg)

    with StateStore(cfg.state_db) as store:
        requeued = store.requeue_stale_processing_jobs()
        if requeued:
            log.warning(
                "Requeued %d job(s) left in progress by an interrupted run", requeued
            )
        try:
            if phase_arg in ("review", "both"):
                review_provider.preflight()
                _run_phase(cfg, storage, store, review_provider, run_report,
                           Phase.REVIEW, states, run_id, on_event)

            if phase_arg in ("revise", "both"):
                revision_provider.preflight()
                # Fresh scan so this phase sees the reviews just written (spec section 14).
                revise_states = scanner.scan_month(cfg, storage, month_dir)
                _run_phase(cfg, storage, store, revision_provider, run_report,
                           Phase.REVISE, revise_states, run_id, on_event)

        except ProviderError as exc:
            log.error("Cannot start: %s", exc)
            run_report.finished = datetime.now(ZoneInfo(cfg.timezone)).strftime(
                "%d-%b-%Y %H:%M:%S"
            )
            print(report.render(run_report))
            return 2

    run_report.finished = datetime.now(ZoneInfo(cfg.timezone)).strftime("%d-%b-%Y %H:%M:%S")
    summary = report.render(run_report)
    print(summary)
    log.info("Run finished")
    on_event("finished", {"summary": summary})

    if cfg.notify.enabled:
        from . import notify

        notify.send(cfg, summary, run_report)

    return 1 if report.has_failures(run_report) else 0
