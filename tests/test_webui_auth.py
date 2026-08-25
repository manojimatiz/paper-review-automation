"""Web access control: who can see and do what once logins exist.

Every check here is server-side. Hiding a button in a template is presentation,
not security — a writer must be refused even when calling the API directly.
"""

import pytest

flask = pytest.importorskip("flask", reason="web UI is optional")
pytest.importorskip("flask_login", reason="web UI login is optional")

from paper_automation import auth  # noqa: E402


@pytest.fixture
def webapp(cfg, monkeypatch):
    """The Flask app wired to the temporary config, with logins in play."""
    from paper_automation import config as config_module
    from webui import app as webapp_module

    monkeypatch.setattr(webapp_module, "current_config", lambda: cfg)
    monkeypatch.setattr(config_module, "load", lambda path=None, base_dir=None: cfg)
    monkeypatch.setattr(webapp_module, "_state_db", lambda: cfg.state_db)
    webapp_module.app.config["TESTING"] = True
    webapp_module.app.secret_key = "test-key"
    return webapp_module


@pytest.fixture
def accounts(cfg):
    """Two already-settled accounts (past the forced first-password-change),
    so the tests in this file can exercise ordinary post-login behavior
    without every one of them re-deriving the sign-up/approval flow — that
    flow has its own dedicated tests further down."""
    auth.create_user(cfg.state_db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(cfg.state_db, "alice", "adminpass123")
    auth.create_user(cfg.state_db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.complete_first_login(cfg.state_db, "bob", "writerpass123")


def sign_in(webapp, username, password):
    client = webapp.app.test_client()
    client.post("/login", data={"username": username, "password": password})
    return client


# --- no accounts: the historical local single-user mode -----------------------


def test_without_accounts_no_login_is_required(webapp, make_client):
    make_client("Vani.docx")
    assert webapp.auth_enabled() is False
    assert webapp.app.test_client().get("/").status_code == 200


def test_without_accounts_settings_is_reachable(webapp):
    assert webapp.app.test_client().get("/settings").status_code == 200


# --- with accounts: login required -------------------------------------------


def test_anonymous_is_redirected_to_login(webapp, accounts):
    assert webapp.app.test_client().get("/").status_code == 302


def test_anonymous_api_is_refused(webapp, accounts):
    assert webapp.app.test_client().get("/api/scan").status_code == 302


def test_login_page_renders(webapp, accounts):
    res = webapp.app.test_client().get("/login")
    assert res.status_code == 200
    assert b"Sign in" in res.data


def test_wrong_password_shows_one_generic_message(webapp, accounts):
    res = webapp.app.test_client().post(
        "/login", data={"username": "bob", "password": "wrong"}
    )
    assert b"Incorrect username or password" in res.data


def test_unknown_user_gets_the_same_message(webapp, accounts):
    """Identical to a wrong password, so usernames cannot be enumerated."""
    res = webapp.app.test_client().post(
        "/login", data={"username": "ghost", "password": "wrong"}
    )
    assert b"Incorrect username or password" in res.data


def test_successful_login_reaches_the_dashboard(webapp, accounts, make_client):
    make_client("Vani.docx")
    client = sign_in(webapp, "alice", "adminpass123")
    assert client.get("/").status_code == 200


def test_logout_ends_the_session(webapp, accounts):
    client = sign_in(webapp, "alice", "adminpass123")
    client.get("/logout")
    assert client.get("/").status_code == 302


def test_disabled_account_loses_access_immediately(webapp, accounts, cfg):
    client = sign_in(webapp, "bob", "writerpass123")
    assert client.get("/").status_code == 200
    auth.set_disabled(cfg.state_db, "bob", True)
    assert client.get("/").status_code == 302


# --- writer restrictions -----------------------------------------------------


def test_writer_sees_only_their_own_employee(webapp, accounts, make_client):
    make_client("Impana.docx", employee="Suchitra", client="Impana")
    make_client("Vani.docx", employee="Janani", client="Vani")

    client = sign_in(webapp, "bob", "writerpass123")
    rows = client.get("/api/scan").get_json()["rows"]
    assert sorted({r["employee"] for r in rows}) == ["Suchitra"]


def test_admin_sees_every_employee(webapp, accounts, make_client):
    make_client("Impana.docx", employee="Suchitra", client="Impana")
    make_client("Vani.docx", employee="Janani", client="Vani")

    client = sign_in(webapp, "alice", "adminpass123")
    rows = client.get("/api/scan").get_json()["rows"]
    assert sorted({r["employee"] for r in rows}) == ["Janani", "Suchitra"]


def test_writer_cannot_open_settings(webapp, accounts):
    assert sign_in(webapp, "bob", "writerpass123").get("/settings").status_code == 403


def test_writer_cannot_open_the_prompt_editor(webapp, accounts):
    assert sign_in(webapp, "bob", "writerpass123").get("/prompts").status_code == 403


def test_writer_cannot_open_the_jobs_page(webapp, accounts):
    assert sign_in(webapp, "bob", "writerpass123").get("/jobs").status_code == 403


def test_admin_can_open_the_jobs_page(webapp, accounts):
    assert sign_in(webapp, "alice", "adminpass123").get("/jobs").status_code == 200


def test_writer_cannot_save_a_prompt_through_the_api(webapp, accounts):
    client = sign_in(webapp, "bob", "writerpass123")
    res = client.post("/prompts", data={"phase": "review", "action": "save", "body": "x"})
    assert res.status_code == 403


def test_writer_cannot_start_a_run_through_the_api(webapp, accounts, make_client):
    """The button is hidden for writers, but the API is what actually matters."""
    make_client("Vani.docx")
    client = sign_in(webapp, "bob", "writerpass123")
    assert client.post("/api/run", json={"phase": "both"}).status_code == 403


def test_writer_cannot_change_the_schedule(webapp, accounts):
    client = sign_in(webapp, "bob", "writerpass123")
    assert client.post("/api/schedule", json={"enabled": True}).status_code == 403


def test_writer_cannot_trigger_a_backup(webapp, accounts):
    client = sign_in(webapp, "bob", "writerpass123")
    assert client.post("/api/backup", json={}).status_code == 403


def test_writer_cannot_create_a_folder(webapp, accounts, cfg):
    client = sign_in(webapp, "bob", "writerpass123")
    res = client.post("/api/create-folder", json={"month": "August 2026", "employee": "Someone"})
    assert res.status_code == 403
    assert not (cfg.research_papers_root / "August 2026" / "Someone").exists()


def test_writer_cannot_open_another_employees_file(webapp, accounts, make_client):
    folder = make_client("Vani.docx", employee="Janani", client="Vani")
    client = sign_in(webapp, "bob", "writerpass123")
    res = client.post("/api/open", json={"path": str(folder / "Vani.docx")})
    assert res.status_code == 403


def test_writer_dashboard_hides_the_add_folder_panel(webapp, accounts, make_client):
    make_client("Impana.docx", employee="Suchitra", client="Impana")
    body = sign_in(webapp, "bob", "writerpass123").get("/").data
    assert b"addFolderForm" not in body


def test_admin_dashboard_shows_the_add_folder_panel(webapp, accounts, make_client):
    make_client("Vani.docx")
    body = sign_in(webapp, "alice", "adminpass123").get("/").data
    assert b"addFolderForm" in body


def test_writer_dashboard_hides_the_run_controls(webapp, accounts, make_client):
    make_client("Impana.docx", employee="Suchitra", client="Impana")
    client = sign_in(webapp, "bob", "writerpass123")
    assert b"btnStart" not in client.get("/").data


def test_admin_dashboard_keeps_the_run_controls(webapp, accounts, make_client):
    make_client("Vani.docx")
    client = sign_in(webapp, "alice", "adminpass123")
    assert b"btnStart" in client.get("/").data


def test_writer_is_shown_no_link_to_a_page_they_cannot_open(webapp, accounts, make_client):
    """A visible link to /settings would only ever produce a 403."""
    make_client("Impana.docx", employee="Suchitra", client="Impana")
    body = sign_in(webapp, "bob", "writerpass123").get("/").data
    assert b"/settings" not in body


def test_admin_is_shown_the_settings_link(webapp, accounts, make_client):
    make_client("Vani.docx")
    body = sign_in(webapp, "alice", "adminpass123").get("/").data
    assert b"/settings" in body


# --- network exposure gate ---------------------------------------------------


def test_refuses_to_serve_the_lan_without_any_accounts(webapp, cfg):
    """The core safety rule: an unauthenticated panel must never be reachable
    from other machines."""
    cfg.web_host = "0.0.0.0"
    with pytest.raises(SystemExit) as excinfo:
        webapp.main(port=5099, open_browser=False)
    assert excinfo.value.code == 2


def test_localhost_without_accounts_is_still_allowed(webapp, cfg, monkeypatch):
    """Local single-user mode stays zero-setup — no login, no refusal."""
    served = {}
    monkeypatch.setattr("waitress.serve", lambda app, **kw: served.update(kw))
    cfg.web_host = "127.0.0.1"
    webapp.main(port=5099, open_browser=False)
    assert served["host"] == "127.0.0.1"


def test_lan_binding_is_allowed_once_accounts_exist(webapp, cfg, accounts, monkeypatch):
    served = {}
    monkeypatch.setattr(
        "waitress.serve", lambda app, **kw: served.update(kw)
    )
    cfg.web_host = "0.0.0.0"
    webapp.main(port=5099, open_browser=False)
    assert served["host"] == "0.0.0.0"


# --- path scoping helper -----------------------------------------------------


def test_path_belongs_to_employee_accepts_their_own_folder(cfg, make_client):
    from paper_automation import service

    folder = make_client("Impana.docx", employee="Suchitra", client="Impana")
    assert service.path_belongs_to_employee(
        folder / "Impana.docx", cfg, "Suchitra"
    ) is True


def test_path_belongs_to_employee_rejects_another_folder(cfg, make_client):
    from paper_automation import service

    folder = make_client("Vani.docx", employee="Janani", client="Vani")
    assert service.path_belongs_to_employee(
        folder / "Vani.docx", cfg, "Suchitra"
    ) is False


def test_path_belongs_to_employee_rejects_a_traversal_attempt(cfg, make_client):
    """A ".." path must not climb out of the writer's folder into another."""
    from paper_automation import service

    make_client("Impana.docx", employee="Suchitra", client="Impana")
    other = make_client("Vani.docx", employee="Janani", client="Vani")
    sneaky = (
        cfg.research_papers_root / "August 2026" / "Suchitra" / ".."
        / "Janani" / "Vani" / "Vani.docx"
    )
    assert other.exists()
    assert service.path_belongs_to_employee(sneaky, cfg, "Suchitra") is False


def test_path_outside_the_root_is_rejected(cfg, tmp_path):
    from paper_automation import service

    assert service.path_belongs_to_employee(
        tmp_path / "elsewhere.docx", cfg, "Suchitra"
    ) is False


def test_writer_history_is_scoped_to_their_employee(webapp, accounts, cfg, tmp_path):
    from paper_automation.models import FolderState, Phase, ProcessingState
    from paper_automation.state import StateStore

    with StateStore(cfg.state_db) as store:
        for employee in ("Suchitra", "Janani"):
            folder = FolderState(employee=employee, client="C", folder=tmp_path)
            store.record("August 2026", folder, ProcessingState.COMPLETED,
                         phase=Phase.REVIEW)

    client = sign_in(webapp, "bob", "writerpass123")
    body = client.get("/history").data
    assert b"Suchitra" in body
    assert b"Janani" not in body
