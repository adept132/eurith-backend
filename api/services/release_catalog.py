"""Read-only selection policy for the installed Android release catalog."""
from dataclasses import dataclass
from typing import Any
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from api.services.models import AppRelease


@dataclass(frozen=True)
class LatestResult:
    current_version_code: int
    update_available: bool
    mandatory: bool
    current_release_withdrawn: bool
    release: AppRelease | None


async def _published_direct(
    session: AsyncSession, platform: str, channel: str
) -> AppRelease | None:
    return (
        await session.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "direct_apk",
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.version_code.desc(), AppRelease.published_at.desc())
        )
    ).scalars().first()


async def _installed_direct_is_withdrawn(
    session: AsyncSession, platform: str, channel: str, version_code: int
) -> bool:
    return (
        await session.execute(
            select(AppRelease).where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "direct_apk",
                    AppRelease.version_code == version_code,
                    AppRelease.status == "withdrawn",
                )
            )
        )
    ).scalars().first() is not None


async def _compatible_ota(
    session: AsyncSession,
    platform: str,
    channel: str,
    version_code: int,
    runtime_version: str | None,
) -> AppRelease | None:
    if runtime_version is None:
        return None
    return (
        await session.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "eas_update",
                    AppRelease.version_code == version_code,
                    AppRelease.runtime_version == runtime_version,
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.published_at.desc())
        )
    ).scalars().first()


def _mandatory_for_available_release(
    release: AppRelease,
    current_version_code: int,
    current_is_withdrawn: bool,
) -> bool:
    return (
        bool(release.is_mandatory)
        or (
            release.min_supported_version_code is not None
            and current_version_code < release.min_supported_version_code
        )
        or current_is_withdrawn
    )


async def latest_instruction(session: AsyncSession, query: Any) -> LatestResult:
    """Choose a direct binary first, then an exactly compatible EAS update."""

    platform = getattr(query, "platform")
    channel = getattr(query, "channel")
    current_version_code = getattr(query, "current_version_code")
    runtime_version = getattr(query, "runtime_version")

    direct = await _published_direct(session, platform, channel)
    current_is_withdrawn = await _installed_direct_is_withdrawn(
        session, platform, channel, current_version_code
    )
    if direct is not None and direct.version_code > current_version_code:
        mandatory = _mandatory_for_available_release(
            direct, current_version_code, current_is_withdrawn
        )
        return LatestResult(
            current_version_code=current_version_code,
            update_available=True,
            mandatory=mandatory,
            current_release_withdrawn=current_is_withdrawn,
            release=direct,
        )

    ota = await _compatible_ota(
        session, platform, channel, current_version_code, runtime_version
    )
    if ota is not None:
        mandatory = _mandatory_for_available_release(
            ota, current_version_code, current_is_withdrawn
        )
        return LatestResult(
            current_version_code=current_version_code,
            update_available=True,
            mandatory=mandatory,
            current_release_withdrawn=current_is_withdrawn,
            release=ota,
        )
    return LatestResult(
        current_version_code=current_version_code,
        update_available=False,
        mandatory=False,
        current_release_withdrawn=current_is_withdrawn,
        release=None,
    )


