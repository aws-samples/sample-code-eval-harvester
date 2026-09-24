"""Every generated-output tree is excluded from every static-analysis tool that walks the repo.

Being in ``.gitignore`` is not being excluded from the linters, and that gap is a bug this repo
already shipped: two eval trials under ``eval/runs/_staging/`` leave two copies of the
``eval_harvest`` package on disk, mypy finds a duplicate module name, and — the dangerous part —
*stops checking entirely*, so the whole source tree goes untyped behind a single unrelated error.

Three tools walk from the repository root and each carries its own exclude list (mypy's
``exclude``, ruff's ``extend-exclude``, bandit's ``exclude_dirs``). Nothing keeps those three lists
in step with ``.gitignore``, so this test pins the one relationship that matters: the generated
trees this repo's own tooling writes are named in all three. A new generated-output producer added
to ``.gitignore`` alone reproduces the same bug for a different directory, and this test is what
notices.

Deliberately *not* a diff against ``.gitignore``: most gitignore entries (``.env``, ``*.pem``,
``__pycache__``) have no business being in a linter's exclude list, so the comparison would be
noise. The list below is literal on purpose.
"""

import tomllib
from pathlib import Path

# tests/unit/test_static_analysis_excludes.py -> parents[1] = tests/, parents[2] = repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# The generated-output trees this repository's own tooling writes, all gitignored. `work/` holds
# mining clones (arbitrary third-party Python), `eval/runs/` holds one copy of the eval_harvest
# package per trial (the duplicate-module collision), `eval/reports/` holds rendered eval output. None is ours
# to lint, type-check, or security-scan. Keep this list in step with `.gitignore`.
GENERATED_OUTPUT_DIRS = ("work", "eval/runs", "eval/reports")

# Where each tool keeps its exclude list in `pyproject.toml`, as a path through the parsed table.
# Every tool that walks from the repository root belongs here.
EXCLUDE_LIST_LOCATIONS = (
    ("tool", "mypy", "exclude"),
    ("tool", "ruff", "extend-exclude"),
    ("tool", "bandit", "exclude_dirs"),
)


def _exclude_list(manifest: dict[str, object], location: tuple[str, ...]) -> list[str]:
    """The exclude list at ``location`` in the parsed manifest, or ``[]`` when the key is absent."""
    current: object = manifest
    for key in location:
        if not isinstance(current, dict) or key not in current:
            return []
        current = current[key]
    return current if isinstance(current, list) else []


def _covers(exclude_list: list[str], generated_dir: str) -> bool:
    """True when some entry in ``exclude_list`` names ``generated_dir``.

    The three tools spell their entries differently — mypy takes a regex (``"eval/runs/"``), ruff a
    path glob, bandit a bare directory — so an entry counts when it is the directory with an
    optional leading ``./`` and an optional trailing ``/``. Matching on the normalized spelling
    keeps the test about *coverage* rather than about each tool's punctuation.
    """
    return any(entry.removeprefix("./").rstrip("/") == generated_dir for entry in exclude_list)


def test_generated_output_dirs_are_excluded_from_static_analysis() -> None:
    """Every generated-output tree appears in mypy's, ruff's, and bandit's excludes."""
    manifest = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    missing = [
        (".".join(location), generated_dir)
        for location in EXCLUDE_LIST_LOCATIONS
        for generated_dir in GENERATED_OUTPUT_DIRS
        if not _covers(_exclude_list(manifest, location), generated_dir)
    ]
    assert missing == [], f"generated-output dirs missing from static-analysis excludes: {missing}"
