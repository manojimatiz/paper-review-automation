"""Which models are available, and which one is actually being used.

The hard requirement here is that the list must not go stale when a new model
ships. A hard-coded catalogue fails that on day one, so this uses four mechanisms
in order of reliability:

1. **Aliases.** Both CLIs accept an alias — "opus", "sonnet" — that resolves to the
   latest model of that family. Choosing an alias means never editing anything
   when a new version lands. This is the recommended option and the default.

2. **Detection.** Both CLIs write a session log recording the model they actually
   ran. Reading it reports the truth rather than what we assume, and any model seen
   that way is added to the list automatically.

3. **User entry.** A model can always be typed in by hand, so a brand-new model is
   usable the day it is announced without waiting for this file to be updated.
   Anything entered is remembered and offered next time.

4. **models.json.** A plain file next to the config that can be edited to add or
   relabel entries permanently.

Deliberately absent: any attempt to invent OpenAI model names. The Codex CLI does
not enumerate its models and guessing would put names in a dropdown that fail only
when a user selects them.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

REGISTRY_FILENAME = "models.json"

# Where each CLI records what it actually ran.
_SESSION_LOGS = {
    "codex": (Path.home() / ".codex" / "sessions", "*.jsonl"),
    "claude": (Path.home() / ".claude" / "projects", "*.jsonl"),
}
_MODEL_IN_LOG = re.compile(r'"model"\s*:\s*"([^"]+)"')


@dataclass
class ModelOption:
    id: str
    label: str
    note: str = ""
    # An alias tracks the newest model in its family, so it never needs updating.
    alias: bool = False
    # Rough capability order, for "upgrade or downgrade" sense in the UI.
    tier: int = 2
    source: str = "built-in"


def _claude_defaults() -> list[ModelOption]:
    return [
        ModelOption("", "Default (whatever Claude Code is set to)", "Follows the app's own setting.", tier=2),
        ModelOption("opus", "Opus — most capable", "Alias: always the newest Opus.", alias=True, tier=3),
        ModelOption("sonnet", "Sonnet — balanced", "Alias: always the newest Sonnet.", alias=True, tier=2),
        ModelOption("haiku", "Haiku — fastest, cheapest", "Alias: always the newest Haiku.", alias=True, tier=1),
        ModelOption("claude-opus-5", "Claude Opus 5", "Pinned version.", tier=3),
        ModelOption("claude-sonnet-5", "Claude Sonnet 5", "Pinned version.", tier=2),
        ModelOption("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "Pinned version.", tier=1),
    ]


def _codex_defaults() -> list[ModelOption]:
    # The Codex CLI accepts a free-form --model and provides no way to list what is
    # available, so only the default is offered here. Real model names arrive
    # through detection, or by being typed in once.
    return [
        ModelOption("", "Default (whatever Codex is set to)", "Follows the Codex app's own setting.", tier=2),
    ]


DEFAULTS = {"claude": _claude_defaults, "codex": _codex_defaults}


@dataclass
class Registry:
    claude: list[ModelOption] = field(default_factory=list)
    codex: list[ModelOption] = field(default_factory=list)

    def for_provider(self, provider: str) -> list[ModelOption]:
        return self.claude if provider == "claude" else self.codex

    def ids(self, provider: str) -> list[str]:
        return [option.id for option in self.for_provider(provider)]


def _merge(base: list[ModelOption], extra: list[ModelOption]) -> list[ModelOption]:
    """Add entries whose id is not already present, preserving order."""
    seen = {option.id for option in base}
    for option in extra:
        if option.id not in seen:
            seen.add(option.id)
            base.append(option)
    return base


def load(registry_path: Path | None = None) -> Registry:
    """Built-in options, plus anything in models.json, plus anything detected."""
    registry = Registry(claude=_claude_defaults(), codex=_codex_defaults())

    if registry_path and registry_path.exists():
        try:
            raw = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            # A broken file must not stop the pipeline; the built-ins still work.
            log.warning("Ignoring %s: %s", registry_path, exc)
        else:
            for provider in ("claude", "codex"):
                entries = [
                    ModelOption(
                        id=str(item.get("id", "")),
                        label=str(item.get("label") or item.get("id", "")),
                        note=str(item.get("note", "")),
                        alias=bool(item.get("alias", False)),
                        tier=int(item.get("tier", 2)),
                        source="models.json",
                    )
                    for item in raw.get(provider, [])
                    if isinstance(item, dict)
                ]
                _merge(registry.for_provider(provider), entries)

    for provider in ("claude", "codex"):
        detected = detect_recent_models(provider)
        _merge(
            registry.for_provider(provider),
            [
                ModelOption(mid, mid, "Seen in a recent session.", source="detected")
                for mid in detected
            ],
        )
    return registry


def remember(registry_path: Path, provider: str, model_id: str) -> bool:
    """Persist a hand-entered model so it appears in the list next time."""
    model_id = (model_id or "").strip()
    if not model_id or provider not in DEFAULTS:
        return False
    if model_id in {option.id for option in DEFAULTS[provider]()}:
        return False

    raw = {}
    if registry_path.exists():
        try:
            raw = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            raw = {}
    entries = raw.setdefault(provider, [])
    if any(isinstance(e, dict) and e.get("id") == model_id for e in entries):
        return False

    entries.append({"id": model_id, "label": model_id, "note": "Added by you."})
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return True


def detect_recent_models(provider: str, scan_files: int = 6) -> list[str]:
    """Model ids seen in the CLI's own recent session logs, newest first.

    Best effort by design: the logs are the CLI's private format and may change.
    A failure here costs a nicety, never a run.
    """
    entry = _SESSION_LOGS.get(provider)
    if not entry:
        return []
    root, pattern = entry
    if not root.is_dir():
        return []

    try:
        files = sorted(
            root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
        )[:scan_files]
    except OSError:
        return []

    found: list[str] = []
    for path in files:
        try:
            # Session logs grow large; the model appears early, so read a slice.
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(200_000)
        except OSError:
            continue
        for match in _MODEL_IN_LOG.finditer(text):
            model_id = match.group(1)
            if model_id and model_id not in found:
                found.append(model_id)
    return found


def active_model(provider: str, configured: str | None) -> dict:
    """What the next run will use, and what the last run actually used.

    `configured` wins when set. When it is blank the CLI picks, so the honest
    answer is the last model observed rather than a guess.
    """
    detected = detect_recent_models(provider, scan_files=2)
    last_used = detected[0] if detected else ""
    configured = (configured or "").strip()

    if configured:
        resolved = f"{configured} (latest in family)" if _is_alias(provider, configured) else configured
        return {
            "provider": provider,
            "configured": configured,
            "effective": resolved,
            "last_used": last_used,
            "pinned": True,
        }
    return {
        "provider": provider,
        "configured": "",
        "effective": last_used or "chosen by the app",
        "last_used": last_used,
        "pinned": False,
    }


def _is_alias(provider: str, model_id: str) -> bool:
    return any(
        option.id == model_id and option.alias for option in DEFAULTS[provider]()
    )


def as_dicts(options: list[ModelOption]) -> list[dict]:
    return [asdict(option) for option in options]
