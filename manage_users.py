"""Create and manage web-UI logins.

    py manage_users.py list
    py manage_users.py add alice --role ADMIN
    py manage_users.py add bob --role WRITER --employee "Suchitra"
    py manage_users.py approve bob
    py manage_users.py reject bob
    py manage_users.py passwd alice
    py manage_users.py reset-password bob
    py manage_users.py disable bob
    py manage_users.py enable bob
    py manage_users.py delete bob

`add` creates an account that is active immediately, with the fixed default
password ("iMatiz") — you're vouching for it directly, so there's no approval
queue to pass through. The everyday way a new account is requested is the web
UI's own /signup page, which lands here as a pending request; `approve` (or
`reject`) is how you act on it. Either way, the first login on the default
password immediately forces a real one to be chosen — see paper_automation/
auth.py's module docstring for the full account lifecycle.

`passwd` lets you set an exact password by hand. `reset-password` instead
sets it back to the default and routes the person through the same forced
change again next login — useful for "I forgot my password."

`disable`/`enable` revoke and restore access without touching the record.
`delete` is the one genuinely destructive command here — it permanently
removes the account and is refused for the last remaining Admin, so it can't
be used to lock everyone out.
"""

import argparse
import getpass
import sys
from pathlib import Path

from paper_automation import auth
from paper_automation import config as config_module


def _read_password(prompt: str) -> str:
    """Prompt without echoing when there is a console.

    Windows' getpass reads the console device directly and ignores a piped
    stdin, which would hang a scripted setup, so fall back to a plain read when
    stdin is not a terminal. Nothing is echoed either way.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\n")


def _prompt_password() -> str:
    first = _read_password("New password: ")
    second = _read_password("Repeat password: ")
    if first != second:
        print("Passwords did not match.", file=sys.stderr)
        raise SystemExit(1)
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show all accounts, including pending requests")

    add = sub.add_parser("add", help="Create an account, active immediately")
    add.add_argument("username")
    add.add_argument("--role", choices=[r.value for r in auth.Role], required=True)
    add.add_argument(
        "--employee",
        default="",
        help="Employee folder a User is restricted to (required for role=WRITER)",
    )

    approve = sub.add_parser("approve", help="Approve a pending sign-up request")
    approve.add_argument("username")

    reject = sub.add_parser("reject", help="Decline a pending sign-up request")
    reject.add_argument("username")

    passwd = sub.add_parser("passwd", help="Set a specific password by hand")
    passwd.add_argument("username")

    sub.add_parser("reset-password", help="Reset to the default password").add_argument("username")

    disable = sub.add_parser("disable", help="Revoke an account's access")
    disable.add_argument("username")

    enable = sub.add_parser("enable", help="Restore a disabled account")
    enable.add_argument("username")

    delete = sub.add_parser("delete", help="Permanently remove an account")
    delete.add_argument("username")

    args = parser.parse_args(argv)

    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    db = Path(cfg.state_db)

    try:
        if args.command == "list":
            accounts = auth.list_users(db)
            if not accounts:
                print("No accounts yet. Create one with:  py manage_users.py add <name> --role ADMIN")
                return 0
            print(f"{'USERNAME':<20} {'ROLE':<8} {'EMPLOYEE':<20} STATUS")
            for a in accounts:
                if not a.approved and not a.disabled:
                    status = "pending"
                elif a.disabled:
                    status = "disabled"
                else:
                    status = "active"
                role_label = "ADMIN" if a.role is auth.Role.ADMIN else "USER"
                print(f"{a.username:<20} {role_label:<8} {a.employee:<20} {status}")
            return 0

        if args.command == "add":
            user = auth.create_user(db, args.username, auth.Role(args.role), args.employee)
            print(f"Created {user.username} ({user.role.value}) — active immediately, "
                  f"default password. They'll be asked to set their own on first login.")
            return 0

        if args.command == "approve":
            auth.approve_user(db, args.username)
            print(f"Approved {args.username} — default password is now active.")
            return 0

        if args.command == "reject":
            auth.reject_user(db, args.username)
            print(f"Rejected {args.username}.")
            return 0

        if args.command == "passwd":
            auth.set_password(db, args.username, _prompt_password())
            print(f"Password updated for {args.username}.")
            return 0

        if args.command == "reset-password":
            auth.reset_to_default_password(db, args.username)
            print(f"{args.username}'s password was reset to the default. "
                  f"They'll be asked to set a new one on next login.")
            return 0

        if args.command in ("disable", "enable"):
            auth.set_disabled(db, args.username, args.command == "disable")
            print(f"{args.username} is now {args.command}d.")
            return 0

        if args.command == "delete":
            auth.delete_user(db, args.username)
            print(f"Deleted {args.username}.")
            return 0

    except auth.AuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
