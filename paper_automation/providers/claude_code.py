"""Claude Code CLI — the revision stage.

Authenticates with the user's Claude account, so no API key is involved.
"""

from pathlib import Path

from . import discovery
from .subprocess_provider import SubprocessProvider


class ClaudeCodeProvider(SubprocessProvider):
    name = "claude"
    command_name = "claude"
    label_base = "claude-code"
    # --allowedTools is variadic, so a trailing prompt argument would be consumed as
    # another tool name. The prompt goes on stdin instead.
    uses_stdin = True

    def _locate(self) -> Path | None:
        return discovery.find_claude(self.config.binary_path)

    def _build_args(self, binary: Path, workdir: Path, prompt: str) -> list[str]:
        args = [
            str(binary),
            "--print",
            "--add-dir", str(workdir),
            # acceptEdits allows writing output.md without prompting, while still
            # refusing anything riskier. The tool allowlist keeps the session to
            # reading and writing files in the scratch directory. Comma-separated in
            # one argument so the variadic flag cannot swallow what follows.
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
        ]
        if self.config.model:
            args += ["--model", self.config.model]
        args += self.config.extra_args
        return args
