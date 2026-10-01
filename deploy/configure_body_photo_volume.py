"""One-time, idempotent host setup for the private body photo bind mount.

Run as root in a local, offline Docker container with /opt/eurith bound at /host.
The script deliberately prints no Compose values, which may contain secrets.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import pathlib
import re
import stat
import sys
import tempfile

import yaml


ROOT = pathlib.Path("/host")
COMPOSE = ROOT / "compose.yaml"
BACKUPS = ROOT / "backups"
PHOTO_HOST = ROOT / "private" / "body-photos"
PHOTO_CONTAINER = "/var/lib/eurith/body-photos"
VOLUME = f"/opt/eurith/private/body-photos:{PHOTO_CONTAINER}:rw"


def require_regular(path: pathlib.Path, mode: int | None = None) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"unsafe regular file: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError(f"unexpected mode: {path}")
    return info


def require_directory(path: pathlib.Path, mode: int | None = None) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe directory: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError(f"unexpected mode: {path}")
    return info


def ensure_photo_directory() -> None:
    for path in (PHOTO_HOST.parent, PHOTO_HOST):
        if not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
            os.chown(path, 1000, 1000)
            os.chmod(path, 0o700)
        info = require_directory(path, 0o700)
        if (info.st_uid, info.st_gid) != (1000, 1000):
            raise RuntimeError(f"unexpected owner: {path}")


def patch_compose(raw: str) -> str:
    newline = "\r\n" if "\r\n" in raw else "\n"
    if "\n" not in raw:
        raise RuntimeError("Compose format not recognized")
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("services"), dict):
        raise RuntimeError("Compose services missing")
    api = parsed["services"].get("api")
    if not isinstance(api, dict) or not isinstance(api.get("environment"), dict) or not isinstance(api.get("volumes"), list):
        raise RuntimeError("Compose API shape changed")
    environment = api["environment"]
    volumes = api["volumes"]
    if "BODY_PHOTO_ROOT" in environment or any("body-photos" in str(item) for item in volumes):
        if environment.get("BODY_PHOTO_ROOT") == PHOTO_CONTAINER and VOLUME in volumes:
            return raw
        raise RuntimeError("conflicting body photo configuration")

    # Edit only the API service. Preserve every other byte of the host Compose.
    lines = raw.splitlines(keepends=True)
    api_start = next((index for index, line in enumerate(lines) if line.rstrip("\r\n") == "  api:"), None)
    if api_start is None:
        raise RuntimeError("API service marker missing")
    api_end = next((index for index in range(api_start + 1, len(lines)) if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[index])), len(lines))
    environment_lines = [index for index in range(api_start + 1, api_end) if lines[index].rstrip("\r\n") == "    environment:"]
    volume_lines = [index for index in range(api_start + 1, api_end) if lines[index].rstrip("\r\n") == "    volumes:"]
    if len(environment_lines) != 1 or len(volume_lines) != 1:
        raise RuntimeError("API environment or volume marker ambiguous")
    insertions = [
        (environment_lines[0] + 1, f"      BODY_PHOTO_ROOT: {PHOTO_CONTAINER}{newline}"),
        (volume_lines[0] + 1, f"      - {VOLUME}{newline}"),
    ]
    for index, value in sorted(insertions, reverse=True):
        lines.insert(index, value)
    updated = "".join(lines)
    expected = copy.deepcopy(parsed)
    expected["services"]["api"]["environment"]["BODY_PHOTO_ROOT"] = PHOTO_CONTAINER
    expected["services"]["api"]["volumes"].insert(0, VOLUME)
    if yaml.safe_load(updated) != expected:
        raise RuntimeError("Compose semantic comparison failed")
    return updated


def write_exclusive(path: pathlib.Path, data: bytes, uid: int, gid: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def main() -> None:
    os.umask(0o077)
    require_directory(ROOT)
    backup_info = require_directory(BACKUPS, 0o700)
    if (backup_info.st_uid, backup_info.st_gid) != (0, 0):
        raise RuntimeError("backup directory owner changed")
    compose_info = require_regular(COMPOSE, 0o600)
    if (compose_info.st_uid, compose_info.st_gid) != (1000, 1000):
        raise RuntimeError("Compose owner changed")
    original = COMPOSE.read_bytes()
    updated = patch_compose(original.decode("utf-8"))
    if sys.argv[1:] == ["--check"]:
        print("body_photo_compose=would_update" if updated.encode("utf-8") != original else "body_photo_compose=already_configured")
        return
    if sys.argv[1:]:
        raise RuntimeError("usage: configure_body_photo_volume.py [--check]")
    ensure_photo_directory()
    if updated.encode("utf-8") == original:
        print("body_photo_compose=already_configured")
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUPS / f"compose-before-body-photos-{stamp}.yaml"
    write_exclusive(backup, original, 0, 0)
    fd, temp_name = tempfile.mkstemp(prefix=".compose-body-photos-", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, 1000, 1000)
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, COMPOSE)
        directory_fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"body_photo_compose=updated backup={backup}")


if __name__ == "__main__":
    main()
