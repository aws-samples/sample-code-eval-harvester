"""Harbor's AWS Lambda MicroVM environment, backed by the published SDK.

Install with `uv sync --group eval` and load
`harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`. Each trial owns one
ARM64 MicroVM. The SDK owns daemon provisioning, image build context and reuse,
guest identity/environment resolution, and streaming command completion. Every
SDK call that reaches AWS or the VM is awaited through its `_async` twin, so the
network work runs on the SDK's own runtime and Harbor's event loop never blocks.

Required settings are MICROVM_BUCKET, MICROVM_BUILD_ROLE_ARN and AWS_REGION
(or the corresponding constructor options). An optional guest execution role
remains accessible to task code. Harbor/model integrations can also forward
host credentials; see eval/README.md for the Bedrock integration's constraints.

Only public-network, single-container Linux tasks are supported. Omitting an
egress connector does not block internet access. CPU and memory select a size
class, not a hard limit. The platform bounds a VM's lifetime at eight hours.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, override

from harbor.environments.base import BaseEnvironment, ExecResult, OutputCallback
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.environments.definition import (
    effective_exec_cwd,
    parse_dockerfile_workdir,
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.environments.tar_transfer import extract_dir_from_bytes, pack_dir_to_bytes
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.utils.optional_import import MissingExtraError


class _LambdaMicrovmsEnvType(str):
    """Keep Harbor 0.22's .value contract without patching its built-in registry."""

    @property
    def value(self) -> str:
        return str(self)


_LAMBDA_MICROVMS = _LambdaMicrovmsEnvType("lambda-microvms")

# Optional dependencies live in the eval group, not in the dependency-free CLI.
microvms: Any = None
boto3: Any = None
try:
    import boto3 as _boto3
    import microvms as _microvms

    microvms = _microvms
    boto3 = _boto3
    _HAS_MICROVMS = True
except ImportError:
    _HAS_MICROVMS = False

_EXTRA = "lambda-microvms"
_SERVICE_NAME = "lambda-microvms"
_ENV_BUCKET = "MICROVM_BUCKET"
_ENV_BUILD_ROLE_ARN = "MICROVM_BUILD_ROLE_ARN"
_ENV_EXECUTION_ROLE_ARN = "MICROVM_EXECUTION_ROLE_ARN"
_ENV_AGENTD_BINARY = "HARBOR_AGENTD_BINARY"
_DOCKERFILE_ENTRY = "Dockerfile"
_MANAGED_BASE_IMAGE_NAME = "al2023-1"
_MAX_DURATION_SEC = 28_800
_DEFAULT_READY_TIMEOUT_SEC = 300.0
_IMAGE_NAME_MAX_LEN = 64
_IMAGE_NAME_HASH_LEN = 12
_OUTPUT_CALLBACK_TIMEOUT_SEC = 30.0


def sanitize_image_name(raw: str, *, max_len: int = _IMAGE_NAME_MAX_LEN) -> str:
    """Fit *raw* to the platform's ImageName rule: ``[a-zA-Z0-9-_]+``, ≤64."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-_")
    if not safe:
        safe = "harbor"
    if len(safe) <= max_len:
        return safe
    digest = hashlib.sha256(raw.encode()).hexdigest()[:_IMAGE_NAME_HASH_LEN]
    head = safe[: max_len - _IMAGE_NAME_HASH_LEN - 1].rstrip("-_")
    return f"{head}-{digest}"


class LambdaMicrovmsEnvironment(BaseEnvironment):
    """One Harbor trial in an SDK-managed Lambda MicroVM."""

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        s3_bucket: str | None = None,
        build_role_arn: str | None = None,
        execution_role_arn: str | None = None,
        region: str | None = None,
        agentd_binary: str | Path | None = None,
        image_name: str | None = None,
        image_name_prefix: str | None = None,
        base_image_name: str = _MANAGED_BASE_IMAGE_NAME,
        s3_key_prefix: str = "harbor/lambda-microvms",
        max_duration_sec: int = _MAX_DURATION_SEC,
        max_idle_sec: int | None = None,
        ready_timeout_sec: float = _DEFAULT_READY_TIMEOUT_SEC,
        **kwargs: Any,
    ) -> None:
        if not _HAS_MICROVMS:
            raise MissingExtraError(package="microvms", extra=_EXTRA)

        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            **kwargs,
        )

        self.s3_bucket = s3_bucket or os.environ.get(_ENV_BUCKET)
        if not self.s3_bucket:
            raise ValueError(
                "Lambda MicroVMs environment needs an S3 bucket for the image "
                f"build artifact: pass the 's3_bucket' kwarg or set {_ENV_BUCKET}."
            )
        self.build_role_arn = build_role_arn or os.environ.get(_ENV_BUILD_ROLE_ARN)
        if not self.build_role_arn:
            raise ValueError(
                "Lambda MicroVMs environment needs the IAM role the platform "
                "assumes to build the image: pass the 'build_role_arn' kwarg or "
                f"set {_ENV_BUILD_ROLE_ARN}."
            )
        self.execution_role_arn = execution_role_arn or os.environ.get(_ENV_EXECUTION_ROLE_ARN)
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not self.region:
            raise ValueError(
                "Lambda MicroVMs environment could not resolve an AWS region. Pass the 'region' kwarg or set AWS_REGION."
            )
        self._microvms_region = microvms.Region.parse(self.region)

        self._agentd_binary_path = (
            Path(agentd_binary)
            if agentd_binary
            else (Path(os.environ[_ENV_AGENTD_BINARY]) if os.environ.get(_ENV_AGENTD_BINARY) else None)
        )
        self.base_image_name = base_image_name
        self.s3_key_prefix = s3_key_prefix.strip("/")
        if not 1 <= max_duration_sec <= _MAX_DURATION_SEC:
            raise ValueError(
                f"max_duration_sec={max_duration_sec} is outside 1..{_MAX_DURATION_SEC}; "
                "eight hours is the platform ceiling on one MicroVM."
            )
        self.max_duration_sec = max_duration_sec
        # Idle suspension defaults off for the VM's whole life: Harbor's own
        # exec polling is the inbound traffic the idle timer measures, and a
        # host-side gap between phases must not freeze a trial. The duration
        # ceiling still bounds an orphaned VM.
        self.max_idle_sec = max_idle_sec if max_idle_sec is not None else max_duration_sec
        self.ready_timeout_sec = ready_timeout_sec
        if image_name and image_name_prefix:
            raise ValueError("Pass only image_name_prefix; image_name is its deprecated alias.")
        self.image_name_prefix = sanitize_image_name(image_name_prefix or image_name or f"harbor-{environment_name}", max_len=51)
        if image_name:
            self.logger.warning(
                "image_name is deprecated: use image_name_prefix. The SDK appends a "
                "content hash, so this option no longer selects an exact image name."
            )

        self._use_prebuilt = should_use_prebuilt_docker_image(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            force_build=False,
        )
        self._dockerfile_workdir = (
            None if self._use_prebuilt else parse_dockerfile_workdir(self.environment_dir / _DOCKERFILE_ENTRY)
        )

        # Harbor and the optional native SDK are untyped to this package (see pyproject.toml).
        self._sandbox: Any | None = None
        self._session: Any | None = None
        self._keepalive: Any | None = None
        self._microvm_id: str | None = None
        self._agentd_bytes: bytes | None = None
        self._image_name_cache: str | None = None
        self._numeric_groups: dict[int, int | None] = {}
        self._transition: asyncio.Task[Any] | None = None
        self._stop_lock = asyncio.Lock()

    # ── contract metadata ────────────────────────────────────────────────

    @staticmethod
    @override
    def type() -> _LambdaMicrovmsEnvType:
        return _LAMBDA_MICROVMS

    @classmethod
    @override
    def preflight(cls) -> None:
        if not _HAS_MICROVMS:
            raise MissingExtraError(package="microvms", extra=_EXTRA)
        session = boto3.session.Session()
        if _SERVICE_NAME not in session.get_available_services():
            raise SystemExit(
                "The installed boto3 does not know the 'lambda-microvms' service. Upgrade with: pip install 'boto3>=1.43.35'"
            )
        if session.get_credentials() is None:
            raise SystemExit(
                "Lambda MicroVMs requires AWS credentials. Configure them with "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or an "
                "instance role and try again."
            )

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # A MicroVM size class is a baseline with a fixed 4x ceiling, so the
        # task's cpus/memory select a class rather than set a hard limit.
        return EnvironmentResourceCapabilities(cpu_request=True, memory_request=True)

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        # Omitting INTERNET_EGRESS does not seal outbound traffic. The SDK cannot
        # verify VPC routes either, so Harbor must refuse no-network and allowlists.
        return EnvironmentCapabilities()

    @override
    def _validate_definition(self) -> None:
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise ValueError(
                "Lambda MicroVMs environment does not support Docker Compose task "
                "environments; the MicroVM runs a single container."
            )
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            extra_docker_compose_paths=self.extra_docker_compose_paths,
        )

    # ── SDK image and VM lifecycle ──────────────────────────────────────

    async def _await_transition(self, operation: Coroutine[Any, Any, Any]) -> Any:
        """Await a build or launch that a cancelled caller must not lose track of.

        The SDK lets a cancelled lifecycle transition run to completion, so a
        cancelled launch still allocates its VM. The task kept here is what stop()
        waits for before it terminates, so that VM cannot escape cleanup.
        """
        self._transition = asyncio.ensure_future(operation)
        return await asyncio.shield(self._transition)

    async def _settle_transition(self) -> None:
        """Wait for an in-flight build or launch to finish, whatever its outcome."""
        transition, self._transition = self._transition, None
        if transition is None:
            return
        await asyncio.wait([transition])
        if not transition.cancelled() and transition.exception() is not None:
            self.logger.debug(f"MicroVM transition before teardown failed: {transition.exception()}")

    async def _sandbox_or_create(self) -> Any:
        if self._sandbox is None:
            self._sandbox = await microvms.Sandbox.create_async(self._microvms_region)
        return self._sandbox

    def size_class(self) -> Any:
        """Select the smallest baseline that covers Harbor's resource requests."""
        return microvms.SizeClass.from_request(cpus=self._effective_cpus, memory_mib=self._effective_memory_mb)

    def harness_dockerfile(self) -> str:
        """Wrap the task Dockerfile with the SDK's validated daemon stanza."""
        task = (
            f"FROM {self.task_env_config.docker_image}\n"
            if self._use_prebuilt
            else (self.environment_dir / _DOCKERFILE_ENTRY).read_text()
        )
        return microvms.wrap_dockerfile(task)

    def base_image(self) -> Any:
        """Pair the managed base with the task's FROM, including a digest pin."""
        base = microvms.BaseImage.from_dockerfile(self.harness_dockerfile())
        return microvms.BaseImage(self.base_image_name, base.docker_ref, self._dockerfile_workdir or "")

    async def _load_agentd(self) -> bytes:
        """Use the SDK's matching, verified download or an explicit ARM64 binary."""
        if self._agentd_bytes is None:
            self._agentd_bytes = await microvms.provision_agentd_async(binary=self._agentd_binary_path)
        return self._agentd_bytes

    @property
    def image_name(self) -> str:
        """The ensured image's name, or its configured prefix before building."""
        return self._image_name_cache or self.image_name_prefix

    async def _ensure_image(self, force_build: bool) -> tuple[str, str]:
        # Provisioning and local checks precede creation of the AWS client.
        binary = await self._load_agentd()
        dockerfile = self.harness_dockerfile()
        base, size = self.base_image(), self.size_class()
        sandbox = await self._sandbox_or_create()
        ensured = await self._await_transition(
            sandbox.ensure_image_async(
                name_prefix=self.image_name_prefix,
                binary=binary,
                dockerfile=dockerfile,
                context_dir=None if self._use_prebuilt else str(self.environment_dir),
                s3_bucket=self.s3_bucket,
                s3_key_prefix=self.s3_key_prefix,
                build_role_arn=self.build_role_arn,
                size=size,
                base_image=base,
                force=force_build,
                tags={
                    "harbor:environment": _LAMBDA_MICROVMS.value,
                    "harbor:task": sanitize_image_name(self.environment_name),
                },
                wait_timeout=float(self.task_env_config.build_timeout_sec),
            )
        )
        self._image_name_cache = ensured.image.name
        for warning in ensured.warnings:
            self.logger.warning(warning)
        return str(ensured.image.identifier), str(ensured.image.version)

    async def _run_and_record(self, sandbox: Any, image_arn: str, image_version: str) -> Any:
        # run_async waits for the platform and the daemon; a second ready poll is redundant.
        session = await sandbox.run_async(
            image_identifier=image_arn,
            image_version=image_version,
            execution_role_arn=self.execution_role_arn,
            egress=True,
            max_idle_sec=self.max_idle_sec,
            max_duration_sec=self.max_duration_sec,
            ready_timeout=self.ready_timeout_sec,
            token_scope=self.session_id,
        )
        # A Sandbox property waits for the sandbox's lock, which an exec holds while it runs.
        # Read the id now, before any exec, so teardown never reads it on the event loop.
        self._microvm_id = sandbox.microvm_id
        return session

    async def _launch(self, image_arn: str, image_version: str) -> None:
        sandbox = await self._sandbox_or_create()
        self._session = await self._await_transition(self._run_and_record(sandbox, image_arn, image_version))
        self._keepalive = self._session.keep_awake(while_busy=False)

    @override
    async def start(self, force_build: bool) -> None:
        try:
            image_arn, image_version = await self._ensure_image(force_build)
            await self._launch(image_arn, image_version)
            dirs = list(
                dict.fromkeys(
                    [
                        str(EnvironmentPaths.agent_dir),
                        str(EnvironmentPaths.verifier_dir),
                        *self._mount_targets(writable_only=True),
                    ]
                )
            )
            await self.ensure_dirs(dirs)
            await self._upload_environment_dir_after_start()
        except BaseException:
            # Includes Harbor cancelling a slow build/launch. Teardown waits for that
            # transition to finish, so a VM allocated after cancellation cannot escape cleanup.
            await self.stop(delete=True)
            raise

    @override
    async def stop(self, delete: bool) -> None:
        await asyncio.shield(self._stop(delete))

    async def _stop(self, delete: bool) -> None:
        async with self._stop_lock:
            keepalive, self._keepalive = self._keepalive, None
            self._session = None
            if keepalive is not None:
                try:
                    await keepalive.stop_async()
                except Exception as exc:
                    self.logger.warning(f"MicroVM keepalive stop failed: {exc}")
            await self._settle_transition()
            sandbox = self._sandbox
            if sandbox is None:
                return
            # With no recorded id no session exists, so no exec holds the sandbox's lock.
            microvm_id = self._microvm_id if self._microvm_id is not None else sandbox.microvm_id
            if not delete:
                if microvm_id:
                    state = await sandbox.suspend_async()
                    self.logger.info(f"Suspended MicroVM {microvm_id} (delete=False, state={state})")
                return
            report = await sandbox.terminate_async(wait_for_terminated=True)
            if (
                report.leaked
                or report.failures
                or report.undeleted
                or (microvm_id is not None and report.lifecycle != "TERMINATED")
            ):
                # Retain the native handle so another stop can retry.
                raise RuntimeError(
                    f"MicroVM {microvm_id} teardown failed: lifecycle={report.lifecycle} "
                    f"undeleted={report.undeleted} failures={report.failures}"
                )
            self._sandbox = None
            self._microvm_id = None

    # ── command execution ───────────────────────────────────────────────

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("MicroVM is not running; call start() first.")
        return self._session

    @staticmethod
    async def _deliver_output(callback: OutputCallback, chunks: asyncio.Queue[Any]) -> None:
        """Hand each chunk to Harbor's callback in arrival order, until the None sentinel."""
        while (chunk := await chunks.get()) is not None:
            # A stalled consumer must not hold the trial indefinitely.
            await asyncio.wait_for(callback(chunk.text(), chunk.stream), timeout=_OUTPUT_CALLBACK_TIMEOUT_SEC)

    async def _run_to_completion(self, session: Any, command: str, **options: Any) -> Any:
        """Run one command, streaming its output to Harbor's callback when one is set.

        The SDK calls on_output on the event loop's thread and needs a plain function,
        while Harbor's callback is a coroutine. Chunks go through a queue to a delivery
        task; when that task fails, the next chunk raises its error, which stops the
        SDK's delivery, and the SDK re-raises it once the exec is acknowledged.
        """
        callback = self._output_callback()
        if callback is None:
            return await session.run_to_completion_async(command, **options)
        chunks: asyncio.Queue[Any] = asyncio.Queue()
        delivery = asyncio.create_task(self._deliver_output(callback, chunks))

        def on_output(chunk: Any) -> None:
            if delivery.done():
                delivery.result()
            chunks.put_nowait(chunk)

        try:
            result = await session.run_to_completion_async(command, on_output=on_output, **options)
        except BaseException:
            delivery.cancel()
            raise
        chunks.put_nowait(None)
        await delivery
        return result

    async def _numeric_user_group(self, session: Any, user: str | int | None) -> int | None:
        # The SDK resolves named users' primary groups, but deliberately leaves
        # numeric users in the daemon's group. Preserve Harbor's passwd gid for
        # numeric UIDs too, without duplicating the SDK's identity/env machinery.
        if user is None or not str(user).isdigit():
            return None
        uid = int(user)
        if uid not in self._numeric_groups:
            try:
                passwd = await session.download_file_async("/etc/passwd")
            except microvms.MicrovmError as exc:
                if not self._is_not_found(exc):
                    raise
                passwd = b""
            self._numeric_groups[uid] = next(
                (
                    int(fields[3])
                    for line in passwd.decode("utf-8", "replace").splitlines()
                    if len(fields := line.split(":")) == 7
                    and fields[2].isdigit()
                    and int(fields[2]) == uid
                    and fields[3].isdigit()
                ),
                None,
            )
        return self._numeric_groups[uid]

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        session = self._require_session()
        run_as = self._resolve_user(user)
        try:
            group = await self._numeric_user_group(session, run_as)
            result = await self._run_to_completion(
                session,
                command,
                shell="bash",  # nosec B604: selects the guest shell over SDK HTTPS, no host shell
                inherit_image_env=True,
                cwd=effective_exec_cwd(cwd, self.task_env_config.workdir, self._dockerfile_workdir),
                env=self._merge_env(env),
                user="root" if run_as is None else run_as,
                group=group,
                timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
                exec_id=uuid.uuid4().hex,
            )
        except asyncio.CancelledError:
            # Cancelling the SDK call stops its drive but leaves the exec running in the
            # VM, and a one-shot kill can miss an exec not yet registered. This trial owns
            # the VM, so end it before returning cancellation.
            await self.stop(delete=True)
            raise
        except microvms.MicrovmError as exc:
            raise RuntimeError(f"MicroVM exec failed: {exc}") from exc
        annotations = [f"[microvms] {note}" for note in result.notes]
        code = result.posix_exit_code
        if result.timed_out or result.synthesized:
            code = 124
            duration = f" after {timeout_sec} seconds" if timeout_sec is not None else ""
            annotations.append(f"Command timed out{duration}")
        stderr = "\n".join(part for part in [result.stderr, *annotations] if part)
        return ExecResult(
            stdout=result.stdout or None,
            stderr=stderr or None,
            return_code=-1 if code is None else code,
        )

    # ── file transfer ────────────────────────────────────────────────────

    @staticmethod
    def _is_not_found(exc: BaseException) -> bool:
        return getattr(exc, "wire_kind", None) == "NotFound"

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        session = self._require_session()
        source = Path(source_path)
        mode = f"{source.stat().st_mode & 0o777:04o}"
        try:
            await session.upload_file_async(target_path, source.read_bytes(), mode=mode)
        except microvms.MicrovmError as exc:
            raise RuntimeError(f"Failed to upload {source} to MicroVM {target_path}: {exc}") from exc

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        session = self._require_session()
        source = Path(source_dir)
        if not source.is_dir():
            self.logger.warning(f"No files to upload from {source}")
            return
        # Uncompressed: the daemon's tar route extracts a plain tar stream.
        archive = await asyncio.to_thread(pack_dir_to_bytes, source, compress=False)
        try:
            await session.upload_tar_async(target_dir, archive.getvalue())
        except microvms.MicrovmError as exc:
            raise RuntimeError(f"Failed to upload directory {source} to MicroVM {target_dir}: {exc}") from exc

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        session = self._require_session()
        try:
            data = await session.download_file_async(source_path)
        except microvms.MicrovmError as exc:
            if self._is_not_found(exc):
                raise FileNotFoundError(f"MicroVM file not found: {source_path}") from exc
            raise RuntimeError(f"Failed to download MicroVM file {source_path}: {exc}") from exc
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        session = self._require_session()
        try:
            data = await session.download_tar_async(source_dir)
        except microvms.MicrovmError as exc:
            if self._is_not_found(exc):
                raise FileNotFoundError(f"MicroVM directory not found: {source_dir}") from exc
            raise RuntimeError(f"Failed to download MicroVM directory {source_dir}: {exc}") from exc
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)
