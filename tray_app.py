"""System tray controller for the web control panel.

    py tray_app.py            # run the persistent tray icon (starts the server)
    py tray_app.py --open     # one-shot: open the dashboard, starting it first if needed

Two ways this gets launched, and they're deliberately not the same code path:

* Login autostart (scripts/register_tray_autostart.ps1) launches plain
  `tray_app.py` — no browser tab pops up at every login, it just quietly
  starts the server and sits in the tray.
* The desktop shortcut (scripts/install_desktop_icon.ps1) launches
  `tray_app.py --open` — a lightweight one-shot action, not a second tray
  icon: if the dashboard is already reachable (the common case, since
  autostart already started it), it just opens a browser tab and exits;
  only if nothing answers does it fall back to starting the real tray
  process itself. This is what stops a double-click from spawning a second
  server bound to the same port, or a second tray icon.

The server itself runs as a CHILD PROCESS (`py ui.py --no-browser`), not
inside this process. Stop is `Popen.terminate()` — the same failure mode as
an unexpected crash, which the pipeline already recovers from automatically
on the next start via requeue_stale_processing_jobs() (see phases.py). This
is a deliberate simplicity trade-off, not an oversight: no graceful-shutdown
plumbing needed, at the cost of an in-flight review/revision being cut off
rather than finishing first when you click Stop.
"""

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import paper_automation

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 5000
APP_NAME = "Paper Review Automation"
APP_NAME_WITH_VERSION = f"{APP_NAME} v{paper_automation.__version__}"

# Defensive: pythonw.exe as the target shouldn't need this, but if _pythonw()
# ever falls back to a console-mode interpreter (e.g. pythonw.exe missing
# next to sys.executable on some install), this stops that fallback from
# flashing a console on every spawn.
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _pythonw() -> str:
    """The windowless interpreter next to the current one, so the server
    subprocess never flashes a console window."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def _service_exe() -> Path:
    """The sibling frozen exe that runs ui.py's server, next to this one."""
    return Path(sys.executable).with_name("PaperReviewAutomationService.exe")


def _tray_exe() -> Path:
    """This program's own installed exe, for open_or_start()'s fallback spawn."""
    return Path(sys.executable).with_name("PaperReviewAutomation.exe")


def _port_is_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """True when something is already listening — used both to detect an
    already-running server (don't start a second one) and to confirm a
    freshly-started one actually came up."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ServerController:
    """Owns the child process. No tray/GUI code in here, so the process
    management logic can be tested without a display."""

    def __init__(self, base_dir: Path = BASE_DIR, port: int = DEFAULT_PORT):
        self.base_dir = base_dir
        self.port = port
        self._process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        # Not a process we spawned ourselves, but something is answering on
        # the port anyway (e.g. started by another tray instance) — treat it
        # as running rather than trying to bind a conflicting second server.
        return self._process is None and _port_is_open(self.port)

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, "Already running."
        if _frozen():
            args = [str(_service_exe()), "--no-browser", "--port", str(self.port)]
        else:
            args = [_pythonw(), "ui.py", "--no-browser", "--port", str(self.port)]
        self._process = subprocess.Popen(
            args,
            cwd=str(self.base_dir),
            creationflags=_CREATE_NO_WINDOW,
        )
        # main()'s LAN-without-accounts gate (and any config error) fails
        # fast — give it a moment, then check whether it's actually still up.
        time.sleep(1.5)
        if self._process.poll() is not None:
            code = self._process.returncode
            self._process = None
            return False, (
                "Could not start (exit code %s). Common cause: web_host is "
                "set to a network address in config.toml with no accounts "
                "configured yet — see README's 'Sharing the panel on your "
                "network'." % code
            )
        return True, "Started."

    def stop(self) -> tuple[bool, str]:
        if self._process is None:
            if _port_is_open(self.port):
                return False, "Running, but not something this tray started — stop it manually."
            return True, "Already stopped."
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._process = None
        return True, "Stopped."


def _icon_image(running: bool):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (5, 150, 105, 255) if running else (148, 163, 184, 255)  # done-green / idle-gray
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    draw.rectangle((22, 16, 42, 48), fill=(255, 255, 255, 255))  # a plain "page" glyph
    return img


def build_icon(controller: ServerController):
    import pystray
    from pystray import MenuItem

    def open_dashboard(icon=None, item=None):
        webbrowser.open(f"http://127.0.0.1:{controller.port}/")

    def toggle(icon, item):
        ok, message = controller.stop() if controller.running else controller.start()
        icon.notify(message, title=APP_NAME)
        icon.icon = _icon_image(controller.running)

    def quit_app(icon, item):
        if controller.running:
            controller.stop()
        icon.stop()

    def toggle_text(item):
        return "Stop service" if controller.running else "Start service"

    def status_text(item):
        if not controller.running:
            return "Stopped"
        return f"Reachable at http://{socket.gethostname()}:{controller.port}/"

    menu = pystray.Menu(
        MenuItem("Open dashboard", open_dashboard),
        MenuItem(toggle_text, toggle),
        MenuItem(status_text, None, enabled=False),
        MenuItem(f"v{paper_automation.__version__}", None, enabled=False),
        MenuItem("Exit", quit_app),
    )
    return pystray.Icon(
        "paper_review_automation", _icon_image(controller.running),
        APP_NAME_WITH_VERSION, menu
    )


def run_tray() -> None:
    controller = ServerController()
    controller.start()
    icon = build_icon(controller)
    icon.run()


def open_or_start() -> None:
    """The desktop shortcut's one-shot action — see the module docstring."""
    port = DEFAULT_PORT
    if _port_is_open(port):
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return

    if _frozen():
        args = [str(_tray_exe())]
    else:
        args = [_pythonw(), str(BASE_DIR / "tray_app.py")]
    subprocess.Popen(
        args,
        cwd=str(BASE_DIR),
        creationflags=_CREATE_NO_WINDOW,
    )
    for _ in range(20):  # up to ~10s for the server to come up
        time.sleep(0.5)
        if _port_is_open(port):
            break
    webbrowser.open(f"http://127.0.0.1:{port}/")


def main() -> int:
    if "--open" in sys.argv[1:]:
        open_or_start()
    else:
        run_tray()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
