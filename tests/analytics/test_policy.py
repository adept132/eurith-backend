import pytest
from api.services.analytics.policy import eligible, strictly_greater
from api.services.analytics.load import normalize_set


def test_roles_and_amrap(raw):
    for metric in ['e1rm','pr','intensity','rir','work_sets','reps','tonnage']:
        assert eligible(raw(effort_level='failure'),metric)
        assert not eligible(raw(set_type='warmup'),metric)
        assert not eligible(raw(is_completed=False),metric)
        assert not eligible(raw(is_anomalous=True),metric)
        assert not eligible(raw(finished_at=None),metric)
    for metric in ['tonnage','drop_sets','reps']:
        assert eligible(raw(set_type='drop'),metric)
    for metric in ['e1rm','pr','intensity','rir','work_sets']:
        assert not eligible(raw(set_type='drop'),metric)
    assert not eligible(raw(set_type='unknown'),'tonnage')
    assert eligible(raw(is_deload=True),'intensity')
    assert eligible(raw(load_context=None),'work_sets')
    assert eligible(raw(load_context=None,effort_level='medium'),'rir')
    assert not eligible(raw(effort_level=None),'rir')
    assert not eligible(raw(reps=1.5),'reps')
    assert not eligible(raw(),'typo')


def test_tolerance_before_rounding():
    assert not strictly_greater(100+1e-9,100)
    assert strictly_greater(100+2e-8,100)
    assert not strictly_greater(1e8+0.00005,1e8)
    assert strictly_greater(1e8+0.0002,1e8)
    assert not strictly_greater(float('inf'),100)
    assert not strictly_greater(float('nan'),100)

@pytest.mark.parametrize('effort,rir',[('light',3),('easy',3),('medium',2),
    ('prefailure',1),('failure',0),('warmup',4),('warmup_effort',4),(None,None),('other',None)])
def test_explicit_rir_only(raw,effort,rir):
    assert normalize_set(raw(effort_level=effort)).rir == rir

@pytest.mark.parametrize('rep_basis', [None, 'unrecognized'])
def test_unknown_dumbbell_rep_basis_blocks_weighted_metrics(raw, rep_basis):
    context = dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                   implement_count=2)
    if rep_basis is not None:
        context['rep_basis'] = rep_basis
    fact = raw(weight_kg=20, reps=20, effort_level='medium', load_context=context)
    normalized = normalize_set(fact)
    assert normalized.e1rm_weight_kg == 20
    assert normalized.e1rm_reps is None
    assert normalized.tonnage_kg is None
    assert 'unknown_rep_basis' in normalized.unavailable
    for metric in ['e1rm', 'pr', 'intensity', 'tonnage']:
        assert not eligible(fact, metric)
    for metric in ['work_sets', 'reps', 'rir']:
        assert eligible(fact, metric)
    assert normalized.rir == 2
