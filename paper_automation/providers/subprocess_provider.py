"""Shared subprocess plumbing and failure classification for CLI providers."""

import logging
import subprocess
import sys
from pathlib import Path

from ..models import FailureKind, ProviderError
from .base import CliProvider
from .failures import classify, summarise_failure

log = logging.getLogger(__name__)

__all__ = ["SubprocessProvider", "classify", "summarise_failure"]

# These calls run from a windowless (pythonw.exe-run) web UI process, and
# codex/claude are console-subsystem executables — without this flag Windows
# briefly opens and closes a visible console for every call.
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class SubprocessProvider(CliProvider):
    """A provider backed by a local executable run in the scratch directory."""

    command_name = "tool"
    # Send the prompt on stdin rather than as a trailing argument. Necessary when the
    # tool has variadic flags, which would otherwise swallow the prompt as another
    # flag value.
    uses_stdin = False

    def __init__(self, provider_config):
        self.config = provider_config
        self._binary: Path | None = None
        self._version: str = ""

    # Subclasses set label_base rather than model_label, so the property below is
    # not shadowed by a plain class attribute.
    label_base = "provider"

    @property
    def model_label(self) -> str:
        """What goes into the audit trail: the tool, plus the model asked for."""
        model = (self.config.model or "").strip()
        return f"{self.label_base}:{model}" if model else self.label_base

    # --- to be supplied by subclasses ---

    def _locate(self) -> Path | None:
        raise NotImplementedError

    def _build_args(self, binary: Path, workdir: Path, prompt: str) -> list[str]:
        raise NotImplementedError

    # --- shared behaviour ---

    @property
    def binary(self) -> Path:
        if self._binary is None:
            found = self._locate()
            if found is None:
                raise ProviderError(
                    FailureKind.BINARY_MISSING,
                    f"Could not find the {self.command_name} executable. Install the "
                    f"app, or set providers.{self.name}.binary_path in config.toml.",
                )
            self._binary = found
            log.info("Using %s at %s", self.command_name, found)
        return self._binary

    def preflight(self) -> None:
        """Confirm the binary exists and runs before any client is processed."""
        try:
            result = subprocess.run(
                [str(self.binary), "--version"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(Path.home()),
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                FailureKind.BINARY_MISSING,
                f"{self.command_name} --version timed out; the install may be broken.",
            ) from exc
        except OSError as exc:
            raise ProviderError(
                FailureKind.BINARY_MISSING, f"Could not run {self.binary}: {exc}"
            ) from exc

        if result.returncode != 0:
            raise ProviderError(
                FailureKind.BINARY_MISSING,
                f"{self.command_name} --version failed: "
                f"{(result.stderr or result.stdout).strip()[:300]}",
            )
        self._version = result.stdout.strip()
        log.info("%s version: %s", self.command_name, self._version)

    def version(self) -> str:
        """The CLI's reported version, or "" if it cannot be determined.

        Never raises: this feeds a status display, and a missing app should show
        as unavailable rather than break the page.
        """
        if getattr(self, "_version", None):
            return self._version
        try:
            result = subprocess.run(
                [str(self.binary), "--version"],
                capture_output=True, text=True, timeout=30, cwd=str(Path.home()),
                creationflags=_CREATE_NO_WINDOW,
            )
        except (ProviderError, subprocess.SubprocessError, OSError):
            return ""
        self._version = result.stdout.strip() if result.returncode == 0 else ""
        return self._version

    def _invoke(self, workdir: Path, prompt: str) -> str:
        args = self._build_args(self.binary, workdir, prompt)
        log.debug("Running: %s", " ".join(args[:6]) + " ...")
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                cwd=str(workdir),
                input=prompt if self.uses_stdin else None,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                FailureKind.TIMEOUT,
                f"{self.command_name} exceeded {self.config.timeout_seconds}s.",
            ) from exc
        except OSError as exc:
            raise ProviderError(
                FailureKind.BINARY_MISSING, f"Could not run {self.binary}: {exc}"
            ) from exc

        combined = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            kind = classify(combined)
            raise ProviderError(
                kind,
                f"{self.command_name} exited {result.returncode} ({kind.value}): "
                f"{summarise_failure(combined)}",
            )

        # A zero exit is success. Do NOT inspect the text for failure keywords here:
        # stdout carries the review or the revised manuscript, so any keyword search
        # lets the paper's own wording condemn a run that worked. If the tool really
        # did give up, it wrote no output file, and CliProvider.generate classifies
        # it there — where the text is genuinely diagnostic.
        return combined
