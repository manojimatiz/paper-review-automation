# PyInstaller spec for Paper Review Automation.
#
# Build with (from the project root):
#     py -m PyInstaller installer\paper_review_automation.spec --distpath dist --workpath build --noconfirm
#
# Produces one shared output folder (dist\PaperReviewAutomation\) containing
# four executables that all share one Python runtime/DLL set, rather than
# four separate ~80MB onefile bundles:
#
#   PaperReviewAutomation.exe          tray_app.py   (windowed)  - the tray icon
#   PaperReviewAutomationService.exe   ui.py         (windowed)  - the web server,
#                                                                   spawned by the tray
#   paper-review-run.exe               run.py        (console)   - Task Scheduler target
#   paper-review-users.exe             manage_users.py (console) - admin CLI tool
#   FirstRunSetup.exe                  installer/first_run_wizard.py (windowed)
#
# webui/templates and webui/static are bundled as data so Flask can find them
# at sys._MEIPASS-relative paths at runtime (Flask's default template/static
# folder resolution "just works" here because PyInstaller preserves the
# webui/ package's relative layout under _MEIPASS). models.json and
# config.example.toml are bundled too: the first-run wizard reads the latter
# as a template, and model_registry.py reads the former.

import sys
from pathlib import Path

block_cipher = None
PROJECT_ROOT = Path(SPECPATH).resolve().parent

COMMON_DATAS = [
    (str(PROJECT_ROOT / "webui" / "templates"), "webui/templates"),
    (str(PROJECT_ROOT / "webui" / "static"), "webui/static"),
    (str(PROJECT_ROOT / "models.json"), "."),
    (str(PROJECT_ROOT / "config.example.toml"), "."),
]

# Bundled into every exe (not just COMMON_DATAS' service-only set) since
# paper_automation.__version__ is read by all five entry points.
VERSION_DATA = [(str(PROJECT_ROOT / "VERSION"), ".")]

COMMON_HIDDENIMPORTS = [
    "flask", "flask_login", "waitress", "werkzeug.security",
    "pystray", "pystray._win32", "PIL", "PIL.Image", "PIL.ImageDraw",
    "docx", "requests", "tomllib", "sqlite3", "zoneinfo",
]

# This dev machine's site-packages also holds an unrelated data-science/Jupyter
# stack (torch, pandas, scipy, pyarrow, matplotlib, jedi, zmq, ...) — none of it
# is imported by this project (see requirements.txt), but PyInstaller's static
# analysis pulls in anything reachable, ballooning the build to ~1GB. Exclude
# it explicitly rather than relying on "nothing imports it" to hold forever.
COMMON_EXCLUDES = [
    "torch", "torchaudio", "torchvision", "pyarrow", "scipy", "pandas",
    "matplotlib", "jedi", "IPython", "zmq", "notebook", "jupyter",
    "jupyter_client", "jupyter_core", "aiohttp", "google", "sklearn",
    "numpy", "numpy.testing", "tkinter.test",
]


def analysis(script: str, datas=None):
    return Analysis(
        [str(PROJECT_ROOT / script)],
        pathex=[str(PROJECT_ROOT)],
        binaries=[],
        datas=(datas or []) + VERSION_DATA,
        hiddenimports=COMMON_HIDDENIMPORTS,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=COMMON_EXCLUDES,
        noarchive=False,
        cipher=block_cipher,
    )


a_tray = analysis("tray_app.py")
a_service = analysis("ui.py", datas=COMMON_DATAS)
a_run = analysis("run.py")
a_users = analysis("manage_users.py")
a_wizard = analysis("installer/first_run_wizard.py", datas=[
    (str(PROJECT_ROOT / "config.example.toml"), "."),
])

MERGE(
    (a_tray, "tray_app", "PaperReviewAutomation"),
    (a_service, "ui", "PaperReviewAutomationService"),
    (a_run, "run", "paper-review-run"),
    (a_users, "manage_users", "paper-review-users"),
    (a_wizard, "first_run_wizard", "FirstRunSetup"),
)


def build_exe(a, name, console):
    pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
    return EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
    ), pyz


exe_tray, pyz_tray = build_exe(a_tray, "PaperReviewAutomation", console=False)
exe_service, pyz_service = build_exe(a_service, "PaperReviewAutomationService", console=False)
exe_run, pyz_run = build_exe(a_run, "paper-review-run", console=True)
exe_users, pyz_users = build_exe(a_users, "paper-review-users", console=True)
exe_wizard, pyz_wizard = build_exe(a_wizard, "FirstRunSetup", console=False)

coll = COLLECT(
    exe_tray, a_tray.binaries, a_tray.zipfiles, a_tray.datas,
    exe_service, a_service.binaries, a_service.zipfiles, a_service.datas,
    exe_run, a_run.binaries, a_run.zipfiles, a_run.datas,
    exe_users, a_users.binaries, a_users.zipfiles, a_users.datas,
    exe_wizard, a_wizard.binaries, a_wizard.zipfiles, a_wizard.datas,
    strip=False,
    upx=False,
    name="PaperReviewAutomation",
)
