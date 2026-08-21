from datetime import datetime, timezone

import pytest

from api.schemas.sync import SyncWorkoutSnapshot


BASE = {
    "client_uuid": "w-1",
    "source": "free",
    "started_at": datetime.now(timezone.utc),
}


def test_sync_snapshot_accepts_paused_state():
    row = SyncWorkoutSnapshot(
        **BASE,
        status="paused",
        paused_at=BASE["started_at"],
        paused_duration_seconds=30,
    )

    assert row.status == "paused"
    assert row.paused_at == BASE["started_at"]
    assert row.paused_duration_seconds == 30


def test_pause_duration_cannot_be_negative():
    with pytest.raises(ValueError):
        SyncWorkoutSnapshot(
            **BASE,
            status="active",
            paused_duration_seconds=-1,
        )
