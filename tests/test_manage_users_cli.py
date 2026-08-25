"""manage_users.py CLI: create, approve/reject, reset, disable/enable."""

from pathlib import Path

import manage_users as cli
from paper_automation import auth


def _write_config(tmp_path: Path) -> Path:
    root = tmp_path / "Papers"
    root.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        f'research_papers_root = "{root.as_posix()}"\n'
        f'state_db = "{(tmp_path / "state.sqlite3").as_posix()}"\n',
        encoding="utf-8",
    )
    return path, tmp_path / "state.sqlite3"


def test_add_creates_an_active_account_with_no_password_prompt(tmp_path, capsys):
    config_path, db = _write_config(tmp_path)
    code = cli.main(["--config", str(config_path), "add", "alice", "--role", "ADMIN"])
    assert code == 0
    assert auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD) is not None
    assert "active immediately" in capsys.readouterr().out


def test_add_writer_requires_employee(tmp_path, capsys):
    config_path, db = _write_config(tmp_path)
    code = cli.main(["--config", str(config_path), "add", "bob", "--role", "WRITER"])
    assert code == 1
    assert "employee folder" in capsys.readouterr().err


def test_list_shows_pending_and_active(tmp_path, capsys):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    cli.main(["--config", str(config_path), "list"])
    out = capsys.readouterr().out
    assert "alice" in out and "active" in out
    assert "bob" in out and "pending" in out


def test_approve_activates_a_pending_request(tmp_path, capsys):
    config_path, db = _write_config(tmp_path)
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    code = cli.main(["--config", str(config_path), "approve", "bob"])
    assert code == 0
    assert auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD) is not None


def test_reject_disables_a_pending_request(tmp_path):
    config_path, db = _write_config(tmp_path)
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    code = cli.main(["--config", str(config_path), "reject", "bob"])
    assert code == 0
    assert [u for u in auth.list_users(db) if u.username == "bob"][0].disabled is True


def test_reset_password_routes_back_through_forced_change(tmp_path):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "realpassword1")
    code = cli.main(["--config", str(config_path), "reset-password", "alice"])
    assert code == 0
    user = auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD)
    assert user is not None and user.must_change_password is True


def test_disable_then_enable(tmp_path):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    cli.main(["--config", str(config_path), "disable", "alice"])
    assert auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD) is None
    cli.main(["--config", str(config_path), "enable", "alice"])
    assert auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD) is not None


def test_passwd_prompts_and_sets_an_exact_password(tmp_path, monkeypatch):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    monkeypatch.setattr(cli, "_prompt_password", lambda: "handpicked99")
    code = cli.main(["--config", str(config_path), "passwd", "alice"])
    assert code == 0
    assert auth.authenticate(db, "alice", "handpicked99") is not None


def test_delete_removes_the_account(tmp_path):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    code = cli.main(["--config", str(config_path), "delete", "bob"])
    assert code == 0
    assert [u.username for u in auth.list_users(db)] == ["alice"]


def test_delete_refuses_the_last_admin(tmp_path, capsys):
    config_path, db = _write_config(tmp_path)
    auth.create_user(db, "alice", auth.Role.ADMIN)
    code = cli.main(["--config", str(config_path), "delete", "alice"])
    assert code == 1
    assert "last remaining Admin" in capsys.readouterr().err
    assert [u.username for u in auth.list_users(db)] == ["alice"]


def test_error_on_unknown_user_is_reported(tmp_path, capsys):
    config_path, _db = _write_config(tmp_path)
    code = cli.main(["--config", str(config_path), "approve", "ghost"])
    assert code == 1
    assert "No such user" in capsys.readouterr().err
