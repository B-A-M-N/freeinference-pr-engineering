"""Tests for mission state: finding lifecycle, staleness, coverage, stage transitions."""

from datetime import timedelta

import pytest

from freeinference_pr.mission_state import (
    MANDATORY_COVERAGE,
    CoverageItem,
    Finding,
    MissionState,
    VerificationRecord,
    mission_key,
    new_id,
)
from freeinference_pr.pr_workflow import MAX_CORRECTION_CYCLES, next_stage


def make_state(read_only: bool = True) -> MissionState:
    return MissionState(
        mission_id=new_id("mission"),
        mission_key=mission_key("HarvardMadSys/HybridInference", 1781),
        repository="HarvardMadSys/HybridInference",
        pr_number=1781,
        objective="Review the retry logic.",
        read_only=read_only,
        coverage=[CoverageItem(requirement=r, satisfied=False) for r in MANDATORY_COVERAGE],
    )


def test_mission_key_is_stable_and_slug():
    assert mission_key("HarvardMadSys/HybridInference", 1781) == "harvardmadsys-hybridinference-pr1781"


def test_finding_lifecycle_happy_path():
    finding = Finding(finding_id=new_id("find"), title="Race in retry loop")
    finding.transition("ASSIGNED")
    finding.transition("CORRECTED")
    finding.transition("VERIFIED")
    finding.transition("CLOSED")
    assert finding.status == "CLOSED"


def test_finding_lifecycle_rejects_invalid_transition():
    finding = Finding(finding_id=new_id("find"), title="x")
    with pytest.raises(ValueError):
        finding.transition("VERIFIED")


def test_upsert_findings_deduplicates_by_title():
    state = make_state()
    added = state.upsert_findings(
        [{"title": "Race in retry loop", "severity": "high", "evidence": ["src/x.py:10"]}],
        source_stage="INVESTIGATION",
    )
    again = state.upsert_findings([{"title": "race in retry loop"}], source_stage="REVIEW")
    assert len(added) == 1
    assert again == []
    assert len(state.findings) == 1


def test_source_change_invalidates_verifications():
    state = make_state()
    state.verifications.append(VerificationRecord(verification_id=new_id("verif"), passed=True))
    state.invalidate_verifications()
    assert all(v.stale for v in state.verifications)


def test_unresolved_critical_blocks_completion():
    state = make_state()
    critical = Finding(finding_id=new_id("find"), title="Security hole", severity="critical")
    state.findings.append(critical)
    assert state.unresolved_critical()
    critical.transition("ASSIGNED")
    critical.transition("CORRECTED")
    critical.transition("VERIFIED")
    assert not state.unresolved_critical()


def test_next_stage_read_only_skips_implementation():
    state = make_state(read_only=True)
    state.stage = "INVESTIGATION"
    assert next_stage(state) == "REVIEW"


def test_next_stage_corrective_loops_back_on_critical():
    state = make_state(read_only=False)
    state.stage = "VERIFICATION"
    state.findings.append(Finding(finding_id=new_id("find"), title="Bug", severity="critical"))
    assert next_stage(state) == "IMPLEMENTATION"


def test_next_stage_correction_cycle_bound():
    state = make_state(read_only=False)
    state.stage = "VERIFICATION"
    state.findings.append(Finding(finding_id=new_id("find"), title="Bug", severity="critical"))
    state.correction_cycles = MAX_CORRECTION_CYCLES
    assert next_stage(state) != "IMPLEMENTATION"


def test_coverage_complete_requires_mandatory_items():
    state = make_state()
    assert not state.coverage_complete()
    for item in state.coverage:
        item.satisfied = True
    assert state.coverage_complete()
