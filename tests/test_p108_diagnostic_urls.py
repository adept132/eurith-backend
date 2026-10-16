"""Pure URL selection must not mutate the legacy path under combined flags."""
import runpy
from pathlib import Path
from types import SimpleNamespace


def test_combined_compact_unprofiled_urls():
    module = runpy.run_path(str(Path(__file__).parents[1] / 'docs/validation/p1-08/artifacts/task21/profile_retained_history.py'))
    cases = module['request_cases'](SimpleNamespace(unprofiled=True, compact=True, case=None), 7, [10, 11])
    assert cases == [('legacy', '/progress/exercise-history/7'),
        ('exercise', '/progress/analytics/exercises/7?period=all&encoding=compact-v1'),
        ('comparison', '/progress/analytics/blocks/compare?block_a_id=10&block_b_id=11&encoding=compact-v1')]
