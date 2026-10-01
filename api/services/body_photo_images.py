"""Validate and normalize untrusted progress photos before private storage."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
FULL_EDGE = 2048
THUMB_EDGE = 320
_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}

# Pillow checks dimensions while decoding as well as our explicit product limit.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


@dataclass(frozen=True)
class NormalizedPhoto:
    full: bytes
    thumbnail: bytes
    width: int
    height: int


def _jpeg(image: Image.Image, quality: int) -> bytes:
    out = BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True, exif=b"")
    return out.getvalue()


def normalize_photo(data: bytes, content_type: str) -> NormalizedPhoto:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Размер фото должен быть не больше 10 МБ")
    expected = _FORMATS.get(content_type.lower())
    if expected is None:
        raise ValueError("Поддерживаются только JPEG и PNG")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format != expected:
                    raise ValueError("Тип файла не совпадает с изображением")
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("Слишком большое разрешение фото")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                full = oriented.convert("RGB")
        full.thumbnail((FULL_EDGE, FULL_EDGE), Image.Resampling.LANCZOS)
        thumb = full.copy()
        thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        return NormalizedPhoto(
            full=_jpeg(full, 85),
            thumbnail=_jpeg(thumb, 78),
            width=full.width,
            height=full.height,
        )
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Не удалось прочитать фото") from exc
