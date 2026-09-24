"""Tool-use + trajectory metrics over a captured trial (Workstream G, G-3).

These read a :class:`~eval.models.TrialTrajectory` (what G-1 captured) and compute the *tool-use
accuracy* signal G-3's report needs: which ``eval-harvest`` verbs the agent used and in what order,
how often it repeated a call needlessly, and — the signal that matters most for the core bet —
whether it **acted on the CLI's refusals** (a refusal followed by a corrected call) rather than
giving up. Pure functions of a trajectory: no model, no network, no VM, so the whole thing is
exercised offline in the gate.

Nothing under ``src/eval_harvest/`` imports this package (S-18); it lives under ``eval/`` alongside
the judge and the harness.
"""

from __future__ import annotations

from eval.metrics.tool_use import ToolUseAnalyzer, ToolUseMetrics

__all__ = ["ToolUseAnalyzer", "ToolUseMetrics"]
