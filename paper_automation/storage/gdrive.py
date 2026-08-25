"""Google Drive backend — not implemented.

Not needed for the current deployment: Drive for Desktop syncs the folder tree to
a local path, so LocalStorage handles Drive-hosted papers already. Point
`research_papers_root` at the synced path.

If direct API access is ever required, implement StorageBackend here using
google-api-python-client. The only real work is maintaining a logical-path to
folder-ID cache, since Drive addresses everything by ID:

  - find_folder   -> files.list with q="name=? and mimeType='application/vnd.google-apps.folder' and ? in parents"
  - list_folders  -> same query, filtered to the folder mimeType
  - list_files    -> same query, excluding the folder mimeType
  - download_file -> files.get_media
  - upload_file   -> files.create with a parent ID; must check existence first and refuse to overwrite
  - create_folder -> files.create with the folder mimeType

Credentials belong in an environment variable or a secure store, never in code
(spec section 39).
"""

from pathlib import Path

from .base import StorageBackend


class GoogleDriveStorage(StorageBackend):
    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "The Google Drive backend is not implemented. Use Drive for Desktop and "
            "point research_papers_root at the synced local folder, or implement "
            "this class."
        )

    def find_folder(self, path: Path) -> Path | None: ...
    def list_folders(self, path: Path) -> list[Path]: ...
    def list_files(self, path: Path) -> list[Path]: ...
    def file_exists(self, path: Path) -> bool: ...
    def download_file(self, path: Path, destination: Path) -> Path: ...
    def upload_file(self, source: Path, destination: Path) -> Path: ...
    def create_folder(self, path: Path) -> Path: ...
