"""Judge↔human alignment (Workstream G, G-2): the step that turns the judge into a measurement.

Before G-3 trusts the quality judge, we check it against human judgment: a small set of datapoints
labeled good or deliberately broken, the judge run over them, and the agreement recorded against a
stated bar. Only a recorded result at or above the bar licenses the judge (:mod:`eval.judge.gate`).

The alignment is reproducible offline via the recorded-judge pattern (tech plan §12): each labeled
datapoint pins a model reply, so the measurement runs without a model in the tree. The set carries a
genuine judge↔human disagreement, so the agreement is below 1.0 — proof the measurement discriminates
rather than rubber-stamping. Refreshing the number against a live model is the human-gated step
(``python -m eval.alignment.run_alignment --model <id>``), mirroring the rest of Workstream G.
"""
