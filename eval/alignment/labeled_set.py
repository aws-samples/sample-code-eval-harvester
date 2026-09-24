"""The human-labeled alignment set: datapoints with a human verdict and a pinned model reply.

One JSON file per labeled datapoint under ``labeled/``, so adding a case is dropping a file (the
objectives convention). Each file carries the datapoint the judge reads, the source PR context, a
**human** label (is this a good datapoint?), and a pinned model reply the recorded run replays. The
set is deliberately mixed: clean datapoints, one broken per rubric criterion, and one case where the
recorded judge disagrees with the human — so the measured agreement is a real number below 1.0.

The pinned reply is stored as structured JSON (``recorded_reply``) rather than an escaped string, so
the files stay readable; the loader serializes it back to the raw text the judge's parser consumes,
which still exercises the full parse path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eval.judge.models import Datapoint, PrContext

#: Where the labeled datapoints live. One ``*.json`` file per case.
LABELED_DIR: Path = Path(__file__).parent / "labeled"


class LabeledDatapoint(BaseModel):
    """One labeled alignment case: a datapoint, its context, the human verdict, and a pinned reply."""

    model_config = ConfigDict(frozen=True)

    id: str
    human_overall_pass: bool
    human_note: str
    datapoint: Datapoint
    pr_context: PrContext
    recorded_reply: dict[str, Any]

    @property
    def recorded_response(self) -> str:
        """The pinned reply as the raw text the judge's parser consumes (structured reply → JSON string)."""
        return json.dumps(self.recorded_reply)


class LabeledSet:
    """Loads and validates the labeled alignment set from ``labeled/``. Holds no state."""

    @classmethod
    def load_all(cls, labeled_dir: Path = LABELED_DIR) -> tuple[LabeledDatapoint, ...]:
        """Every labeled datapoint under ``labeled_dir``, sorted by id, each validated on load."""
        cases = [cls.load_one(path) for path in sorted(labeled_dir.glob("*.json"))]
        return tuple(sorted(cases, key=lambda case: case.id))

    @staticmethod
    def load_one(path: Path) -> LabeledDatapoint:
        """Parse and validate one labeled-datapoint file into a :class:`LabeledDatapoint`."""
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        return LabeledDatapoint.model_validate(document)
