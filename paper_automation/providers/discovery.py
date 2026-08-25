"""Locate the CLI binaries that ship with the Codex and Claude desktop apps.

Both install under a version- or hash-named directory that changes when the app
updates, so the path is discovered at run time rather than hardcoded. A pinned
`binary_path` in config always wins.
"""

import os
import shutil
from pathlib import Path

_LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
_ROAMING = Path(os.environ.get("APPDATA", ""))

# Ordered by preference; the first pattern with a match wins.
CODEX_PATTERNS = (
    _LOCAL / "OpenAI" / "Codex" / "bin" / "*" / "codex.exe",
    _LOCAL / "OpenAI" / "Codex" / "bin" / "codex.exe",
)

CLAUDE_PATTERNS = (
    _ROAMING / "Claude" / "claude-code" / "*" / "claude.exe",
    _LOCAL / "Packages" / "Claude_*" / "LocalCache" / "Roaming" / "Claude"
    / "claude-code" / "*" / "claude.exe",
)


def _newest_match(pattern: Path) -> Path | None:
    """Resolve a glob pattern to its most recently modified match."""
    parts = pattern.parts
    for index, part in enumerate(parts):
        if "*" in part:
            root = Path(*parts[:index])
            relative = str(Path(*parts[index:])).replace("\\", "/")
            if not root.is_dir():
                return None
            matches = [p for p in root.glob(relative) if p.is_file()]
            return max(matches, key=lambda p: p.stat().st_mtime, default=None)
    return pattern if pattern.is_file() else None


def find(command: str, patterns: tuple[Path, ...], override: str | None = None) -> Path | None:
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    on_path = shutil.which(command)
    if on_path:
        return Path(on_path)

    for pattern in patterns:
        match = _newest_match(pattern)
        if match:
            return match
    return None


def find_codex(override: str | None = None) -> Path | None:
    return find("codex", CODEX_PATTERNS, override)


def find_claude(override: str | None = None) -> Path | None:
    return find("claude", CLAUDE_PATTERNS, override)
