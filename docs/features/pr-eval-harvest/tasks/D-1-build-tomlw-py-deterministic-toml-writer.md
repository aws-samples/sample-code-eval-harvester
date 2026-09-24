---
id: D-1
title: "[Emission] Build tomlw.py: deterministic stdlib TOML writer"
feature: pr-eval-harvest
workstream: Emission
status: done
complexity: M
implements: [TP-2, NFR-1, NFR-5]
user_story: US-6
blocked_by: [A-1]
blocks: [D-2, C-2]
---

# D-1: [Emission] Build tomlw.py: deterministic stdlib TOML writer

## Context

`task.toml` and the dataset artefacts must be emitted byte-stably — Harbor computes a content hash over the task, and NFR-1 requires two emits from one candidate to be identical. `tomllib` reads TOML but nothing in the stdlib writes it, and a third-party writer is disallowed (zero runtime deps, ADR-2). So this task builds a small hand-rolled deterministic TOML writer — `emit_document`, the digest-stable serializer everything that writes `task.toml` goes through. Everything in Workstream D and C-2's template emit depend on it.

**North star:** the emitted task runs under Harbor, and re-emitting it is byte-identical so regeneration is safe.
**Implements:** TP-2, NFR-1, NFR-5  ·  **User story:** US-6

## Design References

- **Tech plan:** `../tech-plan.md` §2.3 TP-2 — "a deterministic, hand-rolled TOML writer (stdlib `tomllib` reads; nothing in stdlib writes)"
- **Tech plan:** §11 ADR-2 — "write it with a small hand-rolled deterministic writer (`tomlw.py`)"; a library does not guarantee byte-stable output across versions, which is why this is hand-rolled
- **Tech plan:** §8 — the `task.toml` shape the writer must emit (sections, arrays, nested tables, ISO timestamps as strings)
- **API surface to build:** `emit_document(doc: TomlValue) -> str` taking nested dicts of `TomlValue`, with a `TomlFloat` wrapper for pinned float formatting. This is the surface `harbor.py`'s `task_config_document` (D-2) serializes through

## What To Build

1. Create `src/eval_harvest/tomlw.py`.
2. Define the `TomlValue` type (str, int, float-wrapper, bool, ISO-datetime-as-string, list, dict) and a `TomlFloat` wrapper if float formatting needs pinning.
3. Write `emit_document(document: Mapping[str, TomlValue]) -> bytes` (or `str`): serialize deterministically — stable key order (preserve insertion order; the caller controls order), tables and array-of-tables (`[[rule]]`), inline arrays, proper string escaping, no trailing whitespace, `\n` line endings on all platforms (NFR-4/NFR-1).
4. Guarantee byte-stability: same input dict → identical bytes every run, no dict-hash-order dependence, no wall clock.
5. Handle the exact value kinds `task.toml` needs (§8): strings, integers (`pr_numbers = [1234]`), floats (`timeout_sec = 1800.0` — must render with the `.0`), booleans (`change_risk_disagreement = true`), string arrays, nested `[metadata.origin]`/`[metadata.harvest]` tables.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/tomlw.py` | Create | The deterministic writer |
| `tests/test_tomlw.py` | Create | Round-trip + golden-file tests |

## Schemas & Contracts

**API (the public surface this module exposes):**
```
emit_document(document: Mapping[str, TomlValue]) -> bytes
TomlFloat(value: float)   # pins float rendering, e.g. 1800.0 not 1800
```
Round-trip contract: `tomllib.loads(emit_document(d).decode())` equals `d` (modulo the `TomlFloat` wrapper). Byte contract: `emit_document(d) == emit_document(d)` across runs and platforms.

**Migration:** none.
**Backward compatibility:** none — new module.

## How To Verify

```bash
mise run test -- tests/test_tomlw.py
mise run lint
mise run typecheck
```

Then by hand: emit a document containing every value kind, re-parse with `tomllib`, confirm it round-trips; emit twice and `diff` the bytes.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_round_trips_through_tomllib` | `tomllib.loads(emit_document(d))` == `d` for every value kind | A serializer that emits TOML Harbor cannot parse |
| `test_byte_identical_across_runs` | two emits of the same dict are identical bytes (golden file) | Dict-hash-order or float-format nondeterminism breaking Harbor's content hash (NFR-1) |
| `test_float_renders_with_point` | `TomlFloat(1800.0)` renders `1800.0`, not `1800` | `timeout_sec` emitted as an int, failing Harbor's schema (§8) |
| `test_array_of_tables_and_nested_tables` | `[[rule]]` and `[metadata.origin]` serialize correctly | Malformed provenance/risk-map TOML |
| `test_string_escaping` | quotes/backslashes/newlines in a string are escaped | A review-body-derived string breaking the TOML (also §3 security-adjacent) |
| `test_line_endings_are_lf` | output uses `\n` on all platforms | CRLF on Windows changing the content hash (NFR-4/NFR-1) |

## Acceptance Criteria

- [ ] `emit_document` round-trips through `tomllib` for every value kind
- [ ] Two emits of the same dict are byte-identical, on every platform (NFR-1)
- [ ] Floats render with a decimal point (`1800.0`); LF line endings
- [ ] Array-of-tables and nested tables serialize correctly
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Building the actual `task.toml` document dict — that is `harbor.py` (D-2), which calls this writer.
- TOML *reading* — use stdlib `tomllib` directly; do not re-implement a parser.

## Notes & Gotchas

- **Digest stability is the whole point (TP-2, NFR-1).** A library "does not guarantee" byte-stability across versions, which is why this writer is hand-rolled. Pin key order (insertion order), float format, and line endings explicitly.
- **`timeout_sec = 1800.0`** must keep its `.0` — Harbor's schema expects a float there (§8, D-2's validators check it). This is the most likely subtle bug; `test_float_renders_with_point` guards it.
- **No wall clock, no `hash()`-ordered iteration** in the output path (§3 determinism budget).

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md)
**Blocks:** [D-2](D-2-port-harbor-py-constants-and-validators.md), [C-2](C-2-build-riskmap-py-parse-risk-map-compute-structural-risk.md)

