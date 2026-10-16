"""Create a NEW isolated acceptance DB from a known baseline checkout, then expand.

Requires the same explicit local credentials as run_p108_tests.py. Never drops or
reuses a database. Writes the new URL only to the requested private credential file.
"""
import argparse
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from run_p108_tests import configure

parser=argparse.ArgumentParser()
parser.add_argument('--name',required=True)
parser.add_argument('--baseline-source',type=Path,required=True)
parser.add_argument('--baseline-revision',required=True)
parser.add_argument('--private-url-file',type=Path,required=True)
args=parser.parse_args()
if not re.fullmatch(r'eurith_p108_test_[a-z0-9_]+',args.name):parser.error('local test database name required')
configure()
url=os.environ['TEST_DATABASE_URL']

async def create():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    engine=create_async_engine(url,isolation_level='AUTOCOMMIT')
    async with engine.connect() as connection:
        if await connection.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'),{'name':args.name}):
            raise SystemExit('Refused: database already exists; choose a fresh test name')
        await connection.execute(text('CREATE DATABASE '+args.name))
    await engine.dispose()

asyncio.run(create())
target=urlunparse(urlparse(url)._replace(path='/'+args.name))
env=dict(os.environ,DATABASE_URL=target,TEST_DATABASE_URL=target)
baseline=args.baseline_source.resolve()
assert (baseline/'app/database.py').is_file()
subprocess.run([sys.executable,'-c','import asyncio;from app.database import init_db;asyncio.run(init_db())'],cwd=baseline,env=env,check=True)
subprocess.run([sys.executable,'-m','alembic','stamp',args.baseline_revision],cwd=baseline,env=env,check=True)
subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=Path(__file__).resolve().parents[1],env=env,check=True)
args.private_url_file.write_text(target,encoding='utf-8')
print('Created baseline schema and applied expand migrations:',args.name)
