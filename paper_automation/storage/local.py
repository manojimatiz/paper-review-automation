"""Local filesystem backend.

Also covers Google Drive folders synced to disk by Drive for Desktop, which is
the common case on Windows.
"""

import shutil
from pathlib import Path

from .base import StorageBackend, TargetExistsError


class LocalStorage(StorageBackend):
    def find_folder(self, path: Path) -> Path | None:
        return path if path.is_dir() else None

    def list_folders(self, path: Path) -> list[Path]:
        if not path.is_dir():
            return []
        return sorted(
            (p for p in path.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
        )

    def list_files(self, path: Path) -> list[Path]:
        if not path.is_dir():
            return []
        return sorted(
            (p for p in path.iterdir() if p.is_file()), key=lambda p: p.name.lower()
        )

    def file_exists(self, path: Path) -> bool:
        return path.is_file()

    def download_file(self, path: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        return destination

    def upload_file(self, source: Path, destination: Path) -> Path:
        if destination.exists():
            raise TargetExistsError(
                f"Refusing to overwrite an existing file: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def create_folder(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path
