from __future__ import annotations

from api.services.models import WorkoutPlan


def _plan(plan_id: int, name: str) -> WorkoutPlan:
    return WorkoutPlan(
        id=plan_id,
        app_user_id=42,
        name=name,
        day_tag="push",
        micro_tag="adaptive",
        meso_tag="adaptive",
    )


def test_plan_version_provenance_never_cascades_to_its_predecessor():
    old = _plan(10, "Push v1")
    new = _plan(11, "Push v2")

    old.is_archived = False
    new.supersedes_plan_id = old.id
    new.is_archived = True

    assert new.supersedes_plan_id == 10
    assert old.is_archived is False

    foreign_key = next(iter(WorkoutPlan.__table__.c.supersedes_plan_id.foreign_keys))
    assert foreign_key.target_fullname == "workout_plans.id"
    assert foreign_key.ondelete == "SET NULL"
    assert foreign_key.constraint.name == "fk_workout_plans_supersedes_plan_id"


def test_plan_versions_start_unarchived_at_revision_zero():
    assert WorkoutPlan.__table__.c.is_archived.default.arg is False
    assert WorkoutPlan.__table__.c.is_archived.server_default.arg == "false"
    assert WorkoutPlan.__table__.c.revision.default.arg == 0
    assert WorkoutPlan.__table__.c.revision.server_default.arg == "0"
