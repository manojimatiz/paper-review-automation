"""Entry point for the nightly research-paper automation.

    py run.py                      # normal scheduled run (scans once, exits)
    py run.py --dry-run            # report decisions, change nothing
    py run.py --test-mode          # one employee, one client, mock model
    py run.py --phase review       # run a single phase
    py run.py --month "July 2026"  # re-run a specific month
    py run.py --loop               # stay resident, rescanning inside the
                                    # configured processing window (see
                                    # schedule_start/schedule_end/scan_interval_minutes
                                    # in config.toml)
"""

import argparse
import logging
import sys
from pathlib import Path

from paper_automation import config as config_module
from paper_automation import logging_setup, scanner
from paper_automation.models import Decision, Phase
from paper_automation.storage import build_storage

log = logging.getLogger("run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report decisions without creating or modifying any file",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Limit to the first employee and client, and use the mock model",
    )
    parser.add_argument(
        "--phase",
        choices=["review", "revise", "both"],
        default="both",
        help="Which phase to run (default: both, review first)",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "mock", "real"],
        default=None,
        help="Force the mock or real models (default: mock in test mode, real otherwise)",
    )
    parser.add_argument("--month", help='Override the month folder, e.g. "August 2026"')
    parser.add_argument(
        "--backup-now",
        action="store_true",
        help=(
            "Copy the state database (job history, prompts, accounts) to "
            "backup_dir with a timestamp, then exit. Nothing else runs. "
            "Register this as its own separate Task Scheduler entry for "
            "regular backups."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Stay resident and rescan every scan_interval_minutes inside the "
            "configured processing window, instead of scanning once and exiting"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def render_dry_run(states, month: str) -> str:
    """Spec section 45: say what would happen, touch nothing."""
    lines = [
        "=" * 64,
        "DRY RUN — no files will be created or modified",
        "=" * 64,
        f"Month: {month}",
        "",
    ]
    if not states:
        lines.append("No client folders found.")
        return "\n".join(lines)

    employee = None
    for state in states:
        if state.employee != employee:
            employee = state.employee
            lines.append(f"[{employee}]")
        review = state.decide(Phase.REVIEW)
        revise = state.decide(Phase.REVISE)

        if review.decision is Decision.COMPLETED:
            action = "already complete — skip"
        elif review.decision is Decision.PROCESS:
            action = "would send to Codex for review"
        elif revise.decision is Decision.PROCESS:
            action = "would send to Claude for revision"
        else:
            action = f"skip — {review.reason}"

        counts = (
            f"{len(state.originals)} original / {len(state.reviews)} review "
            f"/ {len(state.finals)} final"
        )
        lines.append(f"  {state.client:<24} {counts:<34} -> {action}")
        if state.ignored:
            names = ", ".join(p.name for p in state.ignored)
            lines.append(f"  {'':<24} ignored: {names}")
    return "\n".join(lines)


def run_once(cfg, storage, args) -> int:
    """One scan-and-process cycle. Called once by a normal run, or repeatedly
    by the --loop scheduler (paper_automation.scheduler)."""
    month = args.month or scanner.current_month(cfg)
    log.info("Target month: %s (timezone %s)", month, cfg.timezone)

    if not storage.find_folder(cfg.research_papers_root):
        log.error(
            "research_papers_root does not exist: %s", cfg.research_papers_root
        )
        return 2

    month_dir = scanner.month_folder(cfg, storage, month)
    if month_dir is None:
        if not cfg.create_missing_month:
            # Spec section 30: never create it unless the admin opted in.
            log.warning(
                "Current month folder not found: %s", cfg.research_papers_root / month
            )
            log.warning("Automation terminated safely.")
            return 0
        month_dir = cfg.research_papers_root / month
        storage.create_folder(month_dir)
        log.info("Created the month folder: %s", month_dir)

    states = scanner.scan_month(cfg, storage, month_dir)
    log.info(
        "Found %d client folder(s) across %d employee folder(s)",
        len(states),
        len({s.employee for s in states}),
    )

    if cfg.dry_run:
        print(render_dry_run(states, month))
        return 0

    from paper_automation import phases

    return phases.run(cfg, storage, month, month_dir, states, args.phase)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        cfg.dry_run = True
    if args.test_mode:
        cfg.test_mode = True
    if args.provider:
        cfg.provider_mode = args.provider

    log_file = logging_setup.setup(cfg.log_dir, cfg.timezone, args.verbose)
    log.info("Run started; log file: %s", log_file)
    if cfg.dry_run:
        log.info("DRY RUN — no files will be written")
    if cfg.test_mode:
        log.info("TEST MODE — first employee and client only, mock model")

    storage = build_storage(cfg)

    if args.backup_now:
        from paper_automation import backup

        target = backup.backup_state_db(cfg.state_db, cfg.backup_dir)
        if target is None:
            log.info("Nothing to back up yet: %s does not exist.", cfg.state_db)
        else:
            log.info("Backed up %s to %s", cfg.state_db, target)
            print(f"Backed up to: {target}")
        return 0

    if args.loop and cfg.dry_run:
        log.info("--loop has no effect under --dry-run; running a single scan instead.")
        args.loop = False

    if args.loop:
        from paper_automation import scheduler

        return scheduler.run_loop(cfg, storage, args, run_once)

    return run_once(cfg, storage, args)


if __name__ == "__main__":
    raise SystemExit(main())
