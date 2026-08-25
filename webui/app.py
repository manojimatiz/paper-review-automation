"""Flask front end for the research-paper automation.

Thin by design: every route delegates to paper_automation.service and renders the
result. Keeping the logic out of here is what lets the same pipeline grow a Drive
backend, or a different front end, without this file changing.

SECURITY MODEL
--------------
Two modes, chosen automatically by whether any accounts exist:

* No accounts (`auth.any_users_exist()` is False) — the historical single-user
  local mode. Every request is treated as an implicit admin. This is only safe
  because `web_host` defaults to 127.0.0.1, so nothing off this machine can
  reach it. `main()` refuses to start in this mode if `web_host` is not
  loopback, rather than silently serving an unauthenticated panel to the LAN.
* Accounts exist — every page and API route requires a login. A User (WRITER)
  sees only their own employee folder's papers and cannot start runs or
  change settings; Admin sees everything.

Two ways an account comes into existence: `/signup` (public, self-service —
creates a PENDING request with no usable password at all until an admin
approves it) or an admin creating one directly on `/users` (immediately
active). Either way, first login is on the fixed default password `iMatiz`
and is immediately followed by a forced "set your own password" screen —
enforced by the `_require_password_change` before_request hook below, not
just by hiding the rest of the UI.

Session cookies are HttpOnly + SameSite=Lax. There is no HTTPS here: on a
trusted office LAN that is a deliberate, documented trade-off, but it does mean
passwords cross the network in the clear — do not expose this beyond a network
you control.

/login is rate-limited per source IP (in-process, resets on restart) to slow
down password guessing — this is a LAN convenience tool, not a hardened auth
server, so the bar is "meaningfully slower than instant," not "unbreakable."
"""

import logging
import os
import secrets
import threading
import time
import webbrowser
from functools import wraps
from pathlib import Path
from threading import Timer

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, session, url_for
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required, login_user, logout_user
)

from paper_automation import auth
from paper_automation import config as config_module
from paper_automation import service
from paper_automation.storage import build_storage

BASE_DIR = Path(__file__).resolve().parent.parent

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# A fresh key per process means restarting the app invalidates old sessions.
# PAPER_AUTOMATION_SECRET_KEY keeps them valid across restarts when set.
app.secret_key = os.environ.get("PAPER_AUTOMATION_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JavaScript cannot read the session cookie
    SESSION_COOKIE_SAMESITE="Lax",  # not sent on cross-site form posts
)

runs = service.RunManager()
storage = build_storage(config_module.load())

log = logging.getLogger("webui")

login_manager = LoginManager(app)
login_manager.login_view = "login"


class WebUser(UserMixin):
    """Adapts paper_automation.auth.User to what Flask-Login expects."""

    def __init__(self, account: auth.User):
        self.account = account

    def get_id(self) -> str:
        return self.account.username

    def __getattr__(self, name):
        return getattr(self.account, name)


# The implicit account used when nobody has configured a login yet. Local-only
# mode: main() will not start on a non-loopback host without real accounts.
_LOCAL_ADMIN = auth.User("local", auth.Role.ADMIN)


def _state_db() -> Path:
    return Path(config_module.load().state_db)


def auth_enabled() -> bool:
    try:
        return auth.any_users_exist(_state_db())
    except config_module.ConfigError:
        return False


@login_manager.user_loader
def _load_user(username: str):
    account = auth.load_user(_state_db(), username)
    return WebUser(account) if account else None


# Shown to nobody-in-particular: the login page renders through the same base
# template, which expects a `user`. Grants nothing — every real check goes
# through @protected / @controller_only, which run before a template does.
_ANONYMOUS = auth.User("", auth.Role.WRITER)


def active_user() -> auth.User:
    """The account for this request — real when logins are set up, the implicit
    local admin when they are not, anonymous on the login page itself."""
    if not auth_enabled():
        return _LOCAL_ADMIN
    if not current_user.is_authenticated:
        return _ANONYMOUS
    return current_user.account


def protected(view):
    """Require a login, but only once accounts exist.

    Keeps the historical zero-setup local experience working while making the
    login mandatory the moment anyone creates an account.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not auth_enabled():
            return view(*args, **kwargs)
        return login_required(view)(*args, **kwargs)

    return wrapper


def controller_only(view):
    """Additionally require a role allowed to start runs and change settings.

    Writers are refused with 403 — enforced server-side, not merely hidden in
    the template, so a writer cannot trigger a run by calling the API directly.
    """

    @wraps(view)
    @protected
    def wrapper(*args, **kwargs):
        if not active_user().can_control_runs:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Not allowed."}), 403
            abort(403)
        return view(*args, **kwargs)

    return wrapper


@app.context_processor
def _inject_user():
    """Templates get `user`, `auth_on`, and a pending-request count (for the
    Users nav badge) without every route passing them."""
    user = active_user()
    pending = 0
    if auth_enabled() and user.is_admin:
        pending = sum(1 for a in auth.list_users(_state_db()) if not a.approved and not a.disabled)
    return {"user": user, "auth_on": auth_enabled(), "pending_users": pending}


def current_config():
    """Reloaded per request so a settings change takes effect immediately."""
    return config_module.load()


def config_error_page(exc):
    return render_template("error.html", message=str(exc)), 200


class _LoginRateLimiter:
    """Fixed-window per-IP throttle on failed logins.

    In-process and unbounded in memory, which is fine at LAN scale (a handful
    of IPs, cleared on every restart) but would not be the right shape for a
    public-facing service.
    """

    WINDOW_SECONDS = 300
    MAX_FAILURES = 8

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _recent(self, ip: str, now: float) -> list[float]:
        return [t for t in self._failures.get(ip, ()) if now - t < self.WINDOW_SECONDS]

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip, time.time())) >= self.MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._failures[ip] = self._recent(ip, now) + [now]

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


_login_limiter = _LoginRateLimiter()


@app.before_request
def _require_password_change():
    """Nothing is reachable on a must-change-password account except the page
    that lets them change it — enforced here, not merely by hiding the link,
    so there is no way to route around it by guessing a URL."""
    if not auth_enabled() or not current_user.is_authenticated:
        return None
    if not current_user.must_change_password:
        return None
    if request.endpoint in ("account", "logout", "static"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "Set a new password first."}), 403
    return redirect(url_for("account"))


# ------------------------------------------------------------------------ auth


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled():
        return redirect(url_for("dashboard"))
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    error = ""
    username = ""
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if _login_limiter.blocked(ip):
            error = "Too many attempts. Wait a few minutes and try again."
            log.warning("Login rate-limited for %s", ip)
        else:
            username = (request.form.get("username") or "").strip()
            try:
                account = auth.authenticate(
                    _state_db(), username, request.form.get("password") or ""
                )
            except auth.PendingApprovalError as exc:
                error = str(exc)
                account = None
            if account is None and not error:
                # One message for every cause, so a stranger cannot learn which
                # usernames exist by comparing responses.
                error = "Incorrect username or password."
                _login_limiter.record_failure(ip)
                log.warning("Failed login for %r from %s", username, ip)
            elif account is not None:
                _login_limiter.clear(ip)
                session.clear()  # new session id on privilege change
                login_user(WebUser(account))
                log.info("%s signed in from %s", account.username, ip)
                return redirect(url_for("dashboard"))

    return render_template("login.html", error=error, username=username)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Public, self-service account request. No password field here at
    all — see auth.request_signup()'s docstring for why."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    error = ""
    sent = False
    username = ""
    role = "WRITER"
    employee = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        role = request.form.get("role") or "WRITER"
        employee = (request.form.get("employee") or "").strip()
        try:
            auth.request_signup(_state_db(), username, auth.Role(role), employee)
            sent = True
        except (auth.AuthError, ValueError) as exc:
            error = str(exc)

    return render_template(
        "signup.html", error=error, sent=sent,
        username=username, role=role, employee=employee,
    )


@app.route("/logout")
def logout():
    if auth_enabled():
        logout_user()
    session.clear()
    return redirect(url_for("login") if auth_enabled() else url_for("dashboard"))


@app.route("/account", methods=["GET", "POST"])
@protected
def account():
    """Doubles as the forced first-password-change popup and the ongoing,
    voluntary change — the form fields differ (see account.html) but both
    post here."""
    if not auth_enabled():
        # Nothing to manage in local single-user mode — there is no account.
        return redirect(url_for("dashboard"))

    error = ""
    message = ""
    forced = current_user.must_change_password

    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if new_password != confirm:
            error = "The new password and confirmation do not match."
        else:
            try:
                if forced:
                    auth.complete_first_login(
                        _state_db(), active_user().username, new_password
                    )
                    return redirect(url_for("dashboard"))
                else:
                    auth.change_own_password(
                        _state_db(), active_user().username,
                        request.form.get("current_password") or "", new_password,
                    )
                    message = "Password updated."
            except auth.AuthError as exc:
                error = str(exc)

    return render_template(
        "account.html", forced=forced, error=error, message=message, active="account",
    )


# ----------------------------------------------------------------------- pages


@app.route("/")
@protected
def dashboard():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return config_error_page(exc)

    month = request.args.get("month") or None
    data = service.scan(cfg, storage, month, active_user().employee_filter)
    return render_template(
        "dashboard.html",
        data=data,
        status=runs.status(),
        schedule=service.schedule_status(),
        models=service.model_status(cfg, BASE_DIR),
        can_open=service.supports_open_file(storage),
        active="dashboard",
    )


@app.route("/history")
@protected
def history():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return config_error_page(exc)

    month = request.args.get("month") or None
    return render_template(
        "history.html",
        rows=service.history(
            cfg, limit=300, month=month,
            employee_filter=active_user().employee_filter,
        ),
        logs=service.log_files(cfg) if active_user().can_control_runs else [],
        months=service.available_months(cfg, storage),
        selected_month=month or "",
        active="history",
    )


@app.route("/jobs")
@controller_only
def jobs():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return config_error_page(exc)

    status = request.args.get("status") or None
    return render_template(
        "jobs.html",
        data=service.job_overview(cfg, status),
        health=service.system_health(cfg, storage, runs.status()),
        active="jobs",
    )


@app.route("/prompts", methods=["GET", "POST"])
@controller_only
def prompt_editor():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return config_error_page(exc)

    message = error = ""
    if request.method == "POST":
        action = request.form.get("action", "save")
        phase = request.form.get("phase", "")
        if phase not in ("review", "revise"):
            error = "Unknown prompt."
        elif action == "save":
            result = service.save_prompt(
                cfg, phase, request.form.get("body", ""), active_user().username
            )
            message, error = (result["message"], "") if result["ok"] else ("", result["message"])
        elif action == "reset":
            message = service.reset_prompt(cfg, phase)["message"]
        elif action == "restore":
            try:
                version = int(request.form.get("version", ""))
            except ValueError:
                error = "Unknown version."
            else:
                result = service.activate_prompt(cfg, phase, version)
                message, error = (result["message"], "") if result["ok"] else ("", result["message"])

    return render_template(
        "prompts.html",
        prompts=service.prompt_overview(cfg),
        task_mode=cfg.task_mode,
        message=message,
        error=error,
        active="prompts",
    )


@app.route("/users", methods=["GET", "POST"])
@controller_only
def users():
    message = error = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        username = request.form.get("username", "")
        try:
            if action == "add":
                role = auth.Role(request.form.get("role") or "WRITER")
                employee = (request.form.get("employee") or "").strip()
                auth.create_user(_state_db(), username, role, employee)
                message = f"Created {username} — active immediately with the default password."
            elif action == "approve":
                auth.approve_user(_state_db(), username)
                message = f"Approved {username}."
            elif action == "reject":
                auth.reject_user(_state_db(), username)
                message = f"Rejected {username}."
            elif action == "disable":
                auth.set_disabled(_state_db(), username, True)
                message = f"Disabled {username}."
            elif action == "enable":
                auth.set_disabled(_state_db(), username, False)
                message = f"Enabled {username}."
            elif action == "rename":
                new_username = request.form.get("new_username", "")
                auth.rename_user(_state_db(), username, new_username)
                message = f"Renamed {username} to {new_username}."
            elif action == "reset-password":
                auth.reset_to_default_password(_state_db(), username)
                message = f"{username}'s password was reset to the default."
            elif action == "delete":
                auth.delete_user(_state_db(), username)
                message = f"Deleted {username}."
            else:
                error = "Unknown action."
        except auth.AuthError as exc:
            error = str(exc)

    return render_template(
        "users.html",
        accounts=auth.list_users(_state_db()),
        message=message, error=error, active="users",
    )


@app.route("/settings", methods=["GET", "POST"])
@controller_only
def settings():
    path = BASE_DIR / config_module.DEFAULT_CONFIG_NAME
    message = ""
    error = ""

    if request.method == "POST":
        updates = {
            key: request.form.get(key)
            for key in service.EDITABLE
            if key in request.form
        }
        # An unchecked checkbox submits nothing, so absence means false.
        updates["create_missing_month"] = "create_missing_month" in request.form
        try:
            applied = service.update_config_file(path, updates)

            # Model choice lives inside a [providers.*] table, so it is written
            # separately. "__other__" means the free-text box was used.
            for provider in ("codex", "claude"):
                chosen = (request.form.get(f"model_{provider}") or "").strip()
                if chosen == "__other__":
                    chosen = (request.form.get(f"model_{provider}_other") or "").strip()
                    if chosen:
                        service.model_registry.remember(
                            service.registry_path(BASE_DIR), provider, chosen
                        )
                if service.update_provider_model(path, provider, chosen):
                    applied.append(f"{provider} model")

            config_module.load()  # fail loudly here rather than on the next run
            message = (
                "Saved: " + ", ".join(applied) if applied else "No changes to save."
            )
        except (config_module.ConfigError, OSError) as exc:
            error = str(exc)

    try:
        cfg = current_config()
        values = service.config_values(cfg)
    except config_module.ConfigError as exc:
        return config_error_page(exc)

    return render_template(
        "settings.html",
        values=values,
        config_path=str(path),
        message=message,
        error=error,
        schedule=service.schedule_status(),
        models=service.model_status(cfg, BASE_DIR),
        registry_path=str(service.registry_path(BASE_DIR)),
        active="settings",
    )


# ------------------------------------------------------------------------- api


@app.post("/api/run")
@controller_only
def api_run():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    payload = request.get_json(silent=True) or {}
    options = {
        "phase": payload.get("phase", "both"),
        "month": payload.get("month") or None,
    }
    if payload.get("test_mode"):
        cfg.test_mode = True
    if payload.get("provider"):
        cfg.provider_mode = payload["provider"]

    result = runs.start(cfg, storage, options)
    return jsonify(result), (200 if result["ok"] else 409)


@app.get("/api/status")
@protected
def api_status():
    return jsonify(runs.status())


@app.get("/api/scan")
@protected
def api_scan():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        service.scan(
            cfg, storage, request.args.get("month") or None,
            active_user().employee_filter,
        )
    )


@app.get("/api/preview")
@protected
def api_preview():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        service.dry_run_preview(
            cfg, storage, request.args.get("month") or None,
            active_user().employee_filter,
        )
    )


@app.post("/api/open")
@protected
def api_open():
    """Open a paper in Explorer on the *server* machine.

    Only meaningful for someone sitting at that PC, and a writer must not be
    able to aim it at another employee's folder — so the path is checked
    against their own scope before anything is opened.
    """
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    target = (request.get_json(silent=True) or {}).get("path", "")
    if not target:
        return jsonify({"ok": False, "message": "No path given."}), 400

    employee = active_user().employee_filter
    if employee and not service.path_belongs_to_employee(Path(target), cfg, employee):
        log.warning(
            "%s tried to open a path outside their folder: %r",
            active_user().username, target,
        )
        return jsonify({"ok": False, "message": "Not allowed."}), 403

    return jsonify(service.open_in_explorer(Path(target), cfg))


@app.post("/api/backup")
@controller_only
def api_backup():
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify(service.backup_now(cfg))


@app.post("/api/create-folder")
@controller_only
def api_create_folder():
    """Create an employee folder, or a client folder under one (spec section 38)."""
    try:
        cfg = current_config()
    except config_module.ConfigError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    payload = request.get_json(silent=True) or {}
    month = (payload.get("month") or "").strip()
    employee = (payload.get("employee") or "").strip()
    client = (payload.get("client") or "").strip()
    if not month or not employee:
        return jsonify({"ok": False, "message": "Month and employee are required."}), 400

    if client:
        result = service.create_client_folder(cfg, storage, month, employee, client)
    else:
        result = service.create_employee_folder(cfg, storage, month, employee)
    return jsonify(result), (200 if result["ok"] else 400)


@app.post("/api/schedule")
@controller_only
def api_schedule():
    payload = request.get_json(silent=True) or {}
    result = service.set_schedule(
        bool(payload.get("enabled")), BASE_DIR, payload.get("at", "09:00")
    )
    result["schedule"] = service.schedule_status()
    return jsonify(result)


@app.errorhandler(403)
def _forbidden(_exc):
    return render_template(
        "error.html",
        message="You do not have permission to view that page.",
    ), 403


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(port: int = 5000, open_browser: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    cfg = config_module.load()
    host = cfg.web_host
    lan = host not in _LOOPBACK

    # SECURITY GATE: refuse to serve an unauthenticated panel to the network.
    # Anyone who could reach it would have full control of the pipeline and read
    # access to every client's papers.
    if lan and not auth_enabled():
        print(
            f"\n  Refusing to start.\n\n"
            f"  config.toml sets web_host = {host!r}, which makes this control panel\n"
            f"  reachable from other PCs — but no logins exist yet, so anyone on the\n"
            f"  network could use it.\n\n"
            f"  Create an administrator account first:\n\n"
            f"      py manage_users.py add <name> --role ADMIN\n\n"
            f"  Or set web_host = \"127.0.0.1\" to keep it on this PC only.\n"
        )
        raise SystemExit(2)

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"\n  Paper Review Automation UI\n  {url}")
    if lan:
        print(f"  Also reachable on this network at port {port} (host {host}).")
        print("  Traffic is plain HTTP — use only on a network you trust.")
    if not auth_enabled():
        print("  No logins configured — running in local single-user mode.")
    print("  Press Ctrl+C to stop.\n")

    try:
        from waitress import serve
    except ImportError:
        # Threaded so status polling stays responsive while a run occupies a worker.
        log.warning("waitress is not installed; falling back to the Flask dev server.")
        app.run(host=host, port=port, debug=False, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
