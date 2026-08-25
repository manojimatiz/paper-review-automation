"""The --loop scan window: a virtual clock drives it so no test sleeps for real."""

from datetime import datetime, time, timedelta

from paper_automation import config as config_module
from paper_automation import scheduler


def _cfg(tmp_path, **overrides):
    return config_module.Config(
        research_papers_root=tmp_path,
        state_db=tmp_path / "state.sqlite3",
        log_dir=tmp_path / "logs",
        scratch_dir=tmp_path / "scratch",
        **overrides,
    )


def _virtual_clock(start: datetime):
    clock = {"now": start}

    def now_fn():
        return clock["now"]

    def sleep_fn(seconds):
        clock["now"] += timedelta(seconds=seconds)

    return now_fn, sleep_fn, clock


def test_run_once_is_never_called_outside_the_window(tmp_path):
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(18, 0))
    now_fn, sleep_fn, _ = _virtual_clock(datetime(2026, 8, 24, 19, 0))
    calls = []

    exit_code = scheduler.run_loop(
        cfg, None, None, lambda c, s, a: (calls.append(1), 0)[1],
        now_fn=now_fn, sleep_fn=sleep_fn,
    )
    assert calls == []
    assert exit_code == 0


def test_run_loop_waits_for_the_window_to_open(tmp_path):
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(9, 1),
               scan_interval_minutes=15)
    now_fn, sleep_fn, clock = _virtual_clock(datetime(2026, 8, 24, 8, 0))
    calls = []

    def fake_run_once(c, s, a):
        calls.append(clock["now"])
        return 0

    scheduler.run_loop(cfg, None, None, fake_run_once, now_fn=now_fn, sleep_fn=sleep_fn)
    assert len(calls) == 1
    assert calls[0] == datetime(2026, 8, 24, 9, 0)


def test_run_loop_runs_multiple_cycles_inside_the_window(tmp_path):
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(9, 46),
               scan_interval_minutes=15)
    now_fn, sleep_fn, clock = _virtual_clock(datetime(2026, 8, 24, 9, 0))
    calls = []

    def fake_run_once(c, s, a):
        calls.append(clock["now"])
        return 0

    scheduler.run_loop(cfg, None, None, fake_run_once, now_fn=now_fn, sleep_fn=sleep_fn)
    assert calls == [
        datetime(2026, 8, 24, 9, 0),
        datetime(2026, 8, 24, 9, 15),
        datetime(2026, 8, 24, 9, 30),
        datetime(2026, 8, 24, 9, 45),
    ]


def test_run_loop_stops_starting_new_cycles_once_the_window_closes(tmp_path):
    """It never calls run_once again after schedule_end - the "don't kill work in
    progress, just stop starting new work" rule (spec section 11)."""
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(9, 10),
               scan_interval_minutes=15)
    now_fn, sleep_fn, clock = _virtual_clock(datetime(2026, 8, 24, 9, 0))
    calls = []

    def fake_run_once(c, s, a):
        calls.append(clock["now"])
        return 0

    scheduler.run_loop(cfg, None, None, fake_run_once, now_fn=now_fn, sleep_fn=sleep_fn)
    assert len(calls) == 1  # only the 9:00 cycle; 9:15 would already be past 9:10


def test_run_loop_returns_the_last_cycles_exit_code(tmp_path):
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(9, 16),
               scan_interval_minutes=15)
    now_fn, sleep_fn, _ = _virtual_clock(datetime(2026, 8, 24, 9, 0))
    codes = iter([0, 1])

    result = scheduler.run_loop(
        cfg, None, None, lambda c, s, a: next(codes), now_fn=now_fn, sleep_fn=sleep_fn
    )
    assert result == 1


def test_run_loop_passes_cfg_storage_args_through(tmp_path):
    cfg = _cfg(tmp_path, schedule_start=time(9, 0), schedule_end=time(9, 1))
    now_fn, sleep_fn, _ = _virtual_clock(datetime(2026, 8, 24, 9, 0))
    seen = []

    def fake_run_once(c, s, a):
        seen.append((c, s, a))
        return 0

    storage_sentinel = object()
    args_sentinel = object()
    scheduler.run_loop(cfg, storage_sentinel, args_sentinel, fake_run_once,
                        now_fn=now_fn, sleep_fn=sleep_fn)
    assert seen == [(cfg, storage_sentinel, args_sentinel)]
