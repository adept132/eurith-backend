"""Repeated history work must scale with facts/winners, not consumers."""
from dataclasses import replace
from unittest.mock import patch

from api.services.analytics import load, strength


def test_lombardi_prepared_once_per_immutable_fact(raw):
    row = load.normalize_set(raw())
    with patch.object(strength, 'lombardi', wraps=load.lombardi) as calculate:
        assert strength._e1rm(row) == strength._e1rm(row)
        strength.session_points([row])
        strength.record_history([row])
        assert calculate.call_count == 1
        changed = replace(row, e1rm_weight_kg=100)
        assert strength._e1rm(changed) == 2 * strength._e1rm(row)
        assert calculate.call_count == 2


def test_equal_records_do_not_allocate_or_compare_every_source(raw):
    rows = [load.normalize_set(raw(set_id=i)) for i in range(1, 101)]
    with patch.object(strength, 'strictly_greater', wraps=strength.strictly_greater) as compare:
        with patch.object(strength, '_record_entry', wraps=strength._record_entry) as allocate:
            history = strength.record_history(rows[::-1])
            assert allocate.call_count == 1
        assert compare.call_count == 0
    assert history['best']['set_id'] == 1
    assert history['events'] == []
    assert history['set_volume']['set_id'] == 1


def test_context_cache_is_request_local_and_exact(raw):
    cache = {}
    with patch.object(load.json, 'dumps', wraps=load.json.dumps) as encode:
        a = load.normalize_set(raw(), comparison_cache=cache)
        b = load.normalize_set(raw(set_id=2, weight_kg=75), comparison_cache=cache)
        assert a.comparison_key == b.comparison_key
        assert a.e1rm_weight_kg != b.e1rm_weight_kg
        assert encode.call_count == 1
    for value in (True, 1, 1.0, -0.0, 0.0, ['malformed'], {'x': 1}):
        item = raw(load_context={'schema_version': value, 'model': 'barbell', 'input_basis': 'full'})
        assert load.normalize_set(item, comparison_cache=cache) == load.normalize_set(item)


def test_publication_visits_each_identity_but_not_equal_distinct_models():
    from pydantic import BaseModel
    from api.services.analytics.publication import _models

    class Node(BaseModel):
        data_revision: int

    first, equal, stale = Node(data_revision=4), Node(data_revision=4), Node(data_revision=3)
    shared = [first, {'baseline': equal}]
    graph = {'a': shared, 'b': (shared, {'nested': stale})}
    assert [id(node) for node in _models(graph)] == [id(first), id(equal), id(stale)]
