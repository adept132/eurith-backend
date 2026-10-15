import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from api.services.analytics.load import lombardi, normalize_set


def test_lombardi_without_rir(raw):
    assert lombardi(100, 8) == pytest.approx(123.1144413345)
    assert normalize_set(raw(effort_level='failure')).e1rm_weight_kg == 50
    assert normalize_set(raw(effort_level='light')).e1rm_weight_kg == 50


def test_unbounded_repetitions():
    assert all(lombardi(100, r) is not None for r in [13,37,50,301,1001])

@pytest.mark.parametrize('weight,reps', [(0,10),(-1,10),(float('nan'),10),
    (float('inf'),10),(100,0),(100,-1),(100,1.5),(100,True),(100,None)])
def test_invalid_formula_inputs(weight,reps):
    assert lombardi(weight,reps) is None


def test_paired_and_per_side(raw):
    paired = dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                  implement_count=2, rep_basis='simultaneous')
    result = normalize_set(raw(weight_kg=20, reps=10, load_context=paired))
    assert result.tonnage_kg == 400
    assert result.e1rm_weight_kg == 20
    assert normalize_set(raw(weight_kg=20,reps=10,load_context={**paired,
        'rep_basis':'per_side','left_reps':10,'right_reps':10})).tonnage_kg == 400
    assert normalize_set(raw(weight_kg=20,reps=20,load_context={**paired,
        'rep_basis':'total','left_reps':10,'right_reps':10})).tonnage_kg == 400
    assert normalize_set(raw(weight_kg=20,reps=10,load_context={**paired,
        'implement_count':1})).tonnage_kg == 200


def test_missing_base_is_not_zero(raw):
    context = dict(schema_version=1,model='barbell',input_basis='plates_only')
    assert normalize_set(raw(load_context=context)).tonnage_kg is None
    assert normalize_set(raw(load_context={**context,'base_weight_kg':0})).tonnage_kg == 500
    assert normalize_set(raw()).tonnage_kg == 500

@pytest.mark.parametrize('added,assistance,expected', [(10,0,90),(0,20,60),(0,0,80),(None,0,None),(0,None,None),(0,80,None),(0,90,None)])
def test_body_components(raw,added,assistance,expected):
    n = normalize_set(raw(load_context=dict(schema_version=1,model='bodyweight',
        input_basis='body_components',added_load_kg=added,assistance_kg=assistance),
        session_context={'body_mass':{'weight_kg':80,'measured_on':'2026-10-01'}}))
    assert n.e1rm_weight_kg == expected
    assert n.tonnage_kg == (expected*10 if expected else None)


def test_partial_and_unknown_load(raw):
    for context in [None,{},dict(schema_version=1,model='partial_bodyweight',input_basis='body_components')]:
        n = normalize_set(raw(load_context=context))
        assert n.e1rm_weight_kg is None and n.tonnage_kg is None
        assert n.unavailable

@pytest.mark.parametrize('value', [-1,float('nan'),float('inf')])
def test_invalid_load_components(raw,value):
    assert normalize_set(raw(weight_kg=value)).tonnage_kg is None
    assert normalize_set(raw(load_context=dict(schema_version=1,model='barbell',
        input_basis='plates_only',base_weight_kg=value))).tonnage_kg is None


def test_comparison_retains_components_without_setup_splitting(raw):
    a = normalize_set(raw())
    assert a.comparison_key == normalize_set(raw(setup_id='different')).comparison_key
    assert a.comparison_key != normalize_set(raw(load_context=dict(schema_version=1,
        model='stack',input_basis='displayed'))).comparison_key


def test_golden_vectors(raw):
    vectors=json.loads((Path(__file__).parents[1]/'fixtures/p108_analytics_golden.json').read_text())
    for vector in vectors['cases']:
        n=normalize_set(raw(**vector['input']))
        for key,value in vector['expected'].items():
            actual=getattr(n,key)
            assert (list(actual) if isinstance(actual,tuple) else actual) == value, vector['name']

def test_side_repetition_e1rm_is_per_implement(raw):
    context=dict(schema_version=1,model='dumbbell',input_basis='per_implement',
        implement_count=2,rep_basis='total',left_reps=10,right_reps=10)
    n=normalize_set(raw(weight_kg=20,reps=20,load_context=context))
    assert n.e1rm_reps == 10
    n=normalize_set(raw(weight_kg=20,reps=19,load_context={**context,'right_reps':9}))
    assert n.e1rm_reps is None and n.tonnage_kg == 380
    assert 'ambiguous_e1rm_reps' in n.unavailable
    n=normalize_set(raw(weight_kg=20,reps=20,load_context={**context,'left_reps':None,'right_reps':None}))
    assert n.e1rm_reps is None and n.tonnage_kg == 400


def test_numeric_context_canonicalization(raw):
    from decimal import Decimal
    context=dict(schema_version=1,model='barbell',input_basis='plates_only',base_weight_kg=Decimal('20.00'))
    assert normalize_set(raw(load_context=context)).comparison_key == normalize_set(raw(
        load_context={**context,'base_weight_kg':20.0})).comparison_key


def test_validated_body_mass_date_uses_capture_timezone(raw):
    n=normalize_set(raw(load_context=dict(schema_version=1,model='bodyweight',
        input_basis='body_components',added_load_kg=0,assistance_kg=0),
        started_at=datetime(2026,10,1,23,30,tzinfo=timezone.utc),
        session_context={'body_mass':{'weight_kg':80,'measured_on':'2026-10-02'}}))
    # In Moscow this workout starts on October 2; capture validated its local date.
    assert n.e1rm_weight_kg == 80
    assert n.tonnage_kg == 800
    assert not n.unavailable


def test_golden_formulas():
    vectors=json.loads((Path(__file__).parents[1]/'fixtures/p108_analytics_golden.json').read_text())
    for vector in vectors['formula_cases']:
        assert lombardi(vector['weight_kg'],vector['reps']) == vector['e1rm']
