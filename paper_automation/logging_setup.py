"""Logging: one dated file per run plus console output."""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SECRET_KEYS = ("password", "token", "webhook", "api_key", "secret", "auth")


class _RedactSecrets(logging.Filter):
    """Guard against a credential reaching a log file (spec section 39)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        lowered = message.lower()
        if any(key in lowered for key in _SECRET_KEYS) and "=" in message:
            parts = []
            for chunk in message.split():
                if "=" in chunk and any(k in chunk.lower() for k in _SECRET_KEYS):
                    parts.append(chunk.split("=", 1)[0] + "=***")
                else:
                    parts.append(chunk)
            record.msg = " ".join(parts)
            record.args = ()
        return True


def setup(log_dir: Path, tz: str, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    log_file = log_dir / f"run-{stamp}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")
    redact = _RedactSecrets()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    console.addFilter(redact)
    root.addHandler(console)

    return log_file
