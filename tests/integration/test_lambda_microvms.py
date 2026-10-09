"""Unit tests for the packaged AWS Lambda MicroVMs (microvms-agentd) environment.

This is the project's own MIT-0 behaviour suite for `harvest_env/src/harvest_env/lambda_microvms.py`
— the environment eval-harvest loads into an unmodified Harbor by import path (H-3). It stubs
every AWS/VM boundary (``Sandbox`` and ``Session``) with signature-checked fakes
that record their calls; the ``microvms`` value types (``SizeClass``, ``Region``, ``BaseImage``) are
real and need no credentials.

The suite needs the `eval` group synced (harbor + harvest-env + microvms/boto3), so it carries the
same visible-skip treatment as the other Harbor-dependent modules: `mise run test` syncs `dev` only
and the whole module skips with a named reason; `mise run test-harbor` syncs `eval` and runs it.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import tarfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

EVAL_GROUP_SKIP_REASON = (
    "the `eval` group is not synced: harbor and the packaged `harvest-env` environment come from it, "
    "and `mise run test` syncs `dev` only. Run `mise run test-harbor` to sync `eval` and run this module."
)

# Attempted, not probed: importing what these tests use is the only honest answer to "is the eval
# group synced". Everything below the environment is stubbed, so the real imports are the module
# under test (`harvest_env`), Harbor's public API it plugs into, and the `microvms`/`botocore` value
# types the fakes stand in for.
try:
    import microvms
    from harbor.environments.base import ExecResult
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
    from harbor.utils.optional_import import MissingExtraError
    from harvest_env.lambda_microvms import LambdaMicrovmsEnvironment

    from harvest_env import lambda_microvms as lm
except ImportError:
    EVAL_GROUP_IS_SYNCED = False
else:
    EVAL_GROUP_IS_SYNCED = True

requires_eval_group = pytest.mark.skipif(not EVAL_GROUP_IS_SYNCED, reason=EVAL_GROUP_SKIP_REASON)
# `anyio` (a Harbor dependency, so already in the `eval` group) runs the `async def` tests; pin its
# backend to asyncio via the fixture below so they do not also parametrize over trio.
pytestmark = [requires_eval_group, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "harbor-artifacts"
BUILD_ROLE = f"arn:aws:iam::{ACCOUNT}:role/microvm-build"


def fake_aarch64_elf(payload: bytes = b"agentd") -> bytes:
    header = bytearray(b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 9)  # e_ident, 16 bytes
    header += (2).to_bytes(2, "little")  # e_type
    header += (0xB7).to_bytes(2, "little")  # e_machine = EM_AARCH64
    return bytes(header) + payload


# The extension exposes dynamic result objects. Any is confined to fake SDK boundaries,
# and inspect.signature binds each call to the installed SDK before recording it.


def _validate_sdk_call(method: Callable[..., object], *args: object, **kwargs: Any) -> None:
    inspect.signature(method).bind(object(), *args, **kwargs)


class FakeExecResult:
    """An SDK result whose POSIX outcome is supplied by the test, never inferred by the fake."""

    def __init__(
        self,
        *,
        posix_exit_code: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        notes: list[str] | None = None,
        timed_out: bool = False,
        synthesized: bool = False,
    ) -> None:
        self.posix_exit_code = posix_exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.notes = notes or []
        self.timed_out = timed_out
        self.synthesized = synthesized


class FakeChunk:
    """One SDK output chunk sent through the synchronous callback."""

    def __init__(self, stream: str, text: str) -> None:
        self.stream = stream
        self._text = text

    def text(self) -> str:
        return self._text


class FakeKeepAwake:
    """Tracks whether the provider stops its trial-wide keepalive."""

    def __init__(self) -> None:
        self.stopped = 0
        self.error: Exception | None = None

    def stop(self) -> None:
        self.stopped += 1
        if self.error:
            raise self.error


class FakeSession:
    """Records completion and file operations without executing a command."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.responder: Callable[[str, dict[str, Any]], FakeExecResult] = lambda command, kwargs: FakeExecResult()
        self.chunks: list[FakeChunk] = []
        self.uploaded_files: list[dict[str, Any]] = []
        self.uploaded_tars: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.downloaded_files: list[str] = []
        self.tars: dict[str, bytes] = {}
        self.keep_awake_calls: list[dict[str, Any]] = []
        self.keepalive = FakeKeepAwake()

    def run_to_completion(self, command: str, **kwargs: Any) -> FakeExecResult:
        _validate_sdk_call(microvms.Session.run_to_completion, command, **kwargs)
        self.runs.append({"command": command, **kwargs})
        callback = kwargs.get("on_output")
        if callback:
            for chunk in self.chunks:
                callback(chunk)
        return self.responder(command, kwargs)

    def keep_awake(self, **kwargs: Any) -> FakeKeepAwake:
        _validate_sdk_call(microvms.Session.keep_awake, **kwargs)
        self.keep_awake_calls.append(kwargs)
        return self.keepalive

    def upload_file(self, path: str, data: bytes, *, mode: str | None = None) -> None:
        self.uploaded_files.append({"path": path, "data": data, "mode": mode})

    def upload_tar(self, remote: str, archive: bytes) -> None:
        self.uploaded_tars.append({"remote": remote, "archive": archive})

    def download_file(self, path: str) -> bytes:
        self.downloaded_files.append(path)
        if path not in self.files:
            raise _protocol_not_found()
        return self.files[path]

    def download_tar(self, remote: str) -> bytes:
        if remote not in self.tars:
            raise _protocol_not_found()
        return self.tars[remote]


def _protocol_not_found() -> Exception:
    exc = microvms.ProtocolError("404 from the daemon")
    exc.wire_kind = "NotFound"
    return cast(Exception, exc)


class FakeImage:
    """One image returned by SDK ensure_image."""

    def __init__(self, name: str = "harbor-my-task-abcdef012345") -> None:
        self.name = name
        self.identifier = f"arn:aws:lambda:{REGION}:{ACCOUNT}:microvm-image:{name}"
        self.version = "3.0"


class FakeReport:
    """The externally observable teardown report."""

    def __init__(
        self,
        *,
        leaked: bool = False,
        failures: list[str] | None = None,
        undeleted: list[str] | None = None,
        lifecycle: str = "TERMINATED",
    ) -> None:
        self.leaked = leaked
        self.undeleted = undeleted or []
        self.failures = failures or []
        self.lifecycle = lifecycle


class FakeSandbox:
    """Records SDK lifecycle calls, checking their installed argument signatures."""

    def __init__(self) -> None:
        self.ensure_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.terminate_calls: list[dict[str, Any]] = []
        self.build_error: Exception | None = None
        self.microvm_id: str | None = None
        self.endpoint: str | None = None
        self.session = FakeSession()
        self.image = FakeImage()
        self.warnings: list[str] = []
        self.terminated = 0
        self.suspended = 0
        self.report = FakeReport()
        self.worker_threads: list[int] = []

    def ensure_image(self, **kwargs: Any) -> SimpleNamespace:
        _validate_sdk_call(microvms.Sandbox.ensure_image, **kwargs)
        self.worker_threads.append(threading.get_ident())
        self.ensure_calls.append(kwargs)
        if self.build_error:
            raise self.build_error
        return SimpleNamespace(image=self.image, warnings=self.warnings, reused=False)

    def run(self, **kwargs: Any) -> FakeSession:
        _validate_sdk_call(microvms.Sandbox.run, **kwargs)
        self.worker_threads.append(threading.get_ident())
        self.run_calls.append(kwargs)
        self.microvm_id = "mvm-123"
        self.endpoint = "mvm-123.example.aws"
        return self.session

    def terminate(self, **kwargs: Any) -> FakeReport:
        _validate_sdk_call(microvms.Sandbox.terminate, **kwargs)
        self.worker_threads.append(threading.get_ident())
        self.terminate_calls.append(kwargs)
        self.terminated += 1
        return self.report

    def suspend(self) -> str:
        self.worker_threads.append(threading.get_ident())
        self.suspended += 1
        return "SUSPENDED"


# ── construction helpers ───────────────────────────────────────────────


@pytest.fixture
def agentd_path(tmp_path: Path) -> Path:
    path = tmp_path / "agentd"
    path.write_bytes(fake_aarch64_elf())
    return path


@pytest.fixture
def microvm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROVM_BUCKET", BUCKET)
    monkeypatch.setenv("MICROVM_BUILD_ROLE_ARN", BUILD_ROLE)
    monkeypatch.delenv("MICROVM_EXECUTION_ROLE_ARN", raising=False)
    monkeypatch.delenv("HARBOR_AGENTD_BINARY", raising=False)
    monkeypatch.setenv("AWS_REGION", REGION)


def _make_env(
    tmp_path: Path,
    agentd_path: Path,
    *,
    dockerfile: str | None = "FROM ubuntu:24.04\nWORKDIR /app\n",
    compose: str | None = None,
    extra_files: dict[str, str] | None = None,
    task_env_config: EnvironmentConfig | None = None,
    network_policy: NetworkPolicy | None = None,
    **kwargs: Any,
) -> LambdaMicrovmsEnvironment:
    env_dir = tmp_path / "environment"
    env_dir.mkdir(exist_ok=True)
    if dockerfile is not None:
        (env_dir / "Dockerfile").write_text(dockerfile)
    if compose is not None:
        (env_dir / "docker-compose.yaml").write_text(compose)
    for rel, text in (extra_files or {}).items():
        target = env_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    kwargs.setdefault("agentd_binary", agentd_path)
    return LambdaMicrovmsEnvironment(
        environment_dir=env_dir,
        environment_name="my-task",
        session_id="my-task__abc123__env",
        trial_paths=trial_paths,
        task_env_config=task_env_config or EnvironmentConfig(),
        network_policy=network_policy,
        **kwargs,
    )


def _wire(env: LambdaMicrovmsEnvironment) -> FakeSandbox:
    sandbox = FakeSandbox()
    env._sandbox = sandbox
    return sandbox


def _wire_session(env: LambdaMicrovmsEnvironment) -> FakeSession:
    session = FakeSession()
    env._session = session
    return session


# ── construction and contract ──────────────────────────────────────────


def test_missing_extra_raises(tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lm, "_HAS_MICROVMS", False)
    with pytest.raises(MissingExtraError, match="lambda-microvms"):
        _make_env(tmp_path, agentd_path)


def test_kwargs_override_cli_env_vars(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        s3_bucket="other-bucket",
        build_role_arn="arn:aws:iam::1:role/other",
        execution_role_arn="arn:aws:iam::1:role/exec",
        region="eu-west-1",
    )
    assert env.s3_bucket == "other-bucket"
    assert env.build_role_arn == "arn:aws:iam::1:role/other"
    assert env.execution_role_arn == "arn:aws:iam::1:role/exec"
    assert env.region == "eu-west-1"


def test_env_vars_shared_with_microvm_cli(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MICROVM_EXECUTION_ROLE_ARN", "arn:aws:iam::1:role/exec")
    env = _make_env(tmp_path, agentd_path)
    assert env.s3_bucket == BUCKET
    assert env.build_role_arn == BUILD_ROLE
    assert env.execution_role_arn == "arn:aws:iam::1:role/exec"
    assert env.region == REGION


@pytest.mark.parametrize(
    ("missing", "match"),
    [("MICROVM_BUCKET", "s3_bucket"), ("MICROVM_BUILD_ROLE_ARN", "build_role_arn")],
)
def test_missing_bucket_or_role_is_rejected(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch, missing: str, match: str
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=match):
        _make_env(tmp_path, agentd_path)


def test_missing_region_is_rejected(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(ValueError, match="region"):
        _make_env(tmp_path, agentd_path)


def test_unsupported_region_is_refused_by_the_client(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    with pytest.raises(microvms.MicrovmError):
        _make_env(tmp_path, agentd_path, region="mars-north-1")


def test_duration_ceiling_is_enforced(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    with pytest.raises(ValueError, match="eight hours"):
        _make_env(tmp_path, agentd_path, max_duration_sec=28_801)


def test_idle_defaults_to_the_duration_ceiling(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, max_duration_sec=7200)
    assert env.max_idle_sec == 7200
    env = _make_env(tmp_path, agentd_path, max_idle_sec=900)
    assert env.max_idle_sec == 900


def test_capabilities(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    assert env.capabilities.disable_internet is False
    assert env.capabilities.network_allowlist is False
    assert env.capabilities.docker_compose is False
    caps = LambdaMicrovmsEnvironment.resource_capabilities()
    assert caps.cpu_request and caps.memory_request
    assert not caps.cpu_limit and not caps.memory_limit


# Parametrized by value, not by `NetworkMode` member: decorator arguments are evaluated at import,
# and without the `eval` group `NetworkMode` is never imported, so the module must still collect
# (and skip) rather than error. The policy is built outside `pytest.raises` because Harbor itself
# rejects `allowed_hosts` with `no-network`; only the provider's refusal may satisfy the assertion.
@pytest.mark.parametrize(("mode", "hosts"), [("no-network", []), ("allowlist", ["pypi.org"])])
def test_unenforceable_network_policies_are_rejected(
    tmp_path: Path, agentd_path: Path, microvm_env: None, mode: str, hosts: list[str]
) -> None:
    policy = NetworkPolicy(network_mode=NetworkMode(mode), allowed_hosts=hosts)
    with pytest.raises(ValueError, match="[Nn]etwork|no-network|allowlist"):
        _make_env(tmp_path, agentd_path, network_policy=policy)


def test_compose_tasks_are_rejected(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    with pytest.raises(ValueError, match="Docker Compose"):
        _make_env(tmp_path, agentd_path, compose="services: {}\n")


def test_missing_definition_is_rejected(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    with pytest.raises(FileNotFoundError):
        _make_env(tmp_path, agentd_path, dockerfile=None)


# ── preflight ──────────────────────────────────────────────────────────


def test_preflight_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lm, "_HAS_MICROVMS", False)
    with pytest.raises(MissingExtraError):
        LambdaMicrovmsEnvironment.preflight()


def test_preflight_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def get_available_services(self) -> list[str]:
            return ["lambda-microvms"]

        def get_credentials(self) -> None:
            return None

    monkeypatch.setattr(lm, "boto3", SimpleNamespace(session=SimpleNamespace(Session=Session)))
    with pytest.raises(SystemExit, match="AWS credentials"):
        LambdaMicrovmsEnvironment.preflight()


def test_preflight_old_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def get_available_services(self) -> list[str]:
            return ["lambda"]

        def get_credentials(self) -> object:
            return object()

    monkeypatch.setattr(lm, "boto3", SimpleNamespace(session=SimpleNamespace(Session=Session)))
    with pytest.raises(SystemExit, match="boto3>=1.43.35"):
        LambdaMicrovmsEnvironment.preflight()


def test_preflight_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def get_available_services(self) -> list[str]:
            return ["lambda-microvms"]

        def get_credentials(self) -> object:
            return object()

    monkeypatch.setattr(lm, "boto3", SimpleNamespace(session=SimpleNamespace(Session=Session)))
    LambdaMicrovmsEnvironment.preflight()


@pytest.mark.parametrize(
    ("cpus", "memory_mb", "expected_mib"),
    [
        (None, None, 2048),  # the platform default class, not the smallest
        (1, 512, 2048),  # 1 vCPU baseline needs the 2048 class
        (None, 1024, 1024),
        (2, 4096, 4096),
        (4, None, 8192),
        (None, 5000, 8192),
    ],
)
def test_size_class_covers_the_request(
    tmp_path: Path,
    agentd_path: Path,
    microvm_env: None,
    cpus: int | None,
    memory_mb: int | None,
    expected_mib: int,
) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        task_env_config=EnvironmentConfig(cpus=cpus, memory_mb=memory_mb),
    )
    assert env.size_class().baseline_mib == expected_mib


def test_size_class_over_the_largest_is_rejected(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, task_env_config=EnvironmentConfig(memory_mb=16_384))
    with pytest.raises(microvms.InvalidArgError, match="largest"):
        env.size_class()


def test_harness_dockerfile_appends_daemon_stanza(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, dockerfile="FROM python:3.12-slim\nRUN pip install x\nUSER nobody")
    text = env.harness_dockerfile()
    assert text.startswith("FROM python:3.12-slim\nRUN pip install x\nUSER nobody\n")
    assert "USER root\n" in text
    assert "COPY agentd /agentd\n" in text
    assert "ENV AGENTD_PORT=9000\n" in text
    assert text.rstrip().endswith('ENTRYPOINT []\nCMD ["/agentd"]')


def test_harness_dockerfile_for_prebuilt_image(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile=None,
        task_env_config=EnvironmentConfig(docker_image="ghcr.io/org/task:1"),
    )
    assert env.harness_dockerfile().startswith("FROM ghcr.io/org/task:1\n")
    assert env._dockerfile_workdir is None


def test_base_image_pairs_with_the_task_from(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile="FROM --platform=linux/arm64 ubuntu:24.04 AS runtime\nWORKDIR /work\n",
    )
    base = env.base_image()
    assert base.name == "al2023-1"
    assert base.docker_ref == "ubuntu:24.04"
    assert base.working_dir == "/work"


def test_base_image_uses_managed_al2023_when_from_matches(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    managed = microvms.BaseImage.al2023()
    env = _make_env(tmp_path, agentd_path, dockerfile=f"FROM {managed.docker_ref}\n")
    assert env.base_image().docker_ref == managed.docker_ref
    pinned = f"FROM {managed.docker_ref}@sha256:{'a' * 64}\n"
    env = _make_env(tmp_path, agentd_path, dockerfile=pinned)
    assert env.base_image().docker_ref == f"{managed.docker_ref}@sha256:{'a' * 64}"


# ── SDK image preparation ──────────────────────────────────────────────


def test_provision_agentd_delegates_and_caches_override(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path | None] = []

    def provision_agentd(*, binary: Path | None = None) -> bytes:
        calls.append(binary)
        return b"verified agentd"

    monkeypatch.setattr(microvms, "provision_agentd", provision_agentd)
    env = _make_env(tmp_path, agentd_path)
    assert env._load_agentd() == b"verified agentd"
    assert env._load_agentd() == b"verified agentd"
    assert calls == [agentd_path]


@pytest.mark.parametrize("force", [False, True])
async def test_ensure_image_delegates_build_and_cache_policy(
    tmp_path: Path, agentd_path: Path, microvm_env: None, force: bool
) -> None:
    env = _make_env(tmp_path, agentd_path, task_env_config=EnvironmentConfig(cpus=2, memory_mb=4096))
    sandbox = _wire(env)
    assert await env._ensure_image(force_build=force) == (sandbox.image.identifier, "3.0")
    [call] = sandbox.ensure_calls
    assert call["name_prefix"] == "harbor-my-task"
    assert call["binary"] == agentd_path.read_bytes()
    assert call["dockerfile"] == microvms.wrap_dockerfile("FROM ubuntu:24.04\nWORKDIR /app\n")
    assert call["context_dir"] == str(tmp_path / "environment")
    assert call["s3_bucket"] == BUCKET
    assert call["s3_key_prefix"] == "harbor/lambda-microvms"
    assert call["build_role_arn"] == BUILD_ROLE
    assert call["size"].baseline_mib == 4096
    assert call["base_image"].docker_ref == "ubuntu:24.04"
    assert call["base_image"].working_dir == "/app"
    assert call["force"] is force
    assert call["wait_timeout"] == float(env.task_env_config.build_timeout_sec)
    assert call["tags"]["harbor:environment"] == "lambda-microvms"
    assert env.image_name == sandbox.image.name
    await env.stop(delete=True)


async def test_ensure_prebuilt_image_excludes_task_context(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile=None,
        extra_files={"data.txt": "not build context"},
        task_env_config=EnvironmentConfig(docker_image="ghcr.io/org/task:1"),
    )
    sandbox = _wire(env)
    await env._ensure_image(force_build=False)
    assert sandbox.ensure_calls[0]["context_dir"] is None
    assert sandbox.ensure_calls[0]["dockerfile"].startswith("FROM ghcr.io/org/task:1\n")
    await env.stop(delete=True)


async def test_ensure_image_surfaces_sdk_context_warnings(
    tmp_path: Path, agentd_path: Path, microvm_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    sandbox.warnings = ["Skipping symlink assets/link"]
    with caplog.at_level("WARNING"):
        await env._ensure_image(force_build=False)
    assert "Skipping symlink assets/link" in caplog.text
    await env.stop(delete=True)


def test_image_prefix_reserves_sdk_hash_and_legacy_alias_warns(
    tmp_path: Path, agentd_path: Path, microvm_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    env = _make_env(tmp_path, agentd_path, image_name_prefix="org/task." + "x" * 100)
    assert len(env.image_name_prefix) <= 51
    assert lm.re.fullmatch(r"[a-zA-Z0-9_-]+", env.image_name_prefix)
    with caplog.at_level("WARNING"):
        legacy = _make_env(tmp_path, agentd_path, image_name="old-image")
    assert legacy.image_name_prefix == "old-image"
    assert "content hash" in caplog.text and "deprecated" in caplog.text
    with pytest.raises(ValueError, match="image_name_prefix"):
        _make_env(tmp_path, agentd_path, image_name="old", image_name_prefix="new")


def test_sdk_rejects_incomplete_task_dockerfile(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, dockerfile="FROM ubuntu:24.04\nRUN echo trailing \\\n")
    with pytest.raises(microvms.InvalidArgError):
        env.harness_dockerfile()


# ── VM lifecycle and cancellation ──────────────────────────────────────


async def test_start_launches_prepares_dirs_and_keeps_entire_trial_awake(
    tmp_path: Path, agentd_path: Path, microvm_env: None
) -> None:
    env = _make_env(tmp_path, agentd_path, execution_role_arn="arn:aws:iam::1:role/exec", max_duration_sec=7200)
    sandbox = _wire(env)
    await env.start(force_build=False)
    [launch] = sandbox.run_calls
    assert launch["image_identifier"] == sandbox.image.identifier
    assert launch["image_version"] == "3.0"
    assert launch["egress"] is True
    assert launch["execution_role_arn"] == "arn:aws:iam::1:role/exec"
    assert launch["max_duration_sec"] == 7200
    assert launch["max_idle_sec"] == 7200
    assert launch["token_scope"] == "my-task__abc123__env"
    assert launch["ready_timeout"] == 300.0
    assert sandbox.session.keep_awake_calls == [{"while_busy": False}]
    mkdir_run = next(run for run in sandbox.session.runs if run["command"].startswith("mkdir -p"))
    assert str(EnvironmentPaths.agent_dir) in mkdir_run["command"]
    assert str(EnvironmentPaths.verifier_dir) in mkdir_run["command"]
    assert mkdir_run["user"] == "root"
    assert not any("/proc/" in run["command"] or "/etc/passwd" in run["command"] for run in sandbox.session.runs)
    await env.stop(delete=True)
    assert sandbox.session.keepalive.stopped == 1
    assert sandbox.terminate_calls == [{"wait_for_terminated": True}]
    assert len(set(sandbox.worker_threads)) == 1
    assert sandbox.worker_threads[0] != threading.get_ident()


@pytest.mark.parametrize("stage", ["launch", "prepare_dirs"])
async def test_start_failure_terminates_vm(tmp_path: Path, agentd_path: Path, microvm_env: None, stage: str) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)

    def fail_launch(**kwargs: Any) -> FakeSession:
        sandbox.microvm_id = "mvm-partial"
        raise microvms.LaunchDiedError("image hook timed out")

    def fail_command(command: str, kwargs: dict[str, Any]) -> FakeExecResult:
        raise microvms.RetryableError("directory preparation failed")

    if stage == "launch":
        sandbox.run = fail_launch  # type: ignore[method-assign]
    else:
        sandbox.session.responder = fail_command
    with pytest.raises((microvms.LaunchDiedError, RuntimeError), match="failed|timed out"):
        await env.start(force_build=False)
    assert sandbox.terminated == 1
    assert env._sandbox is None and env._session is None
    assert sandbox.session.keepalive.stopped == (0 if stage == "launch" else 1)


async def test_cancelled_launch_waits_for_allocation_before_teardown(
    tmp_path: Path, agentd_path: Path, microvm_env: None
) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    entered, release = threading.Event(), threading.Event()
    original_run = sandbox.run

    def slow_run(**kwargs: Any) -> FakeSession:
        entered.set()
        assert release.wait(timeout=5), "test did not release the launch worker"
        return original_run(**kwargs)

    sandbox.run = slow_run  # type: ignore[method-assign]
    task = asyncio.create_task(env.start(force_build=False))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
    assert sandbox.microvm_id == "mvm-123"
    assert sandbox.terminated == 1
    assert env._sandbox is None and env._session is None
    assert len(set(sandbox.worker_threads)) == 1


@pytest.mark.parametrize(
    "report",
    [
        FakeReport(leaked=True),
        FakeReport(failures=["terminate denied"]),
        FakeReport(undeleted=["mvm-123"]),
        FakeReport(lifecycle="TERMINATING"),
    ],
)
async def test_failed_teardown_retains_handle_for_retry(
    tmp_path: Path, agentd_path: Path, microvm_env: None, report: FakeReport
) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    sandbox.run()
    sandbox.report = report
    with pytest.raises(RuntimeError, match="teardown failed"):
        await env.stop(delete=True)
    assert env._sandbox is sandbox and env._session is None
    sandbox.report = FakeReport()
    await env.stop(delete=True)
    assert sandbox.terminated == 2
    assert env._sandbox is None


async def test_stop_without_delete_suspends_and_can_later_delete(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    await env.start(force_build=False)
    await env.stop(delete=False)
    assert sandbox.suspended == 1 and sandbox.terminated == 0
    assert sandbox.session.keepalive.stopped == 1
    assert env._sandbox is sandbox and env._session is None
    await env.stop(delete=True)
    assert sandbox.terminated == 1


async def test_stop_without_launch_does_not_construct_sdk_sandbox(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        pytest.fail("stop constructed a real AWS sandbox")

    monkeypatch.setattr(microvms, "Sandbox", refuse)
    await _make_env(tmp_path, agentd_path).stop(delete=True)


async def test_keepalive_failure_still_terminates_vm(
    tmp_path: Path, agentd_path: Path, microvm_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    await env.start(force_build=False)
    sandbox.session.keepalive.error = microvms.RetryableError("health poll failed")
    with caplog.at_level("WARNING"):
        await env.stop(delete=True)
    assert sandbox.terminated == 1
    assert "health poll failed" in caplog.text


# ── SDK completion and Harbor overlays ─────────────────────────────────


async def test_exec_requires_start(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    with pytest.raises(RuntimeError, match="start\\(\\)"):
        await env.exec("true")


async def test_exec_delegates_native_environment_user_and_shell_handling(
    tmp_path: Path, agentd_path: Path, microvm_env: None
) -> None:
    env = _make_env(tmp_path, agentd_path, task_env_config=EnvironmentConfig(env={"TASK": "t"}), persistent_env={"RUN": "r"})
    session = _wire_session(env)
    session.responder = lambda command, kwargs: FakeExecResult(stdout="ok\n")
    with env.scoped_exec_env({"SCOPED": "s", "TASK": "scoped"}):
        result = await env.exec("echo hi", env={"TASK": "per-command"}, timeout_sec=30)
    assert result == ExecResult(stdout="ok\n", stderr=None, return_code=0)
    [run] = session.runs
    assert run["command"] == "echo hi"
    assert run["shell"] == "bash" and run["inherit_image_env"] is True
    assert run["cwd"] == "/app"
    assert run["user"] == "root"
    assert run["timeout_sec"] == 30.0
    assert uuid.UUID(run["exec_id"]).version == 4
    assert run["env"] == {"TASK": "scoped", "RUN": "r", "SCOPED": "s"}
    assert run["on_output"] is None


async def test_exec_cwd_precedence(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, task_env_config=EnvironmentConfig(workdir="/cfg"))
    session = _wire_session(env)
    await env.exec("true")
    await env.exec("true", cwd="/explicit")
    assert [run["cwd"] for run in session.runs] == ["/cfg", "/explicit"]
    bare = _make_env(tmp_path, agentd_path, dockerfile="FROM ubuntu:24.04\n")
    bare_session = _wire_session(bare)
    await bare.exec("true")
    assert bare_session.runs[0]["cwd"] is None


async def test_exec_preserves_default_user_and_numeric_primary_group(
    tmp_path: Path, agentd_path: Path, microvm_env: None
) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.files["/etc/passwd"] = b"root:x:0:0::/root:/bin/bash\nagent:x:1000:1001::/home/agent:/bin/bash\n"
    with env.with_default_user("agent"):
        await env.exec("id")
        await env.exec("id", user=0)
    with env.with_default_user(1000):
        await env.exec("id")
        await env.exec("id", user=4242)
    assert [run["user"] for run in session.runs] == ["agent", 0, 1000, 4242]
    assert [run["group"] for run in session.runs] == [None, 0, 1001, None]
    assert all(run["inherit_image_env"] for run in session.runs)


@pytest.mark.parametrize(
    ("result", "timeout", "code", "note"),
    [
        (FakeExecResult(posix_exit_code=3, stderr="bad"), None, 3, "bad"),
        (FakeExecResult(posix_exit_code=143), 10, 143, None),
        (FakeExecResult(posix_exit_code=139), 10, 139, None),
        (FakeExecResult(posix_exit_code=124, timed_out=True), 10, 124, "timed out after 10"),
        (FakeExecResult(posix_exit_code=124, synthesized=True, notes=["output unknown"]), 5, 124, "output unknown"),
        (FakeExecResult(notes=["output truncated", "writers may be alive"]), None, 0, "writers may be alive"),
        (FakeExecResult(posix_exit_code=None), None, -1, None),
    ],
)
async def test_exec_uses_sdk_outcome_and_notes(
    tmp_path: Path,
    agentd_path: Path,
    microvm_env: None,
    result: FakeExecResult,
    timeout: int | None,
    code: int,
    note: str | None,
) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = lambda command, kwargs: result
    output = await env.exec("cmd", timeout_sec=timeout)
    assert output.return_code == code
    assert output.stdout is None
    if note is None:
        assert output.stderr is None
    else:
        assert note in (output.stderr or "")
    for sdk_note in result.notes:
        assert f"[microvms] {sdk_note}" in (output.stderr or "")


async def test_exec_failure_is_reported(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)

    def fail(command: str, kwargs: dict[str, Any]) -> FakeExecResult:
        raise microvms.RetryableError("503 not bootstrapped")

    session.responder = fail
    with pytest.raises(RuntimeError, match="exec failed.*503"):
        await env.exec("true")


async def test_exec_bridges_output_callback_to_event_loop(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.chunks = [FakeChunk("stdout", "one\n"), FakeChunk("stderr", "warn\n"), FakeChunk("stdout", "two\n")]
    session.responder = lambda command, kwargs: FakeExecResult(stdout="one\ntwo\n", stderr="warn\n")
    seen: list[tuple[str, str]] = []
    callback_threads: list[int] = []

    async def callback(text: str, stream: str) -> None:
        seen.append((stream, text))
        callback_threads.append(threading.get_ident())

    with env.scoped_output_callback(callback):
        result = await env.exec("cmd")
    assert seen == [("stdout", "one\n"), ("stderr", "warn\n"), ("stdout", "two\n")]
    assert callback_threads == [threading.get_ident()] * 3
    assert result.stdout == "one\ntwo\n" and result.stderr == "warn\n"


async def test_cancelled_exec_terminates_vm_even_before_registration(
    tmp_path: Path, agentd_path: Path, microvm_env: None
) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    await env.start(force_build=False)
    entered, release = threading.Event(), threading.Event()
    completed = threading.Event()

    def delayed_registration(command: str, kwargs: dict[str, Any]) -> FakeExecResult:
        entered.set()
        try:
            assert release.wait(timeout=5), "test did not release the exec worker"
            return FakeExecResult()
        finally:
            completed.set()

    sandbox.session.responder = delayed_registration
    task = asyncio.create_task(env.exec("sleep 999"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert sandbox.terminated == 1
        assert env._session is None and env._sandbox is None
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)


# ── file transfer ──────────────────────────────────────────────────────


async def test_upload_file_keeps_mode(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    await env.upload_file(script, "/workspace/run.sh")
    assert session.uploaded_files == [{"path": "/workspace/run.sh", "data": b"#!/bin/sh\n", "mode": "0755"}]


async def test_upload_dir_sends_a_plain_tar(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "a.txt").write_text("A")
    (source / "empty").mkdir()

    await env.upload_dir(source, "/workspace/src")

    [tar_call] = session.uploaded_tars
    assert tar_call["remote"] == "/workspace/src"
    assert not tar_call["archive"].startswith(b"\x1f\x8b")  # not gzipped
    with tarfile.open(fileobj=io.BytesIO(tar_call["archive"])) as tar:
        names = sorted(tar.getnames())
    assert names == [".", "./empty", "./nested", "./nested/a.txt"]


async def test_upload_missing_dir_warns_and_skips(
    tmp_path: Path, agentd_path: Path, microvm_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    with caplog.at_level("WARNING"):
        await env.upload_dir(tmp_path / "missing", "/x")
    assert session.uploaded_tars == []
    assert "No files to upload" in caplog.text


async def test_download_file_and_not_found(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.files["/logs/out.txt"] = b"hello"
    target = tmp_path / "deep" / "out.txt"
    await env.download_file("/logs/out.txt", target)
    assert target.read_bytes() == b"hello"
    with pytest.raises(FileNotFoundError):
        await env.download_file("/logs/missing", tmp_path / "m")


async def test_download_dir_extracts_and_not_found(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("./result.json")
        payload = b'{"ok": true}'
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    session.tars["/logs/verifier"] = buffer.getvalue()

    target = tmp_path / "verifier"
    await env.download_dir("/logs/verifier", target)
    assert (target / "result.json").read_bytes() == b'{"ok": true}'
    with pytest.raises(FileNotFoundError):
        await env.download_dir("/logs/missing", tmp_path / "m")


async def test_other_daemon_refusals_surface_as_runtime_errors(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)

    def refuse(*_: Any, **__: Any) -> None:
        raise microvms.ProtocolError("400 bad mode")

    session.upload_file = refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="bad mode"):
        await env.upload_file(agentd_path, "/x")


async def test_numeric_primary_group_is_cached_per_uid(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.files["/etc/passwd"] = b"agent:x:1000:1001::/home/agent:/bin/bash\n"
    await env.exec("id", user=1000)
    await env.exec("id", user=1000)
    assert [run["group"] for run in session.runs] == [1001, 1001]
    assert session.downloaded_files == ["/etc/passwd"]


async def test_stderr_preserves_command_newlines(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = lambda command, kwargs: FakeExecResult(stderr="\noriginal\n")
    result = await env.exec("cmd")
    assert result.stderr == "\noriginal\n"


async def test_cancelled_stop_finishes_shielded_teardown(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    sandbox = _wire(env)
    await env.start(force_build=False)
    entered, release = threading.Event(), threading.Event()
    original_terminate = sandbox.terminate

    def delayed_terminate(**kwargs: Any) -> FakeReport:
        entered.set()
        assert release.wait(timeout=5), "test did not release the teardown worker"
        return original_terminate(**kwargs)

    sandbox.terminate = delayed_terminate  # type: ignore[method-assign]
    task = asyncio.create_task(env.stop(delete=True))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        # The second stop waits for the shielded first stop's lock to clear.
        await asyncio.wait_for(env.stop(delete=True), timeout=5)
    finally:
        release.set()
    assert sandbox.terminated == 1
    assert env._sandbox is None and env._session is None
