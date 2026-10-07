"""Explicit admin-only insertion of immutable EAS catalog records."""
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from api.schemas.releases import EasCatalogRegistration
from api.services.models import AppRelease


class RegistrationConflict(ValueError):
    pass


async def register_eas_releases(session: AsyncSession, command: EasCatalogRegistration) -> list[AppRelease]:
    """Caller owns commit; validation/conflicts roll back the whole batch."""
    result = []
    async with session.begin_nested():
        # Serialize admin commands, including overlapping version-code batches.
        await session.execute(text("SELECT pg_advisory_xact_lock(173872901)"))
        for version in sorted(command.version_codes):
            group_id = str(command.eas_update_group_id)
            update_id = str(command.eas_update_id) if command.eas_update_id else None
            identity = dict(platform="android", channel=command.channel,
                version_code=version, eas_update_group_id=group_id)
            expected = dict(**identity, delivery_method="eas_update",
                version_name=command.version_name, runtime_version=command.runtime_version,
                eas_update_id=update_id, source_commit=command.source_commit,
                release_notes=command.release_notes.model_dump(), ci_run_id=command.ci_run_id,
                is_mandatory=command.mandatory,
                min_supported_version_code=command.min_supported_version_code)
            existing = (await session.execute(select(AppRelease).where(
                AppRelease.platform == "android", AppRelease.channel == command.channel,
                AppRelease.version_code == version, AppRelease.eas_update_group_id == group_id
            ))).scalar_one_or_none()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in expected.items()):
                    raise RegistrationConflict("existing EAS group has different immutable metadata")
                # Retrying registration never reverses an operator withdrawal.
                result.append(existing)
                continue
            row = AppRelease(id=uuid4(), **expected, status="published",
                idempotency_key=f"admin:eas:{command.channel}:{version}:{group_id}")
            session.add(row)
            await session.flush()
            result.append(row)
    return result