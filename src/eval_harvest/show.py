"""The ``show`` verb: print just the diff hunk(s) covering one review comment's line range.

Without ``show``, the only way to see the few lines a comment points at is to open the whole
``patches/pr-<n>-iter<i>.patch`` file — pushing megabytes of patch text through the driving agent's
context just to classify a handful of comments. Nothing in the design intends the agent to read
those patches; they exist for the container that grades the datapoint. ``show`` closes the gap:
given a candidate and a comment id, it renders only the hunk whose new-side span covers that
comment, with a little surrounding context, so the agent gets what it needs to judge the comment
without paying for the file.

``show`` is a read-only view over ``capture``'s output (tech plan §5.3). It renders bytes it already
holds and makes no judgment (§5.1); it reads the candidate and the materialized patch and writes
nothing (§6.2). Overlap is decided by :mod:`~eval_harvest.diffspan` — the one new-side span parser —
so ``show`` and ``verify`` never disagree about which hunk a line falls in. The block segmentation
here detects only the two structural boundaries (``+++ b/<path>`` and ``@@``); it never re-parses the
numeric spans inside a ``@@`` header, which are diffspan's alone.
"""

from __future__ import annotations

from typing import Final

from eval_harvest import diffspan
from eval_harvest.candidate import CommentDict

#: The ``+++ b/<path>`` header names the file the hunks that follow apply to (the post-change path,
#: which is what a comment's ``path`` is recorded against).
_NEW_FILE_PREFIX: Final = "+++ b/"

#: A unified-diff hunk opens with ``@@``. Detecting the boundary is a prefix test; the ``+start,count``
#: inside it is parsed by :func:`diffspan.new_side_spans`, never here.
_HUNK_PREFIX: Final = "@@"


class Show:
    """Select the covering hunk(s) for a comment and render them (human or ``--json``). Holds no state."""

    @staticmethod
    def hunks_for_location(diff: str, path: str, line_start: int, line_end: int) -> list[str]:
        """The hunk blocks of ``diff`` whose new-side span overlaps ``[line_start, line_end]`` on ``path``.

        Each block is its ``@@ … @@`` header plus its body, verbatim and newline-terminated. Overlap is
        decided by :func:`diffspan.location_in_spans` against the span :func:`diffspan.new_side_spans`
        parses for the one hunk — so the same geometry that put the iteration in the comment's
        ``in_diff_iterations`` selects the hunk here. A ``diff`` that never touches ``path`` yields
        nothing, as does a location on no new-side line.
        """
        covering: list[str] = []
        for hunk_path, block in Show._hunk_blocks(diff):
            if hunk_path != path:
                continue
            spans = diffspan.new_side_spans(f"{_NEW_FILE_PREFIX}{path}\n{block}")
            if diffspan.location_in_spans(path, line_start, line_end, spans):
                covering.append(block)
        return covering

    @staticmethod
    def _hunk_blocks(diff: str) -> list[tuple[str, str]]:
        """Every hunk in ``diff`` as ``(post-change path, block text)``, the block being its header + body.

        Segments on the two structural boundaries only: a ``+++ b/<path>`` line names the file the
        following hunks apply to, and each ``@@`` line opens a hunk. The block text is newline-terminated
        so a rendered hunk is a well-formed diff fragment.
        """
        blocks: list[tuple[str, str]] = []
        current_path = ""
        current_hunk: list[str] = []
        for line in diff.splitlines():
            if line.startswith(_NEW_FILE_PREFIX):
                Show._append_hunk(blocks, current_path, current_hunk)
                current_hunk = []
                current_path = line[len(_NEW_FILE_PREFIX) :]
            elif line.startswith(_HUNK_PREFIX):
                Show._append_hunk(blocks, current_path, current_hunk)
                current_hunk = [line]
            elif current_hunk:
                current_hunk.append(line)
        Show._append_hunk(blocks, current_path, current_hunk)
        return blocks

    @staticmethod
    def _append_hunk(blocks: list[tuple[str, str]], path: str, hunk: list[str]) -> None:
        """Record a completed hunk (header + body) under its file path, skipping an empty accumulator."""
        if hunk and path:
            blocks.append((path, "\n".join(hunk) + "\n"))

    # ───────────────────────────── context trimming ─────────────────────────────

    @staticmethod
    def _trim_hunk(hunk: str, line_start: int, line_end: int, context: int) -> str:
        """One hunk trimmed to ``context`` body lines either side of the comment's own lines.

        The ``@@ … @@`` header is always kept — its trailing text is the function context git guessed,
        often the most useful line for judging a comment. New-side line numbers are walked from the
        header's ``+start`` (read via :func:`diffspan.new_side_spans`, not re-parsed) so the lines the
        comment points at can be found; a removed (``-``) line carries no new-side number and is kept
        only when it falls inside the retained window. With nothing in range — which a covering hunk
        never produces — the hunk is returned whole rather than silently emptied.
        """
        lines = hunk.splitlines()
        header, body = lines[0], lines[1:]
        spans = diffspan.new_side_spans(f"{_NEW_FILE_PREFIX}x\n{hunk}")
        new_start = spans["x"][0][0] if spans.get("x") else 1
        marked = Show._body_lines_in_range(body, new_start, line_start, line_end)
        if not marked:
            return hunk
        keep_from = max(0, marked[0] - context)
        keep_to = min(len(body) - 1, marked[-1] + context)
        return "\n".join([header, *body[keep_from : keep_to + 1]]) + "\n"

    @staticmethod
    def _body_lines_in_range(body: list[str], new_start: int, line_start: int, line_end: int) -> list[int]:
        """The indices of the body lines whose new-side line number falls in ``[line_start, line_end]``.

        Walks the hunk body assigning new-side numbers: a context (`` ``) or added (``+``) line occupies
        the next new-side line, a removed (``-``) line occupies none. So the returned indices are exactly
        the lines the comment points at, which anchor the context window.
        """
        marked: list[int] = []
        new_line = new_start
        for index, line in enumerate(body):
            if line.startswith("-"):
                continue
            if line_start <= new_line <= line_end:
                marked.append(index)
            new_line += 1
        return marked

    @staticmethod
    def _trimmed_text(hunks: list[str], comment: CommentDict, context: int) -> str:
        """Every covering hunk trimmed to ``context`` and concatenated — the shared body of both renderings."""
        return "".join(Show._trim_hunk(hunk, comment["line_start"], comment["line_end"], context) for hunk in hunks)

    # ───────────────────────────── rendering ─────────────────────────────

    @staticmethod
    def render(comment: CommentDict, iteration_index: int, hunks: list[str], context: int) -> str:
        """The human rendering: a header naming the comment, its location, its body, then the trimmed hunk(s).

        The header carries the iteration, comment id, author role and timestamp; the second line is
        ``path:line_start-line_end``; the body is quoted; then the covering hunk(s) trimmed to ``context``
        lines either side of the comment's own lines. Byte-identical for a human and an agent (FR-4): no
        colour, no terminal detection.
        """
        location = f"{comment['path']}:{comment['line_start']}-{comment['line_end']}"
        header = f"iteration {iteration_index} · comment {comment['id']} · {comment['author_role']} · {comment['created_at']}"
        quoted_body = "\n".join(f"> {body_line}" for body_line in comment["body"].splitlines()) or ">"
        return f"{header}\n{location}\n\n{quoted_body}\n\n{Show._trimmed_text(hunks, comment, context)}"

    @staticmethod
    def as_json(comment: CommentDict, iteration_index: int, hunks: list[str], context: int) -> dict[str, object]:
        """The ``--json`` payload: the comment's identity and location plus the trimmed covering hunk text.

        Exactly the documented keys, and the same context-trimmed hunk the human rendering shows, so a
        driver that branches on ``--json`` sees what a human would.
        """
        return {
            "comment": comment["id"],
            "iteration_index": iteration_index,
            "path": comment["path"],
            "line_start": comment["line_start"],
            "line_end": comment["line_end"],
            "body": comment["body"],
            "hunk": Show._trimmed_text(hunks, comment, context),
        }
