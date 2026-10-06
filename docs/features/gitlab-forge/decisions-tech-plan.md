# Decisions — gitlab-forge (technical plan)

A running log of the decisions that shaped this feature's technical plan. One paragraph each: what
was decided, and why it was decided that way. Where a decision reverses an earlier one, the reversal
says so. Requirements-level decisions live in `decisions-prd.md`.

## 2026-10-06 — review follow-ups on PR #5

**Reverses 2026-10-05: the forge is read from the remote every fetch uses, not always `origin`.**
Detection read `remote.origin.url`, but survey fetched from `origin`, or from the first remote when
there was none. A GitLab clone whose only remote is `upstream` was therefore taken for GitHub
`unknown/unknown`. Survey then fetched `refs/pull/*` and blocked every merge request with
`no-pull-head-ref`, while GitLab capture fetched from a hard-coded `origin`. The remote is now chosen
once (`ForgeHost.default_remote_name`: `origin`, else the first) and carried as
`ForgeRemote.git_remote` into detection, survey's fetch and remedy, and every capture fetch (§2).

**An HTTP(S) remote keeps its port; an SSH remote does not.** The port was matched and dropped
everywhere. For `https://host:8443/…` that sent the clone URL, `pr_url`, and `glab --hostname` to
`:443`. The web port is part of the web host, so it is kept. An `ssh://` port belongs to the SSH
daemon, not to the web server, so it is still dropped, and so is an explicit default port.

**A non-JSON `glab` body is a `ForgeError`.** A proxy can answer with exit 0 and an HTML sign-in page.
`json.loads` then raised `JSONDecodeError`, which survey and batch capture do not contain, so one bad
response aborted a whole `capture --from-survey` run. The body is now parsed through one helper that
raises `ForgeError` quoting its first 200 characters, which turns the failure into a per-MR
`forge-unavailable` refusal.

**`--forge github` refuses a non-github.com host** (see `decisions-prd.md`). Refusing was chosen over
passing `--hostname` to `gh` because the clone URL and every `gh api` path would also need the host.
That is a feature, not a fix.

## 2026-10-05 — GitLab merge requests (issue #2)

**The forge comes from the clone's configured `origin` URL, read once.** (Reversed 2026-10-06: it is
now the remote the fetches use.) Host `github.com` is GitHub, a host naming `gitlab` is GitLab, and
`--forge` covers a self-managed host with any other name. The configured URL is read rather than
`git remote get-url`'s, because the latter applies `insteadOf` rewrites, which are transport aliases
and not the project's name. An unparseable remote still falls back to GitHub's last-two-segments slug
(local fixtures rely on it), but `--forge gitlab` on one is a usage error rather than a guessed
`gitlab.com` (ADR-1).

**GitLab is reshaped into GitHub's payloads, not given its own pipeline.** One implementation of
iteration ordering, binding, and recoverability stays authoritative; `gitlab.py` only reshapes.
GraphQL was chosen over REST because it pages in one call and a public project can be read without a
token. That is what let `tests/fixtures/glab/recorded/` hold verbatim gitlab.com responses (ADR-1).

**A verdict binds by time; a comment thread binds by its root.** GitLab records no SHA on an approval,
and it re-anchors a DiffNote's position forward when the line survives a push. This was observed on
the recorded !3978, where a comment written against `0b9a6767` now reports `a29c81f1`. Binding each
reply by its own time split threads apart, and binding by raw position put comments on versions they
predate. So the anchor is the root note, read against the head that was current when it was written,
and the line is mapped back through `git diff -U0` (ADR-1).

**`GITLAB_TOKEN` survives into `glab`'s environment, while `GITHUB_TOKEN` is stripped from `gh`'s.**
For `gh`, an ambient token silently overrides the operator's login. For a self-managed GitLab in CI it
is the intended credential, and `glab` has no other non-interactive path. Prompts, update checks, and
telemetry are disabled.

**Force-pushed rounds are fetched by SHA.** GitLab keeps every commit a note references, so capture
fetches the engaged commits the MR head does not reach into `refs/remotes/mr-versions/`. That is
outside `refs/remotes/pr/`, so survey's head count is unchanged. The fixture for the negative case must
prune the origin, not just unreference the commit, because protocol v2 serves any object the server
still holds.
