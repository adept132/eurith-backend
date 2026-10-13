"""Rest timer policy defaults, deep patching, and configured recommendation."""

from api.schemas.rest_timer import RestTimerPolicy, RestTimerPolicyPatch


def rest_timer_policy(settings: dict | None) -> RestTimerPolicy:
    """Read old or partial JSONB settings without writing defaults to storage."""
    return RestTimerPolicy.model_validate((settings or {}).get("rest_timer_policy") or {})


def merge_rest_timer_policy(current: RestTimerPolicy, patch: RestTimerPolicyPatch) -> RestTimerPolicy:
    """Replace only supplied leaves, retaining the server-owned version."""
    values = current.model_dump()
    for section, leaves in patch.model_dump(exclude_unset=True).items():
        values[section].update(leaves)
    return RestTimerPolicy.model_validate(values)


def configured_rest_seconds(tier: int, effort: str | None, policy: RestTimerPolicy) -> int:
    """Apply the configured formula with the shared 60–600 second bounds."""
    selected_tier = tier if type(tier) is int and tier in (1, 2, 3) else 2
    base = getattr(policy.base_seconds, f"tier{selected_tier}")
    normalized_effort = "light" if effort == "easy" else effort
    adjustment = getattr(policy.effort_seconds, normalized_effort, 0) if normalized_effort in (
        "light", "medium", "prefailure", "failure"
    ) else 0
    return max(60, min(600, base + adjustment))
