"""Language acknowledgement must match the queued profile-settings mutation."""
import pytest
import pytest_asyncio

from api.services.models import AppUserProfile


@pytest_asyncio.fixture
async def language_profile(db, test_user):
    profile = AppUserProfile(app_user_id=test_user.id, settings={
        "weight_unit": "lbs",
        "weight_steps": {"stack": 5},
        "plate_config_lbs": {"plates": [{"weight": 10, "count": 2}]},
        "custom_future_setting": {"enabled": True},
    })
    db.add(profile)
    await db.commit()
    return profile


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_language_patch_is_acknowledged_and_read_back(
    client, auth_headers, language_profile, db, language
):
    original = dict(language_profile.settings)
    response = await client.patch("/profile/settings", headers=auth_headers,
                                  json={"language": language})
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {**original, "language": language}
    reread = await client.get("/profile", headers=auth_headers)
    assert reread.status_code == 200, reread.text
    assert reread.json()["settings"] == {**original, "language": language}
    await db.refresh(language_profile)
    assert language_profile.settings == {**original, "language": language}
    unrelated = await client.patch("/profile/settings", headers=auth_headers,
                                   json={"reminders_enabled": False})
    assert unrelated.json()["settings"]["language"] == language


@pytest.mark.asyncio
async def test_legacy_profile_returns_default_language_without_destroying_settings(
    client, auth_headers, language_profile, db
):
    original = dict(language_profile.settings)
    response = await client.get("/profile", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {**original, "language": "ru"}
    await db.refresh(language_profile)
    assert language_profile.settings == original
    patch = await client.patch("/profile/settings", headers=auth_headers,
                               json={"weight_unit": "kg"})
    assert patch.status_code == 200, patch.text
    assert patch.json()["settings"] == {**original, "weight_unit": "kg", "language": "ru"}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["de", "", "EN", 5, True, ["ru"]])
async def test_invalid_language_is_rejected_without_mutating_profile(
    client, auth_headers, language_profile, db, language
):
    original = dict(language_profile.settings)
    response = await client.patch("/profile/settings", headers=auth_headers,
                                  json={"language": language, "weight_unit": "kg"})
    assert response.status_code == 422, response.text
    await db.refresh(language_profile)
    assert language_profile.settings == original