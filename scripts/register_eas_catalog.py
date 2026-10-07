"""Register a verified EAS group for explicit installed Android build codes.

Run inside the production API container with its configured DATABASE_URL.
No HTTP publishing endpoint or APK artifact hosting is enabled by this command.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from api.schemas.releases import EasCatalogRegistration


def parse_command(argv=None) -> EasCatalogRegistration:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-code", type=int, action="append", required=True)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--channel", default="production-direct")
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--update-id")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--notes-ru", required=True)
    parser.add_argument("--notes-en", required=True)
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--min-supported-version-code", type=int)
    arguments = parser.parse_args(argv)
    return EasCatalogRegistration(channel=arguments.channel, version_codes=arguments.version_code,
        version_name=arguments.version_name, runtime_version=arguments.runtime_version,
        eas_update_group_id=arguments.group_id, eas_update_id=arguments.update_id,
        source_commit=arguments.source_commit, ci_run_id=arguments.ci_run_id,
        release_notes={"ru": arguments.notes_ru, "en": arguments.notes_en},
        mandatory=arguments.mandatory,
        min_supported_version_code=arguments.min_supported_version_code)


async def run(command: EasCatalogRegistration):
    from app.database import SessionLocal, engine
    from api.services.release_catalog_admin import register_eas_releases
    try:
        async with SessionLocal() as session, session.begin():
            rows = await register_eas_releases(session, command)
            summary = [{"id": str(row.id), "version_code": row.version_code,
                "channel": row.channel, "runtime_version": row.runtime_version,
                "eas_update_group_id": row.eas_update_group_id,
                "source_commit": row.source_commit, "status": row.status} for row in rows]
        print(json.dumps(summary))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        command = parse_command()
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise ValueError("admin registration requires root inside the API container")
        if not os.environ.get("DATABASE_URL", "").startswith("postgresql+asyncpg://"):
            raise ValueError("set the deployment DATABASE_URL explicitly")
        asyncio.run(run(command))
    except (ValueError, ValidationError) as error:
        print(f"Registration rejected: {error}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        # Driver exceptions may include database credentials; show only the class.
        print(f"Registration failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)