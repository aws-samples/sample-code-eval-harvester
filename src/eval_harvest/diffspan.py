"""New-side unified-diff span parsing — the single implementation, deliberately.

Two questions in this pipeline are the same geometric question: *which iterations' diffs show the
line a review comment points at* (``candidate.in_diff_iterations``, FR-12) and *does an oracle
finding point at a line the change actually touches* (``verify``'s ``finding-line-absent``, FR-34).
Both reduce to "parse a unified diff's new-side hunk spans, then test a location against them".

It is easy for this parsing to end up copied — once in ``verify.py``, once in an ad-hoc script, and
implicitly in whatever a reader assumes. Copies of a geometric rule drift, and when they drift the
two questions above get different answers for the same diff — which reads as "the agent placed the
finding wrong" rather than as a parser bug. This module is the one implementation; ``verify.py`` and
``candidate.py`` both call it and neither re-parses.

Stdlib only (``re``), no project imports, so both callers can depend on it without a cycle.
"""

from __future__ import annotations

import re
from typing import Final

#: A unified-diff hunk header: `@@ -<old_start>[,<old_count>] +<new_start>[,<new_count>] @@`. The
#: new-side start/count give the line span the change touches — what an oracle finding must fall in.
_HUNK_HEADER: Final = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

#: The `+++ b/<path>` line that names the file a following run of hunks applies to.
_NEW_FILE_HEADER: Final = re.compile(r"^\+\+\+ b/(?P<path>.*)$")


def new_side_spans(diff: str) -> dict[str, list[tuple[int, int]]]:
    """The new-side line spans each file's hunks touch, keyed by the file's post-change path.

    Walks the unified diff: a `+++ b/<path>` line names the current file; each following
    `@@ … +start,count @@` header contributes the inclusive span `[start, start + count - 1]`.

    A hunk with `count == 0` is a pure deletion — it has no new-side line, and its inclusive end
    would be `start - 1`, an inverted range that overlaps whatever precedes it — so it is skipped.
    An omitted count means exactly one line, which is why the default is `"1"` and not `"0"`.
    """
    spans: dict[str, list[tuple[int, int]]] = {}
    current_path = ""
    for line in diff.splitlines():
        file_match = _NEW_FILE_HEADER.match(line)
        if file_match:
            current_path = file_match.group("path")
            continue
        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match and current_path:
            start = int(hunk_match.group("start"))
            count = int(hunk_match.group("count") or "1")
            if count > 0:
                spans.setdefault(current_path, []).append((start, start + count - 1))
    return spans


def location_in_spans(path: str, line_start: int, line_end: int, spans: dict[str, list[tuple[int, int]]]) -> bool:
    """True when `[line_start, line_end]` overlaps a new-side hunk span for `path` in the parsed diff.

    Both guards matter and are load-bearing for ``finding-line-absent``: a path the diff never
    mentions has no spans and is outside, and `line_start <= 0` means the location is not on the new
    side at all (a file-level or outdated comment the forge could not place on a line).
    """
    file_spans = spans.get(path)
    if not file_spans or line_start <= 0:
        return False
    return any(span_start <= line_end and line_start <= span_end for span_start, span_end in file_spans)
