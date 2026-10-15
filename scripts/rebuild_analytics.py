"""Explicit analytics rebuild maintenance; defaults to read-only inventory."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--user-id', type=int)
    target.add_argument('--all-users', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--reconstruct-legacy', action='store_true')
    return parser.parse_args(argv)


async def run(args):
    # Validate before importing app.database, which otherwise has a development
    # fallback. No implicit .env, startup migration, or Firebase initialization.
    from sqlalchemy.engine import make_url
    configured = os.environ.get('DATABASE_URL', '')
    if not configured or make_url(configured).drivername != 'postgresql+asyncpg':
        raise ValueError('explicit async PostgreSQL database configuration required')
    os.environ['SQL_ECHO'] = '0'
    from sqlalchemy import select
    from app.database import SessionLocal, engine
    from api.services.models import AppUser
    from api.services.analytics.rebuild import rebuild_user
    from api.services.analytics.repository import load_facts
    from api.services.analytics.types import AnalyticsQuery
    from api.services.analytics.policy import RULES_VERSION, FORMULA_ID
    try:
        async with SessionLocal() as db:
            statement = select(AppUser.id).order_by(AppUser.id)
            if args.user_id is not None:
                statement = statement.where(AppUser.id == args.user_id)
            users = list((await db.scalars(statement)).all())
        if args.user_id is not None and not users:
            raise ValueError('user not found')
        for user_id in users:
            async with SessionLocal() as db:
                if args.apply:
                    result = await rebuild_user(db, user_id, resume=args.resume,
                                                reconstruct_legacy=args.reconstruct_legacy)
                else:
                    facts = await load_facts(db, user_id, AnalyticsQuery(period='all'))
                    result = dict(status='dry_run', rules_version=RULES_VERSION, formula_id=FORMULA_ID,
                        source_revision=facts.source_revision, workouts=len(facts.global_workouts),
                        sets=len(facts.graph_sets),
                        missing_snapshots=sum(w['analytics_snapshot'] is None for w in facts.global_workouts),
                        missing_body_mass=sum(not (w['analytics_snapshot'] or {}).get('body_mass') for w in facts.global_workouts))
                print(json.dumps(dict(user_id=user_id, **result), sort_keys=True))
    finally:
        await engine.dispose()


def main(argv=None):
    args = arguments(argv)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
