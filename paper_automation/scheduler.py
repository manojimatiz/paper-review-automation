"""In-app processing window and periodic scan loop (spec sections 11-12, 41).

Windows Task Scheduler (scripts/register_task.ps1) remains the single source of
*when this process starts* — its daily trigger is unchanged. What this module adds
is that, under `py run.py --loop`, the process stays resident for the configured
window instead of exiting after one scan, so a paper dropped mid-morning is picked
up without waiting for tomorrow's trigger. A one-shot `py run.py` (no --loop)
never touches this module.

The window is a soft boundary: it stops *starting new scan cycles* once
schedule_end passes, but a cycle already in progress (phases.run(), which itself
may be mid-revision) is never interrupted — spec section 11.
"""

import logging
import time as time_module
from datetime import datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config

log = logging.getLogger(__name__)


def _seconds_until(now: datetime, target: time) -> float:
    target_dt = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    return max(0.0, (target_dt - now).total_seconds())


def run_loop(
    cfg: Config,
    storage,
    args,
    run_once: Callable[[Config, object, object], int],
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] = time_module.sleep,
) -> int:
    """Repeatedly call `run_once(cfg, storage, args)` inside the configured window.

    `now_fn`/`sleep_fn` are injectable so tests can drive the loop without a real
    clock; production callers leave them at their defaults.
    """
    tz = ZoneInfo(cfg.timezone)
    now_fn = now_fn or (lambda: datetime.now(tz))
    start, end = cfg.schedule_start, cfg.schedule_end
    interval_seconds = cfg.scan_interval_minutes * 60

    now = now_fn()
    if now.time() >= end:
        log.info(
            "Outside the processing window (%s-%s); nothing to do.",
            start.strftime("%H:%M"), end.strftime("%H:%M"),
        )
        return 0

    if now.time() < start:
        wait = _seconds_until(now, start)
        log.info(
            "Waiting %.0f minute(s) for the processing window to open at %s",
            wait / 60, start.strftime("%H:%M"),
        )
        sleep_fn(wait)

    exit_code = 0
    cycle = 0
    while True:
        now = now_fn()
        if now.time() >= end:
            log.info(
                "Processing window closed at %s; stopping. Any work already in "
                "progress was left to finish, not interrupted.",
                end.strftime("%H:%M"),
            )
            break

        cycle += 1
        log.info(
            "=== Scan cycle %d (window %s-%s) ===",
            cycle, start.strftime("%H:%M"), end.strftime("%H:%M"),
        )
        exit_code = run_once(cfg, storage, args)

        if now_fn().time() >= end:
            log.info("Processing window closed at %s; stopping.", end.strftime("%H:%M"))
            break
        sleep_fn(interval_seconds)

    return exit_code
