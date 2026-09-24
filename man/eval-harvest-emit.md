# eval-harvest-emit(1)

## NAME

**eval-harvest emit** — turn a filled candidate into a self-contained, verified Harbor task
directory.

## SYNOPSIS

```
eval-harvest emit <candidate> --kind reject|approve [--iteration <n>] [--override <check>] [--clone <dir>]
eval-harvest emit --all --kind reject|approve [--override <check>] [--clone <dir>] [--dataset <dir>]
```

## DESCRIPTION

Reads a filled candidate (plus `rubric.md` and `risk-map.toml`; no git call), assembles the task in
a temp directory, runs `verify`, and promotes it to `tasks/<name>__<kind>/` only on pass. Writes
`environment/Dockerfile`, `change.patch`, `instruction.md`, `tests/`, and `task.toml`. Two emits
from one candidate are byte-identical.

A single `<candidate>` and `--all` are mutually exclusive: passing both, or neither, is a usage
error (exit 2).

## SAMPLE INPUT RESTRICTION (S2)

Use trusted, locally captured candidates; fill only judgment slots and keep paths and
revisions unchanged. A crafted `patch_path` can copy readable files outside the dataset into
`environment/change.patch`. `verify` and `--clone` do not confine those reads. Do not import
third-party candidates or dataset archives. Review task files before sharing or uploading.

This applies to every candidate selected by `--all`. Treat the dataset directory and patch files
as trusted local inputs; do not add symlinks supplied by another party. Passing verification does
not make an imported candidate safe. See the [README restrictions](../README.md#trusted-candidate-inputs-s2).

## METHODOLOGY

**The instruction must not name the outcome.** `instruction.md` is what the evaluated agent reads;
if it names the verdict or the defect, the answer leaks and the datapoint measures reading
comprehension, not review skill. The instruction is uniform across datapoints except for declared
slots.

**The expected verdict follows the human review, not a severity formula.** `--kind` selects which
state of the PR becomes the datapoint:

- `reject` builds from a rejected iteration (expected verdict: *block*) and needs at least one
  substantive defect, else `empty-oracle`;
- `approve` builds from the approved state (expected: *approve*) and is refused if any finding is
  high severity.

A PR whose only comments are nits is an `approve` datapoint, not a reject — a nit is not a blocking
defect. Severity drives finding-level scoring, never the verdict.

**The reject datapoint defaults to the first reviewed state**, because that is the code the reviewer
objected to (ADR-1). Every oracle finding must have a comment inside the emitted iteration's diff, or
it cannot be located in the change under review; `emit` refuses `finding-iteration-mismatch` before
writing anything and names each offending finding and the iteration that carries it — the same rule
`verify`'s `finding-line-absent` applies structurally, moved to the earliest verb that holds the
facts. Pass `--iteration <n>` when the substantive review happened in a later round, to build the
honest datapoint from that iteration; it must name a recoverable index and never changes the verdict
or the oracle. A deliberate reference-only-heavy datapoint can proceed with `--override
finding-iteration-mismatch`, recorded in `task.toml`.

Which findings satisfy each kind is
decided by the fields in the **candidate file**; that contract lives in one place,
`eval-harvest-capture(1)`'s CANDIDATE FILE section, and is not restated here. Tasks are emitted in **`separate`**
verifier mode, and the Dockerfile **seals** the container against the repo's future. `emit` runs
`verify` first and refuses to write a failing datapoint unless you pass an explicit, recorded
`--override <check>`.

**`emit` reports which checks ran and which were left unresolved.** A hard failure (exit 4) blocks
the write; an *unresolved* check is not a pass and never a failure (§7.3). The git-channel checklist
needs a container runtime and the base-exists/patch-applies checks need a `--clone <dir>`; without
them those checks stay unresolved and `emit` still writes the datapoint offline, but it is **not
verified** until you run `eval-harvest verify <task-dir>` (with `--clone`) where a runtime exists.
`emit` names each unresolved check with its reason rather than claiming "all checks passed", and the
`--json` payload carries a `checks` object (`passed`, `unresolved`) so a driver can gate on it.

## BATCH MODE

**Emit a whole dataset with `--all`.** `emit --all --kind <kind>` emits every candidate under
`<dataset>/candidates/`, in sorted order so it never silently stops at the first alphabetical gap,
for the chosen `--kind`. It **reports a per-item refusal and continues** rather than stopping the
batch — one candidate that refuses does not block the rest, and nothing already written is rolled
back. A batch is byte-identical to the equivalent individual `emit <candidate>` invocations.

Exit codes match `capture`'s batch mode and the single-run set: `0` when every candidate was written;
`3` when at least one was refused — a **partial batch exits 3** (some written, some refused) exactly
as an all-refused one does, because a refusal must not be invisible. No new exit code is introduced.
An interrupt stops the batch, reports what completed, and exits non-zero.

The batch summary — attempted / written / refused, one line per refused candidate with its check —
aggregates the per-datapoint unresolved-check reporting: it states how many written datapoints still
have unresolved checks and the single `verify` command that resolves them, replacing ten blocks of
per-candidate output. Progress goes to stderr so `--json` stdout stays parseable; `--json` emits one
`items` array (each entry the single-candidate payload shape) plus a `summary` object.

## EXIT STATUS

`3` (refusal) with `empty-oracle`; `4` when `verify` fails. Unresolved checks do **not** change the
exit code — a datapoint written with unresolved checks still exits `0`. In batch mode (`--all`), `3`
when any candidate was refused (a partial batch exits 3), `0` only when every candidate was written.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`, `eval-harvest-verify(1)`.
