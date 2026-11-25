"""Turn one merged pull request into a coding task: the agent makes the change, the PR's own tests grade it.

The review kind (``emit``) asks an agent to judge a change. This kind asks it to *make* one, from the
repository at the change's base commit, and grades it with the tests the pull request added or changed
(coding-task tech plan §2). ``code`` is offline: it reads the local clone and the dataset's
``coding-profile.toml`` and calls no forge (NFR-2). It writes:

- ``environment/``: a Dockerfile and ``repo.tar.gz``, the base commit's tree from ``git archive`` (no
  history, no file of the change), installed and committed in the image as one commit;
- ``tests/``: the after-versions of the changed test files (hidden until grading), ``spec.json``, and
  the staged grader (``test.sh --stage untrusted|trusted``, ``grader.py``);
- ``solution/``: the source side of the change as ``gold.patch``, for Harbor's oracle agent;
- ``instruction.md``: the pull request's title, the hidden test files and how they run. Never the PR
  body, which describes the fix.

Which tests must pass is not known yet: ``calibrate`` reads Harbor's nop and oracle runs of the task
and writes ``tests/expected.json``. Until then the grader records statuses and gives no reward.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eval_harvest.codeprofile import CodingProfile
from eval_harvest.forge_host import ForgeHost, ForgeHostError
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.harbor import CompiledTask, EmittedFile, Harbor
from eval_harvest.seal import Seal
from eval_harvest.tomlw import TomlFloat, TomlValue, TomlWriter

_TEMPLATE_DIR: Final = Path(__file__).parent / "coding_tpl"
_CONTROL_TOKEN: Final = "EVAL-HARVEST-SCAN-CONTROL-TOKEN"
_CONTROL_FILENAME: Final = ".eval-harvest-scan-control"
#: The place in instruction.md where ``calibrate`` lists the fail-to-pass tests (replaced on every calibration).
CALIBRATION_BEGIN: Final = "<!-- eval-harvest calibrate: begin -->"
CALIBRATION_END: Final = "<!-- eval-harvest calibrate: end -->"
_GIT_PATH_CHUNK: Final = 200
_AGENT_TIMEOUT_SEC: Final = 3600.0


@dataclass(frozen=True, slots=True)
class CodingRefusal:
    """Why no coding task was written, in the FR-2 shape (the CLI adds the datapoint). Plain data."""

    check: str
    offending: str
    next_: str


@dataclass(frozen=True, slots=True)
class ChangeSplit:
    """A change's paths, split into what the agent writes and what grades it. Plain data."""

    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    hidden_test_files: tuple[str, ...]
    deleted_test_files: tuple[str, ...]
    run_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PullRequestChange:
    """One merged pull request, read from the clone: its commits, title and dates. Plain data."""

    repository: str
    pr_number: int
    merge_commit: str
    base_commit: str
    title: str
    base_commit_date: str
    merged_at: str


@dataclass(frozen=True, slots=True)
class CodingTaskResult:
    """A written coding task. Plain data."""

    task_dir: Path
    change: PullRequestChange
    split: ChangeSplit


class CodingTaskHarvester:
    """Builds a coding task directory from a local clone and a profile. Holds no state."""

    @classmethod
    def harvest_coding_task(  # noqa: PLR0913 — the verb's contract: the clone, the PR, the profile, where to write, two overrides
        cls,
        clone: Path,
        pr_number: int,
        profile: CodingProfile,
        dataset: Path,
        *,
        merge_commit: str | None = None,
        repository: str | None = None,
    ) -> CodingTaskResult | CodingRefusal:
        """Write ``tasks/<owner>__<name>__pr<n>-code/`` or say why not."""
        change = cls.read_pull_request_change(clone, pr_number, merge_commit=merge_commit, repository=repository)
        if isinstance(change, CodingRefusal):
            return change
        split = cls.split_change(cls.changed_paths(clone, change.base_commit, change.merge_commit), profile)
        if isinstance(split, CodingRefusal):
            return split
        written = cls.write_task(dataset, cls.task_dir_name(change), cls.task_files(clone, change, split, profile))
        if isinstance(written, CodingRefusal):
            return written
        return CodingTaskResult(task_dir=written, change=change, split=split)

    # ─────────────────────────────── the pull request ───────────────────────────────

    @classmethod
    def read_pull_request_change(
        cls, clone: Path, pr_number: int, *, merge_commit: str | None, repository: str | None
    ) -> PullRequestChange | CodingRefusal:
        """The merge (or squash) commit of the PR, its first parent, its title and dates."""
        name = repository or cls.repository_name(clone)
        if name is None:
            return CodingRefusal(
                "repository-unknown", str(clone), "pass --repo <owner/name>: the clone has no forge remote to read it from"
            )
        commit = merge_commit or cls.find_integration_commit(clone, pr_number)
        if commit is None:
            return CodingRefusal(
                "pr-not-found",
                f"PR {pr_number}",
                "no first-parent commit of HEAD is 'Merge pull request #N' or ends in '(#N)'; pass --merge-commit <sha>",
            )
        code, base = GitCommandRunner.git(clone, "rev-parse", "--verify", "-q", f"{commit}^1")
        if code != 0 or not base:
            return CodingRefusal("no-base-commit", commit, "the integration commit has no parent to start the task from")
        return PullRequestChange(
            repository=name,
            pr_number=pr_number,
            merge_commit=GitCommandRunner.git(clone, "rev-parse", commit)[1],
            base_commit=base,
            title=cls.pull_request_title(clone, commit, pr_number),
            base_commit_date=GitCommandRunner.git(clone, "show", "-s", "--format=%cs", base)[1],
            merged_at=GitCommandRunner.git(clone, "show", "-s", "--format=%cs", commit)[1],
        )

    @staticmethod
    def repository_name(clone: Path) -> str | None:
        """``owner/name`` from the clone's forge remote, or None when it has none."""
        try:
            return ForgeHost.from_clone(clone).project_path
        except ForgeHostError:
            return None

    @staticmethod
    def find_integration_commit(clone: Path, pr_number: int) -> str | None:
        """The newest first-parent commit of HEAD that merged or squashed PR ``pr_number``."""
        merged = re.compile(rf"^Merge pull request #{pr_number}\b")
        squashed = re.compile(rf"\(#{pr_number}\)\s*$")
        _, log = GitCommandRunner.git(clone, "log", "--first-parent", "--format=%H%x1f%s", "HEAD")
        for line in log.splitlines():
            sha, _, subject = line.partition("\x1f")
            if merged.search(subject) or squashed.search(subject):
                return sha
        return None

    @staticmethod
    def pull_request_title(clone: Path, commit: str, pr_number: int) -> str:
        """The title GitHub writes into the commit: a merge's first body line, or a squash's subject without ``(#N)``."""
        _, subject = GitCommandRunner.git(clone, "show", "-s", "--format=%s", commit)
        if subject.startswith(f"Merge pull request #{pr_number}"):
            _, body = GitCommandRunner.git(clone, "show", "-s", "--format=%b", commit)
            first = next((line.strip() for line in body.splitlines() if line.strip()), "")
            return first or subject
        return re.sub(rf"\s*\(#{pr_number}\)\s*$", "", subject)

    @staticmethod
    def task_name(change: PullRequestChange) -> str:
        """Harbor's ``org/name`` id, ``<owner>/<name>__pr<n>-code``: the review kind's naming with this kind's suffix."""
        return f"{ForgeHost.harbor_org_name(change.repository)}__pr{change.pr_number}-code"

    @classmethod
    def task_dir_name(cls, change: PullRequestChange) -> str:
        """The task folder under ``tasks/``: the id with its slash folded, ``<owner>__<name>__pr<n>-code``."""
        return cls.task_name(change).replace("/", "__")

    # ─────────────────────────────── the change ───────────────────────────────

    @staticmethod
    def changed_paths(clone: Path, base: str, merge: str) -> list[tuple[str, str]]:
        """``(status, path)`` for every path the change touched; renames are a delete and an add."""
        _, raw = GitCommandRunner.git_bytes(clone, "diff", "--name-status", "-z", "--no-renames", base, merge)
        fields = [field.decode("utf-8", "surrogateescape") for field in raw.split(b"\0") if field]
        return list(zip(fields[0::2], fields[1::2], strict=True))

    @classmethod
    def split_change(cls, changes: list[tuple[str, str]], profile: CodingProfile) -> ChangeSplit | CodingRefusal:
        """The source side the agent must write and the test side that grades it, or why the PR is not a task."""
        test_patterns = [PathPattern.compile_glob(glob) for glob in profile.test_globs]
        run_patterns = [PathPattern.compile_glob(glob) for glob in profile.run_globs]
        test_paths = tuple(path for _, path in changes if PathPattern.matches(path, test_patterns))
        source_paths = tuple(path for _, path in changes if path not in test_paths)
        hidden = tuple(path for status, path in changes if path in test_paths and status != "D")
        deleted = tuple(path for status, path in changes if path in test_paths and status == "D")
        run_files = tuple(path for path in hidden if PathPattern.matches(path, run_patterns))
        if not test_paths:
            return CodingRefusal("no-test-change", "the change touches no test-side path", "pick a PR that adds or changes tests")
        if not source_paths:
            return CodingRefusal("no-source-change", "the change touches only tests", "there is nothing for an agent to write")
        if not run_files:
            return CodingRefusal(
                "no-run-files",
                ", ".join(hidden) or "(none)",
                "no changed test file matches the profile's run globs; widen [coding] run",
            )
        return ChangeSplit(
            source_paths=source_paths,
            test_paths=test_paths,
            hidden_test_files=hidden,
            deleted_test_files=deleted,
            run_files=run_files,
        )

    # ─────────────────────────────── the task files ───────────────────────────────

    @classmethod
    def task_files(cls, clone: Path, change: PullRequestChange, split: ChangeSplit, profile: CodingProfile) -> list[EmittedFile]:
        """Every file of the task, as bytes (nothing is written yet)."""
        name = cls.task_name(change)
        files = [
            EmittedFile("task.toml", TomlWriter.emit_document(cls.task_config(name, change, split, profile)), role="config"),
            EmittedFile("instruction.md", cls.instruction(change, split, profile).encode()),
            EmittedFile("environment/Dockerfile", cls.dockerfile(name, change, profile).encode()),
            EmittedFile("environment/repo.tar.gz", cls.snapshot(clone, change.base_commit)),
            EmittedFile("tests/test.sh", cls.template("test.sh"), role="verifier", executable=True),
            EmittedFile("tests/grader.py", cls.template("grader.py"), role="verifier", executable=True),
            EmittedFile("tests/spec.json", cls.grading_spec(split, profile), role="verifier"),
            EmittedFile("solution/solve.sh", cls.template("solve.sh"), role="solution", executable=True),
            EmittedFile("solution/gold.patch", cls.gold_patch(clone, change, split.source_paths), role="solution"),
        ]
        files += [cls.hidden_test_file(clone, change.merge_commit, path) for path in split.hidden_test_files]
        return files

    @staticmethod
    def template(name: str) -> bytes:
        """A ``coding_tpl/`` file, verbatim: data the CLI writes, never imports."""
        return (_TEMPLATE_DIR / name).read_bytes()

    @staticmethod
    def task_config(name: str, change: PullRequestChange, split: ChangeSplit, profile: CodingProfile) -> dict[str, TomlValue]:
        """``task.toml``: the agent reaches only the profile's hosts, the grader nothing, in the agent's container."""
        return {
            "schema_version": "1.4",
            "task": {
                "name": name,
                "version": "1.0.0",
                "description": "Make the change a merged pull request made; the tests it added or changed grade it.",
                "keywords": ["coding-agent", "eval-harvest"],
            },
            "agent": {"timeout_sec": TomlFloat(_AGENT_TIMEOUT_SEC)},
            "verifier": {"timeout_sec": TomlFloat(profile.timeout_sec + 300), "network_mode": "no-network"},
            "environment": {"network_mode": "allowlist", "os": "linux", "allowed_hosts": list(profile.allowed_hosts)},
            "metadata": {
                "origin": {
                    "repo": change.repository,
                    "pr_numbers": [change.pr_number],
                    "base_commit": change.base_commit,
                    "merge_commit": change.merge_commit,
                    "base_commit_date": change.base_commit_date,
                    "merged_at": change.merged_at,
                },
                "harvest": {
                    "kind": "coding",
                    "grading": "staged: tests/test.sh --stage untrusted, then --stage trusted (reward.json)",
                    "run_files": list(split.run_files),
                    "calibrated": False,
                },
            },
        }

    @staticmethod
    def instruction(change: PullRequestChange, split: ChangeSplit, profile: CodingProfile) -> str:
        """The PR's title, where the repository is, and how the hidden tests run. Never the PR body."""
        files = " ".join(split.run_files)
        where = "the repository root" if profile.workdir in (".", "") else f"`{profile.workdir}`"
        listed = "\n".join(f"- `{path}`" for path in split.hidden_test_files)
        return (
            f"# {change.title}\n\n"
            f"The repository at `/work/repo` is {change.repository} at the commit just before this change was merged. "
            "Make the change described here.\n\n"
            "## How the change is checked\n\n"
            f"Hidden tests are added to the repository and run from {where} with:\n\n"
            f"    {profile.display_command.replace('{files}', files)}\n\n"
            f"{CALIBRATION_BEGIN}\n{CALIBRATION_END}\n\n"
            "These test files are replaced with their own versions before grading, so edits to them do not count:\n\n"
            f"{listed}\n"
        )

    @staticmethod
    def dockerfile(name: str, change: PullRequestChange, profile: CodingProfile) -> str:
        """The image: the base tree as one commit at /work/repo, installed, with the agents' tools."""
        environment = "".join(f" \\\n    {key}={json.dumps(value)}" for key, value in profile.environment)
        install = f"RUN cd /work/repo && {profile.install}\n" if profile.install else ""
        agent_setup = "".join(f"RUN {command}\n" for command in profile.agent_setup)
        return (
            (_TEMPLATE_DIR / "Dockerfile.tmpl")
            .read_text(encoding="utf-8")
            .format(
                task_name=name,
                repository=change.repository,
                base_short=change.base_commit[:12],
                image=profile.image,
                environment_lines=environment,
                install_lines=install,
                agent_setup_lines=agent_setup,
            )
        )

    @staticmethod
    def snapshot(clone: Path, base_commit: str) -> bytes:
        """The base commit's tree as a gzip tarball with a fixed timestamp: the same commit gives the same bytes."""
        _, tar = GitCommandRunner.git_bytes(clone, "archive", "--format=tar", base_commit)
        return gzip.compress(tar, mtime=0)

    @staticmethod
    def grading_spec(split: ChangeSplit, profile: CodingProfile) -> bytes:
        """``tests/spec.json``, which the grader reads."""
        spec = {
            "workdir": profile.workdir,
            "command": profile.command,
            "run_files": list(split.run_files),
            "test_globs": list(profile.test_globs),
            "deleted": list(split.deleted_test_files),
            "timeout_sec": profile.timeout_sec,
            "env": dict(profile.environment),
        }
        return (json.dumps(spec, indent=2, sort_keys=True) + "\n").encode()

    @staticmethod
    def gold_patch(clone: Path, change: PullRequestChange, source_paths: tuple[str, ...]) -> bytes:
        """The source side of the change as one binary-safe patch (the test side is hidden, not patched in)."""
        parts: list[bytes] = []
        for start in range(0, len(source_paths), _GIT_PATH_CHUNK):
            chunk = source_paths[start : start + _GIT_PATH_CHUNK]
            _, diff = GitCommandRunner.git_bytes(
                clone, "diff", "--binary", "--no-renames", change.base_commit, change.merge_commit, "--", *chunk
            )
            parts.append(diff)
        return b"".join(parts)

    @staticmethod
    def hidden_test_file(clone: Path, merge_commit: str, path: str) -> EmittedFile:
        """One changed test file as the merge has it, under ``tests/files/``."""
        _, blob = GitCommandRunner.git_bytes(clone, "show", f"{merge_commit}:{path}")
        return EmittedFile(f"tests/files/{path}", blob, role="verifier")

    # ─────────────────────────────── writing ───────────────────────────────

    @classmethod
    def write_task(cls, dataset: Path, name: str, files: list[EmittedFile]) -> Path | CodingRefusal:
        """Build the task in a temp dir, check its layout, config and answer absence, then move it into place."""
        compiled = CompiledTask(name=name, files=tuple(files), directory=name, verifier_mode="shared")
        layout = Harbor.validate_task_layout(compiled)
        config = next(file for file in files if file.path == "task.toml")
        violations = layout + Harbor.validate_coding_task_config(tomllib.loads(config.data.decode("utf-8")))
        if violations:
            return CodingRefusal(
                "task-invalid", "; ".join(str(violation) for violation in violations), "report this: code wrote an invalid task"
            )
        tasks = dataset / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=tasks))
        try:
            cls.write_files(staging, files)
            leak = cls.answer_leak(staging)
            if leak is not None:
                return leak
            final = tasks / name
            if final.exists():
                shutil.rmtree(final)
            staging.rename(final)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return tasks / name

    @staticmethod
    def write_files(root: Path, files: list[EmittedFile]) -> None:
        """Write each file; scripts are executable."""
        for emitted in files:
            path = root / emitted.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(emitted.data)
            if emitted.executable:
                path.chmod(0o755)

    @staticmethod
    def answer_leak(task_dir: Path) -> CodingRefusal | None:
        """Refuse when the gold patch shows up in what the agent can read (FR-40), with the control token planted first."""
        with tempfile.TemporaryDirectory() as scratch:
            corpus = Path(scratch) / "task"
            shutil.copytree(task_dir, corpus)
            (corpus / _CONTROL_FILENAME).write_text(f"{_CONTROL_TOKEN}\n", encoding="utf-8")
            report = Seal.scan_agent_visible(corpus, tokens=[], control_token=_CONTROL_TOKEN)
        failures = report.failures
        if not failures:
            return None
        return CodingRefusal(failures[0].check, failures[0].detail, "the answer is visible to the agent; this is a bug in code")


class PathPattern:
    """The test-side and run-file globs, as the grader reads them (``**`` crosses folders). Holds no state."""

    @classmethod
    def compile_glob(cls, pattern: str) -> re.Pattern[str]:
        """A regular expression that matches exactly the paths the glob names."""
        parts: list[str] = []
        index = 0
        while index < len(pattern):
            token, consumed = cls.translate_at(pattern, index)
            parts.append(token)
            index += consumed
        return re.compile("^" + "".join(parts) + "$")

    @staticmethod
    def translate_at(pattern: str, index: int) -> tuple[str, int]:
        """The regular-expression text for the glob token at ``index``, and how many characters it used."""
        if pattern.startswith("**/", index):
            return "(?:.*/)?", 3
        if pattern.startswith("/**", index) and index + 3 == len(pattern):
            return "(?:/.*)?", 3
        if pattern.startswith("**", index):
            return ".*", 2
        if pattern[index] == "*":
            return "[^/]*", 1
        if pattern[index] == "?":
            return "[^/]", 1
        return re.escape(pattern[index]), 1

    @staticmethod
    def matches(path: str, patterns: list[re.Pattern[str]]) -> bool:
        """True when any pattern matches ``path``."""
        return any(pattern.match(path) for pattern in patterns)
