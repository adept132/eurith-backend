from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from api.routers import analytics


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_zone, expected', [('Europe/Moscow', 'Europe/Moscow'), (None, 'UTC')])
async def test_capabilities_reads_only_authenticated_profile_timezone(monkeypatch, profile_zone, expected):
    db = SimpleNamespace(scalar=AsyncMock(return_value=profile_zone))
    monkeypatch.setattr(analytics.state, 'readiness', AsyncMock(return_value={'state': 'ready', 'data_revision': 3}))
    monkeypatch.setattr(analytics, 'analytics_enabled', lambda: True)
    request = Request({'type': 'http', 'query_string': b''})
    result = await analytics.capabilities(request, SimpleNamespace(id=123), db)
    assert result.model_dump()['timezone'] == expected
    query = db.scalar.call_args.args[0]
    assert 'app_user_profiles.app_user_id' in str(query)
    assert 123 in query.compile().params.values()
