"""Explicit local-only PostgreSQL runner; refuses application fallback credentials.

Set TEST_DATABASE_URL or P108_TEST_DATABASE_URL_FILE (an untracked local file).
Usage: python scripts/run_p108_tests.py tests/analytics tests/integration -q
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def configure():
    source = os.environ.get('P108_TEST_DATABASE_URL_FILE')
    url = Path(source).read_text(encoding='utf-8-sig').strip() if source else os.environ.get('TEST_DATABASE_URL', '')
    parsed = urlparse(url)
    if (parsed.scheme not in {'postgresql+asyncpg', 'postgresql'}
            or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
            or not parsed.path.lstrip('/').startswith('eurith_p108_test')
            or parsed.query or parsed.fragment):
        raise SystemExit('Refused: explicitly configure a localhost eurith_p108_test database; no fallback is allowed.')
    os.environ['TEST_DATABASE_URL'] = url
    os.environ['DATABASE_URL'] = url
    os.environ['GOOGLE_CLOUD_PROJECT'] = 'p108-local-tests'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import firebase_admin
    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={'projectId': 'p108-local-tests'})


if __name__ == '__main__':
    configure()
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
