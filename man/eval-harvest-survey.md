# eval-harvest-survey(1)

## NAME

**eval-harvest survey** — list which pull requests are harvestable into review datapoints, and why
the rest are not.

## SYNOPSIS

```
eval-harvest survey --clone <dir> (--repo <org/name> | --from-json <file>) [--no-fetch] [--forge auto|github|gitlab] [--state ...] [--summary]
```

## DESCRIPTION

Reads `gh pr list` (one query per page) and `git log --first-parent`, and emits per-PR mechanical
facts — number, state, SHAs, changed paths, review rounds, blockers — never a quality label. Output
is byte-identical on fixed inputs.

On the online (`--repo`) path, `survey` first fetches every PR's head into `refs/remotes/pr/*` — a
plain clone has no `refs/pull/*/head`, and harvesting is impossible without them. Pass `--no-fetch`
to skip that fetch for a clone you already fetched or a mirror. `--from-json` re-renders a saved
payload and never touches the network. That fetch uses your environment's forge auth — for a private
repo it fails with `pull-head-fetch-failed` unless a non-interactive `git fetch` already works (see
`eval-harvest(1)`).

The forge is read from the clone's remote — `origin`, or its first remote when it has no `origin` —
and that same remote is the one every fetch uses. An HTTPS remote's non-default port is kept as part
of the host. `--forge github` is github.com only; a GitHub Enterprise host is refused (exit 2).

On GitLab (a clone whose remote host names `gitlab`, or any host with `--forge gitlab`), `survey`
lists merge requests with `glab api graphql` against that host — `--repo` is the full project path,
nested groups included — and fetches `refs/merge-requests/*/head` instead. Run
`glab auth login --hostname <host>` once first; a project the login cannot read is refused, not
reported empty. GitLab's `--state closed` excludes merged MRs, and `review_rounds` counts reviewers
who engaged rather than each submission.

## METHODOLOGY

A PR is **harvestable** only if it went through **review iteration**: at least one round where a
reviewer requested changes and the author pushed a fix. That iteration is what supplies the human
verdict a datapoint is graded against.

A repository whose PRs merge with no review iteration cannot be mined — there is nothing to grade
against — so `survey` refuses with `no-review-iteration` and the reviewed-vs-merged count that
justified it, rather than emitting an empty dataset.

A plain clone has no `refs/pull/*/head`, so `survey` fetches them into `refs/remotes/pr/*` before
enumerating; harvesting is impossible without it, and `--no-fetch` opts out. When every PR is
blocked, `survey` refuses `no-harvestable-pr` and prints a blocker histogram naming, for each
blocker, how many PRs it stops and the one command that unblocks the most — never an empty table.

## EXIT STATUS

`3` (refusal) with `no-review-iteration` when no PR went through review, `no-harvestable-pr` when
every PR is blocked, or `pull-head-fetch-failed` when the pull-head fetch could not run.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-capture(1)`.
