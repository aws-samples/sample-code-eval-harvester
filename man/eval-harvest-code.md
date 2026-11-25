# eval-harvest-code(1)

## NAME

**eval-harvest code** — turn one merged pull request into a coding task that its own tests grade, in
stages.

## SYNOPSIS

```
eval-harvest code --pr <n> [--clone <dir>] [--dataset <dir>] [--profile <file>]
                  [--merge-commit <sha>] [--repo <owner/name>] [--json]
```

## DESCRIPTION

Offline: reads the local clone's commits and the dataset's `coding-profile.toml`, calls no forge, and
writes `tasks/<owner>__<name>__pr<n>-code/`. The pull request is found on `HEAD`'s first-parent
history by GitHub's own commit subjects (`Merge pull request #<n> ...`, or a squash ending in
`(#<n>)`); `--merge-commit` names it when neither matches. The task starts from the merge's first
parent, the commit the change was made on.

The change is split by path. The **test side** (the profile's `tests` globs; by default the folder and
file-name conventions `survey` looks for) is hidden from the agent and grades it. The rest, the
**source side**, is what the agent must write; it becomes `solution/gold.patch`, which only Harbor's
oracle agent applies.

| file | what it is |
|---|---|
| `environment/Dockerfile`, `environment/repo.tar.gz` | the base commit's tree (`git archive`, no history), installed, as one commit tagged `harvest-base` at `/work/repo`; `/var/lib/task_base_ref` names it |
| `tests/files/` | the after-version of every changed test-side file |
| `tests/test.sh`, `tests/grader.py`, `tests/spec.json` | the staged grader and what it runs |
| `solution/solve.sh`, `solution/gold.patch` | the reference change, for the oracle agent |
| `instruction.md` | the PR title, the hidden test files, and how they run |
| `task.toml` | the agent reaches only the profile's `allowed_hosts`; the grader no network |

## METHODOLOGY

A coding task asks an agent to make a merged pull request's change, from the repository at the merge's
parent commit, and grades it with the tests the pull request added or changed. The input is the local
clone and the dataset's **coding-profile.toml**; `code` calls no forge. The instruction is the PR title
and the hidden test files, **never the PR body**, which describes the fix.

Grading is staged, as the AgentCore + Harbor workshop runs it. `tests/test.sh --stage untrusted` runs
the tests as the agent user, on a copy of the repository (the staged grader refuses a trial whose
repository changed during that stage), and writes only the JUnit report. Then
`tests/test.sh --stage trusted` runs as another user, after every agent process has ended, and computes the reward from the
task's own files and that report. It reads nothing from the agent's tree, takes no reward, status or
exit code from the first stage, resets `PATH`, and runs a root-owned `python3` in isolated mode. So no
file the agent can write becomes the score. `test.sh` without `--stage` is Harbor's own verifier, in
one stage, writing `reward.txt`; it is kept for Harbor environments that do not stage.

The task is not finished yet: which hidden tests the change makes pass is found by running them. Run
Harbor's nop and oracle agents on the task, then `calibrate` it.

## PROFILE

`coding-profile.toml`, in the dataset directory unless `--profile` names another. Every key is under
`[coding]`; an unknown key is an error.

| key | required | meaning |
|---|---|---|
| `image` | yes | the base image (a Docker Hub image, or the ECR Public mirror of one) |
| `command` | yes | the test command, run from `workdir`; must contain `{files}` and `{junit}` (the JUnit report path) |
| `run` | yes | globs of the test files the command runs, among the changed test files |
| `display_command` | no | the command as the instruction shows it (default: `command` without `--junitxml={junit}`) |
| `install` | no | run at build in `/work/repo`, before the repository is committed |
| `tests` | no | globs of the test side (default: `**/test/**`, `**/tests/**`, `**/test_*`, `**/*.test.*`, ...) |
| `workdir` | no | where the command runs, relative to the repository root (default `.`) |
| `timeout_sec` | no | the test command's time limit (default 900) |
| `env` | no | a table of environment variables for the image and the test command |
| `agent_setup` | no | build commands for the agents' own tools (the agent's network reaches only its model) |
| `allowed_hosts` | no | what the agent may reach (default: the Bedrock endpoints of Codex and Claude Code in us-east-1) |

```toml
[coding]
image = "python:3.12-bookworm"
install = "pip install --no-cache-dir -e . pytest"
command = "python -m pytest -p no:cacheprovider -o junit_family=xunit1 --junitxml={junit} {files}"
display_command = "python -m pytest {files}"
run = ["tests/**/test_*.py"]
agent_setup = ["apt-get update && apt-get install -y nodejs npm && npm install -g @openai/codex@0.162.1"]
```

## EXIT STATUS

0 the task was written · 2 usage (a missing or invalid profile) · 3 refused: `pr-not-found`,
`no-base-commit`, `no-test-change`, `no-source-change`, `no-run-files`, `repository-unknown`,
`task-invalid`, or an answer-absence finding.

## SEE ALSO

eval-harvest-calibrate(1), eval-harvest-verify(1)
