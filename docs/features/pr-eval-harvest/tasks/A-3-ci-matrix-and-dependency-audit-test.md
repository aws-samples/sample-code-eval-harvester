---
id: A-3
title: "[Skeleton] CI matrix + dependency-audit/import-scan + static-analysis excludes"
feature: pr-eval-harvest
workstream: Project skeleton & dev tooling
status: done
complexity: S
implements: [NFR-3, NFR-4, NFR-5, FR-3]
user_story: US-1
blocked_by: [A-1]
blocks: []
---

# A-3: [Skeleton] CI matrix + dependency-audit/import-scan + static-analysis excludes

## Context

Two of the product's spine constraints erode silently unless a machine watches them: "the CLI runs the same on Windows and macOS" (NFR-4) and "no LLM/model client anywhere in the dependency tree" (FR-3/NFR-3/NFR-5). This task lands the CI matrix that runs the suite on all three OSes and the import-scan + manifest test (scenario S-18) that fails the moment a model client or a non-empty runtime dep sneaks in. It can be built as soon as the package exists (A-1); it does not need any verb.

It also owns the two disciplines that keep the pre-merge gate honest and fast. First, the **static-analysis exclude lists must name every generated-output tree**, because being in `.gitignore` is not being excluded from the linters — the eval harness (Workstream G) writes one copy of the `eval_harvest` package per trial under `eval/runs/`, and two copies make mypy report a duplicate module name and then *stop checking entirely*, so the whole source tree silently goes untyped behind a single unrelated error. mypy, ruff, and bandit each carry their own exclude list, so the three must be kept in step with `.gitignore` and pinned by a test. Second, the **suite runs in parallel**: it is dominated by real `git` subprocesses (each paying the macOS Command Line Tools shim tax), so a single worker leaves the other cores idle; `pytest-xdist` with `-n auto` cuts wall time roughly threefold and `check` inherits it for free.

**North star:** the customer trusts the CLI on their own machine, "no LLM in the CLI" stays true as the codebase grows, and the gate's colour always means something.
**Implements:** NFR-3, NFR-4, NFR-5, FR-3  ·  **User story:** US-1

## Design References

- **Tech plan:** `../tech-plan.md` §2.1 FR-3 — "an import-scan check finds no `openai`/`anthropic`/`litellm`/HTTP-to-model-endpoint import"
- **Tech plan:** §2.2 NFR-3/NFR-4/NFR-5 — dependency integrity, Windows/macOS/Linux parity, zero runtime deps
- **Tech plan:** §12, scenario S-18 — "No module under `src/eval_harvest/` imports a model client, and `[project].dependencies` is empty"
- **Tech plan:** §12, scenario S-16 — the full suite green on windows-latest/macos-latest/ubuntu-latest
- **CLAUDE.md** — "Before every commit, `mise run check` must pass … Do not commit red." A gate that is red for a reason unrelated to your change trains whoever hits it to read red as noise

## What To Build

1. Create `tests/test_no_model_client.py` (S-18): (a) parse `pyproject.toml` with `tomllib` and assert `[project].dependencies == []`; (b) walk every `*.py` under `src/eval_harvest/` and, using `ast`, collect all `import`/`from` module names, asserting none matches a banned set (`openai`, `anthropic`, `litellm`, `cohere`, `google.generativeai`, `mistralai`, `ollama`, `groq`, `together`, `replicate`, `vertexai`, and any obvious HTTP-to-model client). A name is banned when it equals one of these or is a submodule of one (`anthropic.types` counts, `coherence` does not). Generic HTTP libraries (`httpx`, `requests`) are intentionally *not* banned — the CLI may use one; only a *model* client is forbidden. Use `ast`, not a regex over source, so a name in a comment or string does not trip it and an aliased import does not hide.
2. Create the CI workflow (`.github/workflows/ci.yml`): a matrix over `windows-latest`, `macos-latest`, `ubuntu-latest`; each job installs `uv`, runs `uv sync`, then `mise run lint`, `mise run typecheck`, and `mise run test`.
3. Ensure `git` and `gh` are available to the tests that shell out (the fixture-repo tests exercise real git). Both ship preinstalled on all three GitHub-hosted runners, so no install step is needed; tests requiring `gh` use the recorded-payload path, so `gh` auth is not needed in CI either.
4. **Exclude every generated-output tree from all three static-analysis tools that walk the repo.** Add the generated-output directories — `work/` (mining clones of arbitrary repositories), `eval/runs/` (one copy of the `eval_harvest` package per trial), `eval/reports/` (rendered eval output) — to mypy's `exclude`, ruff's `extend-exclude`, and bandit's `exclude_dirs` in `pyproject.toml`, each list carrying a comment naming `.gitignore` as the list it must stay in step with, and a reciprocal comment in `.gitignore`. (The separate components `vendor/` and `harvest_env/` are excluded too, for a different reason — they import Harbor internals only present in the `eval` group.) Do not derive the tool excludes *from* `.gitignore` (mypy/ruff do not support it); do not add a `# type: ignore` or per-file override (the collision is module-resolution across generated trees, not a typing complaint).
5. Create `tests/test_static_analysis_excludes.py`: compare a literal list of the generated-output directories (`work`, `eval/runs`, `eval/reports`) against all three parsed exclude lists, tolerant of each tool's spelling (leading `./`, trailing `/`). Do **not** diff `.gitignore` — most gitignore entries have no business in a linter's excludes, so the comparison would be noise.
6. **Run the suite in parallel.** Add `pytest-xdist` to the `dev` dependency group and set `addopts = ["-rs", "-n", "auto"]` in `[tool.pytest.ini_options]`. Keep `-rs` (it prints skip reasons, which the visibly-skipped Harbor-dependent tests depend on — a skip that stops printing reads as coverage). Tests isolate on `tmp_path`, so they are xdist-safe.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `tests/test_no_model_client.py` | Create | S-18: manifest + AST import scan |
| `.github/workflows/ci.yml` | Create | 3-OS matrix running lint/typecheck/test |
| `tests/test_static_analysis_excludes.py` | Create | Pins the generated-output dirs against mypy/ruff/bandit excludes |
| `pyproject.toml` | Modify | Generated-output excludes on all three tools + `.gitignore`-parity comments; `pytest-xdist` in `dev`; `addopts = ["-rs", "-n", "auto"]` |
| `.gitignore` | Modify (comment) | Reciprocal note that the tool exclude lists carry a matching exclusion |

## Schemas & Contracts

None — this task changes no stored data or API surface. It reads `pyproject.toml` (`[project].dependencies`) and the source tree.

## How To Verify

```bash
mise run test -- tests/test_no_model_client.py tests/test_static_analysis_excludes.py
mise run lint
mise run check                    # green, in parallel
mise run typecheck                # must report the full source-file count, not "errors prevented further checking"
```

Then by hand: add `import anthropic` to a throwaway module under `src/eval_harvest/`, run the test, confirm it goes red naming the file; remove it, confirm green. (This is the FR-37 discipline applied to S-18 — watch the guard fail.) Push a branch and confirm all three CI legs run.

Watch the exclusion matter (FR-37 discipline applied to the gate): with two or more trials present under `eval/runs/` — and since Workstream G (which generates them) is a parallel workstream that may not exist yet, plant them by hand to simulate: copy the package tree into two trial dirs, e.g. `eval/runs/t0/.../environment/eval-harvest/src/eval_harvest/` and the same under `.../t1/...`, so two `__init__.py` files share the module name — temporarily revert one tool's generated-output exclusion and confirm the failure returns — for mypy the `Duplicate module named "eval_harvest"` error and its early stop; for bandit and ruff, the generated copies get scanned again — then restore and confirm the full source-file count is back. The second check matters as much as the first: the mypy failure *stops* checking, so a fix that made the error disappear without restoring full coverage would look identical from the outside.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_runtime_deps_empty` | `[project].dependencies` parses to `[]` | A convenience dependency eroding NFR-5 and the FR-3 "list is empty" audit |
| `test_no_model_client_imported` | no `src/eval_harvest/` module imports a banned model client (via AST) | The "no LLM in the CLI" constraint eroding one import at a time (S-18) |
| `test_scan_detects_planted_import` | the scan goes red on a deliberately planted `import anthropic`, green when removed | A scan that cannot actually catch the thing it exists to catch (FR-37 discipline) |
| `test_generated_output_dirs_are_excluded_from_static_analysis` | every generated-output dir (`work`, `eval/runs`, `eval/reports`) appears in mypy's, ruff's, and bandit's excludes | A new generated-output producer added to `.gitignore` only, reproducing the mypy duplicate-module stall for a different directory |

## Acceptance Criteria

- [ ] `[project].dependencies` asserted empty by test
- [ ] AST import scan over `src/eval_harvest/` finds no model client and catches a planted one
- [ ] CI matrix runs `lint`/`typecheck`/`test` on Windows, macOS, and Linux (the workflow is the GitHub Actions config S-16 specifies; its legs trigger on push/PR once the repo is hosted on GitHub)
- [ ] Every generated-output tree (`work/`, `eval/runs/`, `eval/reports/`) is excluded in mypy's, ruff's, and bandit's config, each with a comment naming `.gitignore`, and `tests/test_static_analysis_excludes.py` pins all three
- [ ] `mise run check` stays green — and `mise run typecheck` reports the full source-file count, not "errors prevented further checking" — with two or more eval-harness trials present under `eval/runs/`
- [ ] `pytest-xdist` is in the `dev` group and `-n auto` is in `addopts`; `mise run check` is green in parallel with `-rs` still printing skip reasons
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Fixing any cross-platform failure the matrix surfaces — that belongs to the task that owns the failing code (typically A-2's wrapper).
- Scanning emitted task files for a model client — the emitted verifier *does* ship a judge (that is legitimate; ADR-5). This scan is over the CLI's own `src/` only.
- Where the eval harness's run output lives, or a retention policy for it — that is Workstream G's decision. This task only ensures the gate survives that output existing.
- Splitting the fat integration tests into finer parametrized cases or a shared sealed-repo fixture — a later performance refinement. The parallelism here (`-n auto`) is the win that pays for itself with zero test-shape churn; keep the git-backed seal/verify tests real (never mock git out — that would gut the guard).

## Notes & Gotchas

- **Scan with `ast`, not regex.** The emitted verifier template (D-3) may contain the string "anthropic"/"judge" inside a template file under `src/eval_harvest/verifier_tpl/` — that is data the CLI writes, not an import. Restrict the AST walk to `*.py` modules that are actually imported by the package, and treat template files as data. Record this boundary in a comment, or S-18 will false-positive on D-3's templates.
- **CI needs real git** for the fixture-repo tests but not `gh` auth (recorded payloads). git and gh ship preinstalled on all three GitHub-hosted runners (including windows-latest), so no install step is required — confirm rather than add one.
- **Gitignored is not excluded.** Three tools here carry their own exclude lists; adding a generated directory to `.gitignore` touches none of them. This is the transferable lesson and the reason for the reciprocal comments — a directory that is "not committed" is not thereby "not analysed".
- **"Errors prevented further checking" is the dangerous part.** When mypy hits the duplicate-module collision it stops, so the rest of the source goes unchecked behind a single unrelated error. Verify the file count after excluding, not just the exit code. It also takes *two* trials to reproduce (a duplicate needs a second copy), which is why a single-trial run hides it.
- **bandit does not honour `.gitignore`** (unlike ruff, which does today) — so it was the second tool genuinely walking `eval/runs/`. Naming the trees in all three configs makes coverage independent of each tool's gitignore behaviour.
- **Do not remove `-rs` when editing `addopts`.** The Harbor-dependent tests skip visibly, and a skip that stops printing reads as coverage. `-n auto` uses logical cores, so the CI speedup is smaller than a 12-core dev box — a slower CI number is not a regression.

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md)
**Blocks:** nothing
