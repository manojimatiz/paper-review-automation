"""Accounts, password handling, and the role rules the web UI enforces."""

import sqlite3

import pytest

from paper_automation import auth


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite3"


# --- create_user(): the optional, admin-direct path — immediately active ----


def test_no_users_on_a_fresh_install(db):
    assert auth.any_users_exist(db) is False


def test_creating_a_user_makes_logins_required(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    assert auth.any_users_exist(db) is True


def test_create_user_is_immediately_active(db):
    """No queue for the admin-direct path — the admin is vouching for it."""
    auth.create_user(db, "alice", auth.Role.ADMIN)
    user = auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD)
    assert user is not None and user.username == "alice"


def test_create_user_forces_a_password_change(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    user = auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD)
    assert user.must_change_password is True


def test_password_is_stored_hashed_not_in_plaintext(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        "SELECT password_hash FROM user WHERE username=?", ("alice",)
    ).fetchone()["password_hash"]
    conn.close()
    assert auth.DEFAULT_PASSWORD not in stored
    assert len(stored) > 40


def test_duplicate_username_is_refused(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(db, "alice", auth.Role.ADMIN)


def test_writer_without_an_employee_is_refused(db):
    """A User account with no employee could not be scoped to their own papers."""
    with pytest.raises(auth.AuthError, match="employee folder"):
        auth.create_user(db, "bob", auth.Role.WRITER)


def test_empty_username_is_refused(db):
    with pytest.raises(auth.AuthError, match="cannot be empty"):
        auth.create_user(db, "   ", auth.Role.ADMIN)


# --- request_signup(): the public, self-service path — pending, no password -


def test_signup_creates_a_pending_account(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    accounts = auth.list_users(db)
    assert len(accounts) == 1
    assert accounts[0].approved is False


def test_signup_stamps_the_request_time(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT claim_requested_at FROM user WHERE username=?", ("bob",)).fetchone()
    conn.close()
    assert row["claim_requested_at"] != ""


def test_signup_has_no_usable_password_before_approval(db):
    """Not even the eventual default password works yet — there is genuinely
    no credential until an admin approves."""
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    with pytest.raises(auth.PendingApprovalError):
        auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD)


def test_pending_login_is_refused_regardless_of_the_password_typed(db):
    """The whole point: approval is checked before the password is."""
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    for attempt in ("wrong", "", auth.DEFAULT_PASSWORD, "anything at all"):
        with pytest.raises(auth.PendingApprovalError):
            auth.authenticate(db, "bob", attempt)


def test_pending_account_password_check_is_never_reached(db, monkeypatch):
    """Proves the check-order claim, not just the observable behavior."""
    called = []
    monkeypatch.setattr(
        "paper_automation.auth.check_password_hash",
        lambda *a: called.append(1) or True,
    )
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    with pytest.raises(auth.PendingApprovalError):
        auth.authenticate(db, "bob", "whatever")
    assert called == []


def test_signup_requires_an_employee_for_a_writer_request(db):
    with pytest.raises(auth.AuthError, match="employee folder"):
        auth.request_signup(db, "bob", auth.Role.WRITER)


def test_signup_does_not_require_an_employee_for_an_admin_request(db):
    auth.request_signup(db, "carl", auth.Role.ADMIN)  # must not raise


def test_signup_rejects_a_duplicate_username(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.request_signup(db, "alice", auth.Role.WRITER, "Suchitra")


# --- approve_user() / reject_user() ------------------------------------------


def test_approving_assigns_the_default_password(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    auth.approve_user(db, "bob")
    user = auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD)
    assert user is not None
    assert user.must_change_password is True


def test_approving_an_unknown_user_is_refused(db):
    with pytest.raises(auth.AuthError, match="No such user"):
        auth.approve_user(db, "ghost")


def test_rejecting_disables_without_ever_creating_a_password(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    auth.reject_user(db, "bob")
    with pytest.raises(auth.PendingApprovalError):
        # still not approved — reject just disables, it doesn't "unpend" it
        auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD)


def test_rejected_account_is_kept_not_deleted(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    auth.reject_user(db, "bob")
    assert [u.username for u in auth.list_users(db)] == ["bob"]


# --- authentication (approved accounts) --------------------------------------


def test_correct_password_authenticates(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    user = auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD)
    assert user is not None and user.username == "alice"


def test_wrong_password_is_refused(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    assert auth.authenticate(db, "alice", "wrong") is None


def test_unknown_user_and_wrong_password_are_indistinguishable(db):
    """Both return a bare None, so a stranger cannot enumerate usernames."""
    auth.create_user(db, "alice", auth.Role.ADMIN)
    assert auth.authenticate(db, "alice", "wrong") is None
    assert auth.authenticate(db, "nobody-at-all", "wrong") is None


def test_authenticate_against_a_missing_database_is_safe(tmp_path):
    assert auth.authenticate(tmp_path / "nope.sqlite3", "alice", "x") is None


def test_disabled_account_cannot_log_in(db):
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.set_disabled(db, "bob", True)
    assert auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD) is None


def test_re_enabling_restores_access(db):
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.set_disabled(db, "bob", True)
    auth.set_disabled(db, "bob", False)
    assert auth.authenticate(db, "bob", auth.DEFAULT_PASSWORD) is not None


def test_disabling_keeps_the_record(db):
    """Access is revoked, but the account is never deleted."""
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.set_disabled(db, "bob", True)
    assert [u.username for u in auth.list_users(db)] == ["bob"]


def test_load_user_refuses_a_disabled_account(db):
    """Revoking access takes effect on the next request, not at session expiry."""
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    assert auth.load_user(db, "bob") is not None
    auth.set_disabled(db, "bob", True)
    assert auth.load_user(db, "bob") is None


def test_load_user_refuses_a_pending_account(db):
    auth.request_signup(db, "bob", auth.Role.WRITER, "Suchitra")
    assert auth.load_user(db, "bob") is None


# --- password changes ---------------------------------------------------------


def test_complete_first_login_clears_the_flag(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "brandnewpass9")
    user = auth.authenticate(db, "alice", "brandnewpass9")
    assert user.must_change_password is False


def test_complete_first_login_requires_the_flag_to_be_set(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "brandnewpass9")
    with pytest.raises(auth.AuthError, match="already set"):
        auth.complete_first_login(db, "alice", "anotherpass8")


def test_complete_first_login_does_not_check_a_current_password(db):
    """No current-password field on this flow — the fresh session is the proof."""
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "brandnewpass9")  # no current password given
    assert auth.authenticate(db, "alice", "brandnewpass9") is not None


def test_change_own_password_requires_the_current_one(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "firstpass99")
    with pytest.raises(auth.AuthError, match="incorrect"):
        auth.change_own_password(db, "alice", "wrongcurrent", "secondpass99")


def test_change_own_password_succeeds_with_the_right_current_one(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "firstpass99")
    auth.change_own_password(db, "alice", "firstpass99", "secondpass99")
    assert auth.authenticate(db, "alice", "secondpass99") is not None
    assert auth.authenticate(db, "alice", "firstpass99") is None


def test_reset_to_default_password_routes_back_through_the_forced_flow(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.complete_first_login(db, "alice", "firstpass99")
    auth.reset_to_default_password(db, "alice")
    user = auth.authenticate(db, "alice", auth.DEFAULT_PASSWORD)
    assert user is not None
    assert user.must_change_password is True


def test_set_password_admin_override(db):
    """set_password() is the admin-hand-picks-a-password path — still clears
    must_change_password, since the admin chose a real password directly."""
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.set_password(db, "alice", "handpicked99")
    user = auth.authenticate(db, "alice", "handpicked99")
    assert user.must_change_password is False


def test_setting_a_short_password_is_refused(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    with pytest.raises(auth.AuthError, match="at least"):
        auth.set_password(db, "alice", "tiny")


def test_password_change_for_unknown_user_is_refused(db):
    with pytest.raises(auth.AuthError, match="No such user"):
        auth.set_password(db, "ghost", "supersecret2")


# --- delete_user() --------------------------------------------------------------


def test_delete_user_removes_the_row_entirely(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.delete_user(db, "bob")
    assert [u.username for u in auth.list_users(db)] == ["alice"]


def test_delete_user_of_an_unknown_account_is_refused(db):
    with pytest.raises(auth.AuthError, match="No such user"):
        auth.delete_user(db, "ghost")


def test_delete_refuses_the_last_active_admin(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    with pytest.raises(auth.AuthError, match="last remaining Admin"):
        auth.delete_user(db, "alice")
    assert [u.username for u in auth.list_users(db)] == ["alice"]


def test_delete_allows_an_admin_when_another_admin_remains(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.create_user(db, "carl", auth.Role.ADMIN)
    auth.delete_user(db, "alice")
    assert [u.username for u in auth.list_users(db)] == ["carl"]


def test_delete_allows_a_writer_even_when_they_are_the_only_writer(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    auth.delete_user(db, "bob")  # must not raise — the guard is admin-only
    assert [u.username for u in auth.list_users(db)] == ["alice"]


def test_delete_allows_a_disabled_admin_even_if_it_was_the_only_active_one(db):
    """A disabled admin doesn't count toward "last active admin" — it's
    already useless for logging in, so deleting it changes nothing."""
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.set_disabled(db, "alice", True)
    auth.delete_user(db, "alice")  # must not raise the last-admin guard
    assert auth.list_users(db) == []


def test_delete_allows_a_pending_admin_request(db):
    """A not-yet-approved Admin request was never usable, so it doesn't
    count toward "last active admin" either."""
    auth.request_signup(db, "carl", auth.Role.ADMIN)
    auth.delete_user(db, "carl")  # must not raise
    assert auth.list_users(db) == []


# --- rename_user() -------------------------------------------------------------


def test_rename_user(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.rename_user(db, "alice", "alicia")
    assert auth.authenticate(db, "alicia", auth.DEFAULT_PASSWORD) is not None
    assert auth.load_user(db, "alice") is None


def test_rename_to_an_existing_name_is_refused(db):
    auth.create_user(db, "alice", auth.Role.ADMIN)
    auth.create_user(db, "bob", auth.Role.WRITER, employee="Suchitra")
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.rename_user(db, "alice", "bob")


def test_rename_of_an_unknown_user_is_refused(db):
    with pytest.raises(auth.AuthError, match="No such user"):
        auth.rename_user(db, "ghost", "someone")


# --- roles ---------------------------------------------------------------------


def test_admin_sees_everything_and_can_control_runs():
    user = auth.User("alice", auth.Role.ADMIN)
    assert user.employee_filter is None
    assert user.can_control_runs is True
    assert user.is_admin is True


def test_writer_is_scoped_and_cannot_control_runs():
    user = auth.User("bob", auth.Role.WRITER, employee="Suchitra")
    assert user.employee_filter == "Suchitra"
    assert user.can_control_runs is False


def test_writer_without_an_employee_fails_closed():
    """Falls back to the username, which matches no folder — rather than None,
    which would mean 'show every employee'."""
    user = auth.User("bob", auth.Role.WRITER, employee="")
    assert user.employee_filter == "bob"
    assert user.employee_filter is not None


def test_only_two_roles_exist():
    assert {r.value for r in auth.Role} == {"ADMIN", "WRITER"}
