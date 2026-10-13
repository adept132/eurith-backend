"""Plan generation/explicit draft preview use the effective configured formula."""
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from api.routers import plans
from api.schemas.plan import GenerationComparison
from api.services.models import AppUserProfile
from api.schemas.rest_timer import RestTimerPolicy
from tests.test_generate_endpoint import _ex


@pytest_asyncio.fixture
async def scenario(db, test_user, monkeypatch):
    profile = AppUserProfile(app_user_id=test_user.id, experience_level="intermediate",
                             settings={}, volume_budget={})
    db.add(profile)
    await db.commit()
    blueprint = SimpleNamespace(id=uuid.uuid4(), name="Split", length_days=1,
        slots=[SimpleNamespace(day_order=1, day=SimpleNamespace(name="Push", template_type="push", muscle_targets=[1]))])
    monkeypatch.setattr(plans, "_load_generation_context", AsyncMock(return_value=(profile, blueprint, [_ex(900010, "Грудь")], None)))
    monkeypatch.setattr(plans.VolumeService, "calculate_session_targets", AsyncMock(return_value={"chest": {"target_sets": 4}}))
    monkeypatch.setattr(plans, "_generation_comparison", AsyncMock(return_value=GenerationComparison(applied_from=date.today(), mode="full")))
    return profile


async def generated(client, auth_headers, config=None):
    response = await client.post("/plans/generate", headers=auth_headers, json={"config": {"seed": 7, **(config or {})}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_old_client_partial_profile_explicit_override_and_reproduction(client, auth_headers, scenario, db):
    original = await generated(client, auth_headers)
    assert original["inputs"]["rest_timer_policy"] == RestTimerPolicy().model_dump()
    assert "rest_timer_policy" not in scenario.settings  # No read-time writeback.
    scenario.settings = {"rest_timer_policy": {"base_seconds": {"tier2": 150}}}
    await db.commit()
    saved = await generated(client, auth_headers)
    assert saved["inputs"]["rest_timer_policy"]["base_seconds"] == {"tier1": 180, "tier2": 150, "tier3": 90}
    rows = saved["days"][0]["exercises"]
    assert rows == original["days"][0]["exercises"]
    assert saved["days"][0]["estimated_duration_seconds"] - original["days"][0]["estimated_duration_seconds"] == (rows[0]["target_sets"] - 1) * 30
    assert (await generated(client, auth_headers, {"rest_timer_policy": None}))["inputs"] == saved["inputs"]
    explicit = await generated(client, auth_headers, {"rest_timer_policy": {"base_seconds": {"tier2": 210}}})
    assert explicit["inputs"]["rest_timer_policy"]["base_seconds"]["tier2"] == 210
    assert scenario.settings["rest_timer_policy"]["base_seconds"]["tier2"] == 150
    scenario.settings = {"rest_timer_policy": {"base_seconds": {"tier2": 300}}}
    await db.commit()
    repeated = await generated(client, auth_headers, {"rest_timer_policy": explicit["inputs"]["rest_timer_policy"]})
    assert repeated["days"] == explicit["days"]
    assert repeated["inputs"]["rest_timer_policy"] == explicit["inputs"]["rest_timer_policy"]


@pytest.mark.asyncio
async def test_explicit_preview_reestimates_without_changing_rows(client, auth_headers, scenario):
    original = await generated(client, auth_headers)
    response = await client.post("/plans/generate/preview", headers=auth_headers, json={
        "days": original["days"], "config": {"rest_timer_policy": {"base_seconds": {"tier2": 150}}}})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["inputs"]["rest_timer_policy"]["base_seconds"]["tier2"] == 150
    assert preview["days"][0]["exercises"] == original["days"][0]["exercises"]
    assert preview["days"][0]["estimated_duration_seconds"] > original["days"][0]["estimated_duration_seconds"]
    fixed = await generated(client, auth_headers, {"timer_mode": "fixed", "fixed_rest_seconds": 75,
                                                 "rest_timer_policy": {"base_seconds": {"tier2": 600}}})
    fixed_old = await generated(client, auth_headers, {"timer_mode": "fixed", "fixed_rest_seconds": 75})
    assert fixed["days"] == fixed_old["days"]


@pytest.mark.asyncio
async def test_saved_rule_reestimates_with_explicit_formula_without_profile_or_warmup_write(client, auth_headers, scenario, db):
    scenario.settings = {"warmup": {"between_seconds": 0, "compound_count": 5},
        "rest_timer_policy": {"base_seconds": {"tier2": 300}},
        "generator_rules": [{"scope": "all", "command": {"type": "SET_ALL_SETS", "params": {"n": 3}}}]}
    await db.commit()
    result = await generated(client, auth_headers, {"rest_timer_policy": {"base_seconds": {"tier2": 150}}})
    day = result["days"][0]
    assert day["exercises"][0]["target_sets"] == 3
    assert day["estimated_duration_seconds"] == 90 + 60 + 3 * 45 + 2 * 150
    await db.refresh(scenario)
    assert scenario.settings["warmup"] == {"between_seconds": 0, "compound_count": 5}
    assert scenario.settings["rest_timer_policy"]["base_seconds"]["tier2"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [{"version": 2}, {"base_seconds": {"tier2": True}},
    {"effort_seconds": {"light": 17}}, {"personal_seconds": 30}])
async def test_explicit_invalid_policy_rejected_before_generation(client, auth_headers, scenario, policy):
    response = await client.post("/plans/generate", headers=auth_headers, json={"config": {"rest_timer_policy": policy}})
    assert response.status_code == 422, response.text
