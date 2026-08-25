"""run.py's CLI wiring: --loop delegates to the scheduler, everything else doesn't."""

from pathlib import Path

import run as run_module


def _write_config(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'research_papers_root = "{root.as_posix()}"\n', encoding="utf-8")
    return path


def test_parse_args_recognizes_loop():
    args = run_module.parse_args(["--loop"])
    assert args.loop is True


def test_loop_defaults_to_false():
    args = run_module.parse_args([])
    assert args.loop is False


def test_main_with_loop_calls_the_scheduler(tmp_path, monkeypatch):
    root = tmp_path / "Papers"
    root.mkdir()
    config_path = _write_config(tmp_path, root)

    calls = []
    monkeypatch.setattr(
        "paper_automation.scheduler.run_loop",
        lambda cfg, storage, args, run_once, **kw: calls.append("looped") or 0,
    )

    exit_code = run_module.main(["--config", str(config_path), "--loop"])
    assert calls == ["looped"]
    assert exit_code == 0


def test_main_without_loop_never_calls_the_scheduler(tmp_path, monkeypatch):
    root = tmp_path / "Papers"
    root.mkdir()
    config_path = _write_config(tmp_path, root)

    calls = []
    monkeypatch.setattr(
        "paper_automation.scheduler.run_loop",
        lambda *a, **kw: calls.append("looped") or 0,
    )

    run_module.main(["--config", str(config_path), "--dry-run"])
    assert calls == []


def test_main_with_loop_and_dry_run_ignores_loop(tmp_path, monkeypatch):
    """Dry-run stays a single scan-and-print, per run_once's own short-circuit."""
    root = tmp_path / "Papers"
    root.mkdir()
    config_path = _write_config(tmp_path, root)

    calls = []
    monkeypatch.setattr(
        "paper_automation.scheduler.run_loop",
        lambda *a, **kw: calls.append("looped") or 0,
    )

    exit_code = run_module.main(["--config", str(config_path), "--dry-run", "--loop"])
    assert calls == []
    assert exit_code == 0
