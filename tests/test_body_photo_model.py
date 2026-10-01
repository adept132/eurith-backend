from api.services.models import BodyMeasurement, BodyProgressPhoto, UserAnthropometry


def test_photo_metadata_has_private_owner_and_cleanup_state() -> None:
    columns = BodyProgressPhoto.__table__.columns
    assert {"app_user_id", "taken_on", "storage_key", "thumbnail_key", "state", "client_uuid"} <= set(columns.keys())
    assert columns["storage_key"].nullable is False
    assert columns["thumbnail_key"].nullable is False
    assert columns["app_user_id"].nullable is False


def test_body_entries_track_local_date_and_submitted_fields() -> None:
    assert "measured_on" in UserAnthropometry.__table__.columns
    assert "submitted_fields" in UserAnthropometry.__table__.columns
    assert "measured_on" in BodyMeasurement.__table__.columns
