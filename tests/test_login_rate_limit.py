"""Login throttling: slows down password guessing (spec section 10, hardening)."""

import pytest

flask = pytest.importorskip("flask", reason="web UI is optional")
pytest.importorskip("flask_login", reason="web UI login is optional")

from webui.app import _LoginRateLimiter  # noqa: E402
from paper_automation import auth  # noqa: E402


# --- the limiter in isolation (a fresh instance per test, no shared state) ---


def test_not_blocked_before_any_failures():
    limiter = _LoginRateLimiter()
    assert limiter.blocked("1.2.3.4") is False


def test_blocked_after_max_failures():
    limiter = _LoginRateLimiter()
    for _ in range(_LoginRateLimiter.MAX_FAILURES):
        limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is True


def test_not_yet_blocked_one_short_of_the_max():
    limiter = _LoginRateLimiter()
    for _ in range(_LoginRateLimiter.MAX_FAILURES - 1):
        limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is False


def test_different_ips_are_tracked_separately():
    limiter = _LoginRateLimiter()
    for _ in range(_LoginRateLimiter.MAX_FAILURES):
        limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is True
    assert limiter.blocked("5.6.7.8") is False


def test_clear_resets_the_count():
    limiter = _LoginRateLimiter()
    for _ in range(_LoginRateLimiter.MAX_FAILURES):
        limiter.record_failure("1.2.3.4")
    limiter.clear("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is False


def test_old_failures_outside_the_window_do_not_count(monkeypatch):
    limiter = _LoginRateLimiter()
    import time as time_module

    t = [1000.0]
    monkeypatch.setattr(time_module, "time", lambda: t[0])
    for _ in range(_LoginRateLimiter.MAX_FAILURES):
        limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4") is True

    t[0] += _LoginRateLimiter.WINDOW_SECONDS + 1
    assert limiter.blocked("1.2.3.4") is False


# --- wired into the /login route ---------------------------------------------


@pytest.fixture
def webapp(cfg, monkeypatch):
    from paper_automation import config as config_module
    from webui import app as webapp_module

    monkeypatch.setattr(webapp_module, "current_config", lambda: cfg)
    monkeypatch.setattr(config_module, "load", lambda path=None, base_dir=None: cfg)
    monkeypatch.setattr(webapp_module, "_state_db", lambda: cfg.state_db)
    # A fresh limiter per test — the real one is a module-level singleton and
    # must not accumulate failures across unrelated tests.
    monkeypatch.setattr(webapp_module, "_login_limiter", _LoginRateLimiter())
    webapp_module.app.config["TESTING"] = True
    webapp_module.app.secret_key = "test-key"
    return webapp_module


@pytest.fixture
def accounts(cfg):
    auth.create_user(cfg.state_db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(cfg.state_db, "alice", "adminpass123")


def test_repeated_wrong_passwords_eventually_get_blocked(webapp, accounts):
    client = webapp.app.test_client()
    last = None
    for _ in range(_LoginRateLimiter.MAX_FAILURES + 1):
        last = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert b"Too many attempts" in last.data


def test_a_blocked_ip_cannot_log_in_even_with_the_right_password(webapp, accounts):
    client = webapp.app.test_client()
    for _ in range(_LoginRateLimiter.MAX_FAILURES):
        client.post("/login", data={"username": "alice", "password": "wrong"})
    res = client.post("/login", data={"username": "alice", "password": "adminpass123"})
    assert b"Too many attempts" in res.data


def test_a_successful_login_clears_the_failure_count(webapp, accounts):
    client = webapp.app.test_client()
    for _ in range(_LoginRateLimiter.MAX_FAILURES - 1):
        client.post("/login", data={"username": "alice", "password": "wrong"})
    ok = client.post("/login", data={"username": "alice", "password": "adminpass123"})
    assert ok.status_code == 302  # redirected to the dashboard, not blocked
