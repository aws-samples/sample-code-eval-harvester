# eval-harvest-calibrate(1)

## NAME

**eval-harvest calibrate** — find a coding task's fail-to-pass tests from Harbor's nop and oracle runs
of it.

## SYNOPSIS

```
eval-harvest calibrate <task-dir> --nop <job-dir> --oracle <job-dir> [--json]
```

## DESCRIPTION

Reads files only. `<task-dir>` is a task `code` wrote; `--nop` and `--oracle` are Harbor job
directories that hold trials of that task (Harbor names a trial `<task>__<id>` and keeps the
verifier's files in its `verifier/` folder). Before calibration, the task's grader writes every test's
status to `verifier/results.json` and gives no reward, so those trials end without one; that is
expected.

```sh
harbor run -p tasks/acme__calc__pr7-code -e harbor_env.agentcore_env:AgentCoreEnvironment -a nop    -o jobs --job-name nop
harbor run -p tasks/acme__calc__pr7-code -e harbor_env.agentcore_env:AgentCoreEnvironment -a oracle -o jobs --job-name oracle
eval-harvest calibrate tasks/acme__calc__pr7-code --nop jobs/nop --oracle jobs/oracle
```

It writes `tests/expected.json` and lists the fail-to-pass tests in `instruction.md`, between two
`eval-harvest calibrate` markers, replacing an earlier list.

## METHODOLOGY

Which hidden tests the change makes pass is found by running them, not guessed. Run Harbor's **nop**
agent (the base) and its **oracle** agent (the gold patch) on the task: before calibration its grader
records every test's status and gives no reward. `calibrate` reads those trials. A test is
**fail-to-pass** if it passed in no nop run and in every oracle run, and pass-to-pass if it passed in
every run of both. A test whose result changed between runs of one agent is left out as **flaky**: a
flaky required test would make the task unsolvable at random, so run each agent more than once when
the suite is slow or networked. It writes **expected.json** and lists the fail-to-pass tests in the
instruction. Then run nop and oracle again: **nop must score 0** and oracle 1 before any agent is
benchmarked on the task.

## EXIT STATUS

0 calibrated · 3 refused: `not-a-coding-task`, `no-calibration-runs` (no nop or no oracle trial with
`verifier/results.json`), `no-fail-to-pass` (no hidden test tells the change apart).

## SEE ALSO

eval-harvest-code(1)
