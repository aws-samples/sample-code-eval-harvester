"""The calibration rule: fail-to-pass, pass-to-pass, and the flaky tests left out (coding-task tech plan §5)."""

from __future__ import annotations

from eval_harvest.calibrate import CodingTaskCalibration


class TestCalibrationRule:
    def test_each_test_lands_in_exactly_one_class(self) -> None:
        nop = [{"f::fixed": "failed", "f::kept": "passed", "f::flaky": "passed", "f::broken": "failed"}] * 2
        oracle = [
            {"f::fixed": "passed", "f::kept": "passed", "f::flaky": "passed", "f::broken": "failed", "f::new": "passed"},
            {"f::fixed": "passed", "f::kept": "passed", "f::flaky": "failed", "f::broken": "failed", "f::new": "passed"},
        ]
        fail_to_pass, pass_to_pass, left_out = CodingTaskCalibration.classify(nop, oracle)
        # A test that does not exist on the base (a new file) has not passed there.
        assert fail_to_pass == ("f::fixed", "f::new")
        assert pass_to_pass == ("f::kept",)
        assert left_out == ("f::flaky",)

    def test_a_run_file_with_no_tests_is_never_required(self) -> None:
        nop = [{"f::(no tests reported)": "failed"}]
        oracle = [{"f::(no tests reported)": "passed", "f::t": "passed"}]
        assert CodingTaskCalibration.classify(nop, oracle) == (("f::t",), (), ())

    def test_the_instruction_names_a_test_by_its_runner_name_and_file(self) -> None:
        assert CodingTaskCalibration.display_name("tests/a.py::Suite::case") == "- Suite > case (`tests/a.py`)"
