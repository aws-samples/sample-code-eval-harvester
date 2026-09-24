---
id: C-1
title: "[Artefacts] Build rubric.py: init scaffold + convention surfacing"
feature: pr-eval-harvest
workstream: Artefacts (rubric & risk map)
status: todo
complexity: M
implements: [FR-24, FR-25, FR-26, FR-27]
user_story: US-5
blocked_by: [A-2]
blocks: [D-3]
---

# C-1: [Artefacts] Build rubric.py: init scaffold + convention surfacing

## Context

An eval that judges review quality against generic conventions penalises the agent for missing things the customer's team does not care about. So the agent must build a rubric first — overlaying the repo's own conventions on a standard base — and the CLI has to hand it the repo's stated conventions to overlay rather than make it guess. This task builds the `init` verb (scaffold the dataset skeleton + templates, surface convention files) and the rubric artefact plumbing (version string, referenced by each datapoint). No LLM: the CLI surfaces files and instructions; the agent authors the rubric.

**North star:** the customer gets a signal they can tune against, judged against how *their* team reviews.
**Implements:** FR-24, FR-25, FR-26, FR-27  ·  **User story:** US-5

## Design References

- **Tech plan:** `../tech-plan.md` §9, the `eval-harvest init` contract — reads `CONTRIBUTING*`, `.github/`, `CODEOWNERS`, style files; writes the skeleton + `rubric.template.md` + `risk-map.template.toml`; stdout is the found convention files + the authoring instruction
- **Tech plan:** §5.3 step 1 and §7.1 step 2 — where `init` and rubric authoring land in the flow
- **Tech plan:** §8 "Dataset-root artefacts" — `rubric.md` + a version string; a datapoint records `rubric_version`
- **PRD:** `../prd.md` §6, US-5 acceptance criteria — the overlay method and the versioned, reusable rubric

## What To Build

1. Create `src/eval_harvest/rubric.py`.
2. Register the `init` subcommand in `cli.py`'s dispatcher (the CLI scaffold and dispatcher land in a parallel task, A-1): `eval-harvest init <clone-path> [--dataset <dir>]`, with `--dataset` defaulting to the current directory. It reads the clone (no forge call — online layer, but local reads only).
3. Scan the clone for stated-convention files and report their paths: `CONTRIBUTING*` (any case/extension), everything under `.github/`, `CODEOWNERS` (root, `.github/`, `docs/`), and common style-guide files (`.editorconfig`, `STYLE*`, `.ruff.toml`, `.eslintrc*`, `.prettierrc*`, `.pylintrc`, `.flake8`, and `setup.cfg`/`pyproject.toml` surfaced whole for their `[tool.*]`/`[flake8]` config, not parsed). Top-level names match case-insensitively. Surface paths — do not parse or judge them (FR-25).
4. Write the dataset skeleton: `candidates/`, `tasks/`, `patches/`, `rubric.template.md`, `risk-map.template.toml`. The rubric template describes the overlay method (standard base + repo conventions) as prose the agent fills in (FR-24).
5. Print the found convention files and the methodology instruction: "author `rubric.md` and `risk-map.toml` by overlaying these on the standard base."
6. Add the rubric version plumbing: a helper to read `rubric.md`'s declared version (a `version:` line or frontmatter) so `emit` (D-3) can stamp `rubric_version` into `task.toml` (FR-27) and multiple datapoints can reference the same version (FR-26).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/rubric.py` | Create | `init` verb + convention scan + rubric version helper |
| `src/eval_harvest/cli.py` | Modify | Register the `init` subcommand |
| `tests/test_rubric.py` | Create | Cases from the test table |
| `tests/fixtures/clone_with_conventions/` | Create | A fixture clone carrying CONTRIBUTING/.github/CODEOWNERS |

## Schemas & Contracts

**Dataset skeleton `init` writes:**
```
<dataset>/
  candidates/          (empty)
  tasks/               (empty)
  patches/             (empty — capture materializes diffs here)
  rubric.template.md
  risk-map.template.toml
```
**Rubric artefact (`rubric.md`, agent-authored later):** carries a version string the datapoint references (`rubric_version`, FR-27).

**`rubric.py` public surface** (a stateless `Rubric` namespace of class/static methods, per CODING_STANDARDS; module constants `RUBRIC_FILENAME = "rubric.md"`, `RUBRIC_TEMPLATE_FILENAME = "rubric.template.md"`):
- `Rubric.discover_convention_files(clone: Path) -> list[Path]` — the convention files as clone-relative paths, sorted and de-duplicated (deterministic, FR-25)
- `Rubric.write_skeleton(dataset: Path) -> None` — creates the skeleton dirs and writes both templates; idempotent (`exist_ok=True`)
- `Rubric.read_version(rubric_path: Path) -> str` / `Rubric.declared_version(rubric_text: str) -> str` — the declared version, `""` when the file is absent or declares none
- `Rubric.overlay_instruction() -> str` — the one-line methodology hand-off `init` prints after the found files

**Migration:** none.
**Backward compatibility:** none — new verb.

## How To Verify

```bash
mise run test -- tests/test_rubric.py
mise run lint
mise run typecheck
```

Then by hand: `eval-harvest init tests/fixtures/clone_with_conventions --dataset /tmp/ds` — confirm stdout lists the CONTRIBUTING/.github/CODEOWNERS paths and the skeleton + templates appear under `/tmp/ds`.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_init_writes_skeleton_and_templates` | `candidates/`, `tasks/`, `patches/`, `rubric.template.md`, `risk-map.template.toml` created | A later verb failing because its input directory does not exist |
| `test_init_surfaces_convention_files` | CONTRIBUTING, `.github/*`, CODEOWNERS paths appear in stdout | The agent guessing conventions instead of overlaying the repo's own (FR-25) |
| `test_init_help_carries_overlay_method` | `init --help` describes building a rubric first + the overlay method | US-5's methodology missing from the tool (FR-24) |
| `test_rubric_version_read_back` | the version helper reads `rubric.md`'s declared version | A datapoint stamped with the wrong/blank `rubric_version` (FR-27) |
| `test_init_makes_no_forge_call` | `init` reads the clone but issues no `gh`/network call | `init` reaching the network when it only needs local files |

## Acceptance Criteria

- [ ] `init` writes the dataset skeleton + both templates
- [ ] `init` lists the repo's stated-convention files it found (FR-25)
- [ ] `init --help` instructs the agent to build a rubric first and describes the overlay method (FR-24)
- [ ] The rubric version is readable back for `emit` to stamp (FR-26, FR-27)
- [ ] `init` makes no forge/network call (reads the local clone only)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Authoring the rubric content — that is the agent's judgment step, never the CLI (no LLM; §3).
- Parsing `risk-map.toml` or computing structural risk — that is `riskmap.py` (C-2). `init` only writes the *template*.
- Stamping `rubric_version` into `task.toml` — that is `emit` (D-3), which calls this module's version helper.

## Notes & Gotchas

- **Surface, do not judge (FR-25).** `init` lists convention file *paths*; it does not summarise, rank, or parse them. Parsing is the agent's job.
- **No LLM (§3, FR-3).** This module contains no model call. It is file discovery + templates + a version reader.
- **Anchor the version match at line start.** The `version:` reader matches a frontmatter key or a plain line, case-insensitively, anchored so a lookalike key (`rubric_version:`) or a prose mention is not mistaken for the rubric's own version (FR-27).
- **`init` is "online layer" but local-only:** per §6.2 it reads the clone with plain filesystem/`git` access and makes no forge call — keep the network-free property so the offline guarantee (NFR-2) is not muddied here.

## Dependencies

**Blocked by:** [A-2](A-2-build-cross-platform-gitcmd-wrapper.md)
**Blocks:** [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md)

