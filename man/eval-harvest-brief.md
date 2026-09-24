# eval-harvest-brief(1)

## NAME

**eval-harvest brief** — print one self-contained, patch-free brief for filling a single PR's
candidate.

## SYNOPSIS

```
eval-harvest brief <candidate> [--dataset <dir>] [--body-chars <n>]
```

## DESCRIPTION

Given a captured candidate, `brief` prints everything needed to fill that one candidate and nothing
else: the PR's identity, the rubric version the dataset declares, the change's structural risk, the
review iterations (with recoverability and how many comments each diff covers), which datapoint
kinds are buildable, every inline comment with its in-diff markers, the fill contract, and the
exact next commands. It is a read-only view over `capture`'s output: it reads
`candidates/pr-<n>.json` and the dataset's `rubric.md`, makes no judgment, and writes nothing.

`--dataset <dir>` defaults to the candidate's dataset root, where `brief` reads `rubric.md` for the
version to pin — the same root `emit` and `show` derive. `--body-chars <n>` truncates each comment
body to *n* characters, with a trailing `…` and a pointer to `show` for the rest; the default `0`
prints every body **in full**, because a truncated review comment cannot be classified honestly and
the whole point is that this brief is sufficient on its own.

Each comment is marked `[in reject diff]` or `[not in reject diff]` from its `in_diff_iterations`,
so you can see which comments can anchor a gradeable finding before you fill anything. `brief`
suggests no classification for any comment — the judgment is yours (FR-9).

`brief` refuses, at exit 3 in the four-field shape, with `no-emittable-kind` when no iteration is
recoverable, so neither a reject nor an approve datapoint can be built from the PR. A candidate path
that is not a file is a usage error (exit 2).

`--json` emits the same information as a payload — `repo`, `pr_number`, `pr_url`, `rubric_version`,
`change_risk`, `iterations`, `emittable`, `comments`, `fill_contract`, `next_commands` — with no
prose-only fields, so a driver can branch without parsing the human rendering.

## METHODOLOGY

**One PR, one self-contained brief, one fresh context.** `brief` prints everything needed to fill a
single candidate and nothing else. Hand one PR's self-contained brief to a fresh subagent (or a
fresh session) so the fill work stops accumulating: ten datapoints are ten independent judgments,
not one growing context. That single-large-context walk is what cost the 2026-09-07 acceptance run
roughly a million tokens of the driving agent's context.

`brief` prints **no diff bytes** — use `show --comment <id>` to see the lines a comment points at,
per comment, on demand. It vends facts and the fill contract only, and **suggests no
classification**: the judgment is yours. The field-by-field fill contract is the same one
`eval-harvest-capture(1)` prints under its CANDIDATE FILE section.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`, `eval-harvest-show(1)`, `eval-harvest-annotate(1)`,
`eval-harvest-emit(1)`.
