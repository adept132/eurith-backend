"""P0-13 deliberately reuses the existing per-fact shown-target contract."""
from api.services.progression.repository import _parse_shown_target
from api.services.progression.state import evaluate
from api.services.progression.types import SetFact, Prescription, SetPrescription


def test_accepted_execution_target_is_used_even_with_interleaved_warmup_numbering():
    target = _parse_shown_target(dict(set_number=3, weight_kg=57.5, rep_min=8, rep_max=8,
        rir=2, kind='normal', execution=dict(schema_version=1, planned_set_number=2)))
    assert target is not None
    prescription = Prescription(scheme='double', sets=(SetPrescription(1,60,8,12,2),
        SetPrescription(2,60,8,12,2)), reason_code='progressed', reason_text='')
    fact = SetFact(set_number=3, weight_kg=57.5, reps=8, rir=2,
        shown_target=target, working_ordinal=2, prescription_set_number=2)
    outcome = evaluate(prescription, [fact], 2.5)
    assert outcome.status != 'deviated'
    assert outcome.miss_sets == 0


def test_amrap_target_remains_open_and_preserves_nonzero_effort():
    target = _parse_shown_target(dict(set_number=1, weight_kg=60, rep_min=5, rep_max=None,
        rir=1, kind='amrap', execution=dict(schema_version=1, planned_set_number=1)))
    assert target.rep_max is None
    assert target.rir == 1
