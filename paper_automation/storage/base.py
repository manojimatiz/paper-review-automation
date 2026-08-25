"""Storage abstraction (spec section 31).

Folders and files are addressed by logical path so the processing logic never
depends on the backend. A Google Drive implementation maps these paths onto
folder/file IDs internally; the caller cannot tell the difference.

Deliberate omission: there is no delete or move operation anywhere in this
interface. The pipeline must never remove or relocate a user's files
(spec sections 33 and 49), so the capability simply does not exist.
"""

from abc import ABC, abstractmethod
from pathlib import Path


class StorageError(RuntimeError):
    pass


class TargetExistsError(StorageError):
    """Raised when a write would overwrite an existing file."""


class StorageBackend(ABC):
    @abstractmethod
    def find_folder(self, path: Path) -> Path | None:
        """Return the folder if it exists, else None. Never creates it."""

    @abstractmethod
    def list_folders(self, path: Path) -> list[Path]:
        """Immediate subdirectories, sorted deterministically."""

    @abstractmethod
    def list_files(self, path: Path) -> list[Path]:
        """Immediate files only. Subdirectories are never included."""

    @abstractmethod
    def file_exists(self, path: Path) -> bool: ...

    @abstractmethod
    def download_file(self, path: Path, destination: Path) -> Path:
        """Copy a stored file to a local path for reading."""

    @abstractmethod
    def upload_file(self, source: Path, destination: Path) -> Path:
        """Write a local file into storage. Must refuse to overwrite."""

    @abstractmethod
    def create_folder(self, path: Path) -> Path: ...
