"""AWS Lambda MicroVMs environment for Harbor, driven through microvms-agentd.

Each trial runs inside a Firecracker-isolated AWS Lambda MicroVM. The platform
builds the task's Dockerfile server-side into a snapshot-backed image and
launches one VM per trial from that snapshot. Lambda MicroVMs expose a
per-instance HTTPS endpoint but no exec or file API, so the image carries
``agentd`` (https://github.com/theagenticguy/microvms-agentd), a small daemon
that supplies both, and this provider is a thin client over the published
``microvms`` Python bindings that talk to it.

The bindings own everything that used to need a hand-rolled daemon and client:
the per-VM agent token delivered through ``runHookPayload`` (never baked into
the shared snapshot), proxy-token minting and refresh inside every request,
idempotent detached exec with caller-minted ids, live output streaming with
resume, and confined tar extraction.

Requires:
    - ``pip install 'harbor[lambda-microvms]'``
    - AWS credentials with lambda-microvms, S3, STS, and iam:PassRole access
    - an S3 bucket for the build artifact (kwarg ``s3_bucket`` or
      ``MICROVM_BUCKET``) and an IAM role the platform assumes during the
      image build (kwarg ``build_role_arn`` or ``MICROVM_BUILD_ROLE_ARN``).
      These are the same variables the ``microvm`` CLI reads, so a machine
      set up for that CLI is set up for this provider.

Constraints (from the Lambda MicroVMs platform):
    - ARM64 only: task images must build for linux/arm64.
    - One VM lives at most 8 hours.
    - Network policy is fixed at launch: ``public`` requests the managed
      INTERNET_EGRESS connector, ``no-network`` omits it. Allowlists are not
      supported.
    - Single container: Docker Compose task environments are rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import signal
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import httpx
import platformdirs

from harbor.environments.base import BaseEnvironment, ExecResult, OutputCallback
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
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

# ─── eval-harvest: the project's own Lambda MicroVMs environment (MIT-0) ────────
# This module is the project's own environment. It is installed as the `harvest-env` distribution so
# an unmodified `harbor==0.22.0` can load it by import path (H-3's no-fork design) — rather than a
# /tmp shim or a PYTHONPATH overlay of Harbor's own source tree, which could never work: a regular
# `harbor` package cannot be partially overlaid by a same-named namespace portion. No Harbor source
# is vendored and the change was not upstreamed as a stored patch; a Harbor PR, if ever made, is a
# `git diff` generated at that time.
#
# The one intentional divergence from a hypothetical in-Harbor version is the module-local
# environment-type constant below. Harbor 0.22.0's environment-type enum ships no `lambda-microvms`
# member, and this repo patches neither Harbor nor site-packages, so the type is a local `str` with a
# `.value` rather than an enum member. The module therefore depends on nothing from Harbor's enum,
# and a Harbor upgrade cannot break this import for a reason that has nothing to do with MicroVMs.


class _LambdaMicrovmsEnvType(str):
    """The module-local environment type: a `str` carrying `.value`, so the module needs no Harbor enum member."""

    @property
    def value(self) -> str:
        return str(self)


_LAMBDA_MICROVMS = _LambdaMicrovmsEnvType("lambda-microvms")


microvms: Any = None
boto3: Any = None
BotoCoreError: type[Exception] = Exception
ClientError: type[Exception] = Exception

try:
    import boto3 as _boto3
    import microvms as _microvms
    from botocore.exceptions import (
        BotoCoreError as _BotoCoreError,
        ClientError as _ClientError,
    )

    microvms = _microvms
    boto3 = _boto3
    BotoCoreError = _BotoCoreError
    ClientError = _ClientError
    _HAS_MICROVMS = True
except ImportError:
    _HAS_MICROVMS = False

_EXTRA = "lambda-microvms"
_SERVICE_NAME = "lambda-microvms"

# Environment variables shared with the `microvm` CLI (microvms-agentd README).
_ENV_BUCKET = "MICROVM_BUCKET"
_ENV_BUILD_ROLE_ARN = "MICROVM_BUILD_ROLE_ARN"
_ENV_EXECUTION_ROLE_ARN = "MICROVM_EXECUTION_ROLE_ARN"
_ENV_AGENTD_BINARY = "HARBOR_AGENTD_BINARY"

# The daemon listens here by default; the bindings send hooks.port=9000 and
# refuse a Dockerfile whose AGENTD_PORT disagrees, so this is not configurable.
_AGENT_PORT = 9000
# Zip entry names the platform build looks for, matching microvms-core's
# artifact layout (`COPY agentd /agentd` in the appended stanza).
_DOCKERFILE_ENTRY = "Dockerfile"
_AGENTD_ENTRY = "agentd"
_AGENTD_MODE = 0o755
_MANAGED_BASE_IMAGE_NAME = "al2023-1"
_AGENTD_RELEASE_URL = "https://github.com/theagenticguy/microvms-agentd/releases/download/v{version}/agentd"
# ELF magic plus e_machine == EM_AARCH64 (0xB7) in the little-endian header.
_ELF_MAGIC = b"\x7fELF"
_ELF_MACHINE_OFFSET = 18
_EM_AARCH64 = 0xB7

_MAX_DURATION_SEC = 28_800  # the platform's eight-hour ceiling on one VM
_DEFAULT_READY_TIMEOUT_SEC = 300.0
_DEFAULT_TRANSFER_TIMEOUT_SEC = 600.0
# Client-side ceiling on waiting for an exec Harbor gave no timeout. The VM
# itself cannot outlive _MAX_DURATION_SEC, so this only guards a hung poll.
_EXEC_WAIT_CEILING_SEC = 2 * _MAX_DURATION_SEC
# Grace the daemon gets to SIGTERM/SIGKILL a timed-out child before the client
# gives up on its own clock.
_EXEC_TIMEOUT_GRACE_SEC = 60.0
_IMAGE_POLL_INTERVAL_SEC = 10.0
_IMAGE_DELETE_TIMEOUT_SEC = 300.0
_ACTIVE_IMAGE_STATES = frozenset({"CREATED", "UPDATED"})
_PENDING_IMAGE_STATES = frozenset({"CREATING", "UPDATING"})
_TIMEOUT_SIGNALS = frozenset({int(signal.SIGTERM), int(signal.SIGKILL)})
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# The daemon runs as root and starts children from an empty environment;
# these are what `docker exec` would have given a root shell.
_ROOT_ENV = {"HOME": "/root", "USER": "root", "LOGNAME": "root"}
_ARTIFACT_IGNORE_NAMES = frozenset({".DS_Store", ".git", "__pycache__"})
_IMAGE_NAME_MAX_LEN = 64
_IMAGE_NAME_HASH_LEN = 12

_HARNESS_STANZA = (
    "\n# --- Harbor: microvms-agentd harness (appended) ---\n"
    "USER root\n"
    f"COPY {_AGENTD_ENTRY} /agentd\n"
    f"RUN chmod {_AGENTD_MODE:04o} /agentd\n"
    f"ENV AGENTD_PORT={_AGENT_PORT}\n"
    "ENV AGENTD_LOG=info\n"
    f"EXPOSE {_AGENT_PORT}\n"
    "ENTRYPOINT []\n"
    'CMD ["/agentd"]\n'
)


@dataclass(frozen=True)
class _Identity:
    """One /etc/passwd row inside the VM, resolved once per user key."""

    name: str
    uid: int
    gid: int | None
    home: str | None

    def env(self) -> dict[str, str]:
        env = {"USER": self.name, "LOGNAME": self.name}
        if self.home:
            env["HOME"] = self.home
        return env


def dockerfile_from_ref(dockerfile: str) -> str | None:
    """The image ref in a Dockerfile's first ``FROM``, or None when it has none.

    Mirrors microvms-core's ``dockerfile_from_ref``: loose on case and
    whitespace, skips ``--platform=`` style flags, ignores ``AS name``.
    """
    for line in dockerfile.splitlines():
        words = line.split()
        if not words or words[0].upper() != "FROM":
            continue
        for word in words[1:]:
            if not word.startswith("--"):
                return word
        return None
    return None


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


def require_aarch64_elf(data: bytes, *, source: str) -> None:
    """Refuse a daemon binary the ARM64-only platform could not run.

    The failure this prevents surfaces one build cycle later as a run-hook
    timeout that names nothing about architecture.
    """
    if len(data) < _ELF_MACHINE_OFFSET + 2 or not data.startswith(_ELF_MAGIC):
        raise ValueError(f"agentd binary at {source} is not an ELF executable.")
    machine = int.from_bytes(
        data[_ELF_MACHINE_OFFSET : _ELF_MACHINE_OFFSET + 2], "little"
    )
    if machine != _EM_AARCH64:
        raise ValueError(
            f"agentd binary at {source} is not aarch64 (e_machine={machine:#x}). "
            "Lambda MicroVMs are ARM64-only; use the `agentd` asset from a "
            "microvms-agentd release or build with "
            "`cargo build --target aarch64-unknown-linux-musl`."
        )


def parse_environ(raw: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (as printed from ``/proc/<pid>/environ``)."""
    env: dict[str, str] = {}
    for line in raw.split("\n"):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key and not key.startswith("AGENTD_"):
            env[key] = value
    return env


class LambdaMicrovmsEnvironment(BaseEnvironment):
    """AWS Lambda MicroVM environment backed by the microvms-agentd daemon."""

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
        self.execution_role_arn = execution_role_arn or os.environ.get(
            _ENV_EXECUTION_ROLE_ARN
        )
        self.region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        if not self.region:
            raise ValueError(
                "Lambda MicroVMs environment could not resolve an AWS region. "
                "Pass the 'region' kwarg or set AWS_REGION."
            )
        self._microvms_region = microvms.Region.parse(self.region)

        self._agentd_binary_path = (
            Path(agentd_binary)
            if agentd_binary
            else (
                Path(os.environ[_ENV_AGENTD_BINARY])
                if os.environ.get(_ENV_AGENTD_BINARY)
                else None
            )
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
        self.max_idle_sec = (
            max_idle_sec if max_idle_sec is not None else max_duration_sec
        )
        self.ready_timeout_sec = ready_timeout_sec
        self._explicit_image_name = image_name

        self._use_prebuilt = should_use_prebuilt_docker_image(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            force_build=False,
        )
        self._dockerfile_workdir = (
            None
            if self._use_prebuilt
            else parse_dockerfile_workdir(self.environment_dir / _DOCKERFILE_ENTRY)
        )

        self._boto_session: Any | None = None
        self._api_client: Any | None = None
        self._s3_client: Any | None = None
        self._account_id: str | None = None
        self._agentd_bytes: bytes | None = None
        self._sandbox: Any | None = None
        self._session: Any | None = None
        self._image_env: dict[str, str] = {"PATH": _DEFAULT_PATH}
        self._identities: dict[str, _Identity | None] = {}
        self._image_name_cache: str | None = None

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
                "The installed boto3 does not know the 'lambda-microvms' service. "
                "Upgrade with: pip install 'boto3>=1.43.35'"
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
        # no-network omits the egress connector at launch. Allowlists would
        # need a customer VPC egress connector with a firewall, which this
        # provider does not manage.
        return EnvironmentCapabilities(disable_internet=True)

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

    # ── AWS clients ──────────────────────────────────────────────────────

    def _boto(self) -> Any:
        if self._boto_session is None:
            self._boto_session = boto3.session.Session(region_name=self.region)
        return self._boto_session

    def _api(self) -> Any:
        if self._api_client is None:
            self._api_client = self._boto().client(_SERVICE_NAME)
        return self._api_client

    def _s3(self) -> Any:
        if self._s3_client is None:
            self._s3_client = self._boto().client("s3")
        return self._s3_client

    async def _image_arn(self) -> str:
        """Full image ARN; the platform rejects bare image names."""
        if self._account_id is None:
            response = await asyncio.to_thread(
                self._boto().client("sts").get_caller_identity
            )
            self._account_id = response["Account"]
        return f"arn:aws:lambda:{self.region}:{self._account_id}:microvm-image:{self.image_name}"

    # ── image definition ─────────────────────────────────────────────────

    def size_class(self) -> Any:
        """The smallest size class whose baseline covers the task's request.

        ``cpus``/``memory_mb`` are requests: the platform bills the baseline and
        provisions a fixed 4x ceiling, so a task asking for 2 CPUs / 4 GiB gets
        the 4096 MiB class (2 vCPU baseline, 16 GiB / 8 vCPU ceiling).
        """
        cpus = self._effective_cpus or 0
        memory_mb = self._effective_memory_mb or 0
        if not cpus and not memory_mb:
            return microvms.SizeClass.default_class()
        classes = microvms.SizeClass.all()
        for size in classes:
            if size.baseline_mib >= memory_mb and size.baseline_vcpu >= cpus:
                return size
        largest = classes[-1]
        raise ValueError(
            f"Task requests {cpus} CPU(s) / {memory_mb} MiB, but the largest Lambda "
            f"MicroVM class is {largest.describe()}. Reduce the task's resource "
            "request or use a different environment type."
        )

    def harness_dockerfile(self) -> str:
        """The task Dockerfile (or ``FROM docker_image``) with agentd appended.

        ``USER root`` restores the daemon's privileges when a task Dockerfile
        ends on a non-root user; per-exec demotion is the daemon's job.
        ``ENTRYPOINT []`` plus ``CMD ["/agentd"]`` is the invariant the platform
        trust boundary rests on: no task workload runs before the run hook
        delivers the agent token.
        """
        if self._use_prebuilt:
            base = f"FROM {self.task_env_config.docker_image}\n"
        else:
            base = (self.environment_dir / _DOCKERFILE_ENTRY).read_text()
            if not base.endswith("\n"):
                base += "\n"
        return base + _HARNESS_STANZA

    def base_image(self) -> Any:
        """The ``BaseImage`` whose ``docker_ref`` is the task's own ``FROM``.

        The bindings refuse a Dockerfile whose first ``FROM`` disagrees with the
        base image they were handed, because their default Dockerfile derives
        one from the other. Harbor task Dockerfiles choose their own base, so
        the pairing runs the other way: the managed platform base stays
        ``baseImageArn`` and the task's ``FROM`` becomes the ref it pairs with.
        """
        managed = microvms.BaseImage.al2023()
        from_ref = dockerfile_from_ref(self.harness_dockerfile())
        if (
            self.base_image_name == managed.name
            and from_ref is not None
            and from_ref.split("@sha256:")[0] == managed.docker_ref
        ):
            return managed
        return microvms.BaseImage(
            self.base_image_name,
            from_ref or managed.docker_ref,
            self._dockerfile_workdir or "",
        )

    def _load_agentd(self) -> bytes:
        """The aarch64 daemon binary baked into the image.

        Resolution order: the ``agentd_binary`` kwarg, ``HARBOR_AGENTD_BINARY``,
        then the ``agentd`` asset of the microvms-agentd release matching the
        installed ``microvms`` wheel, cached under the Harbor cache directory.
        """
        if self._agentd_bytes is not None:
            return self._agentd_bytes
        if self._agentd_binary_path is not None:
            source = str(self._agentd_binary_path)
            data = self._agentd_binary_path.read_bytes()
        else:
            version = microvms.core_version()
            cache_path = (
                platformdirs.user_cache_path("harbor")
                / "lambda_microvms"
                / f"agentd-v{version}"
            )
            source = str(cache_path)
            if not cache_path.exists():
                url = _AGENTD_RELEASE_URL.format(version=version)
                self.logger.info(f"Downloading agentd v{version} from {url}")
                response = httpx.get(url, follow_redirects=True, timeout=120.0)
                response.raise_for_status()
                require_aarch64_elf(response.content, source=url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                tmp_path.write_bytes(response.content)
                tmp_path.replace(cache_path)
            data = cache_path.read_bytes()
        require_aarch64_elf(data, source=source)
        self._agentd_bytes = data
        return data

    @property
    def image_name(self) -> str:
        """Content-addressed image name, or the caller's explicit override.

        The identity covers every input that changes the built snapshot: the
        task environment, the daemon binary, the appended stanza, the base
        image, and the size class. A caller-supplied ``image_name`` owns
        uniqueness and compatibility.
        """
        if self._explicit_image_name:
            return self._explicit_image_name
        if self._image_name_cache is not None:
            return self._image_name_cache
        identity = {
            "schema_version": 1,
            "environment_id": self.environment_id,
            "agentd_sha256": hashlib.sha256(self._load_agentd()).hexdigest(),
            "dockerfile_sha256": hashlib.sha256(
                self.harness_dockerfile().encode()
            ).hexdigest(),
            "base_image": self.base_image_name,
            "baseline_mib": self.size_class().baseline_mib,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:_IMAGE_NAME_HASH_LEN]
        self._image_name_cache = sanitize_image_name(
            f"harbor-{self.environment_name}-{digest}"
        )
        return self._image_name_cache

    def build_artifact(self) -> bytes:
        """The zip the platform build receives: Dockerfile, agentd, task files.

        microvms-core's own ``build_artifact`` carries exactly two entries, but
        the S3 upload is the caller's, so Harbor adds the task's build context
        alongside them; ``COPY`` instructions in task Dockerfiles depend on it.
        The daemon entry carries the execute bit explicitly: a non-executable
        binary surfaces as a run-hook timeout that says nothing about modes.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_DOCKERFILE_ENTRY, self.harness_dockerfile())
            agentd_info = zipfile.ZipInfo(_AGENTD_ENTRY)
            agentd_info.compress_type = zipfile.ZIP_DEFLATED
            agentd_info.external_attr = (0o100000 | _AGENTD_MODE) << 16
            archive.writestr(agentd_info, self._load_agentd())
            if not self._use_prebuilt:
                self._add_build_context(archive)
        return buffer.getvalue()

    def _add_build_context(self, archive: zipfile.ZipFile) -> None:
        """Add the task environment directory to the artifact."""
        for path in sorted(self.environment_dir.rglob("*")):
            rel = path.relative_to(self.environment_dir)
            if _ARTIFACT_IGNORE_NAMES & set(rel.parts):
                continue
            if path.is_symlink():
                self.logger.warning(
                    "Skipping symlink in task environment (the MicroVM build "
                    f"artifact cannot carry links): {path}"
                )
                continue
            if not path.is_file():
                continue
            rel_posix = rel.as_posix()
            if rel_posix == _DOCKERFILE_ENTRY:
                continue
            if rel_posix == _AGENTD_ENTRY:
                raise ValueError(
                    f"Task environment file {path} collides with the daemon "
                    f"entry {_AGENTD_ENTRY!r} in the build artifact; rename it."
                )
            archive.write(path, rel_posix)

    # ── image lifecycle ──────────────────────────────────────────────────

    def _sandbox_or_create(self) -> Any:
        if self._sandbox is None:
            self._sandbox = microvms.Sandbox(self._microvms_region)
        return self._sandbox

    async def _describe_image(self, arn: str) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(
                self._api().get_microvm_image, imageIdentifier=arn
            )
        except ClientError as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                return None
            raise

    async def _wait_for_image(self, arn: str) -> str:
        """Poll until the image has an active version; return that version."""
        timeout_sec = max(float(self.task_env_config.build_timeout_sec), 600.0)
        deadline = time.monotonic() + timeout_sec
        while True:
            image = await self._describe_image(arn)
            if image is None:
                raise RuntimeError(
                    f"MicroVM image {self.image_name} disappeared during its build."
                )
            state = str(image.get("state", ""))
            version = image.get("latestActiveImageVersion")
            if state in _ACTIVE_IMAGE_STATES and version:
                return str(version)
            if "FAILED" in state:
                raise RuntimeError(
                    f"MicroVM image {self.image_name} build failed (state={state}, "
                    f"reason={image.get('stateReason', 'unknown')}). Check CloudWatch "
                    f"logs under /aws/lambda-microvms/{self.image_name}."
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"MicroVM image {self.image_name} was not ready after "
                    f"{timeout_sec:.0f}s (state={state})."
                )
            await asyncio.sleep(_IMAGE_POLL_INTERVAL_SEC)

    async def _delete_image(self, arn: str) -> None:
        self.logger.info(f"Deleting MicroVM image {self.image_name} before rebuilding")
        try:
            await asyncio.to_thread(
                self._api().delete_microvm_image, imageIdentifier=arn
            )
        except ClientError as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                raise
        deadline = time.monotonic() + _IMAGE_DELETE_TIMEOUT_SEC
        while await self._describe_image(arn) is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"MicroVM image {self.image_name} was still present "
                    f"{_IMAGE_DELETE_TIMEOUT_SEC:.0f}s after deletion was requested."
                )
            await asyncio.sleep(_IMAGE_POLL_INTERVAL_SEC)

    async def _upload_artifact(self) -> str:
        artifact = await asyncio.to_thread(self.build_artifact)
        key = f"{self.s3_key_prefix}/{self.image_name}/artifact.zip"
        await asyncio.to_thread(
            self._s3().put_object, Bucket=self.s3_bucket, Key=key, Body=artifact
        )
        return f"s3://{self.s3_bucket}/{key}"

    async def _ensure_image(self, force_build: bool) -> tuple[str, str]:
        """Reuse, wait for, or build the content-addressed image.

        Returns ``(image_arn, image_version)``. Concurrent trials of one task
        race to build the same name; the loser's create is refused by the
        platform, so a failed build is re-checked against the account before
        it is reported.
        """
        # The daemon binary feeds the image name; fetch it off the event loop
        # before anything reads the name.
        await asyncio.to_thread(self._load_agentd)
        arn = await self._image_arn()
        existing = await self._describe_image(arn)
        if existing is not None:
            state = str(existing.get("state", ""))
            version = existing.get("latestActiveImageVersion")
            if not force_build:
                if state in _ACTIVE_IMAGE_STATES and version:
                    self.logger.debug(
                        f"Reusing MicroVM image {self.image_name} (version {version})"
                    )
                    return arn, str(version)
                if state in _PENDING_IMAGE_STATES:
                    self.logger.debug(
                        f"MicroVM image {self.image_name} is {state}; waiting for it"
                    )
                    return arn, await self._wait_for_image(arn)
            # A forced rebuild or a failed image: the name is content-addressed,
            # so the only way to a fresh build under it is to delete first.
            await self._delete_image(arn)

        artifact_uri = await self._upload_artifact()
        dockerfile = self.harness_dockerfile()
        self.logger.debug(
            f"Building MicroVM image {self.image_name} from {artifact_uri}"
        )
        try:
            image = await asyncio.to_thread(
                self._sandbox_or_create().build_image,
                name=self.image_name,
                binary=self._load_agentd(),
                code_artifact_uri=artifact_uri,
                build_role_arn=self.build_role_arn,
                size=self.size_class(),
                base_image=self.base_image(),
                dockerfile=dockerfile,
                tags={
                    "harbor:environment": _LAMBDA_MICROVMS.value,
                    "harbor:task": sanitize_image_name(self.environment_name),
                },
            )
        except microvms.MicrovmError as exc:
            # Another trial won the create race: wait on its build instead.
            appeared = await self._describe_image(arn)
            if appeared is None:
                raise RuntimeError(
                    f"Failed to build MicroVM image {self.image_name}: {exc}"
                ) from exc
            self.logger.debug(
                f"MicroVM image {self.image_name} was created concurrently; waiting"
            )
            return arn, await self._wait_for_image(arn)
        return str(image.identifier), str(image.version)

    # ── microvm lifecycle ────────────────────────────────────────────────

    async def _launch(self, image_arn: str, image_version: str) -> None:
        sandbox = self._sandbox_or_create()
        try:
            session = await asyncio.to_thread(
                sandbox.run,
                image_identifier=image_arn,
                image_version=image_version,
                execution_role_arn=self.execution_role_arn,
                egress=not self._network_disabled,
                max_idle_sec=self.max_idle_sec,
                max_duration_sec=self.max_duration_sec,
                ready_timeout=self.ready_timeout_sec,
                token_scope=self.session_id,
            )
            await asyncio.to_thread(session.wait_until_ready, self.ready_timeout_sec)
        except microvms.MicrovmError as exc:
            raise RuntimeError(f"Failed to launch MicroVM: {exc}") from exc
        self._session = session
        self.logger.debug(
            f"Launched MicroVM {sandbox.microvm_id} at {sandbox.endpoint} "
            f"(image {self.image_name}:{image_version})"
        )

    async def _capture_image_env(self) -> None:
        """Recover the image's ``ENV`` for later execs.

        The daemon starts every child from an empty environment so the agent
        token can never leak into one, which also drops the image's own
        ``ENV`` lines (a ``PATH`` pointing at a venv, say). The daemon inherited
        them as the container ``CMD``, so a root exec reads them back from its
        parent's ``/proc`` entry once and every exec re-applies them.
        """
        result = await self._exec_raw(
            "tr '\\0' '\\n' < /proc/$PPID/environ", timeout_sec=30
        )
        env = parse_environ(result.stdout or "") if result.return_code == 0 else {}
        env.setdefault("PATH", _DEFAULT_PATH)
        self._image_env = env

    @override
    async def start(self, force_build: bool) -> None:
        image_arn, image_version = await self._ensure_image(force_build)
        await self._launch(image_arn, image_version)
        await self._capture_image_env()
        # Nothing is bind-mounted in a MicroVM, so the log and mount targets
        # must exist for artifact collection and uploads to land.
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

    @override
    async def stop(self, delete: bool) -> None:
        sandbox = self._sandbox
        if sandbox is None or sandbox.microvm_id is None:
            self._session = None
            return
        microvm_id = sandbox.microvm_id
        if delete:
            report = await asyncio.to_thread(sandbox.terminate)
            if report.leaked:
                self.logger.warning(
                    f"MicroVM {microvm_id} teardown leaked resources: "
                    f"undeleted={report.undeleted} failures={report.failures}"
                )
            else:
                self.logger.debug(f"Terminated MicroVM {microvm_id}")
            self._sandbox = None
            self._session = None
            return
        try:
            state = await asyncio.to_thread(sandbox.suspend)
        except microvms.MicrovmError as exc:
            self.logger.warning(f"Failed to suspend MicroVM {microvm_id}: {exc}")
            return
        self.logger.info(
            f"Suspended MicroVM {microvm_id} (delete=False, state={state}). Resume "
            f"it with: aws lambda-microvms resume-microvm --microvm-identifier {microvm_id}"
        )

    # ── exec ─────────────────────────────────────────────────────────────

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("MicroVM is not running; call start() first.")
        return self._session

    async def _resolve_identity(self, user: str | int | None) -> _Identity | None:
        """Map Harbor's user (name or uid) onto the daemon's numeric uid.

        ``None`` and root both run as the daemon itself. Names are looked up
        in the VM's ``/etc/passwd`` once and cached; the row also supplies the
        ``HOME``/``USER`` a demoted command expects, which the daemon's empty
        child environment would otherwise omit.
        """
        if user is None:
            return None
        key = str(user)
        if key in ("root", "0"):
            return None
        if key in self._identities:
            return self._identities[key]
        script = (
            "while IFS=: read -r name _ uid gid _ home _; do "
            f'if [ "$name" = {shlex.quote(key)} ] || [ "$uid" = {shlex.quote(key)} ]; '
            'then printf "%s:%s:%s:%s\\n" "$name" "$uid" "$gid" "$home"; exit 0; fi; '
            "done < /etc/passwd; exit 1"
        )
        result = await self._exec_raw(script, timeout_sec=30)
        identity: _Identity | None = None
        if result.return_code == 0 and result.stdout:
            name, uid, gid, home = result.stdout.strip().split(":", 3)
            identity = _Identity(
                name=name,
                uid=int(uid),
                gid=int(gid) if gid else None,
                home=home or None,
            )
        elif key.isdigit():
            identity = _Identity(name=key, uid=int(key), gid=None, home=None)
        else:
            raise ValueError(f"Unknown user {user!r} in MicroVM /etc/passwd.")
        self._identities[key] = identity
        return identity

    async def _exec_raw(self, command: str, *, timeout_sec: int | None) -> ExecResult:
        """Run *command* as the daemon user with only the image env applied."""
        return await self._run_exec(
            command,
            cwd=None,
            env=dict(self._image_env),
            identity=None,
            timeout_sec=timeout_sec,
            callback=None,
        )

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        identity = await self._resolve_identity(self._resolve_user(user))
        # Precedence, lowest first: what a root shell expects, the image's own
        # ENV, the demoted user's passwd row, then Harbor's merged env.
        exec_env = {**_ROOT_ENV, **self._image_env}
        if identity is not None:
            exec_env.update(identity.env())
        exec_env.update(self._merge_env(env) or {})
        return await self._run_exec(
            command,
            cwd=effective_exec_cwd(
                cwd, self.task_env_config.workdir, self._dockerfile_workdir
            ),
            env=exec_env,
            identity=identity,
            timeout_sec=timeout_sec,
            callback=self._output_callback(),
        )

    async def _run_exec(
        self,
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str],
        identity: _Identity | None,
        timeout_sec: int | None,
        callback: OutputCallback | None,
    ) -> ExecResult:
        session = self._require_session()
        # The client mints the exec id: a retried start with a known id returns
        # the original exec instead of spawning a second child.
        exec_id = uuid.uuid4().hex
        wait_timeout = (
            float(timeout_sec) + _EXEC_TIMEOUT_GRACE_SEC
            if timeout_sec is not None
            else _EXEC_WAIT_CEILING_SEC
        )
        try:
            # Harbor's exec contract is bash semantics (`set -o pipefail`,
            # arrays, `[[`), and every Harbor-built main image ships bash. The
            # daemon's own `shell=True` is `/bin/sh -c`, which is dash on
            # Debian-family images and rejects `pipefail`; so exec bash as an
            # argv array with no shell in between, the way docker and runloop
            # wrap the command.
            handle = await asyncio.to_thread(
                session.run,
                ["bash", "-c", command],
                shell=False,
                cwd=cwd,
                env=env,
                user=identity.uid if identity else None,
                group=identity.gid if identity else None,
                timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
                exec_id=exec_id,
            )
        except microvms.MicrovmError as exc:
            raise RuntimeError(f"MicroVM exec start failed: {exc}") from exc

        result: Any | None = None
        if callback is not None:
            result = await self._stream_then_ack(handle, callback)
        if result is None:
            try:
                result = await asyncio.to_thread(handle.wait_and_ack, wait_timeout)
            except microvms.TimeoutError:
                # The client clock ran out with the exec still running: kill it
                # so the VM does not keep paying for a hung command.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(handle.kill)
                with contextlib.suppress(Exception):
                    result = await asyncio.to_thread(handle.wait_and_ack, 30.0)
                if result is None:
                    return ExecResult(
                        stdout=None,
                        stderr=f"Command timed out after {wait_timeout:.0f} seconds",
                        return_code=124,
                    )
            except microvms.MicrovmError as exc:
                raise RuntimeError(f"MicroVM exec {exec_id} failed: {exc}") from exc
        return self._to_exec_result(result, timeout_sec)

    async def _stream_then_ack(
        self, handle: Any, callback: OutputCallback
    ) -> Any | None:
        """Follow the exec's output live, then ack it for the full result.

        Returns ``None`` when the stream ended without the daemon's typed
        ``exit`` event (a cut connection past its reconnects), so the caller
        falls back to polling; the exec and its buffered output are untouched.
        """
        loop = asyncio.get_running_loop()

        async def deliver(text: str, stream: Any) -> None:
            await callback(text, stream)

        def pump() -> bool:
            for event in handle.stream():
                if isinstance(event, microvms.OutputChunk):
                    asyncio.run_coroutine_threadsafe(
                        deliver(event.text(), event.stream), loop
                    ).result()
                elif isinstance(event, microvms.Exit):
                    return True
            return False

        try:
            exited = await asyncio.to_thread(pump)
        except microvms.MicrovmError as exc:
            self.logger.debug(f"MicroVM exec stream failed; polling instead: {exc}")
            return None
        if not exited:
            return None
        try:
            return await asyncio.to_thread(handle.ack)
        except microvms.MicrovmError as exc:
            self.logger.debug(f"MicroVM exec ack after stream failed: {exc}")
            return None

    @staticmethod
    def _to_exec_result(result: Any, timeout_sec: int | None) -> ExecResult:
        stdout = result.stdout or None
        stderr = result.stderr or None
        exit_code = result.exit_code
        if exit_code is None:
            sig = result.signal
            if timeout_sec is not None and sig in _TIMEOUT_SIGNALS:
                # The daemon enforced Harbor's timeout with SIGTERM then
                # SIGKILL to the process group; report it the way every other
                # provider does.
                return_code = 124
                message = f"Command timed out after {timeout_sec} seconds"
                stderr = f"{stderr}\n{message}" if stderr else message
            else:
                return_code = 128 + int(sig or 0)
        else:
            return_code = int(exit_code)
        if result.truncated:
            message = "[output truncated by the MicroVM daemon's output cap]"
            stderr = f"{stderr}\n{message}" if stderr else message
        return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)

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
            await asyncio.to_thread(
                session.upload_file, target_path, source.read_bytes(), mode=mode
            )
        except microvms.MicrovmError as exc:
            raise RuntimeError(
                f"Failed to upload {source} to MicroVM {target_path}: {exc}"
            ) from exc

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
            await asyncio.to_thread(session.upload_tar, target_dir, archive.getvalue())
        except microvms.MicrovmError as exc:
            raise RuntimeError(
                f"Failed to upload directory {source} to MicroVM {target_dir}: {exc}"
            ) from exc

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        session = self._require_session()
        try:
            data = await asyncio.to_thread(session.download_file, source_path)
        except microvms.MicrovmError as exc:
            if self._is_not_found(exc):
                raise FileNotFoundError(
                    f"MicroVM file not found: {source_path}"
                ) from exc
            raise RuntimeError(
                f"Failed to download MicroVM file {source_path}: {exc}"
            ) from exc
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        session = self._require_session()
        try:
            data = await asyncio.to_thread(session.download_tar, source_dir)
        except microvms.MicrovmError as exc:
            if self._is_not_found(exc):
                raise FileNotFoundError(
                    f"MicroVM directory not found: {source_dir}"
                ) from exc
            raise RuntimeError(
                f"Failed to download MicroVM directory {source_dir}: {exc}"
            ) from exc
        await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)
