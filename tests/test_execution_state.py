from datetime import datetime, timezone
import pytest
from api.schemas.sync import SyncWorkoutSnapshot


def test_sync_preserves_execution_metadata_instead_of_silently_dropping_it():
    state = dict(schema_version=1, enabled=True, preference_epoch='legacy-default-v1',
                 initialized_at='2026-10-18T12:00:00Z', changed_at='2026-10-18T12:00:00Z', banner_dismissed_at=None)
    snapshot = SyncWorkoutSnapshot.model_validate(dict(client_uuid='test', source='free', status='active',
        started_at=datetime.now(timezone.utc), execution_state=state, exercises=[]))
    assert snapshot.model_dump(mode='json')['execution_state']['enabled'] is True


def test_sync_rejects_draft_metadata_without_a_real_set_identity():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SyncWorkoutSnapshot.model_validate(dict(client_uuid='test', source='free', status='active',
            started_at=datetime.now(timezone.utc), exercises=[dict(client_uuid='exercise', exercise_id=42,
            order_index=0, execution_state=dict(schema_version=1, exercise_id=42,
                slots={'1':dict(state='draft',set_client_uuid=None,updated_at='2026-10-18T12:00:00Z')}), sets=[])]))
