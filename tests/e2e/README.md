# End-to-end tests

A black-box end-to-end suite for the `eval-harvest` CLI. Every test invokes the installed
`eval-harvest` console script as a subprocess and asserts only on its observable contract — exit
codes, stdout/stderr, the four-field refusal shape, and the files each verb writes. Nothing here
imports `eval_harvest`; the suite exercises the tool exactly as a user drives it.

## What it covers

`e2e/test_contract_flow.py` walks the whole pipeline against a real public repository:
`init` → `survey` → `capture` → fill the candidate via `annotate` → `emit --kind reject` /
`emit --kind approve` → `verify` → `dataset`. It asserts the *shape* of each output — JSON keys and
types, the emitted Harbor task directory's layout, `task.toml`'s required sections and metadata —
never specific values, so it is stable against the upstream repository's data changing.

`e2e/test_refusals.py` covers the failure contracts: usage errors (exit 2), the empty-dataset
refusal (exit 3), the approve-with-high-severity coherence refusal (exit 3), and leak detection —
a task whose instruction leaks the change's origin fails `verify` (exit 4) with the four-field
refusal shape.

The candidate is filled mechanically with schema-valid placeholder judgments (each finding pinned
to a real comment location so `verify`'s finding-line check passes); no model is involved.

## Running

Invoke pytest directly against this directory:

```
uv run pytest e2e/ -n0
```

`-n0` runs in-process (the default suite fans across cores with `xdist`; the expensive
network-backed session fixtures here should run once, not once per worker).

The network-backed tests reach GitHub with a `git clone`/`gh` over the network, with `GITHUB_TOKEN`
stripped so `gh` uses its keyring credential (the target repository is public, so reachability —
not authentication — is what matters). When `github.com` is unreachable, those tests **skip with a
reason** rather than fail; the offline usage/refusal tests still run. This suite lives only under
`e2e/` and is not wired into any project task — it is not part of the default test run
(`testpaths = ["tests"]`) and is invoked directly as above.

## Not covered

- The `no-review-iteration` and `no-harvestable-pr` survey refusals — the target repository reviews
  its PRs, so these need a repository (or recorded payload) that does not.
- A squash-merged PR as a distinct case.
