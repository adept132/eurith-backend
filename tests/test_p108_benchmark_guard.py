"""A benchmark must never borrow the integration database, even on localhost."""
import os
from pathlib import Path
import subprocess
import sys


def test_benchmark_rejects_non_benchmark_database_before_connecting():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, TEST_DATABASE_URL='postgresql+asyncpg://unused:unused@127.0.0.1:9/eurith_p108_test_acceptance')
    env.pop('P108_TEST_DATABASE_URL_FILE', None)
    result = subprocess.run([sys.executable, 'scripts/benchmark_analytics.py', '--sizes', '100',
        '--requests', '1', '--runs', '1', '--output', '.pytest_tmp/guard-must-not-exist.json'],
        cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'Refused benchmark: use only eurith_p108_test_benchmark' in result.stderr
    assert 'ConnectionRefusedError' not in result.stderr
