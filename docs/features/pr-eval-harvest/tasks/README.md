# Tasks: pr-eval-harvest

**Tech plan:** [../tech-plan.md](../tech-plan.md)  ·  **PRD:** [../prd.md](../prd.md)
**North star:** an SDE with PRs piling up points a coding agent at this CLI and gets Harbor
review-eval datapoints built from their own repository's PR review history — fast enough to have
them today, without hand-labelling a PR or learning eval methodology.

## Status

| ID | Title | Status | Blocked By | Complexity |
|----|-------|--------|-----------|-----------|
| [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md) | [Skeleton] Scaffold the uv package, CLI dispatch, and refusal/exit-code helper | done | — | S |
| [A-2](A-2-build-cross-platform-gitcmd-wrapper.md) | [Skeleton] Build the cross-platform gitcmd.py subprocess wrapper | done | A-1 | M |
| [A-3](A-3-ci-matrix-and-dependency-audit-test.md) | [Skeleton] CI matrix + dependency-audit/import-scan + static-analysis excludes | done | A-1 | S |
| [B-1](B-1-build-survey-py-mechanical-pr-triage.md) | [Forge] Build survey.py — mechanical PR triage — with a golden-file test | done | A-2 | M |
| [B-2](B-2-build-forge-py-iterations-comments-verdicts.md) | [Forge] Build forge.py: iterations, inline comments, verdicts, timing | done | A-2 | L |
| [B-3](B-3-build-candidate-py-and-wire-capture-verb.md) | [Forge] Build candidate.py + wire the capture verb | done | B-2, C-2 | M |
| [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md) | [Forge] Extract diffspan.py + bind each comment to the iterations whose diff covers it | done | B-2, B-3 | M |
| [B-5](B-5-add-the-show-verb.md) | [Forge] Add the `show` verb: print the diff hunk around one comment | done | B-4 | M |
| [B-6](B-6-add-the-brief-verb.md) | [Forge] Add the `brief` verb: one self-contained per-PR brief | done | B-4, B-5 | M |
| [B-7](B-7-add-the-annotate-verb.md) | [Forge] Add the `annotate` verb: append-only per-comment fill | done | B-3, B-4, F-2 | M |
| [B-8](B-8-add-batch-mode-for-capture-and-emit.md) | [Forge] Batch mode for `capture` and `emit` | done | B-3, D-3 | M |
| [C-1](C-1-build-rubric-py-init-scaffold-convention-surfacing.md) | [Artefacts] Build rubric.py: init scaffold + convention surfacing | done | A-2 | M |
| [C-2](C-2-build-riskmap-py-parse-risk-map-compute-structural-risk.md) | [Artefacts] Build riskmap.py: parse risk-map.toml, compute structural risk | done | A-1, D-1 | S |
| [D-1](D-1-build-tomlw-py-deterministic-toml-writer.md) | [Emission] Build tomlw.py: deterministic stdlib TOML writer | done | A-1 | M |
| [D-2](D-2-port-harbor-py-constants-and-validators.md) | [Emission] Build harbor.py: constants + task-config/layout validators | done | D-1 | L |
| [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md) | [Emission] Build emit.py + verifier_tpl/: assemble the task directory | done | D-2, B-3, C-1, C-2 | L |
| [E-1](E-1-build-seal-py-git-channel-checklist-and-content-scanner.md) | [Verify] Build seal.py: git-channel checklist + new content scanner | done | A-1 | L |
| [E-2](E-2-build-verify-py-structural-checks-absence-scan-control-token.md) | [Verify] Build verify.py: structural checks + absence scan + control token | done | E-1, D-3 | L |
| [E-3](E-3-guard-verification-suite.md) | [Verify] Guard-verification suite: break every US-7 invariant, watch it go red | done | E-2 | M |
| [E-4](E-4-wire-emit-verify-refuse-before-write-and-nfr2-test.md) | [Verify] Wire emit→verify refuse-before-write + NFR-2 offline test | done | E-2 | S |
| [F-1](F-1-build-dataset-py-manifest-and-distribution-report.md) | [Dataset] Build dataset.py: manifest + risk/severity distribution report | done | D-2, D-3 | M |
| [F-2](F-2-man-pages-and-help-methodology-and-parity-test.md) | [Docs] man/ + --help methodology for every verb + human-parity test | done | A-1 | M |
| [G-1](G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md) | [Eval] Eval harness: Harbor task + MicroVMs runner + trajectory capture | done | F-2, D-3, E-4, H-3 | L |
| [G-2](G-2-datapoint-quality-llm-judge-and-alignment.md) | [Eval] Datapoint-quality LLM judge + alignment to human labels | done | G-1 | L |
| [G-3](G-3-tool-use-trajectory-metrics-and-eval-report.md) | [Eval] Tool-use + trajectory metrics, eval report, and the §8 pass bar | done | G-1, G-2 | L |
| [H-1](H-1-establish-microvms-bindings-dependency.md) | [Env] Establish the microvms bindings dependency and its typed surface | done | — | S |
| [H-2](H-2-build-lambda-microvms-environment.md) | [Env] Build the LambdaMicrovmsEnvironment on microvms-agentd | done | H-1 | L |
| [H-3](H-3-load-into-unmodified-harbor-by-import-path.md) | [Env] Load the environment into an unmodified Harbor by import path (no fork) | done | H-2 | S |
| [H-4](H-4-environment-unit-test-suite.md) | [Env] Environment unit-test suite (microvms bindings stubbed) | done | H-2 | L |
| [I-1](I-1-provision-aws-execution-infrastructure.md) | [Infra] Provision AWS execution infrastructure for the live eval run | done | — | M |

## Dependency Graph

```
A-1 ─┬─► A-2 ─┬─► B-1
     │        └─► B-2 ─► B-3 ─► B-4 ─┬─► B-5 ─► B-6
     │                               ├─► B-7        [+ F-2]
     │                               └─► B-8        [+ D-3]
     ├─► C-1
     ├─► D-1 ─┬─► C-2                              (C-2 also needs A-1)
     │        └─► D-2 ─► D-3                       (D-3 also needs B-3, C-1, C-2)
     ├─► E-1 ─► E-2 ─┬─► E-3                       (E-2 also needs D-3)
     │               └─► E-4
     └─► F-2

D-2, D-3 ─► F-1

H-1 ─► H-2 ─┬─► H-3 ─────────────┐
            └─► H-4               ├─► G-1 ─► G-2 ─► G-3   (G-1 also needs F-2, D-3, E-4)
I-1 (independent) ────────────────┘  (I-1 provisions the AWS infra G-1's live run uses)
```

## Ready To Start

All tasks are **done** — the board is fully burned down. Nothing remains to start.

## Parallel Workstreams

- **Fully parallel:** once **A-1** lands, the CLI branches fan out independently — the forge chain
  (B), artefacts (C), emission (D), verification (E), and docs (F-2). The execution substrate (H)
  and AWS infra (I) share no code with the `eval-harvest` package and can proceed from the start;
  H-1 and I-1 have no blockers at all.
- **Integration points:**
  - **D-3 (`emit`)** converges B-3, C-1, C-2, and D-2 — the first place the mining, artefact, and
    Harbor-serialization work meet.
  - **E-2 (`verify`)** converges E-1 (seal) and D-3 (a built task to check).
  - **G-1 (eval harness)** converges the finished CLI (F-2, D-3, E-4) with the execution
    environment (H-3); it is the end-to-end core-bet measurement and the last thing to build.

## Requirement Coverage

Every requirement in the tech plan maps to at least one task.

| Requirement | Tasks |
|------------|-------|
| TP-1 | A-1 |
| TP-2 | D-1 |
| TP-3 | B-2 |
| TP-4 | D-2 |
| TP-5 | E-1 |
| NFR-1 | D-1, D-3 |
| NFR-2 | A-2, E-4 |
| NFR-3 | A-3 |
| NFR-4 | A-2, A-3 |
| NFR-5 | A-1, A-3, D-1 |
| NFR-6 | E-1, E-2, E-3 |
| NFR-7 | B-2 |
| NFR-8 | D-3 |
| FR-1 | B-5, B-6, B-7, F-2 |
| FR-2 | A-1, B-1, B-4, B-8, E-2 |
| FR-3 | A-3 |
| FR-4 | A-1, B-5, B-6, F-2 |
| FR-5 | B-1 |
| FR-6 | B-2, B-4, B-8 |
| FR-7 | B-2 |
| FR-8 | B-1, B-2 |
| FR-9 | B-3, B-4, B-5, B-6, B-7 |
| FR-10 | B-1, B-2, B-3, B-7, B-8 |
| FR-11 | B-1, B-8 |
| FR-12 | B-2, B-3, B-4, B-5 |
| FR-13 | B-3, B-7, F-2 |
| FR-14 | D-3 |
| FR-15 | B-3, B-7, E-2, F-2 |
| FR-16 | B-3, D-3, F-2 |
| FR-17 | D-3 |
| FR-18 | D-3 |
| FR-19 | B-3, D-3 |
| FR-20 | B-3, C-2 |
| FR-21 | D-3 |
| FR-22 | C-2 |
| FR-23 | F-1 |
| FR-24 | C-1 |
| FR-25 | C-1 |
| FR-26 | C-1 |
| FR-27 | C-1, D-3 |
| FR-28 | D-2, D-3 |
| FR-29 | D-3, F-1 |
| FR-30 | D-3 |
| FR-31 | D-3 |
| FR-32 | D-2, D-3 |
| FR-33 | D-3 |
| FR-34 | B-4, E-2 |
| FR-35 | E-1, E-2 |
| FR-36 | E-1, E-2 |
| FR-37 | E-3 |
| FR-38 | E-4 |
| FR-39 | E-2 |
| FR-40 | D-3, E-1 |

Workstreams **G** (core-bet eval), **H** (Lambda MicroVMs execution environment), and **I** (AWS
infrastructure) trace to their §13 tasks and the §8 success criteria rather than to FR/NFR ids —
they build and run the measurement, not the CLI's own requirements.

## Conventions

- Update a task's `status` in its frontmatter and in the table above when you pick it up or finish it.
- A task file is the source of truth for its own scope; this index is the source of truth for the
  dependency graph and ordering.
</content>
