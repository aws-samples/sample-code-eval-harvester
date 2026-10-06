# Decisions — gitlab-forge (PRD)

A running log of the decisions that shaped this feature's requirements. One paragraph each: what was
decided, and why it was decided that way. Where a decision reverses an earlier one, the reversal says
so. The product-level decisions this feature builds on live in
[`../pr-eval-harvest/decisions-prd.md`](../pr-eval-harvest/decisions-prd.md).

## 2026-10-06 — review follow-ups on PR #5

**GitHub Enterprise stays out of scope, and is now refused rather than mis-read.** `--forge github`
used to accept any host. It recorded that host in `pr_url`, but cloned from and queried github.com.
Supporting GHE properly means passing a host to every `gh` call and to the clone URL. That is a scope
decision in its own right, so for now a non-github.com host is a usage error that names the limit.

**The design moved out of pr-eval-harvest into its own feature folder.** The GitLab work had been
written into the pr-eval-harvest PRD §10, tech plan ADR-7, and both decision logs. It now lives here,
so it is self-contained and can serve as a brownfield eval datapoint. The pr-eval-harvest PRD keeps
only a one-line pointer.

## 2026-10-05 — GitLab brought into scope (issue #2)

**GitLab merge requests are harvestable; other forges stay out.** The customer's repositories include
a self-managed GitLab with Microsoft SSO configured inside GitLab: people sign in through the identity
provider, `glab` authenticates to the API with a personal access token, and git uses an SSH remote.
That shape needs no browser session at run time, so it fits a CLI driven by an agent. A host whose SSO
proxy sits in front of the API as well (the whole host behind a sign-in wall) needs a session cookie
on every call and is not supported yet. `glab`'s `custom_headers` is the likely seam for a follow-up.
