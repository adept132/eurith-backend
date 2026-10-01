from io import BytesIO

import pytest
from PIL import Image

from api.services.body_photo_images import normalize_photo


def _image_bytes(fmt: str, size: tuple[int, int], *, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size, "red")
    out = BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def test_normalization_removes_exif_and_makes_private_thumbnail() -> None:
    source = _image_bytes("JPEG", (40, 20), orientation=6)

    result = normalize_photo(source, "image/jpeg")

    full = Image.open(BytesIO(result.full))
    thumb = Image.open(BytesIO(result.thumbnail))
    assert full.format == "JPEG"
    assert full.size == (20, 40)
    assert len(full.getexif()) == 0
    assert thumb.format == "JPEG"
    assert len(thumb.getexif()) == 0
    assert max(thumb.size) <= 320
    assert (result.width, result.height) == full.size


def test_normalization_resizes_large_png() -> None:
    result = normalize_photo(_image_bytes("PNG", (3000, 1500)), "image/png")
    full = Image.open(BytesIO(result.full))
    assert full.size == (2048, 1024)
    assert len(result.full) <= 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"not an image", "image/jpeg"),
        (_image_bytes("JPEG", (20, 20)), "image/png"),
        (b"x" * (10 * 1024 * 1024 + 1), "image/jpeg"),
    ],
    ids=["corrupted", "mismatched", "too-large"],
)
def test_normalization_rejects_invalid_input(data: bytes, content_type: str) -> None:
    with pytest.raises(ValueError):
        normalize_photo(data, content_type)


def test_normalization_rejects_too_many_pixels() -> None:
    source = _image_bytes("PNG", (5000, 4200))
    with pytest.raises(ValueError):
        normalize_photo(source, "image/png")
