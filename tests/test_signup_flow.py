"""End-to-end web flow: /signup -> admin approval -> forced first login ->
/users management. Complements test_auth.py's lower-level auth.py tests and
test_webui_auth.py's already-settled-account access-control tests.
"""

import pytest

flask = pytest.importorskip("flask", reason="web UI is optional")
pytest.importorskip("flask_login", reason="web UI login is optional")

from paper_automation import auth  # noqa: E402


@pytest.fixture
def webapp(cfg, monkeypatch):
    from paper_automation import config as config_module
    from webui import app as webapp_module

    monkeypatch.setattr(webapp_module, "current_config", lambda: cfg)
    monkeypatch.setattr(config_module, "load", lambda path=None, base_dir=None: cfg)
    monkeypatch.setattr(webapp_module, "_state_db", lambda: cfg.state_db)
    webapp_module.app.config["TESTING"] = True
    webapp_module.app.secret_key = "test-key"
    return webapp_module


@pytest.fixture
def admin(cfg, webapp):
    """A settled admin account, signed in, ready to approve requests."""
    auth.create_user(cfg.state_db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(cfg.state_db, "alice", "adminpass123")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "alice", "password": "adminpass123"})
    return client


# --- /signup -------------------------------------------------------------------


def test_signup_page_renders_when_logged_out(webapp):
    res = webapp.app.test_client().get("/signup")
    assert res.status_code == 200
    assert b"Request an account" in res.data


def test_signup_submits_a_pending_request(webapp, cfg):
    res = webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    assert b"Request sent to admin" in res.data
    accounts = auth.list_users(cfg.state_db)
    assert len(accounts) == 1
    assert accounts[0].approved is False


def test_signup_does_not_start_a_session(webapp, cfg):
    client = webapp.app.test_client()
    client.post("/signup", data={"username": "bob", "role": "WRITER", "employee": "Suchitra"})
    # A pending account cannot be "logged in" — confirm no session leaked one.
    assert client.get("/").status_code in (302, 200)  # redirected to /login, not the dashboard
    body = client.get("/", follow_redirects=True).data
    assert b"Sign in" in body


def test_signup_requires_an_employee_for_a_writer_request(webapp):
    res = webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "",
    })
    assert b"employee folder" in res.data
    assert b"Request sent to admin" not in res.data


def test_signup_duplicate_username_shows_an_error(webapp, cfg):
    auth.create_user(cfg.state_db, "bob", auth.Role.WRITER, employee="Suchitra")
    res = webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    assert b"already exists" in res.data


# --- login on a pending account -------------------------------------------------


def test_login_on_a_pending_account_shows_approval_pending(webapp, cfg):
    webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    res = webapp.app.test_client().post("/login", data={
        "username": "bob", "password": auth.DEFAULT_PASSWORD,
    })
    assert b"waiting for an administrator" in res.data


def test_login_on_a_pending_account_is_refused_with_any_password(webapp, cfg):
    webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    res = webapp.app.test_client().post("/login", data={
        "username": "bob", "password": "literally anything",
    })
    assert b"waiting for an administrator" in res.data


# --- /users: admin visibility and actions ---------------------------------------


def test_users_page_requires_admin(webapp, cfg):
    auth.create_user(cfg.state_db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.complete_first_login(cfg.state_db, "bob", "writerpass123")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "bob", "password": "writerpass123"})
    assert client.get("/users").status_code == 403


def test_users_page_lists_a_pending_request(admin, webapp, cfg):
    webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    body = admin.get("/users").data
    assert b"bob" in body
    assert b"Pending" in body


def test_admin_approves_a_pending_request(admin, webapp, cfg):
    webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    res = admin.post("/users", data={"action": "approve", "username": "bob"})
    assert b"Approved bob" in res.data

    fresh = webapp.app.test_client()
    login_res = fresh.post("/login", data={"username": "bob", "password": auth.DEFAULT_PASSWORD})
    assert login_res.status_code == 302  # succeeded, redirected onward


def test_admin_rejects_a_pending_request(admin, webapp, cfg):
    webapp.app.test_client().post("/signup", data={
        "username": "bob", "role": "WRITER", "employee": "Suchitra",
    })
    admin.post("/users", data={"action": "reject", "username": "bob"})
    fresh = webapp.app.test_client()
    res = fresh.post("/login", data={"username": "bob", "password": auth.DEFAULT_PASSWORD})
    assert b"waiting for an administrator" in res.data or b"Incorrect" in res.data


def test_admin_adds_a_user_directly_and_it_is_immediately_active(admin, webapp, cfg):
    res = admin.post("/users", data={
        "action": "add", "username": "carol", "role": "WRITER", "employee": "Priya",
    })
    assert b"active immediately" in res.data
    fresh = webapp.app.test_client()
    login_res = fresh.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    assert login_res.status_code == 302


def _find(cfg, username):
    return next(u for u in auth.list_users(cfg.state_db) if u.username == username)


def test_admin_disables_and_enables_a_user(admin, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    admin.post("/users", data={"action": "disable", "username": "carol"})
    assert _find(cfg, "carol").disabled is True
    admin.post("/users", data={"action": "enable", "username": "carol"})
    assert _find(cfg, "carol").disabled is False


def test_admin_renames_a_user(admin, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    res = admin.post("/users", data={
        "action": "rename", "username": "carol", "new_username": "caroline",
    })
    assert b"Renamed carol to caroline" in res.data
    assert {u.username for u in auth.list_users(cfg.state_db)} == {"alice", "caroline"}


def test_admin_deletes_a_user(admin, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    res = admin.post("/users", data={"action": "delete", "username": "carol"})
    assert b"Deleted carol" in res.data
    assert {u.username for u in auth.list_users(cfg.state_db)} == {"alice"}


def test_admin_cannot_delete_the_last_admin_through_the_web_ui(admin, cfg):
    res = admin.post("/users", data={"action": "delete", "username": "alice"})
    assert b"last remaining Admin" in res.data
    assert [u.username for u in auth.list_users(cfg.state_db)] == ["alice"]


def test_writer_cannot_delete_a_user(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    auth.complete_first_login(cfg.state_db, "carol", "carolspassword1")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": "carolspassword1"})
    res = client.post("/users", data={"action": "delete", "username": "carol"})
    assert res.status_code == 403
    assert {u.username for u in auth.list_users(cfg.state_db)} == {"carol"}


def test_admin_resets_a_users_password(admin, cfg, webapp):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    auth.complete_first_login(cfg.state_db, "carol", "carolspassword1")
    admin.post("/users", data={"action": "reset-password", "username": "carol"})

    fresh = webapp.app.test_client()
    res = fresh.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    assert res.status_code == 302  # default password works again


# --- the forced first-login password-change popup -------------------------------


def test_forced_popup_appears_instead_of_the_dashboard(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    body = client.get("/", follow_redirects=True).data
    assert b"Set your own password" in body


def test_forced_popup_blocks_every_other_route(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    for path in ("/", "/history"):
        res = client.get(path)
        assert res.status_code == 302
        assert res.headers["Location"].endswith("/account")


def test_forced_popup_blocks_api_routes_too(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    assert client.get("/api/status").status_code == 403


def test_submitting_a_new_password_reaches_the_dashboard(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    res = client.post("/account", data={
        "new_password": "carolsownpass1", "confirm_password": "carolsownpass1",
    })
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/")

    body = client.get("/").data
    assert b"Set your own password" not in body


def test_mismatched_confirmation_is_refused(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    res = client.post("/account", data={
        "new_password": "carolsownpass1", "confirm_password": "somethingelse",
    })
    assert b"do not match" in res.data


def test_new_password_does_not_take_effect_until_confirmed_correctly(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": auth.DEFAULT_PASSWORD})
    client.post("/account", data={
        "new_password": "carolsownpass1", "confirm_password": "somethingelse",
    })
    fresh = webapp.app.test_client()
    still_default = fresh.post("/login", data={
        "username": "carol", "password": auth.DEFAULT_PASSWORD,
    })
    assert still_default.status_code == 302  # default password still works


# --- the ongoing, voluntary /account change (once settled) ----------------------


def test_voluntary_change_requires_the_current_password(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    auth.complete_first_login(cfg.state_db, "carol", "carolsownpass1")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": "carolsownpass1"})
    res = client.post("/account", data={
        "current_password": "wrongone",
        "new_password": "newerpass2", "confirm_password": "newerpass2",
    })
    assert b"incorrect" in res.data


def test_voluntary_change_succeeds_with_the_right_current_password(webapp, cfg):
    auth.create_user(cfg.state_db, "carol", auth.Role.WRITER, employee="Priya")
    auth.complete_first_login(cfg.state_db, "carol", "carolsownpass1")
    client = webapp.app.test_client()
    client.post("/login", data={"username": "carol", "password": "carolsownpass1"})
    res = client.post("/account", data={
        "current_password": "carolsownpass1",
        "new_password": "newerpass2", "confirm_password": "newerpass2",
    })
    assert b"Password updated" in res.data


def test_account_page_in_local_mode_redirects_to_dashboard(webapp):
    """No accounts configured — nothing to manage."""
    client = webapp.app.test_client()
    res = client.get("/account")
    assert res.status_code == 302
