# eval-harvest-show(1)

## NAME

**eval-harvest show** — print just the diff hunk covering one review comment's line range.

## SYNOPSIS

```
eval-harvest show <candidate> --comment <id> [--iteration <n>] [--context <lines>] [--dataset <dir>]
```

## DESCRIPTION

Given a captured candidate and a comment id, `show` prints the comment and only the diff hunk(s)
whose new-side span covers the line range the comment points at, with a few lines of surrounding
context. It is a read-only view over `capture`'s output: it reads `candidates/pr-<n>.json` and the
materialized `patches/`, makes no judgment, and writes nothing.

`--iteration <n>` selects which review round's diff to render; by default `show` uses the comment's
first `in_diff_iterations` entry — the first recoverable iteration whose diff actually covers the
line. An explicit `--iteration` wins, so you can compare how the same line looked across rounds.
`--context <lines>` (default 5) is the number of unified-diff lines shown either side of the
comment's own lines. `--dataset <dir>` defaults to the candidate's dataset root, so `show` and
`emit` read the same patch bytes.

`show` refuses, at exit 3 in the four-field shape, when: the id names no comment
(`unknown-comment`); the comment's line is in no recoverable iteration's diff
(`comment-outside-every-diff`); an explicit `--iteration` names a round with no materialized patch
(`iteration-not-recoverable`); or the recorded patch is missing under the dataset (`missing-patch`).

## SAMPLE INPUT RESTRICTION (S2)

Use trusted, locally captured candidates with unchanged patch paths. `show` reads the
candidate's `patch_path` without confining it to the dataset. Do not inspect an untrusted
candidate with this command; the process can read files outside the dataset.
See the [README restrictions](../README.md#trusted-candidate-inputs-s2).

## METHODOLOGY

**Classify a comment from the lines it points at.** `show` prints just the diff hunk covering a
comment's line range, with a few lines of surrounding context — enough to judge whether the comment
names a real defect and how severe it is. **Reading whole patch files is never necessary**, and
never intended: the patches are materialized for the container that grades the datapoint, not for
you to read. On the 2026-09-07 acceptance run, reading whole patches to place 40 comments cost
roughly 2.1 MB of context; `show` renders each comment's hunk in a handful of lines instead.

By default `show` renders the first iteration whose diff covers the line; pass `--iteration` to see
how that line looked in another review round, and `--context` to widen or narrow the surrounding
lines. `show` makes no judgment and writes nothing.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`, `eval-harvest-emit(1)`.
