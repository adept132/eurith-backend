"""Pure validation and identity helpers for imported exercise aliases."""

from __future__ import annotations

import hashlib
import unicodedata


EXTERNAL_NAME_MAX_CHARS = 200
NORMALIZED_EXTERNAL_NAME_MAX_CHARS = 3_600
EXERCISE_ID_MAX = 2_147_483_647
NORMALIZED_EXTERNAL_NAME_SHA256_HEX_CHARS = 64


def normalize_external_name(external_name: str) -> str:
    """Return the Unicode-stable alias identity without changing the raw value."""
    if not isinstance(external_name, str):
        raise TypeError("external_name must be a string")
    compatible = unicodedata.normalize("NFKC", external_name)
    collapsed = " ".join(compatible.split())
    return unicodedata.normalize("NFKC", collapsed.casefold())


def validate_external_name(external_name: str) -> str:
    """Validate raw and normalized bounds, returning the full normalized value.

    Unicode 15.1's largest single-code-point NFKC/casefold expansion is 18
    characters. Therefore a 200-character raw alias can normalize to at most
    3,600 characters. The database keeps that complete value in ``TEXT``;
    callers must never truncate it because truncation would merge identities.
    """
    if not isinstance(external_name, str):
        raise TypeError("external_name must be a string")
    if not 1 <= len(external_name) <= EXTERNAL_NAME_MAX_CHARS:
        raise ValueError(
            f"external_name must contain 1..{EXTERNAL_NAME_MAX_CHARS} characters"
        )

    normalized_name = normalize_external_name(external_name)
    if not normalized_name:
        raise ValueError("external_name must contain non-whitespace text")
    if len(normalized_name) > NORMALIZED_EXTERNAL_NAME_MAX_CHARS:
        raise ValueError(
            "normalized external_name exceeds the supported Unicode expansion bound"
        )
    return normalized_name


def normalized_external_name_sha256_from_normalized(normalized_name: str) -> str:
    """Return the fixed-size B-tree key for a validated normalized identity."""
    return hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()


def normalized_external_name_sha256(external_name: str) -> str:
    """Normalize, validate, and return the fixed-size identity key."""
    return normalized_external_name_sha256_from_normalized(
        validate_external_name(external_name)
    )


def validate_exercise_id(exercise_id: int) -> int:
    """Validate the PostgreSQL ``INTEGER`` foreign-key range before SQL binding."""
    if type(exercise_id) is not int or not 1 <= exercise_id <= EXERCISE_ID_MAX:
        raise ValueError(f"exercise_id must be in 1..{EXERCISE_ID_MAX}")
    return exercise_id
