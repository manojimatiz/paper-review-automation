"""File identity: a SHA-256 fingerprint used to detect edits and duplicates.

Hashing reads the file directly from disk, the same way docx_io already does —
paths handed around the pipeline are real filesystem paths under the configured
storage root (see paper_automation/storage), not opaque backend handles.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

_CHUNK_SIZE = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    size: int
    modified_time: float

    @classmethod
    def of(cls, path: Path) -> "Fingerprint":
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        return cls(sha256=digest.hexdigest(), size=stat.st_size, modified_time=stat.st_mtime)
