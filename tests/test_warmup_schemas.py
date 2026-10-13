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


def test_snapshot_fields_are_typed_and_omission_is_distinct():
    from api.schemas.sync import SyncSetSnapshot, SyncExerciseSnapshot
    assert "warmup_snapshot" in SyncSetSnapshot.model_fields
    assert "warmup_group" in SyncExerciseSnapshot.model_fields
    row = SyncSetSnapshot(client_uuid="legacy", set_number=1)
    assert "warmup_snapshot" not in row.model_fields_set
    assert "warmup_snapshot" in SyncSetSnapshot(client_uuid="legacy", set_number=1, warmup_snapshot=None).model_fields_set


@pytest.mark.parametrize("bad", [
    {"order": 0}, {"fraction": 1.1}, {"raw_weight_kg": -1},
    {"calculation_version": "invalid"}, {"group_id": "invalid"},
])
def test_invalid_row_snapshot_rejected(bad):
    from api.schemas.sync import SyncSetSnapshot
    from uuid import uuid4
    snapshot = {"group_id": str(uuid4()), "calculation_version": None, "order": 1,
        "fraction": None, "raw_weight_kg": None, "manually_edited": True, **bad}
    with pytest.raises(ValidationError):
        SyncSetSnapshot(client_uuid="row", set_number=1, warmup_snapshot=snapshot)


@pytest.mark.parametrize("case", ["algorithm", "active", "duplicate_version", "source_identity", "source_reps", "source_weight", "source_rir", "step", "inventory"])
def test_invalid_calculation_snapshot_rejected(case):
    from api.schemas.warmups import WarmupGroupSnapshot
    from tests.integration.test_sync_warmup_snapshots import group_fixture
    from uuid import uuid4
    group = group_fixture()
    version = group["versions"][0]
    if case == "algorithm": version["algorithm_version"] = 3
    if case == "active": group["active_version"] = str(uuid4())
    if case == "duplicate_version": group["versions"].append(version)
    if case == "source_identity": version["source"]["set_client_uuid"] = "invented-row-1"
    if case == "source_reps": version["source"]["reps"] = 1001
    if case == "source_weight": version["source"]["weight_kg"] = -1
    if case == "source_rir": version["source"]["rir"] = -1
    if case == "step": version["equipment"]["step"]["value"] = 0
    if case == "inventory": version["equipment"]["inventory"]["items"] = [{"weight": 5, "unit": "kg", "count": -1}]
    with pytest.raises(ValidationError):
        WarmupGroupSnapshot.model_validate(group)


def test_manual_group_retains_historical_versions_and_optional_metadata():
    from api.schemas.warmups import WarmupGroupSnapshot, snapshot_json
    from tests.integration.test_sync_warmup_snapshots import group_fixture
    group = group_fixture()
    group["active_version"] = None
    assert snapshot_json(WarmupGroupSnapshot.model_validate(group)) == group
    del group["versions"][0]["equipment"]["step"]
    del group["versions"][0]["settings"]["warnings"]
    assert snapshot_json(WarmupGroupSnapshot.model_validate(group)) == group


def test_new_algorithm_and_historical_calculation_round_trip_together():
    from copy import deepcopy
    from uuid import uuid4
    from api.schemas.warmups import WarmupGroupSnapshot, snapshot_json
    from tests.integration.test_sync_warmup_snapshots import group_fixture
    group = group_fixture()
    historical = deepcopy(group["versions"][0])
    updated = deepcopy(historical)
    updated["version"] = str(uuid4())
    updated["algorithm_version"] = 2
    group["versions"].append(updated)
    group["active_version"] = updated["version"]
    assert snapshot_json(WarmupGroupSnapshot.model_validate(group)) == group
    assert group["versions"][0] == historical
