"""The packaged Lambda MicroVMs environment is importable, and the dead PYTHONPATH overlay is gone.

The environment cannot be loaded through `harbor[lambda-microvms]`: that is not a real PyPI extra
(so `dockerfile-parse` and the `boto3>=1.43.35` floor never install), and `PYTHONPATH=vendor/.../src`
cannot overlay the `harbor.*` namespace (a regular package wins outright over a same-named namespace
portion). So the environment is packaged as its own `harvest-env` distribution and depends on the
extra's real contents directly.

The import checks below need the `eval` group synced (harbor + harvest-env + its deps), so they
carry the same visible-skip treatment as the other Harbor-dependent modules: `mise run test` syncs
`dev` only and they skip with a named reason; `mise run test-harbor` syncs `eval` and runs them. Two
checks are pure — the import-path constant's spelling and the absence of a PYTHONPATH overlay in
shipped scripts — and run every time, including in the offline gate, because a regression in either
is exactly the failure this module guards.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from eval.runner import (
    LAMBDA_MICROVMS_IMPORT_PATH,
    EnvironmentPreflightError,
    preflight_environment_imports,
)

# tests/integration/ -> parents[2] = repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# The packaged environment's import surface: the directory Harbor imports `harvest_env.*` from, and
# the module file itself. Source-level checks read these directly, so they need neither harbor nor the
# `eval` group — a reference to a Harbor internal creeping back in is a text fact, not a runtime one.
PACKAGED_ENV_DIR = REPO_ROOT / "harvest_env" / "src" / "harvest_env"
PACKAGED_ENV_MODULE = PACKAGED_ENV_DIR / "lambda_microvms.py"

# Attempted, not probed: importing what these tests use is the only honest answer to
# "is the eval group synced". `find_spec` gives false positives against the pruned-but-not-empty tree.
try:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.environment_type import EnvironmentType
    from harbor.utils.import_path import import_class

    importlib.import_module(LAMBDA_MICROVMS_IMPORT_PATH.split(":", 1)[0])
except ImportError:
    EVAL_GROUP_IS_SYNCED = False
else:
    EVAL_GROUP_IS_SYNCED = True

EVAL_GROUP_SKIP_REASON = (
    "the `eval` group is not synced: harbor and the packaged `harvest-env` environment come from it, "
    "and `mise run test` syncs `dev` only. Run `mise run test-harbor` to sync `eval` and run this module."
)

requires_eval_group = pytest.mark.skipif(not EVAL_GROUP_IS_SYNCED, reason=EVAL_GROUP_SKIP_REASON)


# ───────────────────────────── the module is importable (needs the eval group) ─────────────────────────────


@requires_eval_group
def test_environment_module_is_importable() -> None:
    """The configured import path imports and yields the environment class — the shadowing defect gone."""
    module_path, _, symbol = LAMBDA_MICROVMS_IMPORT_PATH.partition(":")
    module = importlib.import_module(module_path)
    cls = getattr(module, symbol)
    assert issubclass(cls, BaseEnvironment)


@requires_eval_group
def test_import_path_constant_matches_a_real_module() -> None:
    """`LAMBDA_MICROVMS_IMPORT_PATH` resolves via the same `import_class` Harbor uses at load time.

    Catches a constant that drifts from the package — a failure that would otherwise surface only
    mid-run, inside Harbor, after AWS calls have already been made."""
    cls = import_class(LAMBDA_MICROVMS_IMPORT_PATH, base=BaseEnvironment, label="environment")
    assert cls.__name__ == "LambdaMicrovmsEnvironment"


@requires_eval_group
def test_eval_group_provides_the_environments_imports() -> None:
    """`dockerfile_parse`, `microvms`, and `boto3` all import — the fake-extra defect stays fixed."""
    for module in ("dockerfile_parse", "microvms", "boto3"):
        importlib.import_module(module)


@requires_eval_group
def test_preflight_passes_when_the_group_is_synced() -> None:
    """With the `eval` group synced, the preflight resolves every import and raises nothing."""
    preflight_environment_imports()


# ───────────────────────────── the preflight names what is missing (pure) ─────────────────────────────


def test_preflight_names_the_missing_package() -> None:
    """A missing dependency is reported by name, before any AWS call — not a bare ModuleNotFoundError.

    Pure: it points the preflight at a module that does not exist, so it needs neither Harbor nor AWS."""
    missing = "a_module_that_does_not_exist_pqrxyz"

    def _raise_import_error() -> None:
        raise ModuleNotFoundError(f"No module named {missing!r}")

    with pytest.raises(EnvironmentPreflightError) as excinfo:
        preflight_environment_imports([(missing, _raise_import_error)])
    assert missing in str(excinfo.value)
    assert "uv sync --group eval" in str(excinfo.value)


# ───────────────────────────── the dead overlay stays dead (pure) ─────────────────────────────


def test_import_path_constant_is_the_packaged_module() -> None:
    """The constant points at the packaged `harvest_env` distribution, not the un-overlayable `harbor.*`."""
    assert LAMBDA_MICROVMS_IMPORT_PATH.startswith("harvest_env.")
    assert not LAMBDA_MICROVMS_IMPORT_PATH.startswith("harbor.")


def _uncommented_lines(text: str) -> str:
    """`text` with shell comment lines dropped, so a test inspects the code and not the prose in it."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_pythonpath_overlay_remains() -> None:
    """No shipped script *sets* PYTHONPATH, and the vendored src path appears nowhere — the dead overlay is gone.

    A regular package cannot be partially overlaid by a same-named namespace portion, so
    `PYTHONPATH=vendor/harbor-lambda-microvms/tree/src` could never make `harbor.environments.*`
    resolve; leaving it in place is a plausible-looking dead end for the next reader. The check
    looks at code, not comments — explaining *why* there is no overlay is exactly what the fix should do."""
    run_eval = (REPO_ROOT / "scripts" / "run-eval.sh").read_text(encoding="utf-8")
    assert "PYTHONPATH=" not in _uncommented_lines(run_eval), "scripts/run-eval.sh still sets PYTHONPATH"
    assert "vendor/harbor-lambda-microvms/tree/src" not in run_eval, "the vendored src overlay path is still referenced"


def test_eval_group_no_longer_declares_the_fake_extra() -> None:
    """No `eval` group entry names the `lambda-microvms` extra — the extra that does not exist on PyPI Harbor.

    Parses the manifest rather than grepping it: the fix's own comment explains the removed extra by
    name, so a substring check would fire on the explanation of the thing being asserted absent."""
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eval_group: list[str] = manifest["dependency-groups"]["eval"]
    offenders = [dep for dep in eval_group if "[" in dep and "lambda-microvms" in dep]
    assert offenders == [], f"the `eval` group still declares a lambda-microvms extra: {offenders}"


def test_eval_group_module_skips_visibly_without_the_eval_group() -> None:
    """The skip carries a named reason and the remedy — and this test never skips, so it always says so."""
    assert "the `eval` group is not synced" in EVAL_GROUP_SKIP_REASON
    assert "mise run test-harbor" in EVAL_GROUP_SKIP_REASON
    assert requires_eval_group.kwargs["reason"] == EVAL_GROUP_SKIP_REASON


# ───────────────────────────── no Harbor-internal patch is needed at runtime ─────────────────────────────


@requires_eval_group
def test_environment_imports_against_stock_harbor() -> None:
    """The packaged module imports even though stock Harbor's `EnvironmentType` has no `LAMBDA_MICROVMS`.

    No mocking: the installed Harbor *is* the unpatched case (this repo patches neither Harbor nor
    site-packages). The module must not member-access an absent enum member — the local
    `_LambdaMicrovmsEnvType` stand-in is what keeps the import working."""
    assert "LAMBDA_MICROVMS" not in EnvironmentType.__members__, "moot unless run against *stock* Harbor"
    module_path = LAMBDA_MICROVMS_IMPORT_PATH.split(":", 1)[0]
    importlib.import_module(module_path)  # must not raise AttributeError on the missing enum member


@requires_eval_group
def test_environment_type_string_is_lambda_microvms() -> None:
    """`type()` returns the exact `"lambda-microvms"` the provider keys on, and `.value` agrees.

    A refactor that changed where the constant lives must not change what it is — the string is what
    AWS receives, so a subtly wrong value would fail at the dispatch, not at import."""
    module_path, _, symbol = LAMBDA_MICROVMS_IMPORT_PATH.partition(":")
    environment = getattr(importlib.import_module(module_path), symbol)
    assert environment.type() == "lambda-microvms"
    assert environment.type().value == "lambda-microvms", "Harbor calls `.value` on the returned type; the stand-in must carry it"


def test_environment_does_not_reference_harbor_environment_type() -> None:
    """No `EnvironmentType` reference remains in the packaged module — import, annotation, or comment.

    Pure (source read): the reference is what breaks the import on a Harbor whose enum lacks the member,
    and the token creeping back is exactly what a resync of the vendored tree would do."""
    source = PACKAGED_ENV_MODULE.read_text(encoding="utf-8")
    assert "EnvironmentType" not in source, "the packaged module must not depend on Harbor's EnvironmentType enum"


def test_packaged_environment_carries_no_harbor_internals() -> None:
    """`factory.py` and `environment_type.py` are not in the packaged module — the registry patch is not load-bearing.

    Pure (directory listing): under H-3's import-path design only `lambda_microvms.py` is the product;
    carrying the other two implies the type→class registry patch is required at runtime, which it is not."""
    carried = {path.name for path in PACKAGED_ENV_DIR.glob("*.py")}
    assert "factory.py" not in carried, "the vendored factory.py is dead weight under the import-path design"
    assert "environment_type.py" not in carried, "the patched enum module is not needed once EnvironmentType is dropped"
