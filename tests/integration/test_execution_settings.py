import uuid
import pytest

pytestmark = pytest.mark.asyncio


async def test_execution_default_is_off_and_patch_round_trips_without_changing_other_settings(client, db, test_user):
    from api.services.models import AppUserProfile
    db.add(AppUserProfile(app_user_id=test_user.id, settings={'weight_unit':'kg'}))
    await db.commit()
    original = await client.get('/profile')
    assert original.status_code == 200, original.text
    assert original.json()['settings']['executable_session']['enabled'] is False
    before = original.json()['settings'].get('weight_unit')
    value = dict(enabled=True, preference_epoch=str(uuid.uuid4()))
    saved = await client.patch('/profile/settings', json=dict(executable_session=value))
    assert saved.status_code == 200, saved.text
    assert saved.json()['settings']['executable_session'] == value
    after = await client.get('/profile')
    assert after.json()['settings']['executable_session'] == value
    assert after.json()['settings'].get('weight_unit') == before
