# PRD: gitlab-forge — harvest GitLab merge requests

> **Status: shipped in PR #5, with review follow-ups.** This is a brownfield feature on top of
> [`pr-eval-harvest`](../pr-eval-harvest/prd.md). That PRD stays the authority for the product, the
> customer, and the datapoint; this one only says what changes so GitLab merge requests can be mined.
> Requirement ids (FR-*, NFR-*, US-*) and section numbers below refer to the pr-eval-harvest PRD.

---

## 1. North Star

> An SDE whose repositories live on GitLab gets the same Harbor review-eval datapoints from their
> merge-request history that a GitHub team gets from its pull requests, using the same verbs, with
> no separate pipeline to learn.

## 2. Problem Statement

The pr-eval-harvest PRD put non-GitHub forges out of scope (§10). The customer's repositories include
a self-managed GitLab, so with that scope the tool cannot mine their own history, which is the label
source the whole product depends on ("their own repository", §1).

GitLab records review history differently from GitHub, and each difference breaks a GitHub
assumption:

- An approval is a system note with a time and no commit SHA.
- A DiffNote's position is re-anchored onto later versions when its line survives a push, so it does
  not record the version the comment was written against.
- Force-pushed versions survive as `refs/keep-around/*`, which can be fetched by SHA but not by ref.
- Projects are nested in groups (`platform/payments/api`), but Harbor's ids are exactly `org/name`.

## 3. Customer

The pr-eval-harvest customer (§3), on GitLab. The setup they bring is a self-managed GitLab with
Microsoft SSO configured *inside* GitLab: people sign in through the identity provider, `glab`
authenticates to the API with a personal access token, and git uses an SSH remote. That setup needs no
browser session at run time, so a CLI driven by an agent can use it.

## 4. User Stories

**GL-1 — Survey and capture a GitLab project with the existing verbs.** `survey` and `capture`
work on a GitLab clone (gitlab.com or self-managed) with no new verb. The forge is detected from the
clone's remote, and `--forge auto|github|gitlab` overrides the detection.

*Acceptance:* a nested-group project keeps its full path as its identity. Merge requests are listed and
read through `glab`. Each verdict binds to the head that was current when it was given, and each
comment binds to the version it was written against. Force-pushed rounds GitLab still serves are
recovered, and the ones it does not are reported unrecoverable rather than invented (FR-8).

**GL-2 — Emit and build a dataset from GitLab candidates.** `emit` and `dataset` accept GitLab
candidates with no forge access (NFR-2).

*Acceptance:* the task clones from the merge request's own host, and nested group segments fold into
Harbor's `org/name` both in each task name and in the dataset name.

**GL-3 — Fail per merge request, loudly, never silently on the wrong forge.** Every forge-side
failure is a refusal of one merge request with its remedy.

*Acceptance:*
- The forge is detected from the same remote every fetch uses.
- A port on an HTTPS remote is kept.
- `--forge github` on a host other than github.com is refused rather than read from github.com.
- A `glab` response that is not JSON (a proxy's sign-in page) refuses that merge request, and the
  rest of a `capture --from-survey` batch still runs.

## 5. Success Criteria

- A GitLab project in a nested group goes through `survey → capture → emit → dataset` and produces a
  manifest Harbor accepts.
- The GitHub path is unchanged: every pre-existing local-fixture test passes. The only edits are the
  test doubles of `Forge.capture`, which mirror its signature and so gained the `remote` keyword.

## 6. Out of Scope

- Forges other than GitHub and GitLab.
- A GitLab host behind an SSO proxy that also intercepts API calls (the whole host behind a sign-in
  wall). `glab`'s `custom_headers` is the likely seam for a later change.
- GitHub Enterprise hosts. `--forge github` is github.com only, because `gh` is called without a
  host and the emitted clone URL is github.com's.
- Private-repository cloning inside the emitted task. The Dockerfile clones anonymously, which is the
  same limit a private GitHub repo has.
