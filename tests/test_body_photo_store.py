from pathlib import Path

import pytest

from api.services.body_photo_store import FilePhotoStore


def test_store_round_trip_stays_under_private_root(tmp_path: Path) -> None:
    store = FilePhotoStore(tmp_path / "private")
    key = "12/1234567890abcdef1234567890abcdef/full.jpg"

    store.put(key, b"jpeg-data")

    assert store.get(key) == b"jpeg-data"
    assert (tmp_path / "private" / key).read_bytes() == b"jpeg-data"
    store.delete(key)
    store.delete(key)
    assert not (tmp_path / "private" / key).exists()


@pytest.mark.parametrize(
    "key",
    ["../outside.jpg", "/tmp/outside.jpg", "12/../outside.jpg", "12\\outside.jpg"],
)
def test_store_rejects_paths_outside_root(tmp_path: Path, key: str) -> None:
    store = FilePhotoStore(tmp_path / "private")

    with pytest.raises(ValueError):
        store.put(key, b"private")
