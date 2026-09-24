"""The `init` verb: scaffold the dataset, surface the repo's own convention files, read a rubric's version.

An eval that grades review quality against generic conventions penalises the agent for missing
things the customer's team does not care about (US-5). So the workflow starts by building a rubric
that overlays the repo's *own* stated conventions on a standard base — and the CLI hands the agent
those conventions to overlay rather than making it guess. This module owns the three mechanical
pieces of that step: the dataset skeleton `init` writes, the discovery of stated-convention files it
surfaces (FR-25), and the version reader `emit` (D-3) calls to stamp `rubric_version` (FR-26, FR-27).

**No LLM, no judgement (§3, FR-3).** `init` lists convention file *paths*; it never summarises,
ranks, or parses them — parsing is the agent's job. The module is file discovery + templates + a
version reader, nothing more.

**Online layer, local reads only (§6.2).** `init` reads the clone with plain filesystem access and
makes no forge call, so the offline guarantee (NFR-2) is not muddied here. The convention scan never
touches `gh` or the network — a test (`test_init_makes_no_forge_call`) pins that.

The `risk-map.template.toml` half is emitted through :meth:`RiskMap.emit_template` (C-2/D-1) so it
is byte-identical across runs; the `rubric.template.md` half is a constant string for the same
reason — the artefact is versioned, and a nondeterministic template would break that (NFR-1).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Final

from eval_harvest.riskmap import RISK_MAP_TEMPLATE_FILENAME, RiskMap

#: The rubric the agent authors, and the template `init` writes for it to fill.
RUBRIC_FILENAME: Final = "rubric.md"
RUBRIC_TEMPLATE_FILENAME: Final = "rubric.template.md"

#: The dataset subdirectories `init` creates empty. `candidates/` and `patches/` are where `capture`
#: (B-2/B-3) materializes the candidate JSON and the per-iteration diffs; `tasks/` is where `emit`
#: (D-3) promotes a verified task directory. Creating them up front means a later verb never fails
#: on a missing input directory.
_SKELETON_DIRS: Final = ("candidates", "tasks", "patches")

#: Top-level convention filenames, matched case-insensitively (a repo may ship `contributing.md`).
#: These are the stated-convention files GitHub and common toolchains recognise; the scan surfaces
#: their paths and never parses them (FR-25). `pyproject.toml`/`setup.cfg` carry tool config in
#: `[tool.*]`/`[flake8]` sections — surfaced whole for the agent to read, not parsed here.
_CONVENTION_PATTERNS: Final = (
    "contributing*",
    "codeowners",
    ".editorconfig",
    "style*",
    ".ruff.toml",
    ".eslintrc*",
    ".prettierrc*",
    ".pylintrc",
    ".flake8",
    "setup.cfg",
    "pyproject.toml",
)

#: GitHub reads `.github/` as the home of contribution conventions (PR/issue templates, workflows,
#: CODEOWNERS); every file under it is surfaced.
_GITHUB_DIRNAME: Final = ".github"

#: The three directories GitHub recognises a CODEOWNERS file in. Root and `.github/` are already
#: covered (by the top-level pattern and the `.github/` walk); `docs/` is the one only this catches.
_CODEOWNERS_DIRS: Final = ("", ".github", "docs")

#: A `version:` declaration — a frontmatter key or a plain line. Anchored at line start so a
#: lookalike key (`rubric_version:`) or a prose mention ("the version: field") does not match; the
#: value's surrounding quotes are stripped. Case-insensitive so `Version:` reads back the same.
_VERSION_DECLARATION = re.compile(r'^\s*version\s*:\s*"?([^"\n]+?)"?\s*$', re.IGNORECASE | re.MULTILINE)

#: The `rubric.template.md` prose. A constant (deterministic, NFR-1) carrying a `version:`
#: frontmatter the helper reads back, the overlay method (US-5, FR-24), and slots the agent fills.
_RUBRIC_TEMPLATE: Final = """\
---
version: v1
---

# Review rubric

Author this rubric by the OVERLAY METHOD (US-5): take a standard review base and overlay your
repository's own stated conventions on top of it, so a datapoint grades a review against how *your*
team reviews — not a generic checklist. `eval-harvest init` listed the convention files it found
(CONTRIBUTING, .github/, CODEOWNERS, style guides); read them and fold their rules in below.

Bump the `version:` above whenever you change the rubric. Every datapoint records the
`rubric_version` it was built against, so a fixed version must mean fixed grading criteria (FR-27).

## Standard base

Grade a review on whether it catches substantive defects — correctness, security, data loss,
breaking changes — and separates them from nits (style or preference) that do not block a merge.

## Your team's conventions (overlay)

- <fold in each rule your CONTRIBUTING / .github / CODEOWNERS / style guides state>
"""

#: The one-line hand-off `init` prints after the found files: what to do with them next.
_OVERLAY_INSTRUCTION: Final = "author rubric.md and risk-map.toml by overlaying these convention files on the standard base"


class Rubric:
    """Scaffold the dataset, surface convention files, and read a rubric's version. Holds no state."""

    # ───────────────────────────── dataset skeleton ─────────────────────────────

    @classmethod
    def write_skeleton(cls, dataset: Path) -> None:
        """Create the empty skeleton dirs and write both templates under ``dataset``.

        Idempotent (``exist_ok=True``): re-running ``init`` over an existing dataset re-writes the
        byte-stable templates without disturbing what the agent has authored in between.
        """
        for skeleton_dir in _SKELETON_DIRS:
            (dataset / skeleton_dir).mkdir(parents=True, exist_ok=True)
        (dataset / RUBRIC_TEMPLATE_FILENAME).write_bytes(cls.rubric_template())
        (dataset / RISK_MAP_TEMPLATE_FILENAME).write_bytes(RiskMap.emit_template())

    @staticmethod
    def rubric_template() -> bytes:
        """The ``rubric.template.md`` bytes: deterministic, LF-terminated UTF-8 (NFR-1)."""
        return _RUBRIC_TEMPLATE.encode("utf-8")

    # ───────────────────────────── convention discovery ─────────────────────────────

    @classmethod
    def discover_convention_files(cls, clone: Path) -> list[Path]:
        """The repo's stated-convention files, as clone-relative paths, sorted (deterministic, FR-25).

        Surfaces paths only — it never opens, summarises, or ranks a file (that is the agent's
        judgement). Three sources, de-duplicated: top-level convention files, everything under
        ``.github/``, and CODEOWNERS in each location GitHub recognises.
        """
        if not clone.is_dir():
            return []
        found: set[Path] = set()
        found |= cls._top_level_matches(clone)
        found |= cls._github_files(clone)
        found |= cls._codeowners_files(clone)
        return sorted(found)

    @staticmethod
    def _top_level_matches(clone: Path) -> set[Path]:
        """Top-level files whose name matches a convention pattern, case-insensitively."""
        matches: set[Path] = set()
        for entry in clone.iterdir():
            if entry.is_file() and any(fnmatch.fnmatch(entry.name.lower(), pattern) for pattern in _CONVENTION_PATTERNS):
                matches.add(entry.relative_to(clone))
        return matches

    @staticmethod
    def _github_files(clone: Path) -> set[Path]:
        """Every file under ``.github/`` (PR/issue templates, workflows, CODEOWNERS), recursively."""
        github = clone / _GITHUB_DIRNAME
        if not github.is_dir():
            return set()
        return {path.relative_to(clone) for path in github.rglob("*") if path.is_file()}

    @staticmethod
    def _codeowners_files(clone: Path) -> set[Path]:
        """CODEOWNERS (any case) in each of the three directories GitHub recognises it in."""
        found: set[Path] = set()
        for subdir in _CODEOWNERS_DIRS:
            base = clone / subdir if subdir else clone
            if not base.is_dir():
                continue
            candidates = (entry for entry in base.iterdir() if entry.is_file() and entry.name.lower() == "codeowners")
            found |= {entry.relative_to(clone) for entry in candidates}
        return found

    # ───────────────────────────── rubric version reader ─────────────────────────────

    @classmethod
    def read_version(cls, rubric_path: Path) -> str:
        """The version declared in the rubric at ``rubric_path``, or ``""`` if the file is absent/blank.

        The helper ``emit`` (D-3) calls to stamp ``rubric_version`` (FR-27). Missing file → ``""`` so
        ``emit`` refuses an unpinned datapoint rather than crashing (``candidate.validate_filled``
        already reports the empty version as ``candidate.rubric-unpinned``).
        """
        if not rubric_path.is_file():
            return ""
        return cls.declared_version(rubric_path.read_text(encoding="utf-8"))

    @staticmethod
    def declared_version(rubric_text: str) -> str:
        """The first ``version:`` declaration in ``rubric_text`` (frontmatter or plain line), else ``""``.

        Anchored at line start, so a lookalike key like ``rubric_version:`` is not mistaken for the
        rubric's own version — a datapoint stamped with the wrong version is the defect FR-27 guards.
        """
        match = _VERSION_DECLARATION.search(rubric_text)
        return match.group(1).strip() if match else ""

    # ───────────────────────────── stdout hand-off ─────────────────────────────

    @staticmethod
    def overlay_instruction() -> str:
        """The methodology line ``init`` prints after the found files (FR-24)."""
        return _OVERLAY_INSTRUCTION
