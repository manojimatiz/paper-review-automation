"""Automated research-paper review and revision pipeline."""

import sys
from pathlib import Path


def _read_version() -> str:
    """The VERSION file is the single source of truth — installer.iss reads
    the same file at compile time, so the two can never drift apart."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent
    try:
        return (base / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0-unknown"


__version__ = _read_version()
