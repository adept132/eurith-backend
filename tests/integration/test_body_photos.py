import asyncio
import uuid
from datetime import date, timedelta
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from api.services.models import AppUser, BodyProgressPhoto, UserCalendarDay
from api.services.body_photo_store import FilePhotoStore
from api.services.body_photos import cleanup_pending_photos
from api.services.account_service import data_summary, purge_user


def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (80, 60), "blue").save(out, "JPEG")
    return out.getvalue()


def _payload(client_uuid: str | None = None):
    return {
        "data": {
            "taken_on": (date.today() - timedelta(days=1)).isoformat(),
            "angle": "front",
            "client_uuid": client_uuid or uuid.uuid4().hex,
        },
        "files": {"file": ("progress.jpg", _jpeg(), "image/jpeg")},
    }


@pytest.mark.asyncio
async def test_owner_can_upload_list_read_and_delete(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))

    created = await client.post("/body/photos", **_payload())
    assert created.status_code == 201, created.text
    photo = created.json()
    assert "storage_key" not in photo
    assert photo["angle"] == "front"
    assert len(list(root.rglob("*.jpg"))) == 2

    listed = await client.get("/body/photos")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [photo["id"]]

    content = await client.get(f"/body/photos/{photo['id']}/content")
    thumb = await client.get(f"/body/photos/{photo['id']}/thumbnail")
    assert content.status_code == thumb.status_code == 200
    assert content.headers["cache-control"] == "private, no-store"
    assert thumb.headers["cache-control"] == "private, no-store"
    assert Image.open(BytesIO(content.content)).format == "JPEG"

    deleted = await client.delete(f"/body/photos/{photo['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/body/photos/{photo['id']}/content")).status_code == 404
    assert list(root.rglob("*.jpg")) == []
    rows = (await db.execute(select(BodyProgressPhoto).where(BodyProgressPhoto.app_user_id == test_user.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_upload_uuid_is_idempotent(client, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    key = uuid.uuid4().hex
    first = await client.post("/body/photos", **_payload(key))
    second = await client.post("/body/photos", **_payload(key))
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(list(root.rglob("*.jpg"))) == 2


@pytest.mark.asyncio
async def test_parallel_upload_same_uuid_keeps_one_object_pair(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    key = uuid.uuid4().hex
    responses = await asyncio.gather(
        client.post("/body/photos", **_payload(key)),
        client.post("/body/photos", **_payload(key)),
    )
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert len(list(root.rglob("*.jpg"))) == 2
    rows = (await db.execute(select(BodyProgressPhoto).where(BodyProgressPhoto.app_user_id == test_user.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_other_users_photo_is_not_revealed(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    other = AppUser(firebase_uid=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@example.com")
    db.add(other)
    await db.flush()
    photo = BodyProgressPhoto(
        app_user_id=other.id, taken_on=date.today(), angle="front",
        storage_key=f"{other.id}/{uuid.uuid4().hex}/full.jpg",
        thumbnail_key=f"{other.id}/{uuid.uuid4().hex}/thumb.jpg",
        mime_type="image/jpeg", byte_size=3, width=1, height=1, state="active",
    )
    db.add(photo)
    await db.commit()
    try:
        assert (await client.get("/body/photos")).json()["items"] == []
        assert (await client.get(f"/body/photos/{photo.id}/content")).status_code == 404
        assert (await client.get(f"/body/photos/{photo.id}/thumbnail")).status_code == 404
        assert (await client.delete(f"/body/photos/{photo.id}")).status_code == 404
    finally:
        await db.delete(other)
        await db.commit()


@pytest.mark.asyncio
async def test_missing_private_root_blocks_photo_upload(client, monkeypatch):
    monkeypatch.delenv("BODY_PHOTO_ROOT", raising=False)
    response = await client.post("/body/photos", **_payload())
    assert response.status_code == 503



@pytest.mark.asyncio
async def test_photo_list_has_stable_cursor_pagination(client, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    ids = set()
    for _ in range(3):
        response = await client.post("/body/photos", **_payload())
        assert response.status_code == 201
        ids.add(response.json()["id"])

    first = (await client.get("/body/photos", params={"limit": 2})).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    second = (await client.get("/body/photos", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert {row["id"] for row in first["items"] + second["items"]} == ids
    assert (await client.get("/body/photos", params={"cursor": "###"})).status_code == 422


@pytest.mark.asyncio
async def test_comparison_uses_explicit_dates_and_calendar(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    before_day = date.today() - timedelta(days=20)
    after_day = before_day + timedelta(days=14)
    photo_ids = []
    for day in (before_day, after_day):
        payload = _payload()
        payload["data"]["taken_on"] = day.isoformat()
        response = await client.post("/body/photos", **payload)
        assert response.status_code == 201
        photo_ids.append(response.json()["id"])
    for day, weight in ((before_day, 80), (after_day, 78)):
        response = await client.post("/body/entry", json={"measured_on": day.isoformat(), "weight": weight})
        assert response.status_code == 200, response.text
    db.add_all([
        UserCalendarDay(app_user_id=test_user.id, target_date=before_day, status="completed", is_rest_day=False),
        UserCalendarDay(app_user_id=test_user.id, target_date=before_day + timedelta(days=1), status="missed", is_rest_day=False),
        UserCalendarDay(app_user_id=test_user.id, target_date=before_day + timedelta(days=2), status="planned", is_rest_day=True),
        UserCalendarDay(app_user_id=test_user.id, target_date=before_day + timedelta(days=3), status="planned", is_blackout=True),
    ])
    await db.commit()
    comparison = await client.get("/body/comparison", params={
        "before_photo_id": photo_ids[0], "after_photo_id": photo_ids[1],
    })
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    assert body["body_metrics"]["weight"]["delta"] == -2
    assert body["body_metrics"]["weight"]["before"]["date"] == before_day.isoformat()
    assert body["adherence"] == {"planned_days": 2, "completed_days": 1, "missed_days": 1, "percent": 50}
    assert body["strength"] == []
    assert (await client.get("/body/comparison", params={
        "before_photo_id": photo_ids[1], "after_photo_id": photo_ids[0],
    })).status_code == 400


@pytest.mark.asyncio
async def test_delete_failure_hides_photo_and_retry_cleans_it(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    photo_id = (await client.post("/body/photos", **_payload())).json()["id"]
    original_delete = FilePhotoStore.delete

    def fail_delete(self, key):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(FilePhotoStore, "delete", fail_delete)
    assert (await client.delete(f"/body/photos/{photo_id}")).status_code == 204
    assert (await client.get(f"/body/photos/{photo_id}/content")).status_code == 404
    row = (await db.execute(select(BodyProgressPhoto).where(BodyProgressPhoto.id == uuid.UUID(photo_id)))).scalar_one()
    assert row.state == "deleting"
    monkeypatch.setattr(FilePhotoStore, "delete", original_delete)
    assert await cleanup_pending_photos(db, test_user.id) == 1
    assert list(root.rglob("*.jpg")) == []


@pytest.mark.asyncio
async def test_account_purge_removes_private_photos_and_summary(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    assert (await client.post("/body/photos", **_payload())).status_code == 201
    assert (await data_summary(db, test_user.id))["body_photos"] == 1
    original_delete = FilePhotoStore.delete
    monkeypatch.setattr(FilePhotoStore, "delete", lambda self, key: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        await purge_user(db, test_user.id)
    await db.rollback()
    assert (await data_summary(db, test_user.id))["body_photos"] == 1
    monkeypatch.setattr(FilePhotoStore, "delete", original_delete)
    assert (await purge_user(db, test_user.id))["body_photos"] == 1
    assert list(root.rglob("*.jpg")) == []


@pytest.mark.asyncio
async def test_invalid_upload_rejected_before_any_private_file(client, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    invalid = _payload()
    invalid["files"] = {"file": ("bad.jpg", b"not a photo", "image/jpeg")}
    assert (await client.post("/body/photos", **invalid)).status_code == 400
    oversized = _payload()
    oversized["files"] = {"file": ("huge.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")}
    assert (await client.post("/body/photos", **oversized)).status_code == 413
    assert list(root.rglob("*.jpg")) == []


@pytest.mark.asyncio
async def test_comparison_rejects_foreign_photo(client, db, test_user, tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BODY_PHOTO_ROOT", str(root))
    own = (await client.post("/body/photos", **_payload())).json()["id"]
    other = AppUser(firebase_uid=uuid.uuid4().hex, email=f"{uuid.uuid4().hex}@example.com")
    db.add(other)
    await db.flush()
    foreign = BodyProgressPhoto(
        app_user_id=other.id, taken_on=date.today(), angle="front",
        storage_key=f"{other.id}/{uuid.uuid4().hex}/full.jpg",
        thumbnail_key=f"{other.id}/{uuid.uuid4().hex}/thumb.jpg",
        mime_type="image/jpeg", byte_size=3, width=1, height=1, state="active",
    )
    db.add(foreign)
    await db.commit()
    assert (await client.get("/body/comparison", params={
        "before_photo_id": own, "after_photo_id": str(foreign.id),
    })).status_code == 404
    await db.delete(other)
    await db.commit()


@pytest.mark.asyncio
async def test_body_entry_date_and_carried_values_are_distinguished(client):
    first = date.today() - timedelta(days=10)
    second = first + timedelta(days=3)
    assert (await client.post("/body/entry", json={
        "measured_on": first.isoformat(), "weight": 80, "body_fat": 20,
    })).status_code == 200
    response = await client.post("/body/entry", json={
        "measured_on": second.isoformat(), "weight": 79,
    })
    assert response.status_code == 200
    assert len(response.json()["history"]["body_fat"]) == 1
    entries = (await client.get("/body/entries")).json()
    assert [entry["date"] for entry in entries] == [second.isoformat(), first.isoformat()]
    assert not any(entry["legacy"] for entry in entries)


@pytest.mark.asyncio
async def test_backdated_entry_does_not_replace_current_body_value(client):
    recent = date.today() - timedelta(days=1)
    older = recent - timedelta(days=30)
    assert (await client.post("/body/entry", json={
        "measured_on": recent.isoformat(), "weight": 80, "measurements": {"waist": 90},
    })).status_code == 200
    response = await client.post("/body/entry", json={
        "measured_on": older.isoformat(), "weight": 75, "measurements": {"waist": 85},
    })
    assert response.status_code == 200
    overview = response.json()
    assert overview["latest_weight"] == 80
    assert overview["measurements"]["waist"] == 90
    assert [point["date"] for point in overview["history"]["weight"]] == [older.isoformat(), recent.isoformat()]
