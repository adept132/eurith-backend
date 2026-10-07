"""Unauthenticated, read-only release discovery; publishing is an admin CLI action."""
from typing import Annotated
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from api.deps import get_db
from api.schemas.releases import LatestReleaseQuery, LatestReleaseResponse, PublicRelease, ReleaseRecord
from api.services.release_catalog import latest_instruction

router = APIRouter(tags=["app-releases"])


@router.get("/app-releases/android/latest", response_model=LatestReleaseResponse)
async def latest_release(request: Request, response: Response,
                         query: Annotated[LatestReleaseQuery, Depends()],
                         db: AsyncSession = Depends(get_db)) -> LatestReleaseResponse:
    result = await latest_instruction(db, query)
    response.headers["Cache-Control"] = "no-store"
    public = None
    if result.release is not None:
        record = ReleaseRecord.model_validate(result.release)
        direct = record.delivery_method == "direct_apk"
        public = PublicRelease(id=record.id, source_commit=record.source_commit,
            delivery_method=record.delivery_method, version_code=record.version_code,
            version_name=record.version_name, min_supported_version_code=record.min_supported_version_code,
            runtime_version=record.runtime_version, release_notes=record.release_notes,
            published_at=record.published_at,
            download_url=(str(request.base_url).rstrip("/") + f"/app-releases/{record.id}/download") if direct else None,
            sha256=record.artifact_sha256 if direct else None,
            size_bytes=record.artifact_size_bytes if direct else None,
            eas_update_group_id=record.eas_update_group_id)
    return LatestReleaseResponse(current_version_code=result.current_version_code,
        update_available=result.update_available, mandatory=result.mandatory,
        current_release_withdrawn=result.current_release_withdrawn, release=public)