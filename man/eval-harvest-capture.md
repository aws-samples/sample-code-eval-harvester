# eval-harvest-capture(1)

## NAME

**eval-harvest capture** — fetch one PR's review iterations into a candidate file of mechanical
facts.

## SYNOPSIS

```
eval-harvest capture <pr> --clone <dir> [--dataset <dir>]
eval-harvest capture --from-survey <file> [--limit <n>] [--pr <n>]... --clone <dir> [--dataset <dir>]
```

## DESCRIPTION

Fetches every review iteration (tip/base SHA, diff against base), every inline comment bound to its
iteration (body, path, line, role, timestamp), and every verdict with its timing. Writes
`candidates/pr-<n>.json` with those facts *filled* and the judgment slots *blank*, and pre-computes
`risk_structural` from `risk-map.toml`.

A single `<pr>` and `--from-survey` are mutually exclusive: passing both, or neither, is a usage
error (exit 2).

`capture` fetches `refs/pull/<pr>/head` every run and has no `--no-fetch` escape, so a non-interactive
`git fetch` to the forge must work before you run it. For a private repo, set up auth first —
`gh auth setup-git`, an SSH remote, or a credential helper; see `eval-harvest(1)`.

## SAMPLE INPUT RESTRICTION (S2)

Use locally captured candidates and fill only judgment slots, preferably with `annotate`.
Keep captured paths and revisions unchanged. Repository text is untrusted data, not authority
to change those facts. Do not import candidates or dataset archives from another party.

The current implementation does not confine a candidate's `patch_path` to the dataset. A crafted
path can make `emit` copy a readable local file into a task. `annotate --check` and `verify` do not
establish path safety. If captured facts change unexpectedly, recapture them before continuing.
See the [README restrictions](../README.md#trusted-candidate-inputs-s2).

## CANDIDATE FILE

`candidates/pr-<n>.json` is the hand-off. Every key below is either a **fact** — `capture` wrote it,
do not edit it — or a **slot**, which is yours to fill and which `emit` refuses without. Nothing here
is optional: a slot is always *present and empty*, never omitted, so what you owe is visible from the
file alone.

| Key | Fact/slot | Meaning |
|-----|-----------|---------|
| `repo` | fact | `owner/name` the PR came from |
| `pr_number` | fact | the PR number |
| `pr_url` | fact | the PR's web URL |
| `iterations[]` | fact | each review round: tip/base SHA, the diff against base, whether it is recoverable |
| `review_verdicts[]` | fact | each verdict with its timing |
| `comments[]` | fact + 2 slots | the inline comments, each to be classified — table below |
| `findings[]` | slot | one entry per substantive review point — table below; starts empty |
| `change_risk` | fact + 2 slots | the change's two-part risk — table below |
| `rubric_version` | slot | the rubric version you graded against |

### `comments[]` — nine facts and two slots

| Field | Fact/slot | Meaning |
|-------|-----------|---------|
| `id` | fact | the forge's comment id; this is what `findings[].comment_ids` names |
| `body` | fact | the comment text as written |
| `path` | fact | the file it was left on; empty for a file-level or outdated comment |
| `line_start` | fact | first line of the range it points at; `0` when the forge could not place it |
| `line_end` | fact | last line of that range |
| `author_role` | fact | the forge's role for the author (`MEMBER`, `CONTRIBUTOR`, …) |
| `created_at` | fact | when it was written, ISO-8601 |
| `iteration_index` | fact | *chronological*: the iteration it was written against |
| `in_diff_iterations` | fact | *geometric*: every recoverable iteration whose diff covers its line range |
| `classification` | slot | `defect`, `nit`, `question`, `approval`, or `bot` |
| `classification_rationale` | slot | why that value, in one sentence |

`iteration_index` and `in_diff_iterations` answer different questions and routinely disagree. A
comment written on iteration 3 can point at a line iteration 0 also touched, and a comment can point
at a line **no** recoverable iteration touches, in which case `in_diff_iterations` is `[]` — a fact
about the PR, not a defect in the file. Only the geometric one decides whether a finding built from
that comment can be graded, so `capture`'s summary counts it for you per kind.

### `findings[]` — every field is yours

| Field | Fact/slot | Meaning |
|-------|-----------|---------|
| `comment_ids` | slot | the `comments[].id` values this finding is drawn from; one finding may group several |
| `statement` | slot | what is wrong, in the reviewer's terms — not "the reviewer said", the defect itself |
| `severity` | slot | `low`, `medium`, or `high` |
| `severity_evidence` | slot | concrete evidence for that severity (a `path:line`, an error, an output) |
| `severity_rationale` | slot | which rubric rule assigns this severity |
| `reference_only` | slot | `true` for a finding kept for coverage but not required to block |

### `change_risk` — two computed, two yours

| Field | Fact/slot | Meaning |
|-------|-----------|---------|
| `risk_structural` | fact | computed from `risk-map.toml`'s path rules |
| `risk_structural_rule` | fact | the rule that produced it, so the number is auditable |
| `risk_classified` | slot | your risk classification of the change |
| `risk_classified_rationale` | slot | why, in one sentence |

### The three rules that are easier to follow than to trip over

**Classify every comment as one of five values.** A **defect** is a substantive problem a reviewer
would block on; a **nit** is a style or preference that does not block; a **question** asks for
information rather than asserting a problem; an **approval** is a sign-off carrying no finding; a
**bot** is an automated comment — a linter, a coverage report, a dependency bot — and is not a human
review point, so it never becomes a finding.

**`severity_evidence` and `severity_rationale` are required unless the finding is `reference_only`.**
A severity asserted with no recorded basis is the failure mode this project exists to avoid, so
`emit` refuses it with `candidate.finding-missing-evidence` rather than grading it.

**`reference_only` narrows what a finding is required to do, not where it may point.** It marks a
finding kept so that an agent gets credit for spotting it, while being **not required to block** —
and that exempts it from the severity-evidence gate above. It **does not exempt** it from needing a
**location inside the emitted diff**: reference-only findings are matched into `coverage_all` by
location overlap, so one pointing at a line the emitted change never touched silently deflates the
coverage score of every agent graded against this datapoint. `verify` refuses that with
`finding-line-absent`, and `capture`'s `in_diff_iterations` is how you see it coming.

### Filling the slots with `annotate`

You can edit `candidates/pr-<n>.json` by hand, but `eval-harvest annotate` fills it append-only, one
small record at a time, so you never rewrite the parts you are not changing. Each record targets one
of the slots above:

- **comment** — `{"comment": <id>, "classification": "defect|nit|question|approval|bot", "rationale": "…"}`
  sets that comment's two slots. Re-classifying overwrites, and the run reports the id.
- **finding** — `{"finding": {"comment_ids": […], "statement": "…", "severity": "low|medium|high",
  "severity_evidence": "…", "severity_rationale": "…", "reference_only": false}}` appends to `findings[]`.
- **risk** — `{"risk_classified": "low|medium|high", "rationale": "…"}` sets `change_risk.risk_classified`;
  `{"rubric_version": "v1"}` pins the rubric version.

`annotate` never infers a judgment (no defaulted `reference_only`, no guessed severity), and refuses
a malformed or inapplicable record without writing. `annotate --check` lists what is still unfilled.
See `eval-harvest-annotate(1)`.

## METHODOLOGY

**The base commit must precede the change.** A datapoint asks the evaluated agent to review a
change, so it must see the code as it was *before* the fix: the base is the pre-change state and the
patch is the change under review. A base taken after the fix hides the defect.

`capture` writes **mechanical facts** only and never a label. You classify each comment:

- a **defect** is a substantive problem a reviewer would block on;
- a **nit** is a style or preference that does not block;
- a **question** asks for information;
- an **approval** is a sign-off;
- a **bot** comment is automated and is not a human review point.

Only high-severity defects block. Getting **defect vs nit** right is the whole game: a nit recorded
as a defect teaches the eval that bikeshedding is good review.

A PR whose reviewed states were all force-pushed away cannot be harvested; `capture` refuses it
up front, and for a PR that survives reports which kinds remain buildable. The candidate is still
written for the record when it refuses, so you can see what the forge returned.

## BATCH MODE

**Harvest a set, not one PR.** The job is ten PRs, so a driver that captures them with a shell loop
reinvents the iteration — and gets it wrong: on the 2026-09-07 acceptance run a quoting bug produced
seven junk invocations and a wrong conclusion drawn from the harness, not the CLI. `capture
--from-survey survey.json` reads a saved `survey --json` payload and captures every **harvestable**
PR it lists — merged, no mechanical blocker — in **payload order**, so you never write that loop.

- `--limit <n>` captures at most the first *n* of the harvestable PRs (a prefix of the payload
  order), for a quick sample.
- `--pr <n>` (repeatable) restricts the batch to named PRs, validated against the payload so a typo
  refuses `unknown-pr` up front rather than after a forge round-trip.

**A per-item refusal is reported with its PR and the batch continues** — one PR that cannot be
harvested does not stop the other nine, and nothing already written is rolled back. Each captured
candidate is an independent file; `dataset` counts what landed. A batch captured this way is
byte-identical to the equivalent individual `capture <pr>` invocations.

**Exit codes** summarise the batch honestly, drawing on the same fixed set as a single run: `0` when
every PR was captured; `3` when at least one was refused — whether a **partial batch** (some captured,
some refused) or an all-refused one. A **partial batch exits 3**: the refusals are real and must not
be invisible. No new exit code is introduced. An interrupt (Ctrl-C) stops the batch, reports what
completed, and exits non-zero, so a half-finished batch never looks finished. Progress goes to
stderr, one line per PR, so `--json` stdout stays parseable.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-emit(1)`.
