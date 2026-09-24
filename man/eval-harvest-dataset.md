# eval-harvest-dataset(1)

## NAME

**eval-harvest dataset** — assemble the Harbor manifest and report the risk and severity
distribution.

## SYNOPSIS

```
eval-harvest dataset --dataset <dir> [--json]
```

## DESCRIPTION

Offline. Reads every `tasks/*` and its `task.toml`, writes `dataset.toml` (the Harbor manifest) and
a local `registry.json`, and prints the distribution counts. Refuses `no-datapoints` on an empty
dataset.

## METHODOLOGY

**The risk distribution is the point, not a summary.** You only switch **automated** approval on for
**low-risk** changes, so the dataset has to hold enough low-risk datapoints to measure the agent
there. `dataset` reports counts per change-risk level and per finding-severity level, so you can see
whether the low-risk slice is large enough to justify turning automation on.

## EXIT STATUS

`3` (refusal) with `no-datapoints` on an empty dataset.

## SEE ALSO

`eval-harvest(1)`, `eval-harvest-emit(1)`.
