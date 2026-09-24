---
id: A-1
title: "[Skeleton] Scaffold the uv package, CLI dispatch, and refusal/exit-code helper"
feature: pr-eval-harvest
workstream: Project skeleton & dev tooling
status: todo
complexity: S
implements: [TP-1, NFR-5, FR-2, FR-4]
user_story: US-1
blocked_by: []
blocks: [A-2, A-3, C-2, D-1, E-1, F-2]
---

# A-1: [Skeleton] Scaffold the uv package, CLI dispatch, and refusal/exit-code helper

## Context

There is no workspace manifest, lockfile, or package on this branch yet — everything is docs. Nothing builds until the package exists. This task creates the one `uv`-managed `eval-harvest` package with a console entry point, dev tooling, and the CLI dispatch skeleton every later verb plugs into. It also lands the shared refusal helper (the FR-2 four-field shape) and the distinct-per-class exit codes, because every refusing verb from B-1 onward depends on them.

**North star:** an SDE points a coding agent at this CLI and gets Harbor review-eval datapoints — the CLI has to be an installable command first.
**Implements:** TP-1, NFR-5, FR-2 (helper), FR-4  ·  **User story:** US-1

## Design References

- **Tech plan:** `../tech-plan.md` §6.1 — the module table; this task creates `cli.py` (dispatch, refusal formatting, exit codes) and the `src/eval_harvest/` layout
- **Tech plan:** §9 "Conventions shared by every verb" — the exit-code table (0/2/3/4/5) and the `--json` refusal object `{ok:false, failures:[{check,datapoint,offending,next}]}`
- **ADR-1** in §11 — why one `uv` package with verb subcommands, not per-verb scripts; the decision is settled
- **ADR-2** in §11 — zero third-party runtime deps; dev-only ruff/pytest/type-checker
- **CLAUDE.md** — "Do not add a `docs/spec/`, requirement ids, or a conformance level"; keep the package small

## What To Build

1. Create `pyproject.toml` for package `eval-harvest`: `[project].dependencies = []` (runtime deps empty — NFR-5/FR-3), `requires-python = ">=3.12"` (the repo's 3.12 pin), the `hatchling` build backend with `packages = ["src/eval_harvest"]` under `[tool.hatch.build.targets.wheel]`, a console entry point `eval-harvest = "eval_harvest.cli:main"`, and a `[dependency-groups]`/dev group of `ruff`, `pytest`, `pytest-xdist`, `mypy` (the chosen type checker), and `pydantic`. Record why `pytest-xdist` and `pydantic` are dev-only: `pytest-xdist` gives the suite `-n auto` parallelism (wired in A-3); `pydantic` will be used by the eval harness (Workstream G, a parallel workstream under `eval/`), which is outside the shipped `eval_harvest` package — the FR-3/NFR-5 audit is `[project].dependencies == []` plus the package's own imports (S-18 scans `src/eval_harvest/` only), so a dev dependency the CLI never imports does not touch it.
2. Create the `src/eval_harvest/` layout: `__init__.py`, `cli.py`. Leave placeholders only where a later task fills them — do not stub modules that later tasks create.
3. Create `mise.toml` with dev tasks: `lint` (ruff check + format-check), `format` (ruff format), `test` (pytest), `typecheck` (`uv run mypy .`), the security gate (below), and `check` — the pre-merge gate — which `depends = ["lint", "typecheck", "security", "test"]`. Name them plainly; later tasks reference these names.
   - **The security gate is a rolled-up `security` task over per-tool sub-tasks** (see the security-gate decision in `../decisions-tech-plan.md`). Create `security:bandit` (bandit — see step 4), `security:secrets` (`gitleaks git . --no-banner --redact`, scanning the git history), and `security:sast` (`semgrep scan --config p/python --config p/terraform --config p/secrets --config p/owasp-top-ten --metrics=off --disable-version-check --error .`). Then `security` runs all of them via a `run` task-list (`[{ task = "security:bandit" }, { task = "security:secrets" }, { task = "security:sast" }]`). checkov is **not** wired here — it joins `security` as `infra-check` in I-1, when the Terraform it scans exists. `semgrep` prints its scan summary (no `--quiet`) so an empty result proves it scanned (NFR-6); `--metrics=off --disable-version-check` stop semgrep hanging on its exit-time network calls after the scan.
   - **Pin the tool versions.** Add `gitleaks` and `semgrep` to `[tools]` at exact versions (not `latest`), alongside `python = "3.12"`; bandit runs via a pinned `uv tool run` (step 4). A floated scanner silently turns a green gate red on another machine.
4. Configure ruff and bandit in `pyproject.toml`: ruff `line-length = 130`, `target-version = "py312"`, `lint.select = ["E", "F", "W", "I", "N", "UP", "B", "SIM", "RET", "PL"]`, a `tests/** = ["PLR2004"]` per-file ignore (tests pin the literal exit-code contract), and `format.line-ending = "lf"`. For the argv-only subprocess pattern the wrapper (A-2) uses, handle subprocess safety through bandit — a justified project-wide `skips = ["B603"]` with a comment (the argv is a literal-led list, `shell=False`, the binary pinned via `executable=`), and `# nosec B404` on the import — rather than ruff's `S`/`flake8-bandit` rules, which are not in the `select` list. bandit also raises **B607** (partial executable path) on each such call because `argv[0]` is a bare program name (`"git"`); this is suppressed with a per-call `# nosec B607` where the calls are written (A-2 and later), not fixed in code — `argv[0]` is kept a literal on purpose so `security:sast` (semgrep) sees a static command, and an absolute `argv[0]` would clear B607 but trip semgrep, so the two cannot both be satisfied in code.
5. In `cli.py`, build the argparse dispatcher on a stateless `Cli` namespace (class/static methods, per CODING_STANDARDS — no instance state): a top-level parser with a subcommand per verb (`init`, `survey`, `capture`, `emit`, `verify`, `dataset`), each registered by its own module later. A module-level `main()` calls `Cli.run(argv)`, which parses argv, dispatches to the named verb, and returns the verb's exit code.
6. Add the shared refusal helper as `Cli.refuse(check, datapoint, offending, next_, *, exit_code, json_mode) -> int` that prints all four fields to stderr (human shape) or the `{ok:false, failures:[...]}` object (`--json`), and returns the exit code. Define the exit codes as an `ExitCode` `IntEnum`: `0` success, `2` usage error, `3` refusal, `4` verification failure, `5` runtime-unavailable.
7. Add a top-level `--json` flag convention (per §9): default is human-readable, `--json` selects the machine object.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `pyproject.toml` | Create | Package metadata, empty runtime deps, hatchling backend, entry point, dev group (incl. `pytest-xdist`, `pydantic`), ruff + bandit + pytest config |
| `mise.toml` | Create | `[tools]` (python + pinned `gitleaks`/`semgrep`); `lint`/`format`/`test`/`typecheck` tasks; the `security:bandit`/`security:secrets`/`security:sast` sub-tasks rolled up into `security`; the `check` gate (`depends` on lint/typecheck/security/test) |
| `src/eval_harvest/__init__.py` | Create | Package marker |
| `src/eval_harvest/cli.py` | Create | Dispatcher, `refuse()` helper, exit-code constants |
| `tests/test_cli.py` | Create | Dispatch + refusal-shape + exit-code tests |
| `uv.lock` | Create | Generated by `uv lock` |

## Schemas & Contracts

This task defines the refusal contract every verb reuses (FR-2). No prior shape exists.

**Refusal object (`--json`) — the shape every refusing verb reuses; the sealer built later in E-1 emits the same object:**
```json
{ "ok": false, "failures": [ { "check": "…", "datapoint": "…", "offending": "…", "next": "…" } ] }
```
**Exit codes:** `0` success · `2` usage · `3` refusal (input cannot become a datapoint) · `4` verification failure · `5` runtime-unavailable.

**Migration:** none — greenfield.
**Backward compatibility:** nothing exists to break — this is the first package on the branch (ADR-1).

## How To Verify

```bash
uv sync
uv run eval-harvest --help
mise run test -- tests/test_cli.py
mise run lint
mise run typecheck
```

Then by hand: `uv run eval-harvest --help` lists the verbs; an unknown verb exits `2`; a triggered `refuse(...)` prints all four fields to stderr and exits with the right code.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_unknown_verb_exits_2` | an unrecognized subcommand exits with code 2 | Usage errors colliding with refusal/verify exit codes (§9) |
| `test_refuse_prints_four_fields` | `refuse(...)` stderr contains check, datapoint, offending, next | An agent-unactionable error that stalls the unsupervised loop (FR-2) |
| `test_refuse_json_shape` | `--json` refusal emits `{ok:false, failures:[{check,datapoint,offending,next}]}` | A `--json` consumer breaking on a shape drift in the refusal object |
| `test_exit_codes_distinct_per_class` | 2/3/4/5 are distinct constants and returned as declared | A caller unable to tell a refusal from a verify failure |

## Acceptance Criteria

- [ ] `uv run eval-harvest --help` runs and lists the verbs
- [ ] `[project].dependencies` is empty (NFR-5); the dev group carries ruff/pytest/pytest-xdist/mypy/pydantic
- [ ] `refuse()` emits all four fields in both human and `--json` shapes with the correct exit code
- [ ] `mise run lint`, `mise run test`, `mise run typecheck`, `mise run security` (bandit + gitleaks + semgrep sub-tasks), and the `mise run check` gate all run (green on the stub); `mise run security:sast` prints its semgrep scan summary and exits rather than hanging
- [ ] All commands in "How To Verify" pass
- [ ] No `docs/spec/`, requirement ids, or conformance level introduced (CLAUDE.md)

## Out Of Scope

- Any verb's actual logic — each verb is its own task; A-1 only registers the subcommand slots and the dispatcher.
- The dependency-audit/import-scan test — that is A-3 (S-18).
- The cross-platform subprocess wrapper — that is A-2.

## Notes & Gotchas

- **Empty runtime deps is the product's spine (FR-3, NFR-3/5).** Do not add a runtime dependency "just for convenience" — A-3's test will fail and the FR-3 audit is "the list is empty."
- **`requires-python`:** `>=3.12` — the repo's 3.12 pin (`mise.toml`, `pyproject.toml` `py312`, mypy); `tomllib` (used later by C-2/D-2) is stdlib from 3.11.
- **Pick the type checker explicitly** (`mypy`) and put it in `mise.toml` + the dev group so A-3's CI matrix runs the same one. `typecheck` runs `uv run mypy .` (the project env) so mypy resolves pytest's types and the installed package.
- **The security gate is a rolled-up `mise run security` task** over per-tool sub-tasks — `security:bandit` (Python SAST), `security:secrets` (gitleaks over history), `security:sast` (semgrep) here, with `infra-check` (checkov) joining in I-1 — and it is part of the `check` gate. Each scanner is its own sub-task so a red gate names the tool. Ruff's `S`/`flake8-bandit` rules are deliberately *not* in `select`; subprocess safety (A-2's argv-only pattern) is expressed as a justified project-wide bandit `B603` skip with a comment plus a per-call `# nosec B607`, not scattered inline suppressions for everything.
- **Pin the scanner versions and keep semgrep honest.** gitleaks and semgrep go in `[tools]` at exact versions, bandit runs via a pinned `uv tool run` — a floated tool has turned this gate red before. semgrep runs without `--quiet` (its summary is the proof it scanned — NFR-6) and with `--metrics=off --disable-version-check` (its exit-time network calls otherwise hang after the scan). Its registry rulesets (`p/*`) are fetched at run time, so `security:sast` needs network.
- **`--json` refusals print to stdout** (the json-to-stdout convention every `--json` verb follows, including the sealer added later in E-1); human refusals go to stderr.

## Dependencies

**Blocked by:** nothing — ready to start
**Blocks:** [A-2](A-2-build-cross-platform-gitcmd-wrapper.md), [A-3](A-3-ci-matrix-and-dependency-audit-test.md), [C-2](C-2-build-riskmap-py-parse-risk-map-compute-structural-risk.md), [D-1](D-1-build-tomlw-py-deterministic-toml-writer.md), [E-1](E-1-build-seal-py-git-channel-checklist-and-content-scanner.md), [F-2](F-2-man-pages-and-help-methodology-and-parity-test.md)
