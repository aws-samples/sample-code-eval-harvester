"""Offline tests for judge↔human alignment (Workstream G, G-2).

Alignment is the step that turns the judge from an opinion into a measurement. These tests run the
measurement over the recorded labeled set and assert: it clears the stated bar; the recorded
`alignment_result.json` the gate reads is consistent with a fresh measurement (no drift, no fudged
number); and the set contains a genuine judge↔human disagreement, so the measured agreement is a real
number below 1.0 — a measurement that could not disagree would prove nothing (the control-token
discipline applied to alignment).
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.alignment.labeled_set import LabeledSet
from eval.alignment.measure import ALIGNMENT_BAR, AlignmentMeasurement
from eval.judge.gate import DEFAULT_ALIGNMENT_PATH
from eval.judge.models import AlignmentResult
from eval.judge.rubric import JUDGE_VERSION


def test_labeled_set_is_mixed_good_and_bad() -> None:
    """The labeled set exists and carries both good and deliberately-broken datapoints (task §3)."""
    labeled = LabeledSet.load_all()
    assert len(labeled) >= 6
    labels = {case.human_overall_pass for case in labeled}
    assert labels == {True, False}


def test_alignment_meets_the_stated_bar() -> None:
    """The judge, run over the labeled set, meets or exceeds the stated agreement bar (How To Verify)."""
    report = AlignmentMeasurement.run(LabeledSet.load_all())
    assert report.result.bar == ALIGNMENT_BAR
    assert report.result.agreement >= report.result.bar
    assert report.result.passed is True


def test_alignment_set_contains_a_real_disagreement() -> None:
    """The set holds a genuine judge↔human disagreement, so agreement is below 1.0 — the measurement discriminates.

    Without at least one disagreement the measurement could rubber-stamp and no one would notice; a
    measurement that cannot come out below the bar is not measuring anything (task Notes; CLAUDE.md's
    'a scanner that finds nothing must prove it scanned something')."""
    report = AlignmentMeasurement.run(LabeledSet.load_all())
    assert report.disagreements  # at least one case where the judge and the human differ
    assert report.result.agreement < 1.0


def test_recorded_alignment_result_matches_a_fresh_measurement() -> None:
    """The committed alignment_result.json the gate trusts is consistent with re-running the measurement.

    Guards against the recorded number drifting from the labeled set (or being hand-edited): the
    agreement, bar, count, and version on disk must equal what the measurement recomputes offline."""
    recorded = AlignmentResult.model_validate(json.loads(DEFAULT_ALIGNMENT_PATH.read_text(encoding="utf-8")))
    fresh = AlignmentMeasurement.run(LabeledSet.load_all()).result
    assert recorded.n_labeled == fresh.n_labeled
    assert recorded.agreement == fresh.agreement
    assert recorded.bar == fresh.bar
    assert recorded.passed == fresh.passed
    assert recorded.judge_version == JUDGE_VERSION


def test_recorded_alignment_result_is_present_and_passing() -> None:
    """A recorded, passing alignment result exists under eval/ — the record that licenses the judge (AC)."""
    assert Path(DEFAULT_ALIGNMENT_PATH).is_file()
    recorded = AlignmentResult.model_validate(json.loads(DEFAULT_ALIGNMENT_PATH.read_text(encoding="utf-8")))
    assert recorded.passed is True
    assert recorded.agreement >= recorded.bar
