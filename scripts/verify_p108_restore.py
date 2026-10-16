"""Local fixture backup/restore drill; never drops or reuses the target DB.

Credentials use run_p108_tests.configure; pg tools receive a private environment,
never a command-line URL. The custom archive may contain fixture personal data:
keep it untracked. Run against an idle test database for exact row-count equality.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse, urlunparse, unquote
from run_p108_tests import configure


async def main(args):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    url = os.environ['TEST_DATABASE_URL']; parsed = urlparse(url)
    source = create_async_engine(url, isolation_level='AUTOCOMMIT')
    async with source.connect() as connection:
        if await connection.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': args.name}):
            raise SystemExit('Refused existing restore database')
    env = dict(os.environ, PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432),
               PGUSER=unquote(parsed.username or ''), PGPASSWORD=unquote(parsed.password or ''))
    subprocess.run([str(args.pg_bin/'pg_dump.exe'), '--format=custom', '--file', str(args.archive),
                    '--dbname', parsed.path.lstrip('/')], env=env, check=True)
    async with source.connect() as connection:
        await connection.execute(text('CREATE DATABASE '+args.name))
    subprocess.run([str(args.pg_bin/'pg_restore.exe'), '--no-owner', '--no-acl', '--exit-on-error',
                    '--dbname', args.name, str(args.archive)], env=env, check=True)
    restored = create_async_engine(urlunparse(parsed._replace(path='/'+args.name)))
    async def counts(engine):
        async with engine.connect() as connection:
            tables = (await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
            assert all(re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', name) for name in tables)
            return {name: await connection.scalar(text('SELECT count(*) FROM "'+name+'"')) for name in tables}
    before, after = await counts(source), await counts(restored)
    assert before == after, 'Restore table counts differ'
    print(json.dumps({'source_database':parsed.path.lstrip('/'), 'restored_database':args.name,
                      'archive_bytes':args.archive.stat().st_size, 'table_count':len(after),
                      'row_counts_equal':True, 'rows':after}, indent=2))
    await source.dispose(); await restored.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--pg-bin', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'eurith_p108_test_[a-z0-9_]+', args.name): parser.error('local test database name required')
    if args.archive.exists(): parser.error('refusing to overwrite archive')
    configure(); asyncio.run(main(args))
