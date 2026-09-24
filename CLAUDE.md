# eval-harvest

Mine a repository's pull-request history into a Harbor evaluation that ranks PR-review agents
on approve/deny accuracy and review thoroughness, without hand-labelling.

The design is written down: read `docs/features/pr-eval-harvest/` — the PRD, the tech plan, the
decisions log, and the task board — before inferring architecture from the code.

## Layout

- `docs/features/pr-eval-harvest/prd.md` — the requirements. Authoritative for scope.
- `docs/features/pr-eval-harvest/tech-plan.md` — the technical design; `tasks/` is the task board.
- `src/eval_harvest/` — the CLI package (zero runtime deps).
- `harvest_env/` — the packaged `harvest-env` distribution: the Lambda MicroVMs execution environment
  for Harbor, built as Workstream H (tech plan §13) and loaded into an unmodified Harbor by import
  path, so no Harbor fork is required. It is the project's own code (MIT-0). The `eval-harvest`
  package never imports it. No Harbor source or patch is stored in this repo: Harbor and the
  `microvms` bindings are plain pinned pip dependencies (the `eval` group in `pyproject.toml`, which
  is the source of truth for their versions), and the environment change was not upstreamed as a
  stored patch — a Harbor PR, if ever made, is a `git diff` generated then.
  Upstream Harbor is <https://github.com/harbor-framework/harbor>.

## Conventions worth keeping

Three rules, each of which this kind of project gets wrong at least once:

- **Read the source, probe the runtime, treat documentation as a hypothesis.** Eighteen claims
  from Harbor's docs did not survive contact with Harbor's code — the emitter (`harbor.py`) is
  written against Harbor's source, not its docs.
- **A guard nobody has watched fail is decorative.** Break the invariant deliberately, watch
  the check go red, revert. This applies to every validity check the PRD's US-7 describes.
- **A scanner that finds nothing must prove it scanned something.** Plant a control token and
  fail when the scan comes back without it. An empty result and a broken scan look identical
  otherwise.

## Coding standards

**All Python here follows [`CODING_STANDARDS.md`](CODING_STANDARDS.md). Read it before writing
code.** In short: classes as stateless namespaces (class/static methods, not objects), verbose
descriptive names, shallow functions (extract loop bodies into named functions), full type hints
with strict mypy, Pydantic for structured data, tests written first and watched fail, and code
shaped for a later Rust/PyO3 port. Lint and type-check frequently: `mise run lint`,
`mise run typecheck`, `mise run check`. Line length is 130. Python is pinned to **3.12**.

**Before every commit, `mise run check` must pass** (ruff lint + format-check, strict mypy,
tests). Do not commit red. Never invoke `python`, `pip`, `ruff`, `mypy`, or `pytest` directly —
always go through `uv` (`uv run …`, `uv tool run …`) or a `mise run <task>`, so the pinned
interpreter and configured tool versions are the ones that run.

## Working here

`pyproject.toml` holds the `eval-harvest` package manifest plus ruff/mypy config, and `mise.toml`
holds the lint/typecheck/test tasks — always go through `uv` or `mise run <task>` so the pinned
interpreter and tool versions are the ones that run.

Do not add a `docs/spec/`, requirement ids, or a conformance level. Those exist to make
independent implementations agree; there is one implementation and one customer.
