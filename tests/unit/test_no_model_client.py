"""Scenario S-18: the CLI carries no LLM/model client (FR-3, NFR-3, NFR-5).

The product's spine is that this pipeline pulls in nothing at run time and, in particular,
never talks to a model itself — the emitted verifier is where a judge lives (ADR-5), not the
CLI. That constraint erodes silently: one convenient ``import anthropic`` or one "just this
once" runtime dependency and the audit ("the list is empty") quietly stops being true. So a
machine watches it.

Two checks:

* ``[project].dependencies`` in ``pyproject.toml`` parses to an empty list.
* No ``*.py`` module under ``src/eval_harvest/`` imports a banned model client. The scan uses
  ``ast`` rather than a regex over source so a banned name inside a comment or string does not
  trip it, and an aliased import (``import anthropic as a``) cannot hide.

The scan is deliberately restricted to the package's own Python modules. Data the CLI *writes*
— the emitted verifier template under ``src/eval_harvest/verifier_tpl/`` (task D-3), which may
legitimately contain the string "anthropic" or "judge" — is not an import and must not
false-positive here. That directory is excluded from the walk; see ``VERIFIER_TPL_DIRNAME``.
"""

import ast
import tomllib
from pathlib import Path

# tests/unit/test_no_model_client.py -> parents[1] = tests/, parents[2] = repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
PACKAGE_ROOT = REPO_ROOT / "src" / "eval_harvest"

# Directory of emitted verifier templates (D-3). It holds data the CLI writes, not modules the
# CLI imports, so it is data for the purposes of this scan and is skipped by the walk.
VERIFIER_TPL_DIRNAME = "verifier_tpl"

# Model-client SDKs the CLI must never import. A module name is banned when it equals one of
# these or is a submodule of one (``anthropic.types`` counts, ``coherence`` does not). Generic
# HTTP libraries (httpx, requests) are intentionally absent — they are not model clients, and
# the CLI is free to use one; what FR-3 forbids is a *model* client.
BANNED_MODEL_CLIENTS = frozenset(
    {
        "openai",
        "anthropic",
        "litellm",
        "cohere",
        "google.generativeai",
        "mistralai",
        "ollama",
        "groq",
        "together",
        "replicate",
        "vertexai",
    }
)


def _is_banned(module_name: str) -> bool:
    """True when ``module_name`` is a banned model client or a submodule of one."""
    return any(module_name == banned or module_name.startswith(f"{banned}.") for banned in BANNED_MODEL_CLIENTS)


def _imported_module_names(source: str) -> set[str]:
    """Every absolute module name imported by ``source``, via ``ast`` (not text matching).

    Relative imports (``from . import x``) are intra-package and cannot name a third-party
    model client, so they are ignored. For ``from pkg import name`` both ``pkg`` and
    ``pkg.name`` are recorded, so ``from google import generativeai`` is caught the same as
    ``import google.generativeai``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}" if module else alias.name)
    return names


def _package_module_paths(package_root: Path) -> list[Path]:
    """Every ``*.py`` module under ``package_root``, excluding the verifier-template data dir."""
    return [path for path in sorted(package_root.rglob("*.py")) if VERIFIER_TPL_DIRNAME not in path.parts]


def _banned_imports_by_file(package_root: Path) -> dict[Path, set[str]]:
    """Map each module that imports a banned client to the banned names it imports (empty if none)."""
    offenders: dict[Path, set[str]] = {}
    for module_path in _package_module_paths(package_root):
        banned = {name for name in _imported_module_names(module_path.read_text(encoding="utf-8")) if _is_banned(name)}
        if banned:
            offenders[module_path] = banned
    return offenders


def test_runtime_deps_empty() -> None:
    """``[project].dependencies`` is empty — the FR-3/NFR-5 audit is literally "the list is empty"."""
    manifest = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert manifest["project"]["dependencies"] == []


def test_no_model_client_imported() -> None:
    """No package module imports a banned model client (S-18), naming any offender."""
    offenders = _banned_imports_by_file(PACKAGE_ROOT)
    assert offenders == {}, f"banned model-client imports found: {offenders}"


def test_scan_detects_planted_import(tmp_path: Path) -> None:
    """The scan goes red on a planted ``import anthropic`` and green once it is removed.

    This is the FR-37 discipline: a scanner that has only ever been seen return "clean" has
    not been shown to detect anything. We plant the thing it exists to catch and confirm it
    catches it — naming the planted file — then confirm removal returns it to green.
    """
    planted = tmp_path / "sneaky.py"
    planted.write_text("import anthropic\n", encoding="utf-8")
    offenders = _banned_imports_by_file(tmp_path)
    assert planted in offenders
    assert offenders[planted] == {"anthropic"}

    planted.unlink()
    assert _banned_imports_by_file(tmp_path) == {}
