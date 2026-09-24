# eval-harvest-init(1)

## NAME

**eval-harvest init** — scaffold the dataset directory and surface the repo's own convention files.

## SYNOPSIS

```
eval-harvest init <clone-path> [--dataset <dir>]
```

## DESCRIPTION

Reads a local clone's convention files (`CONTRIBUTING*`, `.github/`, `CODEOWNERS`, common
style-guide files) and writes the dataset skeleton (`candidates/`, `tasks/`), `rubric.template.md`,
and `risk-map.template.toml`. Leaves the agent everything it needs to author the two versioned
artefacts the later verbs read.

## METHODOLOGY

**Step zero is a local clone.** Before any verb, run `git clone <repo>` yourself and pass that
clone's path. The verbs read the **local clone** and never fetch it for you.

**Set up forge authentication first.** The CLI never handles credentials; it uses your environment's
existing auth. For a **private** repo, make a non-interactive `git fetch` work before harvesting —
`gh auth setup-git`, an SSH remote, or a credential helper — since `survey` and `capture` fetch
`refs/pull/*`. See `eval-harvest(1)`.

**Build the rubric and risk map before any datapoint.** `init` lists the repo's convention files it
found and writes the templates; you author `rubric.md` by the **overlay** method — overlay your
repo's stated conventions on a standard review base — so the rubric is your team's, written down,
inspectable, and versioned with the dataset. The risk map (`risk-map.toml`) records which
directories are high-risk, likewise versioned.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-survey(1)`.
