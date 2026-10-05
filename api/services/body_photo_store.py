"""Private, filesystem-backed storage for body progress photos.

Files live on a persistent server volume outside the public /media mount.
The API owns access checks; this class only accepts server-generated keys.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_KEY = re.compile(r"^[0-9]+/[0-9a-f]{32}/(?:full|thumb)\.jpg$")


class FilePhotoStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid photo key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Photo path escapes storage root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp_name = temp.name
                os.chmod(temp_name, 0o600)
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        try:
            parent.rmdir()
        except OSError:
            pass
