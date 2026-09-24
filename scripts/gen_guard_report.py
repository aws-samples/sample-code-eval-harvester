"""Generate ``docs/features/pr-eval-harvest/guard-report.md`` from the US-7 guard registry (E-3, FR-37).

Run it via ``uv run scripts/gen_guard_report.py`` (never bare ``python`` — CLAUDE.md). The report is a
pure function of ``tests/guard_registry.py``, so it cannot drift from the guards; ``tests/test_guards.py``
asserts the committed report equals this output and fails with a pointer to re-run this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The registry lives under tests/ (importable there as a top-level module, the way the suite imports
# `fixtures`); put that directory on the path so this operational script can import it standalone.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from guard_registry import REPORT_RELATIVE_PATH, render_guard_report, repo_root  # noqa: E402


def main() -> None:
    """Write the rendered guard report to its committed location and report where it went."""
    target = repo_root() / REPORT_RELATIVE_PATH
    target.write_text(render_guard_report(), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
