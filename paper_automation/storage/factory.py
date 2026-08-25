"""Selects a StorageBackend implementation from config.storage_backend."""

from ..config import Config, ConfigError
from .base import StorageBackend
from .local import LocalStorage


def build_storage(cfg: Config) -> StorageBackend:
    if cfg.storage_backend == "local":
        return LocalStorage()
    # config.load() already rejects "gdrive" (and anything else) at load time,
    # so reaching here means a Config was built by hand with a bad value.
    raise ConfigError(f"Unsupported storage_backend: {cfg.storage_backend!r}")
