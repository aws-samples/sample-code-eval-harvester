# eval-harvest-verify(1)

## NAME

**eval-harvest verify** — check a datapoint is sound and its answer is absent from everywhere the
agent can read.

## SYNOPSIS

```
eval-harvest verify <task-dir> [--clone <dir>] [--json]
```

## DESCRIPTION

Offline. Checks the base commit exists and the patch applies at it, that oracle findings reference
real changed lines, and that the rubric is coherent — then runs the answer-absence scans. Also
called internally by `emit` before it writes.

## METHODOLOGY

**The answer must be absent** from everywhere the evaluated agent can read. `verify` runs two scans:

- a **content** scan of every agent-visible file (`instruction.md`, `change.patch`, the mounted
  tree) for the PR number, review text, and verdict; and
- the **git-channel** checklist that catches routes which grep clean but leak under `git show`
  (`objects/info/alternates`, packed refs, an unreachable reflog entry).

A scan that finds nothing must prove it scanned something: a **control token** is planted in the
corpus, and `verify` refuses with `scan-did-not-run` if the scan returns without it — because an
empty result and a broken scan look identical otherwise.

**A token that already exists at the base commit predates the change**, so its presence carries no
information about the review and is not a leak. Telling that apart from a genuine leak needs a
`--clone`: `verify` runs `git grep` for the token against the base commit and, when it finds it,
**suppresses** the hit and reports it with the base commit rather than failing. Without a clone the
hit still fails `answer-present` — guessing that an unresolvable token is benign is not an option —
but the refusal names a runnable `verify --clone …` to resolve it, or `--override answer-present` to
accept it deliberately. The control token is never suppressible, so `scan-did-not-run` keeps its bite.

**Every oracle finding must point at a line the emitted change touched**, `reference_only` findings
included — they are matched into coverage by location overlap, so an unreachable one deflates every
graded agent's score. `finding-line-absent` is that refusal. The rule and the field it applies to are
documented once, in the **candidate file** contract in `eval-harvest-capture(1)`'s CANDIDATE FILE
section; `capture` records `in_diff_iterations` so you can see the answer before you fill anything.

A failure is an instruction to act on: it names the check, the datapoint, the offending content, and
the next command. Fix the datapoint and re-run.

## EXIT STATUS

`0` pass · `4` a check failed · `5` a check was unresolved (runtime absent).

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`, `eval-harvest-emit(1)`.
