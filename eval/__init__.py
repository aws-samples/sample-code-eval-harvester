"""The eval-harvest core-bet eval harness (Workstream G).

This directory is **not** part of the shipped ``eval_harvest`` package: nothing under
``src/eval_harvest/`` imports it (tech plan §3, §13; S-18). Because it lives outside the
zero-runtime-dependency CLI (NFR-5), it is free to depend on Pydantic, a network, a model client,
and the Workstream H Lambda MicroVMs environment — the things the CLI itself must never carry.

The harness authors the eval as a Harbor task, runs it in Lambda MicroVMs against a live agent
(Claude Code on Bedrock) over 2 objectives × k=3 trials, and normalizes each trial into a
``TrialTrajectory`` artifact. The datapoint-quality judge (G-2) and the tool-use/trajectory metrics
and report (G-3) are layered on top of those artifacts.
"""
