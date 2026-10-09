"""Warmup settings validation and inheritance patch contracts."""
import pytest
from pydantic import ValidationError

from api.schemas.warmups import WarmupDefaults, WarmupDefaultsPatch, WarmupOverride


def test_defaults_and_zero_rest():
    assert WarmupDefaults().model_dump() == {
        "compound_count": 3, "isolation_count": 2, "between_seconds": 30,
        "before_work": {"mode": "working"},
    }
    assert WarmupDefaults(between_seconds=0, before_work={"mode": "seconds", "seconds": 0}).before_work.seconds == 0


@pytest.mark.parametrize("model,values", [
    (WarmupDefaults, {"compound_count": 0}),
    (WarmupDefaults, {"isolation_count": 1.5}),
    (WarmupDefaults, {"compound_count": True}),
    (WarmupDefaults, {"between_seconds": -1}),
    (WarmupDefaults, {"before_work": {"mode": "seconds", "seconds": -1}}),
    (WarmupDefaults, {"before_work": {"mode": "seconds"}}),
    (WarmupDefaults, {"before_work": {"mode": "working", "seconds": 5}}),
    (WarmupDefaultsPatch, {"compound_count": None}),
    (WarmupOverride, {"count": 0}),
    (WarmupOverride, {"count": 1.5}),
    (WarmupOverride, {"between_seconds": -1}),
    (WarmupOverride, {"before_work": {"mode": "seconds", "seconds": -1}}),
])
def test_invalid_settings(model, values):
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_partial_and_null_override_are_distinct():
    assert WarmupDefaultsPatch(compound_count=4).model_dump(exclude_unset=True) == {"compound_count": 4}
    assert WarmupOverride(count=None).model_dump(exclude_unset=True) == {"count": None}
    assert WarmupOverride().model_dump(exclude_unset=True) == {}
