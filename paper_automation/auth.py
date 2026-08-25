"""User accounts and authentication (spec section 40).

UI-agnostic, like service.py: no Flask import belongs here. The web UI wraps
these functions with Flask-Login; manage_users.py calls the same ones from the
command line.

ACCOUNT LIFECYCLE
-----------------
Two ways an account comes into existence:

* `request_signup()` — the public, self-service path (`/signup`). Creates a
  PENDING account with no usable password at all: `password_hash` is a hash
  of a random, unrecoverable token, not DEFAULT_PASSWORD, because there is
  genuinely no credential yet. `authenticate()` refuses a pending account
  before it ever looks at the submitted password — the only response is
  "approval pending," for any password whatsoever.
* `create_user()` — the optional, admin-direct path (`manage_users.py add`,
  or the admin "Add user" form). Immediately active, immediately assigned
  DEFAULT_PASSWORD, because the admin is vouching for the account by creating
  it themselves — there is nothing to queue.

`approve_user()` is the only place a pending account's password_hash is ever
set to something real (DEFAULT_PASSWORD). From that point on it behaves like
any admin-created account: it must change that password on first login
(`complete_first_login()`), enforced by the web layer's must-change-password
gate, before it can do anything else.

SECURITY NOTES
--------------
* Passwords are only ever stored as a salted hash (werkzeug's PBKDF2 by
  default) — never in plaintext, never reversibly encrypted, and never logged.
* `authenticate()` returns the same None for "no such user" and "wrong
  password", so a failed login cannot be used to enumerate which usernames
  exist. A pending account is a distinct, explicit `PendingApprovalError`
  rather than folded into that None, because there is no password to have
  gotten right or wrong yet — the distinction is not an information leak
  about credentials, only about queue state.
* A disabled account is refused at login but its row is kept, so revoking
  access does not erase the record that it existed. Rejecting a signup
  request works the same way: disabled=True. `delete_user()` is the one
  genuinely destructive operation in this module — a deliberate, explicit
  admin action (real "Delete" button on `/users`, or
  `manage_users.py delete`), not something any other flow does implicitly.
  It refuses to remove the last remaining active Admin, so it can't be used
  to lock everyone out.
* DEFAULT_PASSWORD is a fixed, publicly-documented string. It is only ever
  live on an account between `approve_user()`/`create_user()` and that
  account's first login — `must_change_password` makes every other route
  unreachable until it's replaced. Still, anyone who knows a newly-approved
  username before they've logged in for the first time could log in ahead of
  them. This is an accepted trade-off for a LAN-only tool, not an oversight.
* This module gates the web UI only. It is not a substitute for the network
  boundary: keep `web_host` at 127.0.0.1 unless logins are actually in use.
"""

import secrets
from enum import Enum
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from .state import StateStore

MIN_PASSWORD_LENGTH = 8

# The fixed starting password assigned the moment an account becomes usable
# (admin-direct creation, or signup approval). Never assigned to a pending
# account — see the module docstring's ACCOUNT LIFECYCLE section.
DEFAULT_PASSWORD = "iMatiz"


class Role(str, Enum):
    ADMIN = "ADMIN"
    WRITER = "WRITER"


class AuthError(RuntimeError):
    pass


class PendingApprovalError(AuthError):
    """Raised by authenticate() for an account still waiting on an admin.

    Deliberately not a subclass mixed into the generic wrong-password case —
    the caller (the /login route) needs to tell them apart to show "approval
    pending" instead of "incorrect username or password."
    """


class User:
    """An authenticated account. Carries no password material."""

    def __init__(
        self,
        username: str,
        role: Role,
        employee: str = "",
        disabled: bool = False,
        approved: bool = True,
        must_change_password: bool = False,
    ):
        self.username = username
        self.role = role
        self.employee = employee
        self.disabled = disabled
        self.approved = approved
        self.must_change_password = must_change_password

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            username=row["username"],
            role=Role(row["role"]),
            employee=row["employee"],
            disabled=bool(row["disabled"]),
            approved=bool(row["approved"]),
            must_change_password=bool(row["must_change_password"]),
        )

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    @property
    def can_control_runs(self) -> bool:
        """Start/stop runs and change settings. A User (WRITER) cannot."""
        return self.role is Role.ADMIN

    @property
    def employee_filter(self) -> str | None:
        """The employee folder this user is restricted to, or None for everything.

        A WRITER always gets a filter; if their account has no employee set, the
        filter is their username, which matches nothing rather than everything —
        failing closed, so a misconfigured writer account cannot see every
        employee's papers.
        """
        if self.role is Role.WRITER:
            return self.employee or self.username
        return None


def _open(state_db: Path) -> StateStore:
    return StateStore(Path(state_db))


def _validate_new_account(username: str, role: Role, employee: str) -> str:
    """Shared checks for both creation paths. Returns the cleaned username."""
    username = (username or "").strip()
    if not username:
        raise AuthError("Username cannot be empty.")
    if role is Role.WRITER and not (employee or "").strip():
        raise AuthError(
            "A User account needs an employee folder name, so their view can "
            "be restricted to their own papers."
        )
    return username


def create_user(state_db: Path, username: str, role: Role, employee: str = "") -> User:
    """The optional, admin-direct path: immediately active, no queue.

    The admin is vouching for this account by creating it themselves, so it
    skips the pending-approval state entirely — but still gets the default
    password and still forces a real one to be set on first login.
    """
    username = _validate_new_account(username, role, employee)
    with _open(state_db) as store:
        if store.get_user(username) is not None:
            raise AuthError(f"User {username!r} already exists.")
        store.create_user(
            username,
            generate_password_hash(DEFAULT_PASSWORD),
            role.value,
            employee.strip(),
            approved=True,
            must_change_password=True,
        )
    return User(username, role, employee.strip(), approved=True, must_change_password=True)


def request_signup(state_db: Path, username: str, role: Role, employee: str = "") -> User:
    """The public, self-service path: creates a pending request only.

    No usable password exists yet — the stored hash is of a random token
    nobody, including the requester, knows. `approve_user()` is what first
    gives the account a real, usable password.
    """
    username = _validate_new_account(username, role, employee)
    unusable = generate_password_hash(secrets.token_urlsafe(32))
    with _open(state_db) as store:
        if store.get_user(username) is not None:
            raise AuthError(f"User {username!r} already exists.")
        store.create_user(
            username, unusable, role.value, employee.strip(),
            approved=False, must_change_password=True,
        )
        store.record_claim_request(username)
    return User(username, role, employee.strip(), approved=False, must_change_password=True)


def authenticate(state_db: Path, username: str, password: str) -> User:
    """Return the User on success.

    Raises PendingApprovalError for a not-yet-approved account — without ever
    comparing the submitted password, since none exists yet to compare
    against. Returns None (not an exception) for every other failure: no such
    user, disabled, or wrong password — one indistinguishable result for all
    three, so a failed login cannot be used to enumerate which usernames
    exist (see the module docstring's SECURITY NOTES).
    """
    if not Path(state_db).exists():
        return None
    with _open(state_db) as store:
        row = store.get_user((username or "").strip())
        if row is None:
            return None
        if not row["approved"]:
            raise PendingApprovalError(
                "This account is waiting for an administrator to approve it."
            )
        if row["disabled"]:
            return None
        if not check_password_hash(row["password_hash"], password or ""):
            return None
        return User.from_row(row)


def approve_user(state_db: Path, username: str) -> None:
    """The moment a pending account first gets a real, usable password."""
    with _open(state_db) as store:
        row = store.get_user(username)
        if row is None:
            raise AuthError(f"No such user: {username!r}")
        store.set_password(username, generate_password_hash(DEFAULT_PASSWORD))
        store.set_user_approved(username, True)
        store.set_must_change_password(username, True)


def reject_user(state_db: Path, username: str) -> None:
    """Declines a pending request. Disabled, never deleted, never given a
    real password — matches how any other account is revoked, not removed."""
    with _open(state_db) as store:
        if store.get_user(username) is None:
            raise AuthError(f"No such user: {username!r}")
        store.set_user_disabled(username, True)


def complete_first_login(state_db: Path, username: str, new_password: str) -> None:
    """The forced popup after a first successful login on the default password.

    No current-password check: the caller only reaches this because Flask-
    Login already has them signed in via a session established moments ago
    with the correct default password. Only does anything while
    must_change_password is actually set, so this can never be repurposed as
    a way to silently reset an already-settled account's password.
    """
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with _open(state_db) as store:
        row = store.get_user(username)
        if row is None:
            raise AuthError(f"No such user: {username!r}")
        if not row["must_change_password"]:
            raise AuthError("This account has already set its own password.")
        store.set_password(username, generate_password_hash(new_password))
        store.set_must_change_password(username, False)


def change_own_password(
    state_db: Path, username: str, current_password: str, new_password: str
) -> None:
    """The ongoing, voluntary change from /account — unlike the forced popup,
    this one verifies the current password first."""
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with _open(state_db) as store:
        row = store.get_user(username)
        if row is None:
            raise AuthError(f"No such user: {username!r}")
        if not check_password_hash(row["password_hash"], current_password or ""):
            raise AuthError("Current password is incorrect.")
        store.set_password(username, generate_password_hash(new_password))
        store.set_must_change_password(username, False)


def reset_to_default_password(state_db: Path, username: str) -> None:
    """Admin-initiated reset for an already-active account (e.g. "I forgot my
    password") — routes them back through the forced popup on next login."""
    with _open(state_db) as store:
        if store.get_user(username) is None:
            raise AuthError(f"No such user: {username!r}")
        store.set_password(username, generate_password_hash(DEFAULT_PASSWORD))
        store.set_must_change_password(username, True)


def delete_user(state_db: Path, username: str) -> None:
    """Permanently remove an account — the one place this module actually
    deletes rather than disables.

    Refused for the last remaining approved, non-disabled Admin: losing it
    would either drop the whole system back to implicit-local-admin mode (if
    it was the only account left at all) or, worse, leave User accounts that
    can still log in with nobody able to reach /users to fix it. That guard
    is what makes "delete" safe to expose as a real button rather than a
    footgun.
    """
    with _open(state_db) as store:
        row = store.get_user(username)
        if row is None:
            raise AuthError(f"No such user: {username!r}")
        is_active_admin = (
            Role(row["role"]) is Role.ADMIN and row["approved"] and not row["disabled"]
        )
        if is_active_admin and store.count_active_admins() <= 1:
            raise AuthError(
                "Cannot delete the last remaining Admin account — create "
                "another Admin first."
            )
        store.delete_user(username)


def rename_user(state_db: Path, old_username: str, new_username: str) -> None:
    new_username = (new_username or "").strip()
    if not new_username:
        raise AuthError("Username cannot be empty.")
    with _open(state_db) as store:
        if store.get_user(old_username) is None:
            raise AuthError(f"No such user: {old_username!r}")
        if new_username != old_username and store.get_user(new_username) is not None:
            raise AuthError(f"User {new_username!r} already exists.")
        store.rename_user(old_username, new_username)


def load_user(state_db: Path, username: str) -> User | None:
    """Re-read an account by name, for restoring a session. Disabled or still-
    pending accounts return None so revoking access (or a request that was
    never actually approved) takes effect on the next request rather than
    whenever the session happens to expire."""
    if not Path(state_db).exists():
        return None
    with _open(state_db) as store:
        row = store.get_user(username)
        if row is None or row["disabled"] or not row["approved"]:
            return None
        return User.from_row(row)


def list_users(state_db: Path) -> list[User]:
    if not Path(state_db).exists():
        return []
    with _open(state_db) as store:
        return [User.from_row(r) for r in store.list_users()]


def set_password(state_db: Path, username: str, password: str) -> None:
    """Admin-chosen password, set directly (not the default) — for when an
    admin needs to hand someone a specific password by hand rather than
    routing through the default-password dance."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with _open(state_db) as store:
        if store.get_user(username) is None:
            raise AuthError(f"No such user: {username!r}")
        store.set_password(username, generate_password_hash(password))
        store.set_must_change_password(username, False)


def set_disabled(state_db: Path, username: str, disabled: bool) -> None:
    with _open(state_db) as store:
        if store.get_user(username) is None:
            raise AuthError(f"No such user: {username!r}")
        store.set_user_disabled(username, disabled)


def any_users_exist(state_db: Path) -> bool:
    """False means nobody has set up a login yet.

    The web UI uses this to decide whether to run in single-user local mode or
    demand a login — and to refuse to start at all when bound to the network
    with no accounts configured.
    """
    if not Path(state_db).exists():
        return False
    with _open(state_db) as store:
        return len(store.list_users()) > 0
