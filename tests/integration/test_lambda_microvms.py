"""Unit tests for the packaged AWS Lambda MicroVMs (microvms-agentd) environment.

This is the project's own MIT-0 behaviour suite for `harvest_env/src/harvest_env/lambda_microvms.py`
— the environment eval-harvest loads into an unmodified Harbor by import path (H-3). It stubs
everything that would touch AWS or a VM (the ``Sandbox``, the ``Session``, boto3 clients) with fakes
that record their calls; the ``microvms`` value types (``SizeClass``, ``Region``, ``BaseImage``) are
real and need no credentials.

The suite needs the `eval` group synced (harbor + harvest-env + microvms/boto3), so it carries the
same visible-skip treatment as the other Harbor-dependent modules: `mise run test` syncs `dev` only
and the whole module skips with a named reason; `mise run test-harbor` syncs `eval` and runs it.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
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
    from botocore.exceptions import ClientError
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


def fake_x86_elf() -> bytes:
    data = bytearray(fake_aarch64_elf())
    data[18:20] = (0x3E).to_bytes(2, "little")  # EM_X86_64
    return bytes(data)


# ── fakes ──────────────────────────────────────────────────────────────


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
        "GetMicrovmImage",
    )


class FakeApi:
    def __init__(self) -> None:
        self.images: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.get_calls = 0

    def get_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        self.get_calls += 1
        if imageIdentifier not in self.images:
            raise _not_found()
        return dict(self.images[imageIdentifier])

    def delete_microvm_image(self, *, imageIdentifier: str) -> None:
        self.deleted.append(imageIdentifier)
        self.images.pop(imageIdentifier, None)


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


class FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": ACCOUNT}


class FakeBotoSession:
    def __init__(self) -> None:
        self.api = FakeApi()
        self.s3 = FakeS3()

    def client(self, name: str) -> Any:
        return {"lambda-microvms": self.api, "s3": self.s3, "sts": FakeSts()}[name]


class FakeExecResult:
    def __init__(
        self,
        *,
        exit_code: int | None = 0,
        signal: int | None = None,
        stdout: str = "",
        stderr: str = "",
        truncated: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.signal = signal
        self.stdout = stdout
        self.stderr = stderr
        self.truncated = truncated


class FakeHandle:
    def __init__(self, result: FakeExecResult, events: list[Any] | None = None) -> None:
        self.result = result
        self.events = events
        self.wait_timeouts: list[float] = []
        self.acked = 0
        self.killed = 0

    def wait_and_ack(self, timeout: float) -> FakeExecResult:
        self.wait_timeouts.append(timeout)
        self.acked += 1
        return self.result

    def ack(self) -> FakeExecResult:
        self.acked += 1
        return self.result

    def kill(self) -> bool:
        self.killed += 1
        return True

    def stream(self, **_: Any) -> Any:
        assert self.events is not None, "stream() called without scripted events"
        yield from self.events


class FakeSession:
    """Answers ``run`` through ``responder(command, kwargs) -> FakeExecResult``."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.handles: list[FakeHandle] = []
        self.responder: Callable[[str, dict[str, Any]], FakeExecResult] = lambda command, kwargs: FakeExecResult()
        self.events: list[Any] | None = None
        self.uploaded_files: list[dict[str, Any]] = []
        self.uploaded_tars: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.tars: dict[str, bytes] = {}
        self.ready_timeouts: list[float] = []

    def wait_until_ready(self, timeout: float) -> None:
        self.ready_timeouts.append(timeout)

    def run(self, command: str | list[str], **kwargs: Any) -> FakeHandle:
        # The environment execs `["bash", "-c", script]`; record the script
        # as ``command`` so assertions read the way the caller wrote it, and
        # keep the raw argv for the one test that checks the wrapper itself.
        script = command
        if isinstance(command, list):
            assert command[:2] == ["bash", "-c"] and len(command) == 3
            script = command[2]
        self.runs.append({"command": script, "argv": command, **kwargs})
        handle = FakeHandle(self.responder(script, kwargs), self.events)  # type: ignore[arg-type]
        self.handles.append(handle)
        return handle

    def upload_file(self, path: str, data: bytes, *, mode: str | None = None) -> None:
        self.uploaded_files.append({"path": path, "data": data, "mode": mode})

    def upload_tar(self, remote: str, archive: bytes) -> None:
        self.uploaded_tars.append({"remote": remote, "archive": archive})

    def download_file(self, path: str) -> bytes:
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
    def __init__(self, identifier: str, version: str = "1.0") -> None:
        self.identifier = identifier
        self.version = version


class FakeReport:
    def __init__(self, *, leaked: bool = False) -> None:
        self.leaked = leaked
        self.undeleted = ["log-group"] if leaked else []
        self.failures: list[str] = []


class FakeSandbox:
    def __init__(self, *, build_error: Exception | None = None) -> None:
        self.build_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.build_error = build_error
        self.microvm_id: str | None = None
        self.endpoint: str | None = None
        self.session = FakeSession()
        self.terminated = 0
        self.suspended = 0
        self.report = FakeReport()

    def build_image(self, **kwargs: Any) -> FakeImage:
        self.build_calls.append(kwargs)
        if self.build_error is not None:
            raise self.build_error
        return FakeImage(f"arn:aws:lambda:{REGION}:{ACCOUNT}:microvm-image:{kwargs['name']}")

    def run(self, **kwargs: Any) -> FakeSession:
        self.run_calls.append(kwargs)
        self.microvm_id = "mvm-123"
        self.endpoint = "mvm-123.example.aws"
        return self.session

    def terminate(self) -> FakeReport:
        self.terminated += 1
        return self.report

    def suspend(self) -> str:
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


def _wire(env: LambdaMicrovmsEnvironment) -> tuple[FakeBotoSession, FakeSandbox]:
    boto = FakeBotoSession()
    sandbox = FakeSandbox()
    env._boto_session = boto
    env._sandbox = sandbox
    return boto, sandbox


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
    assert env.capabilities.disable_internet is True
    assert env.capabilities.network_allowlist is False
    assert env.capabilities.docker_compose is False
    caps = LambdaMicrovmsEnvironment.resource_capabilities()
    assert caps.cpu_request and caps.memory_request
    assert not caps.cpu_limit and not caps.memory_limit


def test_no_network_accepted_allowlist_rejected(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    _make_env(
        tmp_path,
        agentd_path,
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
    )
    with pytest.raises(ValueError, match="allowlist"):
        _make_env(
            tmp_path,
            agentd_path,
            network_policy=NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org"]),
        )


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


# ── image definition ───────────────────────────────────────────────────


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
    with pytest.raises(ValueError, match="largest"):
        env.size_class()


def test_harness_dockerfile_appends_daemon_stanza(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, dockerfile="FROM python:3.12-slim\nRUN pip install x")
    text = env.harness_dockerfile()
    assert text.startswith("FROM python:3.12-slim\nRUN pip install x\n")
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
    assert lm.dockerfile_from_ref("# comment\nfrom   alpine:3\n") == "alpine:3"
    assert lm.dockerfile_from_ref("RUN true\n") is None


def test_base_image_uses_managed_al2023_when_from_matches(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    managed = microvms.BaseImage.al2023()
    env = _make_env(tmp_path, agentd_path, dockerfile=f"FROM {managed.docker_ref}\n")
    assert env.base_image().docker_ref == managed.docker_ref
    pinned = f"FROM {managed.docker_ref}@sha256:{'a' * 64}\n"
    env = _make_env(tmp_path, agentd_path, dockerfile=pinned)
    assert env.base_image().docker_ref == managed.docker_ref


def test_image_name_is_content_addressed_and_valid(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    name = env.image_name
    assert name.startswith("harbor-my-task-")
    assert len(name) <= 64
    assert lm.re.fullmatch(r"[a-zA-Z0-9_-]+", name)
    assert env.image_name == name  # stable across calls

    other_agentd = tmp_path / "agentd2"
    other_agentd.write_bytes(fake_aarch64_elf(b"different daemon"))
    assert _make_env(tmp_path, other_agentd).image_name != name

    bigger = _make_env(tmp_path, agentd_path, task_env_config=EnvironmentConfig(memory_mb=4096))
    assert bigger.image_name != name

    explicit = _make_env(tmp_path, agentd_path, image_name="pinned-image")
    assert explicit.image_name == "pinned-image"


def test_sanitize_image_name() -> None:
    assert lm.sanitize_image_name("org/task.v2") == "org-task-v2"
    long = lm.sanitize_image_name("x" * 100)
    assert len(long) <= 64 and lm.re.fullmatch(r"[a-zA-Z0-9_-]+", long)
    assert lm.sanitize_image_name("///") == "harbor"


def test_build_artifact_carries_context_and_executable_daemon(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile="FROM ubuntu:24.04\nCOPY tests/ /tests/\n",
        extra_files={
            "tests/test_outputs.py": "assert True\n",
            "__pycache__/x.pyc": "junk",
        },
    )
    (tmp_path / "environment" / "link").symlink_to(tmp_path / "environment" / "Dockerfile")
    with zipfile.ZipFile(io.BytesIO(env.build_artifact())) as archive:
        names = archive.namelist()
        assert names[:2] == ["Dockerfile", "agentd"]
        assert "tests/test_outputs.py" in names
        assert "link" not in names
        assert not any(n.startswith("__pycache__") for n in names)
        assert archive.read("Dockerfile").decode() == env.harness_dockerfile()
        assert archive.read("agentd") == fake_aarch64_elf()
        assert (archive.getinfo("agentd").external_attr >> 16) & 0o777 == 0o755


def test_build_artifact_refuses_a_task_file_named_agentd(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path, extra_files={"agentd": "not the daemon"})
    with pytest.raises(ValueError, match="collides"):
        env.build_artifact()


def test_prebuilt_artifact_has_no_context(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile=None,
        extra_files={"data.txt": "uploaded after start instead"},
        task_env_config=EnvironmentConfig(docker_image="ghcr.io/org/task:1"),
    )
    with zipfile.ZipFile(io.BytesIO(env.build_artifact())) as archive:
        assert archive.namelist() == ["Dockerfile", "agentd"]


def test_agentd_binary_must_be_aarch64(tmp_path: Path, microvm_env: None) -> None:
    bad = tmp_path / "agentd-x86"
    bad.write_bytes(fake_x86_elf())
    env = _make_env(tmp_path, bad)
    with pytest.raises(ValueError, match="aarch64"):
        env.build_artifact()
    with pytest.raises(ValueError, match="not an ELF"):
        lm.require_aarch64_elf(b"#!/bin/sh\n", source="script")


def test_agentd_binary_downloaded_to_cache(tmp_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: Any) -> Any:
        calls.append(url)
        return SimpleNamespace(content=fake_aarch64_elf(), raise_for_status=lambda: None)

    monkeypatch.setattr(lm.httpx, "get", fake_get)
    monkeypatch.setattr(lm.platformdirs, "user_cache_path", lambda _: tmp_path / "cache")
    env = _make_env(tmp_path, tmp_path / "unused", agentd_binary=None)
    assert env._load_agentd() == fake_aarch64_elf()
    assert calls == [f"https://github.com/theagenticguy/microvms-agentd/releases/download/v{microvms.core_version()}/agentd"]
    # Second environment reads the cache instead of downloading again.
    _make_env(tmp_path, tmp_path / "unused", agentd_binary=None)._load_agentd()
    assert len(calls) == 1


# ── image lifecycle ────────────────────────────────────────────────────


def _arn(env: LambdaMicrovmsEnvironment) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:microvm-image:{env.image_name}"


async def test_ensure_image_reuses_an_active_image(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATED", "latestActiveImageVersion": "2.0"}

    assert await env._ensure_image(force_build=False) == (_arn(env), "2.0")
    assert sandbox.build_calls == []
    assert boto.s3.puts == []


async def test_ensure_image_builds_through_the_bindings(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile="FROM ubuntu:24.04\nWORKDIR /app\n",
        task_env_config=EnvironmentConfig(cpus=2, memory_mb=4096),
    )
    boto, sandbox = _wire(env)

    arn, version = await env._ensure_image(force_build=False)

    assert (arn, version) == (_arn(env), "1.0")
    [put] = boto.s3.puts
    assert put["Bucket"] == BUCKET
    assert put["Key"] == f"harbor/lambda-microvms/{env.image_name}/artifact.zip"
    [call] = sandbox.build_calls
    assert call["name"] == env.image_name
    assert call["binary"] == fake_aarch64_elf()
    assert call["code_artifact_uri"] == f"s3://{BUCKET}/{put['Key']}"
    assert call["build_role_arn"] == BUILD_ROLE
    assert call["size"].baseline_mib == 4096
    assert call["base_image"].docker_ref == "ubuntu:24.04"
    assert call["dockerfile"] == env.harness_dockerfile()
    assert call["tags"]["harbor:environment"] == "lambda-microvms"


async def test_ensure_image_waits_on_a_concurrent_build(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    monkeypatch.setattr(lm, "_IMAGE_POLL_INTERVAL_SEC", 0.0)
    boto.api.images[_arn(env)] = {"state": "CREATING"}

    async def flip_to_created(*_: Any, **__: Any) -> None:
        boto.api.images[_arn(env)] = {
            "state": "CREATED",
            "latestActiveImageVersion": "1.0",
        }

    monkeypatch.setattr(lm.asyncio, "sleep", flip_to_created)
    assert await env._ensure_image(force_build=False) == (_arn(env), "1.0")
    assert sandbox.build_calls == []


async def test_ensure_image_lost_create_race_waits_for_winner(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    sandbox.build_error = microvms.PlatformError("ConflictException: exists")
    monkeypatch.setattr(lm, "_IMAGE_POLL_INTERVAL_SEC", 0.0)

    real_build = sandbox.build_image

    def losing_build(**kwargs: Any) -> FakeImage:
        # The winner's image appears before our refusal is examined.
        boto.api.images[_arn(env)] = {
            "state": "CREATED",
            "latestActiveImageVersion": "1.0",
        }
        return real_build(**kwargs)

    sandbox.build_image = losing_build  # type: ignore[method-assign]
    assert await env._ensure_image(force_build=False) == (_arn(env), "1.0")


async def test_ensure_image_build_failure_without_image_raises(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    _, sandbox = _wire(env)
    sandbox.build_error = microvms.PlatformError("AccessDenied")
    with pytest.raises(RuntimeError, match="AccessDenied"):
        await env._ensure_image(force_build=False)


async def test_ensure_image_force_build_deletes_first(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATED", "latestActiveImageVersion": "1.0"}

    await env._ensure_image(force_build=True)

    assert boto.api.deleted == [_arn(env)]
    assert len(sandbox.build_calls) == 1


async def test_ensure_image_failed_image_is_rebuilt(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATE_FAILED"}

    await env._ensure_image(force_build=False)

    assert boto.api.deleted == [_arn(env)]
    assert len(sandbox.build_calls) == 1


async def test_wait_for_image_reports_a_failed_build(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, _ = _wire(env)
    boto.api.images[_arn(env)] = {
        "state": "CREATE_FAILED",
        "stateReason": "hooks timed out",
    }
    with pytest.raises(RuntimeError, match="hooks timed out"):
        await env._wait_for_image(_arn(env))


# ── start / stop ───────────────────────────────────────────────────────


def _responder(passwd: str = "agent:x:1000:1000::/home/agent:/bin/bash") -> Callable[[str, dict[str, Any]], FakeExecResult]:
    def respond(command: str, kwargs: dict[str, Any]) -> FakeExecResult:
        if "/proc/$PPID/environ" in command:
            return FakeExecResult(stdout="PATH=/opt/venv/bin:/usr/bin\nAGENTD_PORT=9000\nFOO=bar\n")
        if "/etc/passwd" in command:
            for row in passwd.splitlines():
                name, _, uid, gid, _, home, _ = row.split(":")
                if f"= {name} ]" in command or f"= {uid} ]" in command:
                    return FakeExecResult(stdout=f"{name}:{uid}:{gid}:{home}\n")
            return FakeExecResult(exit_code=1)
        return FakeExecResult(stdout="ok\n")

    return respond


async def test_start_builds_launches_and_prepares_dirs(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
        execution_role_arn="arn:aws:iam::1:role/exec",
        max_duration_sec=7200,
    )
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATED", "latestActiveImageVersion": "3.0"}
    sandbox.session.responder = _responder()

    await env.start(force_build=False)

    [launch] = sandbox.run_calls
    assert launch["image_identifier"] == _arn(env)
    assert launch["image_version"] == "3.0"
    assert launch["egress"] is False
    assert launch["execution_role_arn"] == "arn:aws:iam::1:role/exec"
    assert launch["max_duration_sec"] == 7200
    assert launch["max_idle_sec"] == 7200
    assert launch["token_scope"] == "my-task__abc123__env"
    assert sandbox.session.ready_timeouts == [300.0]
    # The image ENV was captured, minus the daemon's own knobs.
    assert env._image_env == {"PATH": "/opt/venv/bin:/usr/bin", "FOO": "bar"}
    mkdir_run = next(r for r in sandbox.session.runs if r["command"].startswith("mkdir -p"))
    assert str(EnvironmentPaths.agent_dir) in mkdir_run["command"]
    assert str(EnvironmentPaths.verifier_dir) in mkdir_run["command"]
    assert mkdir_run["user"] is None  # root: the daemon's own user


async def test_start_public_network_requests_egress(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATED", "latestActiveImageVersion": "1.0"}
    await env.start(force_build=False)
    assert sandbox.run_calls[0]["egress"] is True


async def test_start_launch_failure_is_reported(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    boto, sandbox = _wire(env)
    boto.api.images[_arn(env)] = {"state": "CREATED", "latestActiveImageVersion": "1.0"}

    def dying_run(**_: Any) -> FakeSession:
        raise microvms.LaunchDiedError("stateReason: image hook timed out")

    sandbox.run = dying_run  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="hook timed out"):
        await env.start(force_build=False)


async def test_stop_delete_terminates_and_reports_leaks(
    tmp_path: Path, agentd_path: Path, microvm_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    env = _make_env(tmp_path, agentd_path)
    _, sandbox = _wire(env)
    sandbox.run()
    env._session = sandbox.session
    sandbox.report = FakeReport(leaked=True)

    with caplog.at_level("WARNING"):
        await env.stop(delete=True)

    assert sandbox.terminated == 1
    assert env._sandbox is None and env._session is None
    assert "log-group" in caplog.text


async def test_stop_without_delete_suspends(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    _, sandbox = _wire(env)
    sandbox.run()
    await env.stop(delete=False)
    assert sandbox.suspended == 1 and sandbox.terminated == 0


async def test_stop_before_launch_is_a_noop(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    _, sandbox = _wire(env)
    await env.stop(delete=True)
    assert sandbox.terminated == 0


# ── exec ───────────────────────────────────────────────────────────────


async def test_exec_requires_start(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    with pytest.raises(RuntimeError, match="start()"):
        await env.exec("true")


async def test_exec_runs_a_shell_script_with_layered_env(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile="FROM ubuntu:24.04\nWORKDIR /app\n",
        task_env_config=EnvironmentConfig(env={"TASK": "t"}),
        persistent_env={"RUN": "r"},
    )
    session = _wire_session(env)
    session.responder = _responder()
    env._image_env = {"PATH": "/opt/venv/bin:/usr/bin", "HOME": "/root"}

    with env.scoped_exec_env({"SCOPED": "s"}):
        result = await env.exec("echo hi", env={"TASK": "override"}, timeout_sec=30)

    assert result == ExecResult(stdout="ok\n", stderr=None, return_code=0)
    [run] = session.runs
    assert run["command"] == "echo hi"
    assert run["argv"] == ["bash", "-c", "echo hi"]
    assert run["shell"] is False
    assert run["cwd"] == "/app"
    assert run["user"] is None and run["group"] is None
    assert run["timeout_sec"] == 30.0
    assert len(run["exec_id"]) == 32
    assert run["env"] == {
        "PATH": "/opt/venv/bin:/usr/bin",
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "TASK": "override",
        "RUN": "r",
        "SCOPED": "s",
    }
    assert session.handles[0].wait_timeouts == [90.0]


async def test_exec_cwd_precedence(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(
        tmp_path,
        agentd_path,
        dockerfile="FROM ubuntu:24.04\nWORKDIR /app\n",
        task_env_config=EnvironmentConfig(workdir="/cfg"),
    )
    session = _wire_session(env)
    await env.exec("true")
    await env.exec("true", cwd="/explicit")
    assert [r["cwd"] for r in session.runs] == ["/cfg", "/explicit"]

    bare = _make_env(tmp_path, agentd_path, dockerfile="FROM ubuntu:24.04\n")
    bare_session = _wire_session(bare)
    await bare.exec("true")
    assert bare_session.runs[0]["cwd"] is None  # inherit the image WORKDIR


async def test_exec_resolves_user_names_to_uids_once(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = _responder()
    env._image_env = {"PATH": "/usr/bin", "HOME": "/root"}

    await env.exec("whoami", user="agent")
    await env.exec("whoami", user="agent")
    with env.with_default_user(1000):
        await env.exec("whoami")

    lookups = [r for r in session.runs if "/etc/passwd" in r["command"]]
    assert len(lookups) == 2  # one per distinct key ("agent", "1000")
    assert lookups[0]["user"] is None  # the lookup itself runs as the daemon
    demoted = [r for r in session.runs if r["command"] == "whoami"]
    assert all(r["user"] == 1000 and r["group"] == 1000 for r in demoted)
    assert demoted[0]["env"]["HOME"] == "/home/agent"
    assert demoted[0]["env"]["USER"] == "agent"


async def test_exec_root_runs_as_the_daemon(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    await env.exec("id", user="root")
    await env.exec("id", user=0)
    assert all(r["user"] is None for r in session.runs)
    assert all("/etc/passwd" not in r["command"] for r in session.runs)


async def test_exec_numeric_uid_without_passwd_row(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = _responder(passwd="")
    await env.exec("id", user=4242)
    run = session.runs[-1]
    assert run["user"] == 4242 and run["group"] is None
    assert run["env"]["USER"] == "4242"


async def test_exec_unknown_user_name_is_an_error(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = _responder(passwd="")
    with pytest.raises(ValueError, match="Unknown user"):
        await env.exec("id", user="ghost")


@pytest.mark.parametrize(
    ("fake", "timeout_sec", "expected_code", "stderr_contains"),
    [
        (FakeExecResult(exit_code=3, stderr="bad"), None, 3, "bad"),
        (FakeExecResult(exit_code=None, signal=15), 10, 124, "timed out after 10"),
        (FakeExecResult(exit_code=None, signal=9), 10, 124, "timed out"),
        (FakeExecResult(exit_code=None, signal=15), None, 143, None),
        (FakeExecResult(exit_code=None, signal=11), 10, 139, None),
        (FakeExecResult(exit_code=0, truncated=True), None, 0, "truncated"),
    ],
)
async def test_exec_result_mapping(
    tmp_path: Path,
    agentd_path: Path,
    microvm_env: None,
    fake: FakeExecResult,
    timeout_sec: int | None,
    expected_code: int,
    stderr_contains: str | None,
) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = lambda command, kwargs: fake
    result = await env.exec("cmd", timeout_sec=timeout_sec)
    assert result.return_code == expected_code
    if stderr_contains is None:
        assert result.stderr is None
    else:
        assert stderr_contains in (result.stderr or "")


async def test_exec_without_timeout_waits_up_to_the_ceiling(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    await env.exec("sleep 1")
    assert session.handles[0].wait_timeouts == [lm._EXEC_WAIT_CEILING_SEC]


async def test_exec_client_timeout_kills_and_reports_124(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)

    class HangingHandle(FakeHandle):
        def wait_and_ack(self, timeout: float) -> FakeExecResult:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                raise microvms.TimeoutError("client deadline")
            return FakeExecResult(exit_code=None, signal=9, stdout="partial")

    def run(command: str, **kwargs: Any) -> HangingHandle:
        handle = HangingHandle(FakeExecResult())
        session.handles.append(handle)
        return handle

    session.run = run  # type: ignore[assignment]
    result = await env.exec("sleep 999", timeout_sec=5)
    assert result.return_code == 124
    assert result.stdout == "partial"
    assert session.handles[0].killed == 1


async def test_exec_start_failure_is_reported(tmp_path: Path, agentd_path: Path, microvm_env: None) -> None:
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)

    def failing_run(command: str, **kwargs: Any) -> FakeHandle:
        raise microvms.RetryableError("503 not bootstrapped")

    session.run = failing_run  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="exec start failed"):
        await env.exec("true")


class FakeChunk:
    def __init__(self, stream: str, data: bytes) -> None:
        self.stream = stream
        self.data = data

    def text(self) -> str:
        return self.data.decode("utf-8", "replace")


class FakeExit:
    pass


class FakeGap:
    pass


def _patch_stream_types(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proxy:
        OutputChunk = FakeChunk
        Exit = FakeExit

        def __getattr__(self, name: str) -> Any:
            return getattr(microvms, name)

    monkeypatch.setattr(lm, "microvms", Proxy())


async def test_exec_streams_output_to_the_callback(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_stream_types(monkeypatch)
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = lambda c, k: FakeExecResult(stdout="line 1\nline 2\n", stderr="warn\n")
    session.events = [
        FakeChunk("stdout", b"line 1\n"),
        FakeGap(),
        FakeChunk("stderr", b"warn\n"),
        FakeChunk("stdout", b"line 2\n"),
        FakeExit(),
    ]
    seen: list[tuple[str, str]] = []

    async def callback(text: str, stream: str) -> None:
        seen.append((stream, text))

    with env.scoped_output_callback(callback):
        result = await env.exec("cmd")

    assert seen == [
        ("stdout", "line 1\n"),
        ("stderr", "warn\n"),
        ("stdout", "line 2\n"),
    ]
    assert result.stdout == "line 1\nline 2\n"
    handle = session.handles[0]
    assert handle.acked == 1 and handle.wait_timeouts == []


async def test_exec_stream_without_exit_falls_back_to_polling(
    tmp_path: Path, agentd_path: Path, microvm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_stream_types(monkeypatch)
    env = _make_env(tmp_path, agentd_path)
    session = _wire_session(env)
    session.responder = lambda c, k: FakeExecResult(stdout="all\n")
    session.events = [FakeChunk("stdout", b"partial")]  # cut connection, no Exit
    seen: list[str] = []

    async def callback(text: str, stream: str) -> None:
        seen.append(text)

    with env.scoped_output_callback(callback):
        result = await env.exec("cmd")

    assert seen == ["partial"]
    assert result.stdout == "all\n"
    handle = session.handles[0]
    assert handle.wait_timeouts == [lm._EXEC_WAIT_CEILING_SEC]
    assert handle.acked == 1


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


def test_parse_environ_drops_daemon_knobs() -> None:
    assert lm.parse_environ("PATH=/bin\nAGENTD_PORT=9000\nEMPTY=\nnoequals\nA=b=c\n") == {
        "PATH": "/bin",
        "EMPTY": "",
        "A": "b=c",
    }
