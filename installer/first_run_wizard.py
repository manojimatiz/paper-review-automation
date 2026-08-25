"""First-run setup wizard for the installed Paper Review Automation.

Run once by the Inno Setup installer right after copying the frozen exes
(installer/installer.iss's post-install [Run] step). Not part of the normal
"py run.py" / "py ui.py" source-run workflow — see README/CLAUDE.md for
that; this only exists to save a new install from three manual steps (copy
config.example.toml, edit research_papers_root, run manage_users.py add).

Writes %USERPROFILE%\\PaperReviewAutomation\\config.toml (research_papers_root
+ timezone from the form, everything else left at config.example.toml's
defaults) and creates the first admin account by calling
paper_automation.auth.create_user() directly — the exact same function the
web UI's /users "Add user" button and manage_users.py add use, so account
creation logic is not duplicated here. The account gets the fixed default
password ("iMatiz") and is forced to set a real one on first login, same as
any other account created this way.
"""

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# When frozen, installer/first_run_wizard.py and the paper_automation package
# are bundled into the same shared dist folder (see paper_review_automation.spec's
# MERGE), so this import resolves the same way it does for the other exes.
from paper_automation import auth
from paper_automation import config as config_module

DATA_DIR = Path.home() / "PaperReviewAutomation"
APP_NAME = "Paper Review Automation — First-run Setup"


def _template_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "config.example.toml"


def _write_config(papers_root: str, timezone: str) -> Path:
    template = _template_path().read_text(encoding="utf-8-sig")
    lines = []
    for line in template.splitlines():
        if line.startswith("research_papers_root ="):
            escaped = papers_root.replace("\\", "/")
            lines.append(f'research_papers_root = "{escaped}"')
        elif line.startswith("timezone ="):
            lines.append(f'timezone = "{timezone}"')
        else:
            lines.append(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config_path = DATA_DIR / "config.toml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        self.papers_root = tk.StringVar(value=str(Path.home() / "Research Papers"))
        self.timezone = tk.StringVar(value="Asia/Kolkata")
        self.admin_name = tk.StringVar(value="")
        self._build()

    def _build(self):
        pad = {"padx": 12, "pady": 6}
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        ttk.Label(
            frame,
            text="Set up Paper Review Automation",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(frame, text="Research papers folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.papers_root, width=48).grid(row=1, column=1, **pad)
        ttk.Button(frame, text="Browse…", command=self._browse).grid(row=1, column=2, **pad)

        ttk.Label(frame, text="Timezone:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.timezone, width=48).grid(row=2, column=1, **pad)

        ttk.Label(frame, text="Your name (first admin account):").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.admin_name, width=48).grid(row=3, column=1, **pad)

        ttk.Label(
            frame,
            text=(
                "Default password: iMatiz — you'll be asked to change it the\n"
                "first time you log in."
            ),
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        ttk.Button(frame, text="Finish setup", command=self._finish).grid(
            row=5, column=0, columnspan=3, pady=(12, 4)
        )

    def _browse(self):
        chosen = filedialog.askdirectory(title="Choose the research papers folder")
        if chosen:
            self.papers_root.set(chosen)

    def _finish(self):
        papers_root = self.papers_root.get().strip()
        timezone = self.timezone.get().strip() or "Asia/Kolkata"
        admin_name = self.admin_name.get().strip()

        if not papers_root:
            messagebox.showerror(APP_NAME, "Please choose a research papers folder.")
            return
        if not admin_name:
            messagebox.showerror(APP_NAME, "Please enter a name for the first admin account.")
            return

        try:
            Path(papers_root).mkdir(parents=True, exist_ok=True)
            config_path = _write_config(papers_root, timezone)
            cfg = config_module.load(path=config_path, base_dir=DATA_DIR)
            auth.create_user(cfg.state_db, admin_name, auth.Role.ADMIN)
        except auth.AuthError as exc:
            messagebox.showerror(APP_NAME, f"Could not create the admin account: {exc}")
            return
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not set up the papers folder: {exc}")
            return
        except config_module.ConfigError as exc:
            messagebox.showerror(APP_NAME, f"Could not write config.toml: {exc}")
            return

        messagebox.showinfo(
            APP_NAME,
            "Setup complete.\n\n"
            f"Login: {admin_name}\nPassword: iMatiz (you'll be asked to change it)\n\n"
            "Open the desktop icon to get started.",
        )
        self.destroy()


def main() -> int:
    Wizard().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
