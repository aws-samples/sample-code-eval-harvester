# Technical Spec: gitlab-forge — harvest GitLab merge requests

**PRD:** [prd.md](./prd.md)
**Builds on:** [pr-eval-harvest tech plan](../pr-eval-harvest/tech-plan.md) — its layering (ADR-4), the
`GitCommandRunner` chokepoint, and the candidate schema (§8) are unchanged.
**Status:** Accepted (2026-10-05); review follow-ups 2026-10-06

---

## 1. Modules

| Module | Role |
|--------|------|
| `forge_host.py` | Parses the clone's remote into a `ForgeRemote` (kind, host, full project path, git remote name). It owns every forge-specific string: the pull-head refspec, the PR/MR web URL, the clone URL, and Harbor's folded `org/name`. Pure, apart from `from_clone` reading git config. |
| `gitlab.py` | Reads merge requests through `glab api graphql` and reshapes them into a `gh pr list` row and the `reviews` / `comments` / `timeline` lists that `Survey.survey_one` and `Forge.reconstruct_facts` already consume. |
| `cli.py` | `--forge auto\|github\|gitlab` on `survey` and `capture`. Both resolve the remote once and branch on `ForgeRemote.kind`. |
| `emit.py`, `dataset.py` | Recover the clone URL from the recorded `pr_url`, and fold nested groups through `ForgeHost.harbor_org_name` for both the task name and the dataset name. |

## 2. Remote resolution

The remote is `origin` if the clone has one, otherwise its first remote (`ForgeHost.default_remote_name`).
That one name is used for detection, for survey's bulk fetch and its printed remedy, and for every
fetch capture makes. It is carried as `ForgeRemote.git_remote`.

The configured URL (`remote.<name>.url`) is read rather than `git remote get-url`'s, because the
latter applies `insteadOf` rewrites, which are transport aliases and not the project's name.

Detection: `github.com` is GitHub, a host naming `gitlab` is GitLab, and anything else needs `--forge`.
An unparseable remote (a local fixture path) falls back to GitHub's last-two-segments slug. In two
cases an explicit `--forge` is refused instead of guessed:

- `--forge gitlab` on an unparseable remote.
- `--forge github` on a host other than github.com.

An HTTP(S) remote's non-default port stays part of the host (`gitlab.corp.example:8443`), so the clone
URL, the MR URL, and `glab --hostname` all keep it. An `ssh://` port is the SSH daemon's, so it is
dropped. An explicit default port (`:443`, `:80`) is dropped too.

## 3. ADR-1: GitLab is read through `glab api graphql` and reshaped into the payloads GitHub's path already consumes

(Recorded as ADR-7 in the pr-eval-harvest tech plan in PR #5; moved here so this feature stands on its own.)

**Status:** Accepted (2026-10-05)
**Requirements affected:** pr-eval-harvest FR-5–FR-8, FR-12, NFR-2, PRD §10

**Context.** `survey` and `capture` were written against `gh`'s JSON. GitLab records three things
differently:

- A verdict is a system note with a time and no SHA.
- A DiffNote's `position` is re-anchored onto later versions when its line survives a push, so it is
  not GitHub's `original_commit_id`.
- Force-pushed versions survive as `refs/keep-around/*`, which can be fetched by SHA but not by ref.

**Decision.** The forge is read once from the clone's remote (§2), and every forge-specific string is
a property of the parsed remote. `gitlab.py` reads GraphQL through the same `GitCommandRunner`
chokepoint and reshapes the results, so iteration ordering, comment binding, and recoverability stay
one implementation.

- **Head timeline:** each push note's `start_sha=`, then `diffHeadSha`.
- **Verdicts:** each binds to the head that was current at its time.
- **Comment threads:** a thread binds to its root note's position. The exception is a head that became
  current only after the root was written: then the thread binds to the head that was current at the
  root's time, and the line is mapped back through a local `git diff -U0`. Replies inherit their
  thread's anchor.
- **Unreached commits:** engaged commits the MR head does not reach are fetched by SHA, best effort,
  into `refs/remotes/mr-versions/<iid>/<sha>`. A commit the forge no longer serves stays unrecoverable
  (FR-8).
- **Emit:** `emit` recovers the clone URL from the recorded `pr_url`, so the offline layer still needs
  no clone and no forge (NFR-2).

**Alternatives considered.**
- *A forge-abstraction layer with a GitLab implementation of every survey/capture step.* Rejected: it
  would mean two copies of iteration ordering, comment binding, and recoverability, and they would
  drift apart.
- *GitLab REST.* Rejected: it takes one call per resource, and `/discussions` needs a token even on a
  public project, so the behaviour could not be recorded and pinned.
- *Taking a DiffNote's position at face value.* Rejected after the recorded gitlab-org/cli !3978
  showed a comment written against one version now positioned on the next.

**Consequences.**
- On GitLab, `survey`'s `review_rounds` is a lower bound: one per engaged reviewer or approver, because
  GitLab keeps each reviewer's current state, not a list of submissions.
- A squash merge with no merge commit exposes no integration SHA in GraphQL and is blocked
  `no-integration-commit`.
- GitLab's `--state closed` excludes merged MRs.
- The emitted Dockerfile clones anonymously, so a private GitLab project's task does not build without
  credentials, which is the same limit a private GitHub repo has.

## 4. Failure containment

Survey and batch capture contain only `ForgeError` (and `SurveyError`). Every GitLab transport failure
is therefore raised as a `ForgeError`, so it refuses one merge request rather than aborting a batch:

- a non-zero `glab` exit;
- GraphQL `errors`;
- `project: null` (missing or unreadable, refused with the `glab auth login --hostname` remedy);
- a body that is not a JSON object, such as a proxy's sign-in page returned with exit 0. The error
  quotes the first 200 characters of the body.

## 5. Testing

- `tests/integration/test_gitlab.py` replays the recorded gitlab.com responses in
  `tests/fixtures/glab/recorded/` against fixture repositories.
- Unit tests cover the parts that need no fixture repository:
  - remote parsing, ports, and the GitHub-host refusal (`tests/unit/test_forge_host.py`);
  - remote selection (`tests/unit/test_forge_host.py`, against a bare `git init`);
  - which remote capture fetches from, and non-JSON `glab` output (`tests/unit/test_gitlab_transport.py`);
  - the folded dataset name (`tests/unit/test_dataset_name.py`).
