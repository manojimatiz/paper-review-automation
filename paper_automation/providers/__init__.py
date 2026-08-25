from .base import CliProvider, MockProvider

__all__ = ["CliProvider", "MockProvider", "build_providers"]


def build_providers(cfg):
    """Return (review_provider, revision_provider).

    provider_mode "auto" means mock in test mode, the real CLIs otherwise.
    """
    mode = (cfg.provider_mode or "auto").lower()
    if mode == "mock" or (mode == "auto" and cfg.test_mode):
        return MockProvider(), MockProvider()

    from .claude_code import ClaudeCodeProvider
    from .codex import CodexProvider

    return CodexProvider(cfg.codex), ClaudeCodeProvider(cfg.claude)
