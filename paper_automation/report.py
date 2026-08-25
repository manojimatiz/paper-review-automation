"""Daily summary (spec sections 26 and 27)."""

from .models import Phase, ProcessingState, RunReport

_SUCCESS = {
    Phase.REVIEW: (ProcessingState.REVIEW_COMPLETED,),
    Phase.REVISE: (ProcessingState.COMPLETED, ProcessingState.REQUIRES_HUMAN_REVIEW),
}


def has_failures(run: RunReport) -> bool:
    return any(r.state is ProcessingState.FAILED for r in run.results)


def needs_human_review(run: RunReport) -> list:
    return [r for r in run.results if r.state is ProcessingState.REQUIRES_HUMAN_REVIEW]


def _phase_block(run: RunReport, phase: Phase, title: str, created_label: str) -> list[str]:
    results = run.for_phase(phase)
    processed = run.count(phase, *_SUCCESS[phase])
    return [
        title,
        "-" * len(title),
        f"Processed: {processed}",
        f"Skipped:   {run.count(phase, ProcessingState.SKIPPED)}",
        f"Failed:    {run.count(phase, ProcessingState.FAILED)}",
        f"Already complete: {run.count(phase, ProcessingState.COMPLETED)}"
        if phase is Phase.REVIEW
        else f"Flagged for human review: {run.count(phase, ProcessingState.REQUIRES_HUMAN_REVIEW)}",
        f"{created_label}: {sum(1 for r in results if r.created)}",
        "",
    ]


def render(run: RunReport) -> str:
    lines = [
        "=" * 48,
        "DAILY RESEARCH PAPER AUTOMATION REPORT",
        "=" * 48,
        "",
        f"Month:      {run.month}",
        f"Start Time: {run.started}",
        f"End Time:   {run.finished}",
        "",
        f"Employee Folders: {run.employees}",
        f"Client Folders:   {run.clients}",
        "",
    ]
    lines += _phase_block(run, Phase.REVIEW, "REVIEW STAGE (Codex)", "Reviews Created")
    lines += _phase_block(
        run, Phase.REVISE, "REVISION STAGE (Claude)", "Final Papers Created"
    )

    completed = run.count(Phase.REVISE, ProcessingState.COMPLETED)
    flagged = needs_human_review(run)
    failed = [r for r in run.results if r.state is ProcessingState.FAILED]

    lines += [
        f"COMPLETED: {completed}",
        f"REQUIRES HUMAN REVIEW: {len(flagged)}",
        f"FAILED: {len(failed)}",
        "",
    ]

    if run.seconds:
        from .service import format_duration

        lines.append(f"Time taken: {format_duration(run.seconds)}")
    if run.tokens:
        lines.append(f"Tokens used (approx): {run.tokens:,}")
    if run.limit_used_percent is not None:
        lines.append(
            f"Subscription window used: {run.limit_used_percent:.0f}%"
        )
    if run.tokens or run.seconds or run.limit_used_percent is not None:
        lines.append("")

    if flagged:
        lines.append("FLAGGED FOR HUMAN REVIEW")
        lines.append("-" * 24)
        for r in flagged:
            lines.append(f"  {r.employee} / {r.client}")
            lines.append(f"    {r.reason}")
        lines.append("")

    if failed:
        lines.append("FAILURES")
        lines.append("-" * 8)
        for r in failed:
            lines.append(f"  {r.employee} / {r.client} [{r.phase.value}]")
            lines.append(f"    {r.reason}")
        lines.append("")

    skipped = [r for r in run.results if r.state is ProcessingState.SKIPPED]
    if skipped:
        lines.append("SKIPPED")
        lines.append("-" * 7)
        for r in skipped:
            lines.append(f"  {r.employee} / {r.client} [{r.phase.value}] — {r.reason}")
        lines.append("")

    lines.append("=" * 48)
    return "\n".join(lines)
