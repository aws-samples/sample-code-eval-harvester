"""Load the declarative objective records under ``eval/objectives/`` into validated ``Objective``s.

Each objective is one TOML file. Adding a third objective is dropping a third file into
``eval/objectives/`` — no code change (G-1 acceptance: "a one-record path to add more"). Every
record is validated through :class:`~eval.models.Objective` on load, so a malformed objective fails
loudly here rather than mid-trial.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from eval.models import Objective

#: Where the objective records live. One ``*.toml`` file per objective.
OBJECTIVES_DIR: Path = Path(__file__).parent / "objectives"


class ObjectiveCatalog:
    """Reads and validates the declarative objective records. Holds no state."""

    @classmethod
    def load_all(cls, objectives_dir: Path = OBJECTIVES_DIR) -> tuple[Objective, ...]:
        """Every objective under ``objectives_dir``, sorted by id, each validated through ``Objective``."""
        objectives = [cls.load_one(path) for path in sorted(objectives_dir.glob("*.toml"))]
        return tuple(sorted(objectives, key=lambda objective: objective.id))

    @staticmethod
    def load_one(path: Path) -> Objective:
        """Parse and validate one objective TOML record into an :class:`Objective`."""
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        return Objective.model_validate(document)
