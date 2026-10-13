"""Rest timer profile contract and server calculation parity."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.schemas.rest_timer import RestTimerPolicy, RestTimerPolicyPatch
from api.services.rest_timer_policy import (
    configured_rest_seconds, merge_rest_timer_policy, rest_timer_policy,
)


GOLDEN = Path(__file__).parent / "fixtures" / "rest_policy_golden.json"


def test_legacy_policy_defaults_and_deep_patch():
    current = rest_timer_policy({"language": "ru"})
    assert current.model_dump() == {
        "version": 1,
        "base_seconds": {"tier1": 180, "tier2": 120, "tier3": 90},
        "effort_seconds": {"light": -30, "medium": 0, "prefailure": 30, "failure": 60},
    }
    merged = merge_rest_timer_policy(current, RestTimerPolicyPatch.model_validate({
        "base_seconds": {"tier2": 150}, "effort_seconds": {"failure": 90},
    }))
    assert merged.base_seconds.model_dump() == {"tier1": 180, "tier2": 150, "tier3": 90}
    assert merged.effort_seconds.failure == 90
    assert configured_rest_seconds(2, "failure", merged) == 240


@pytest.mark.parametrize("payload", [
    {"version": 2}, {"version": None},
    {"base_seconds": None}, {"effort_seconds": None},
    {"base_seconds": {"tier2": None}}, {"effort_seconds": {"failure": None}},
    {"base_seconds": {"tier2": True}}, {"base_seconds": {"tier2": 150.5}},
    {"base_seconds": {"tier2": 152}}, {"base_seconds": {"tier2": 45}},
    {"base_seconds": {"tier2": 615}},
    {"effort_seconds": {"failure": True}}, {"effort_seconds": {"failure": 30.5}},
    {"effort_seconds": {"failure": 17}}, {"effort_seconds": {"failure": -75}},
    {"effort_seconds": {"failure": 195}},
])
def test_patch_rejects_invalid_values_and_server_version(payload):
    with pytest.raises(ValidationError):
        RestTimerPolicyPatch.model_validate(payload)


def test_missing_effort_ignores_custom_medium_and_easy_aliases_light():
    policy = merge_rest_timer_policy(rest_timer_policy(None), RestTimerPolicyPatch.model_validate({
        "effort_seconds": {"medium": 30},
    }))
    assert configured_rest_seconds(2, None, policy) == 120
    assert configured_rest_seconds(2, "unknown", policy) == 120
    assert configured_rest_seconds(2, "easy", policy) == 90


@pytest.mark.parametrize("tier", [None, True, 2.0, 99])
def test_missing_or_invalid_tier_uses_tier_two(tier):
    assert configured_rest_seconds(tier, "medium", rest_timer_policy(None)) == 120


@pytest.mark.parametrize("bad", [True, 1.0, None])
def test_full_policy_rejects_non_integer_version(bad):
    with pytest.raises(ValidationError):
        RestTimerPolicy.model_validate({"version": bad})


def test_golden_recommendations_with_zero_personalization():
    for case in json.loads(GOLDEN.read_text(encoding="utf-8")):
        assert case["personalSeconds"] == 0
        policy = RestTimerPolicy.model_validate(case["policy"])
        assert configured_rest_seconds(case["tier"], case["effort"], policy) == case["expected"]["recommendedSeconds"], case["name"]
