# eval-harvest-annotate(1)

## NAME

**eval-harvest annotate** — merge streamed judgment records into a candidate, or check what is
still unfilled.

## SYNOPSIS

```
eval-harvest annotate <candidate> [--stdin | --from-json <file>] [--check] [--replace-findings] [--dataset <dir>]
```

## DESCRIPTION

`annotate` fills a captured candidate **append-only**, one small record at a time, so you never
rewrite the parts of the file you are not changing. It reads a stream of judgment records — a
comment's classification, a finding, the change risk — and merges each into
`candidates/pr-<n>.json`, writing the file back in the same canonical bytes `capture` would produce.

Give the records on stdin as JSONL (`--stdin`, one JSON object per line) or in a file as a JSON
array (`--from-json <file>`); exactly one source is required, unless `--check` is given (which may
validate the candidate with no input at all). Records apply in input order and the last writer wins
for a scalar field.

Each record is one of three shapes, discriminated by the key it carries:

| Record | Shape | Effect |
|--------|-------|--------|
| comment | `{"comment": <id>, "classification": "defect\|nit\|question\|approval\|bot", "rationale": "…"}` | sets that comment's `classification` and `classification_rationale` |
| finding | `{"finding": {"comment_ids": […], "statement": "…", "severity": "low\|medium\|high", "severity_evidence": "…", "severity_rationale": "…", "reference_only": false}}` | appends to `findings[]` |
| risk | `{"risk_classified": "low\|medium\|high", "rationale": "…"}` (optionally `{"rubric_version": "v1"}`) | sets `change_risk.risk_classified` and its rationale, or pins the rubric version |

Re-classifying an already-classified comment **overwrites** it, and the run reports which ids were
overwritten — re-reading and correcting is normal, a silent overwrite is not. `--replace-findings`
clears `findings[]` before applying, to rescope the finding set in one command. `--dataset` is
accepted for parity with the other verbs and is unused: the candidate path is enough.

`annotate` refuses, at exit 3 in the four-field shape and **without writing anything**, when: a
record is not valid JSON (`malformed-record`, naming the line); a record carries none of the three
keys (`annotate.unrecognised-record`); a comment id names no comment (`annotate.unknown-comment`);
or a finding fails the severity-evidence gate. A partially-applied candidate is worse than an
unfilled one, so any one bad record aborts the whole write.

`annotate --check` validates the candidate and lists what is still unfilled without writing. It
exits `3` while any slot remains and `0` when the candidate is fully and coherently filled, so a
driver can loop `annotate` until `--check` is clean and then `emit`.

## METHODOLOGY

**Fill the candidate append-only, one small record at a time.** `annotate` merges a stream of
judgment records into the candidate so you never reproduce the bytes you are not changing. Pipe
JSONL on stdin or pass a JSON array; records apply in order and the last writer wins.

Re-classifying a comment **overwrites** it, and the run reports which ids it overwrote. An unknown
comment id or an unrecognised record refuses rather than silently dropping a judgment.

`annotate` **never infers a judgment**: it does not guess a severity, default `reference_only`, or
fill the rubric version. A finding that omits one of those is a violation you must resolve — the CLI
records exactly what you said and nothing more.

Run `annotate --check` to validate the candidate as it stands and list what is still unfilled; it
exits non-zero while any slot remains, so a driver can loop until clean. The field-by-field contract
for every slot is in the CANDIDATE FILE section of `eval-harvest-capture(1)`.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`, `eval-harvest-show(1)`, `eval-harvest-emit(1)`.
