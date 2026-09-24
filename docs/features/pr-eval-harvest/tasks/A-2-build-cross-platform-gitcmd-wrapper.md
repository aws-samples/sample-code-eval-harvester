---
id: A-2
title: "[Skeleton] Build the cross-platform gitcmd.py subprocess wrapper"
feature: pr-eval-harvest
workstream: Project skeleton & dev tooling
status: todo
complexity: M
implements: [NFR-4, NFR-2]
user_story: US-1
blocked_by: [A-1]
blocks: [B-1, B-2, C-1]
---

# A-2: [Skeleton] Build the cross-platform gitcmd.py subprocess wrapper

## Context

Every `gh`/`git` call in the package must go through one wrapper so the argv-only, prompt-disabled, token-stripped discipline is enforced in a single place and the naive POSIX-isms (`HOME=/nonexistent`, `os.defpath`) are made portable for Windows (NFR-4). Without this, each online verb would re-implement subprocess handling and the cross-platform + security guarantees would be promises rather than a chokepoint. This is the foundation all forge work (B-1, B-2) and convention surfacing (C-1) build on.

**North star:** the CLI runs the same on the customer's Windows or macOS machine and never lets untrusted repo content reach a shell.
**Implements:** NFR-4, NFR-2 (partial — the argv discipline)  ·  **User story:** US-1

## Design References

- **Tech plan:** `../tech-plan.md` §6.1 module table, `gitcmd.py` row — "one cross-platform wrapper for every `gh`/`git` call; argv lists never shell strings; prompts disabled"
- **Tech plan:** §3 Constraints — "untrusted repo content never reaches a shell … build `argv` lists, never shell strings, and disable prompts"
- **Tech plan:** §4 Risks, row "Cross-platform git internals" — `HOME=/nonexistent` and `os.defpath` are POSIX-isms to fix here

## What To Build

1. Create `src/eval_harvest/gitcmd.py` with a stateless `GitCommandRunner` class (class/static methods, per CODING_STANDARDS — no instance state) holding every entry point. Resolve both executables once at import into module-level `GIT_EXECUTABLE`/`GH_EXECUTABLE` so each call reuses one absolute path rather than re-searching the environment.
2. Resolve the two executables portably via `resolve_git`/`resolve_gh` classmethods that **take an injectable `which` callable** (default `shutil.which`), so the Windows-fallback and defpath-preference paths are unit-testable without the platform: `git` via `which("git", path=os.defpath)` **plus** the real `PATH` as a Windows fallback (on Windows `os.defpath` is `.;` and will not find git) — i.e. `which("git", path=os.defpath) or which("git") or "git"`; `gh` via `which("gh")` on the real `PATH`. Keep the "no venv shim wins" property for git (os.defpath first) but do not hard-code POSIX-only resolution.
3. Write `git(repo, *args) -> tuple[int, str]` (and a `git_with_stderr(repo, *args) -> tuple[int, str, str]` variant `git` delegates to and drops the stderr element) that runs `["git", "-C", str(repo), "--no-pager", *args]` — argv[0] a literal, the resolved absolute binary passed via `executable=GIT_EXECUTABLE` — with `capture_output=True, text=True, check=False`, and a child env that disables prompts. Factor that env into a pure `git_child_env(source_mapping) -> dict[str, str]` helper (data-in / data-out, so the flags are unit-testable without a subprocess). **Do not** pass `HOME=/nonexistent` unconditionally — on Windows git needs a valid `HOME`/`USERPROFILE`. Isolate git from the user's config *portably*: `GIT_CONFIG_NOSYSTEM=1` plus `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` pointed at `os.devnull` (isolated *and* valid on every OS), with `GIT_TERMINAL_PROMPT=0` and `GIT_OPTIONAL_LOCKS=0`; record the reasoning in a comment.
4. Write `gh(argv_tail, *, repo=None) -> tuple[int, str, str]`: builds the argv (appending `--repo <repo>` as two further fragments when given), and derives its child env through a pure `github_child_env(source_mapping) -> dict[str, str]` helper that strips `GITHUB_TOKEN`; returns `(returncode, stripped stdout, stripped stderr)` and never interpolates data into a shell.
5. Ensure `shell=False` everywhere and that no function accepts a pre-joined command string — the only entry points take argv fragments. Keep argv[0] a literal program name (the resolved binary runs via `executable=`) and add the "argv built here, never from a shell string" comment at each call so the security scan reads clean (subprocess safety is handled by a justified project-wide `B603` skip in `pyproject.toml [tool.bandit]`, plus `# nosec B404` on the `import subprocess` line — not an inline per-call suppression, which shifts line-to-line across bandit versions).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/gitcmd.py` | Create | The single subprocess chokepoint |
| `tests/test_gitcmd.py` | Create | Cases from the test table below |

## Schemas & Contracts

**API this wrapper exposes (the contract B-1/B-2/C-1 will call), all class/static methods on `GitCommandRunner`:**
```
GitCommandRunner.git(repo: Path, *args: str) -> tuple[int, str]              # (returncode, stripped stdout)
GitCommandRunner.git_with_stderr(repo: Path, *args: str) -> tuple[int, str, str]  # + stripped stderr
GitCommandRunner.gh(argv_tail: list[str], *, repo: str | None) -> tuple[int, str, str]  # GITHUB_TOKEN stripped, argv-only
GitCommandRunner.resolve_git(which) -> str    # injectable which; os.defpath first, real PATH fallback
GitCommandRunner.resolve_gh(which) -> str
GitCommandRunner.git_child_env(source) -> dict[str, str]     # pure: prompts/locks off, config isolated, home valid
GitCommandRunner.github_child_env(source) -> dict[str, str]  # pure: GITHUB_TOKEN stripped
```
Executable resolution and env are Windows-safe rather than POSIX-only.

**Migration:** none.
**Backward compatibility:** nothing exists to break — this is the first `git`/`gh` wrapper in the package.

## How To Verify

```bash
mise run test -- tests/test_gitcmd.py
mise run lint
mise run typecheck
```

Then by hand: on macOS run a real `git(clone, "rev-parse", "HEAD")` against any local clone and confirm it returns `(0, <sha>)` with no prompt and no PATH-shim surprises.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_git_returns_code_and_stdout` | `git()` returns `(returncode, stripped_stdout)` against a temp repo | A wrapper that swallows the exit code callers branch on |
| `test_no_shell_string_accepted` | the API takes argv fragments; passing a joined string is rejected/typed out | Command injection via a PR title or review body (§3 security) |
| `test_github_token_stripped_from_child` | `GITHUB_TOKEN` set in the parent env is absent from the `gh` child env | An invalid ambient token shadowing the working keyring credential |
| `test_prompts_disabled` | the git child env carries the no-prompt/no-locks flags | A hung invocation waiting on an interactive credential prompt |
| `test_git_resolution_has_windows_fallback` | git resolution falls back to real `PATH` when `os.defpath` misses (drive `resolve_git` with a fake `which`) | Windows CI red because `os.defpath` cannot find git (NFR-4) |
| `test_git_resolution_prefers_defpath` | when `os.defpath` finds git, it wins over a real-`PATH` hit | A venv shim on `PATH` shadowing the system git (the "no venv shim wins" property) |

## Acceptance Criteria

- [ ] All `git`/`gh` invocations are argv lists with `shell=False`; no API accepts a shell string
- [ ] `GITHUB_TOKEN` is stripped from the `gh` child env
- [ ] Prompts and optional locks are disabled on git calls
- [ ] Executable resolution works on Windows (real `PATH` fallback) as well as macOS/Linux
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- The CI matrix that proves cross-platform behaviour — that is A-3 (S-16).
- Any forge logic (PR list, reviews, comments) — B-1/B-2 build on this wrapper.

## Notes & Gotchas

- **`HOME=/nonexistent` is the trap.** It works on POSIX (isolates git from user config) but breaks git on Windows. This is the single most likely NFR-4 failure — solve it deliberately with a comment, and A-3's Windows CI will confirm it.
- **`os.defpath` on Windows** is `.;` — resolving git only against it will miss the install. Keep the `os.defpath` search (it preserves the "no venv shim wins" property for git) but add the real-`PATH` fallback.

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md)
**Blocks:** [B-1](B-1-build-survey-py-mechanical-pr-triage.md), [B-2](B-2-build-forge-py-iterations-comments-verdicts.md), [C-1](C-1-build-rubric-py-init-scaffold-convention-surfacing.md)
