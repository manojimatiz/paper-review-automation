"""Launch the web control panel.

    py ui.py                 # start and open a browser
    py ui.py --port 5001     # use a different port
    py ui.py --no-browser    # start only

Or just double-click "Start UI.bat", which installs Flask on first run.

With no accounts configured this runs in local single-user mode, reachable only
from this PC. To let colleagues use it from their own machines, create logins
with `py manage_users.py add <name> --role ADMIN` and set web_host in
config.toml — see the README's "Sharing the panel on your network".
"""

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        from webui.app import main as run_app
    except ImportError as exc:
        if "flask" in str(exc).lower():
            print(
                "Flask is not installed. Run:\n\n"
                "    py -m pip install flask flask-login waitress\n\n"
                "or double-click 'Start UI.bat', which does it for you.",
                file=sys.stderr,
            )
            return 2
        raise

    run_app(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
