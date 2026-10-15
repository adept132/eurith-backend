"""Goldens from persisted machine/calculator semantics; no live gym resolution."""
import pytest
from api.services.analytics.legacy import legacy_load_context
from api.services.analytics.load import normalize_set


def machine(**changes):
    return dict(dict(gym_id=None,setup_id=None,mode='plate_loaded',loading_sides=2,
        weight_basis='plates_only',base_weight=20,step_value=5,step_unit='kg'), **changes)

@pytest.mark.parametrize('snapshot,expected', [
    (machine(),70), (machine(base_weight=0),50), (machine(base_weight=None),None),
    (machine(step_unit='lb'),70),
    (machine(weight_basis='including_start_weight',base_weight=None),50),
    (machine(mode='stack',weight_basis='displayed',base_weight=None),50)])
def test_saved_machine_goldens(raw,snapshot,expected):
    context = legacy_load_context(snapshot,mode=snapshot['mode'])
    result = normalize_set(raw(weight_kg=50,reps=10,load_context=context))
    assert result.e1rm_weight_kg == expected
    assert result.tonnage_kg == (expected * 10 if expected else None)

@pytest.mark.parametrize('change', [dict(mode='stack'),dict(gym_id='invalid'),
    dict(loading_sides=True),dict(base_weight=-1),dict(base_weight='NaN'),
    dict(weight_basis='unknown'),dict(step_value=0),dict(step_unit='oz'),dict(setup_id='invalid'),
    dict(equipment={'kind':'barbell'})])
def test_contradictions_are_unknown(change):
    assert legacy_load_context(machine(**change),mode='plate_loaded') is None


def test_full_barbell_without_base_and_naked_rows(raw):
    equipment = dict(kind='barbell',gym_profile_id=None,setup_id=None,load_mode=None,
        loading_sides=2,weight_basis='including_start_weight',base_weight_kg=None,
        bar=None,collar=None,inventory=dict(mode='unlimited',items=[]))
    ctx = legacy_load_context({'equipment':equipment},mode=None)
    assert normalize_set(raw(load_context=ctx)).e1rm_weight_kg == 50
    assert legacy_load_context(None,mode=None) is None
    assert legacy_load_context({'equipment_needed':['dumbbell']},mode=None) is None
    assert legacy_load_context({'equipment':dict(equipment, weight_basis='plates_only')},mode=None) is None


def test_proven_per_implement_survives_unknown_count(raw):
    ctx = dict(schema_version=1,model='dumbbell',input_basis='per_implement',rep_basis='simultaneous')
    result = normalize_set(raw(weight_kg=20,reps=10,load_context=ctx))
    assert result.e1rm_weight_kg == 20 and result.e1rm_reps == 10
    assert result.tonnage_kg is None


def test_invalid_equipment_display_unit_is_not_evidence():
    snapshot = machine(display_unit='oz')
    assert legacy_load_context(snapshot,mode='plate_loaded') is None


def test_malformed_display_unit_is_unknown():
    assert legacy_load_context(machine(display_unit=[]), mode='plate_loaded') is None
