"""Codex CLI — the review stage.

Authenticates with the user's ChatGPT account, so no API key is involved.
"""

from pathlib import Path

from . import discovery
from .subprocess_provider import SubprocessProvider


class CodexProvider(SubprocessProvider):
    name = "codex"
    command_name = "codex"
    label_base = "codex"

    def _locate(self) -> Path | None:
        return discovery.find_codex(self.config.binary_path)

    def _build_args(self, binary: Path, workdir: Path, prompt: str) -> list[str]:
        args = [
            str(binary),
            "exec",
            "--cd", str(workdir),
            # The scratch directory is not a repository, and it must be writable so
            # the model can produce output.md. workspace-write confines writes to it
            # rather than bypassing the sandbox entirely.
            "--skip-git-repo-check",
            "--sandbox", "workspace-write",
            "--ephemeral",
            "--color", "never",
        ]
        if self.config.model:
            args += ["--model", self.config.model]
        args += self.config.extra_args
        args.append(prompt)
        return args
