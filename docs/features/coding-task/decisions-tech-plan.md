# Decisions — coding-task (tech plan)

A running log of the technical decisions behind this feature, newest first. Where a decision reverses
an earlier one, it says so.

## 2026-11-25 — first design

**Shared verifier mode, for this kind only (reverses FR-32 for coding tasks).** The review kind's
validator refuses `shared` because a shared verifier is agent-scoreable. A coding task must be graded
on the agent's tree, which a separate verifier does not have, so it uses the workshop's staged grader
in the shared container instead (tech plan ADR-1). `validate_coding_task_config` is a separate entry
point, so the review kind's rule cannot be loosened by accident.

**The contract is the workshop's, read from its code.** The stages, `OUT` and `IN`, `/work/repo`,
`/var/lib/task_base_ref`, and "only the trusted stage's `reward.json` counts" were taken from the
environment's `_staged_grade`. They were checked unchanged between two revisions of it, one of which
adds bytecode removal, a nested-`.git` refusal and a keepalive on the environment's side. Nothing
here depends on those additions.

**The untrusted stage runs on a copy of the repository.** The environment compares the agent's patch
before and after that stage over every file in `/work/repo`, ignored files included. A test run
writes caches and build output, and the grader restores test-side files first, so in place every trial
would be ungraded. Cost: one `cp -a` of the repository per trial.

**The trusted stage picks its own interpreter.** `as-grader` starts it with the image's `PATH`, which
can hold a folder the agent can write. So the stage resets `PATH` and runs only a root-owned
`python3` that no one else can write, outside the agent's folders, with `-I -S`. The two checks guard
each other: removing either one alone still blocks a hostile `python3`. The test breaks both at once
to watch it fail.

**Duplicates fail closed.** The grader names a repeated test id `id #2`. Taking the first copy let a
forged passing copy placed before the real failing one pass the test. A first version of the test put
the forged copy after the real one, where "first copy wins" also gave 0, so that test stayed green
with the guard removed. It now puts it first.

**The snapshot is `git archive` of the base, as one fresh commit in the image.** No history means no
way to read the change from git. The archive is gzip with mtime 0, and the commit has fixed dates, so
the same pull request gives the same bytes.

**The grader is new code in this repository.** `coding_tpl/grader.py` is standard-library Python for
any `python3` from 3.7, because it runs on the target repository's image, not this package's
interpreter. It is a port, in this repository's style, of a grader the same author wrote and
released under MIT-0, the license of this repository. No third-party code or data is included.

**Calibration reads Harbor's trial folders.** Harbor names a trial `<task>__<id>` and downloads the
verifier's files into its `verifier/` folder, so `calibrate` matches trials to the task by that prefix
and reads `verifier/results.json`. It does not parse Harbor's own result files, whose shape is
Harbor's.
