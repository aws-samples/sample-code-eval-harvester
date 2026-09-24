"""Tests for the deterministic stdlib TOML writer (task D-1).

The contract is threefold: what we emit round-trips through ``tomllib`` (Harbor can parse it),
the same input dict emits byte-identical output on every run and platform (Harbor hashes the
task, so NFR-1 forbids any dict-hash-order, float-format, or line-ending drift), and the value
kinds ``task.toml`` needs (§8) render correctly — floats keep their ``.0``, LF endings, nested
tables, and array-of-tables.
"""

import tomllib

from eval_harvest.tomlw import TomlFloat, TomlValue, emit_document


def _every_value_kind() -> dict[str, TomlValue]:
    """A document exercising every value kind the writer must handle (§8)."""
    return {
        "schema_version": "1.4",
        "task": {"name": "org/repo__pr1-reject", "description": 'Review "the" change\n'},
        "agent": {"timeout_sec": TomlFloat(1800.0)},
        "metadata": {
            "origin": {"repo": "org/repo", "pr_numbers": [1234]},
            "harvest": {
                "change_risk_disagreement": True,
                "finding_severities": ["high", "medium"],
                "overrides": [],
            },
        },
        "rules": [
            {"prefix": "src/payments/", "risk": "high"},
            {"prefix": "docs/", "risk": "low"},
        ],
    }


# The golden bytes for ``_every_value_kind()``. Pinning the exact output is what catches
# nondeterminism: any reordering, float reformat, or CRLF creeping in changes these bytes.
GOLDEN = (
    b'schema_version = "1.4"\n'
    b"\n"
    b"[task]\n"
    b'name = "org/repo__pr1-reject"\n'
    b'description = "Review \\"the\\" change\\n"\n'
    b"\n"
    b"[agent]\n"
    b"timeout_sec = 1800.0\n"
    b"\n"
    b"[metadata.origin]\n"
    b'repo = "org/repo"\n'
    b"pr_numbers = [1234]\n"
    b"\n"
    b"[metadata.harvest]\n"
    b"change_risk_disagreement = true\n"
    b'finding_severities = ["high", "medium"]\n'
    b"overrides = []\n"
    b"\n"
    b"[[rules]]\n"
    b'prefix = "src/payments/"\n'
    b'risk = "high"\n'
    b"\n"
    b"[[rules]]\n"
    b'prefix = "docs/"\n'
    b'risk = "low"\n'
)


class TestRoundTrip:
    """What the writer emits, ``tomllib`` reads back unchanged (modulo the TomlFloat wrapper)."""

    def test_round_trips_through_tomllib(self) -> None:
        parsed = tomllib.loads(emit_document(_every_value_kind()).decode())
        assert parsed == {
            "schema_version": "1.4",
            "task": {"name": "org/repo__pr1-reject", "description": 'Review "the" change\n'},
            "agent": {"timeout_sec": 1800.0},
            "metadata": {
                "origin": {"repo": "org/repo", "pr_numbers": [1234]},
                "harvest": {
                    "change_risk_disagreement": True,
                    "finding_severities": ["high", "medium"],
                    "overrides": [],
                },
            },
            "rules": [
                {"prefix": "src/payments/", "risk": "high"},
                {"prefix": "docs/", "risk": "low"},
            ],
        }


class TestByteStability:
    """Same input dict → identical bytes, run to run and platform to platform (NFR-1)."""

    def test_byte_identical_across_runs(self) -> None:
        first = emit_document(_every_value_kind())
        second = emit_document(_every_value_kind())
        assert first == second
        assert first == GOLDEN

    def test_key_order_follows_insertion_not_hash(self) -> None:
        forward: dict[str, TomlValue] = {"b": 1, "a": 2, "c": 3}
        assert emit_document(forward) == b"b = 1\na = 2\nc = 3\n"


class TestValueKinds:
    """The specific renderings §8 and D-2's validators depend on."""

    def test_float_renders_with_point(self) -> None:
        assert emit_document({"timeout_sec": TomlFloat(1800.0)}) == b"timeout_sec = 1800.0\n"
        # A fractional float keeps its digits; it must never round to an int.
        assert emit_document({"ratio": TomlFloat(0.5)}) == b"ratio = 0.5\n"

    def test_array_of_tables_and_nested_tables(self) -> None:
        document: dict[str, TomlValue] = {
            "metadata": {"origin": {"repo": "org/repo"}},
            "rule": [{"prefix": "src/", "risk": "high"}],
        }
        emitted = emit_document(document)
        assert b"[metadata.origin]\n" in emitted
        assert b"[[rule]]\n" in emitted
        parsed = tomllib.loads(emitted.decode())
        assert parsed["metadata"]["origin"]["repo"] == "org/repo"
        assert parsed["rule"] == [{"prefix": "src/", "risk": "high"}]

    def test_string_escaping(self) -> None:
        hostile = 'a "quote", a \\ backslash, a\ttab, and a\nnewline'
        emitted = emit_document({"body": hostile})
        # The escaped string must survive a parse back to the exact original.
        assert tomllib.loads(emitted.decode())["body"] == hostile

    def test_control_character_is_unicode_escaped(self) -> None:
        emitted = emit_document({"body": "bell\x07here"})
        assert b"\\u0007" in emitted
        assert tomllib.loads(emitted.decode())["body"] == "bell\x07here"

    def test_non_bare_key_is_quoted(self) -> None:
        emitted = emit_document({"needs quote": 1})
        assert emitted == b'"needs quote" = 1\n'
        assert tomllib.loads(emitted.decode()) == {"needs quote": 1}

    def test_booleans_and_ints(self) -> None:
        # bool is an int subclass; it must render true/false, not 1/0.
        assert emit_document({"flag": True, "count": 3}) == b"flag = true\ncount = 3\n"


class TestLineEndings:
    """Output is LF-only and newline-terminated on every platform (NFR-4/NFR-1)."""

    def test_line_endings_are_lf(self) -> None:
        emitted = emit_document(_every_value_kind())
        assert b"\r" not in emitted
        assert emitted.endswith(b"\n")
