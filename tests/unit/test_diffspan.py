"""Tests for `diffspan` — the single new-side unified-diff span parser.

It is easy for this parsing to end up copied (once in `verify.py`, once in an ad-hoc script), and
the answer it produces decides two separate things:
which iterations a comment's line lives in (`candidate.in_diff_iterations`) and whether an oracle
finding points at a line the change touches (`verify`'s `finding-line-absent`, FR-34). A parser that
is off by one, or that attributes one file's hunks to another, corrupts both. These tests pin the
geometry directly, on hand-written diffs, so the failure names the span rather than the caller.
"""

from __future__ import annotations

from eval_harvest.diffspan import location_in_spans, new_side_spans

#: A two-file diff. `alpha.py` gains a hunk at new-side 10..12; `beta.py` gains one at 40..41. Both
#: are needed in one diff because the defect this catches is *attribution* — a parser that carries the
#: previous `+++ b/` path forward files beta's hunks under alpha and answers every question wrongly.
_TWO_FILE_DIFF = """diff --git a/alpha.py b/alpha.py
--- a/alpha.py
+++ b/alpha.py
@@ -9,2 +10,3 @@ def alpha() -> None:
 context
+added
 context
diff --git a/beta.py b/beta.py
--- a/beta.py
+++ b/beta.py
@@ -38,3 +40,2 @@ def beta() -> None:
 context
-removed
"""


def test_new_side_spans_reads_multi_file_diff() -> None:
    """Each file's hunks are filed under its own `+++ b/<path>`, with inclusive new-side spans.

    Catches spans attributed to the previous file — the defect that answers a membership question
    against the wrong file entirely, which looks like a plausible answer rather than an error.
    """
    spans = new_side_spans(_TWO_FILE_DIFF)

    assert spans == {"alpha.py": [(10, 12)], "beta.py": [(40, 41)]}


def test_new_side_spans_ignores_pure_deletion_hunk() -> None:
    """A `+<start>,0` hunk contributes no span — a deletion has no new-side line to point at.

    Catches a deletion-only hunk claiming a span. With `count == 0` the inclusive end would be
    `start - 1`, an inverted range that overlaps whatever precedes it, making comments "in diff"
    against lines the change removed.
    """
    deletion_only = """diff --git a/gone.py b/gone.py
--- a/gone.py
+++ b/gone.py
@@ -5,3 +4,0 @@ def gone() -> None:
-first
-second
-third
"""

    assert new_side_spans(deletion_only) == {}


def test_new_side_spans_reads_a_single_line_hunk() -> None:
    """A hunk header with no `,count` means exactly one new-side line: the span is `(start, start)`.

    Catches the omitted-count branch defaulting to zero (which the deletion guard would then drop,
    silently losing a real one-line change) instead of to one.
    """
    single_line = """--- a/one.py
+++ b/one.py
@@ -3 +3 @@
-was
+now
"""

    assert new_side_spans(single_line) == {"one.py": [(3, 3)]}


def test_location_in_spans_boundary_lines() -> None:
    """The first and last lines of a span are inside it; one line either side is outside.

    Catches an off-by-one that silently widens or narrows what `finding-line-absent` accepts and
    which iterations a comment binds to. Both edges are asserted because the two directions are
    separate comparisons in the overlap test, and either can be wrong alone.
    """
    spans = {"alpha.py": [(10, 12)]}

    assert location_in_spans("alpha.py", 10, 10, spans), "the first line of the span is inside it"
    assert location_in_spans("alpha.py", 12, 12, spans), "the last line of the span is inside it"
    assert location_in_spans("alpha.py", 8, 10, spans), "a range ending on the first line overlaps"
    assert location_in_spans("alpha.py", 12, 20, spans), "a range starting on the last line overlaps"
    assert not location_in_spans("alpha.py", 9, 9, spans), "one line before the span is outside"
    assert not location_in_spans("alpha.py", 13, 13, spans), "one line after the span is outside"


def test_location_in_spans_rejects_unknown_path_and_zero_line() -> None:
    """A path with no spans is outside, and so is line 0 — "not on the new side at all".

    Both are behaviour that `finding-line-absent` depends on: a
    file the change never touched, and a comment the forge could not place on the new side (line 0).
    Neither may be treated as a match.
    """
    spans = {"alpha.py": [(10, 12)]}

    assert not location_in_spans("never-touched.py", 10, 12, spans), "a path with no hunks is outside"
    assert not location_in_spans("", 10, 12, spans), "an empty path is outside, not a crash"
    assert not location_in_spans("alpha.py", 0, 0, spans), "line 0 is not a new-side line"
