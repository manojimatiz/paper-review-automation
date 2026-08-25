"""State-database backups: only ever creates, never deletes (spec section 44)."""

from paper_automation import backup


def test_backup_of_a_missing_database_returns_none(tmp_path):
    assert backup.backup_state_db(tmp_path / "nope.sqlite3", tmp_path / "backups") is None


def test_backup_copies_the_database(tmp_path):
    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"pretend sqlite content")
    target = backup.backup_state_db(db, tmp_path / "backups")
    assert target is not None
    assert target.exists()
    assert target.read_bytes() == b"pretend sqlite content"


def test_backup_leaves_the_original_untouched(tmp_path):
    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"original content")
    backup.backup_state_db(db, tmp_path / "backups")
    assert db.exists()
    assert db.read_bytes() == b"original content"


def test_backup_filename_is_timestamped(tmp_path):
    db = tmp_path / "processing.sqlite3"
    db.write_bytes(b"x")
    target = backup.backup_state_db(db, tmp_path / "backups")
    assert target.name.startswith("processing-")
    assert target.suffix == ".sqlite3"


def test_repeated_backups_never_overwrite_or_delete_earlier_ones(tmp_path):
    import time

    db = tmp_path / "state.sqlite3"
    backup_dir = tmp_path / "backups"

    db.write_bytes(b"version one")
    first = backup.backup_state_db(db, backup_dir)

    time.sleep(1.05)  # the timestamp has second resolution
    db.write_bytes(b"version two")
    second = backup.backup_state_db(db, backup_dir)

    assert first != second
    assert first.exists() and second.exists()
    assert first.read_bytes() == b"version one"
    assert second.read_bytes() == b"version two"
    assert len(list(backup_dir.glob("*.sqlite3"))) == 2


def test_latest_backup_is_none_when_none_exist(tmp_path):
    assert backup.latest_backup(tmp_path / "backups") is None


def test_latest_backup_returns_the_newest(tmp_path):
    import time

    db = tmp_path / "state.sqlite3"
    backup_dir = tmp_path / "backups"
    db.write_bytes(b"1")
    first = backup.backup_state_db(db, backup_dir)
    time.sleep(1.05)
    db.write_bytes(b"2")
    second = backup.backup_state_db(db, backup_dir)

    assert backup.latest_backup(backup_dir) == second
    assert backup.latest_backup(backup_dir) != first


# --- config -----------------------------------------------------------------


def test_backup_dir_defaults_relative_to_state_db(tmp_path):
    from paper_automation import config as config_module

    path = tmp_path / "config.toml"
    path.write_text(
        f'research_papers_root = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    cfg = config_module.load(path, base_dir=tmp_path)
    assert cfg.backup_dir == tmp_path / "state" / "backups"


def test_backup_dir_can_be_configured(tmp_path):
    from paper_automation import config as config_module

    path = tmp_path / "config.toml"
    path.write_text(
        f'research_papers_root = "{tmp_path.as_posix()}"\n'
        f'backup_dir = "custom-backups"\n',
        encoding="utf-8",
    )
    cfg = config_module.load(path, base_dir=tmp_path)
    assert cfg.backup_dir == tmp_path / "custom-backups"


# --- run.py --backup-now ----------------------------------------------------


def test_run_backup_now_creates_a_backup(cfg, storage):
    import run as run_module
    from paper_automation.state import StateStore

    with StateStore(cfg.state_db):
        pass  # just needs the schema to exist on disk

    exit_code = run_module.main([
        "--config", str(_write_cfg(cfg)),
        "--backup-now",
    ])
    assert exit_code == 0
    assert list(cfg.backup_dir.glob("*.sqlite3"))


def test_run_backup_now_on_a_fresh_install_does_not_fail(cfg):
    import run as run_module

    exit_code = run_module.main(["--config", str(_write_cfg(cfg)), "--backup-now"])
    assert exit_code == 0


def _write_cfg(cfg) -> "object":
    from pathlib import Path

    path = cfg.research_papers_root.parent / "config.toml"
    path.write_text(
        f'research_papers_root = "{cfg.research_papers_root.as_posix()}"\n'
        f'state_db = "{Path(cfg.state_db).as_posix()}"\n'
        f'log_dir = "{Path(cfg.log_dir).as_posix()}"\n'
        f'backup_dir = "{Path(cfg.backup_dir).as_posix()}"\n',
        encoding="utf-8",
    )
    return path
