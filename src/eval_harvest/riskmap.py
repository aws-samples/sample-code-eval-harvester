"""The structural half of a change's risk: path rules applied to the paths a change touches.

The customer only wants to auto-approve *low-risk* changes (US-4), so every datapoint carries a
change-risk classification. One half is free: teams already segregate risky code by directory, so
a versioned ``risk-map.toml`` of literal path prefixes gives a cheap, auditable structural signal.
``capture`` (B-3) calls :meth:`RiskMap.structural_risk` to pre-compute that half; the agent supplies
the classified half separately (FR-20), and ``emit`` (D-3) records both plus any disagreement
(FR-21). This module owns only the structural computation, the artefact's parse/validate, and the
byte-stable template ``init`` (C-1) writes.

Two decisions carry the design:

- **Literal prefix matching (``path.startswith``), like the ``survey`` verb's path-root model.** No
  glob or regex — the artefact is meant to be read and corrected by an SDE, and the literal model is
  exactly what Harbor's own visibility rules already use. A monorepo whose code lives at ``cdk/src/``
  matches none of the root prefixes, and seeing that is the point.
- **Highest-risk-wins is the safe direction.** A change touching any high-risk path is high.
  Under-classifying a risky change is the expensive error — the whole reason US-4 exists is the
  low-risk slice the customer would let an agent auto-approve.

The template is emitted through :func:`~eval_harvest.tomlw.emit_document` (D-1) rather than a
hand-written string so it is byte-identical across runs and platforms (FR-22, NFR-1): the artefact
is versioned, and a nondeterministic template would break that.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from typing import Final

from eval_harvest.tomlw import TomlValue, emit_document

RISK_MAP_FILENAME: Final = "risk-map.toml"
RISK_MAP_TEMPLATE_FILENAME: Final = "risk-map.template.toml"

#: The risk taxonomy, in ascending severity. Index *is* severity rank, so "highest matching risk"
#: is a plain ``max`` over ranks and the set is closed — a value outside it is a validation error
#: (§8, tech plan §2.1 FR-20; the same {low,medium,high} the finding severities use).
RISK_LEVELS: Final = ("low", "medium", "high")

#: The ``default`` used when a map omits one. ``medium`` (not ``low``) keeps the safe direction:
#: a path nobody has classified is treated as needing review, never waved through.
_FALLBACK_DEFAULT_RISK: Final = "medium"


class RiskMap:
    """Parse ``risk-map.toml``, apply its path rules, and emit the template. Holds no state."""

    # ───────────────────────────── structural risk ─────────────────────────────

    @classmethod
    def structural_risk(cls, changed_paths: Sequence[str], risk_map: Mapping[str, TomlValue]) -> tuple[str, str]:
        """The highest matching risk across ``changed_paths`` and the rule that produced it.

        Applies each rule to each path by literal prefix (``path.startswith``), takes the highest
        matching level (a change touching any high-risk path is high), and returns that level with
        the matched rule rendered as ``"<prefix> → <risk>"`` for the datapoint's
        ``risk_structural_rule`` audit field (FR-20). When no rule matches any path — a monorepo
        layout the map does not cover — the level falls to the map's ``default`` and the rule reads
        ``"default → <default>"`` so the audit trail still says *why* (not a blank).

        Raises:
            ValueError: when a matched rule carries a risk level outside :data:`RISK_LEVELS`.
                Skipping it would silently under-classify, the one error US-4 cannot tolerate;
                :meth:`validate` reports every such rule at once before this is ever called.
        """
        rules = cls._rules(risk_map)
        best: tuple[int, str] | None = None
        for path in changed_paths:
            match = cls._best_match_for_path(path, rules)
            if match is not None and (best is None or match[0] > best[0]):
                best = match
        if best is None:
            default_risk = cls._default_risk(risk_map)
            return default_risk, f"default → {default_risk}"
        return RISK_LEVELS[best[0]], best[1]

    @classmethod
    def _best_match_for_path(cls, path: str, rules: Sequence[TomlValue]) -> tuple[int, str] | None:
        """The highest-ranked rule whose prefix matches ``path``, as ``(rank, "<prefix> → <risk>")``.

        ``None`` when no rule's prefix is a literal prefix of ``path``. Ties resolve to the
        first rule at the top rank, which is deterministic given the map's rule order.
        """
        best: tuple[int, str] | None = None
        for rule in rules:
            prefix, risk = cls._rule_fields(rule)
            if prefix is None or not path.startswith(prefix):
                continue
            rank = cls._risk_rank(risk)
            if best is None or rank > best[0]:
                best = (rank, f"{prefix} → {risk}")
        return best

    @staticmethod
    def _risk_rank(risk: str) -> int:
        """The severity rank of ``risk`` (its index in :data:`RISK_LEVELS`)."""
        try:
            return RISK_LEVELS.index(risk)
        except ValueError:
            raise ValueError(f"{risk!r} is not a risk level; expected one of {list(RISK_LEVELS)}") from None

    # ───────────────────────────── parse & validate ─────────────────────────────

    @staticmethod
    def parse(text: str) -> dict[str, TomlValue]:
        """Parse a ``risk-map.toml`` document with stdlib ``tomllib`` (no runtime dependency, NFR-5).

        Returns the raw value tree — ``version``, ``default``, and a ``rule`` list of
        ``{prefix, risk}`` tables (TOML ``[[rule]]``). Structural validity is :meth:`validate`'s job;
        this only turns bytes into a dict.
        """
        return tomllib.loads(text)

    @classmethod
    def validate(cls, risk_map: Mapping[str, TomlValue]) -> list[str]:
        """Every way a parsed risk map is malformed, as human-readable violation strings.

        Empty list means well-formed. Returns *all* problems rather than raising on the first, so a
        caller (``emit``, D-3) can report every bad rule at once — the same never-raise validator
        shape ``harbor.py`` uses.
        """
        violations: list[str] = []
        violations += cls._check_version(risk_map)
        violations += cls._check_default(risk_map)
        violations += cls._check_rules(risk_map)
        return violations

    @staticmethod
    def _check_version(risk_map: Mapping[str, TomlValue]) -> list[str]:
        version = risk_map.get("version")
        if not isinstance(version, str) or not version:
            return ["`version` is required and must be a non-empty string so datapoints can pin the map they used (FR-22)"]
        return []

    @classmethod
    def _check_default(cls, risk_map: Mapping[str, TomlValue]) -> list[str]:
        default = risk_map.get("default")
        if not isinstance(default, str) or default not in RISK_LEVELS:
            return [f"`default` must be one of {list(RISK_LEVELS)}, not {default!r}"]
        return []

    @classmethod
    def _check_rules(cls, risk_map: Mapping[str, TomlValue]) -> list[str]:
        raw = risk_map.get("rule")
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            return ["`rule` must be an array of tables ([[rule]]), each with a `prefix` and a `risk`"]
        violations: list[str] = []
        for index, rule in enumerate(raw):
            violations += cls._check_one_rule(index, rule)
        return violations

    @classmethod
    def _check_one_rule(cls, index: int, rule: TomlValue) -> list[str]:
        prefix, risk = cls._rule_fields(rule)
        violations: list[str] = []
        if prefix is None:
            violations.append(f"rule[{index}]: `prefix` is required and must be a string")
        if risk not in RISK_LEVELS:
            violations.append(f"rule[{index}]: `risk` must be one of {list(RISK_LEVELS)}, not {risk!r}")
        return violations

    # ───────────────────────────── template emission ─────────────────────────────

    @classmethod
    def template_document(cls) -> dict[str, TomlValue]:
        """The value tree behind ``risk-map.template.toml`` — the starting point ``init`` writes.

        Versioned (``v1``), a ``medium`` default, and two example rules that show the shape and the
        highest-wins intent. The agent overlays its repo's real high-risk directories onto this.
        """
        return {
            "version": "v1",
            "default": _FALLBACK_DEFAULT_RISK,
            "rule": [
                {"prefix": "src/payments/", "risk": "high"},
                {"prefix": "docs/", "risk": "low"},
            ],
        }

    @classmethod
    def emit_template(cls) -> bytes:
        """The ``risk-map.template.toml`` bytes: deterministic, LF-terminated UTF-8 (FR-22, NFR-1)."""
        return emit_document(cls.template_document())

    # ─────────────────────────────────── helpers ───────────────────────────────────

    @staticmethod
    def _rules(risk_map: Mapping[str, TomlValue]) -> Sequence[TomlValue]:
        """The map's ``rule`` list, or an empty tuple when absent or malformed (a map may be
        all-default: every path falls through to ``default``)."""
        raw = risk_map.get("rule")
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            return raw
        return ()

    @staticmethod
    def _default_risk(risk_map: Mapping[str, TomlValue]) -> str:
        """The map's ``default`` risk, falling back to :data:`_FALLBACK_DEFAULT_RISK` when absent."""
        default = risk_map.get("default")
        return default if isinstance(default, str) else _FALLBACK_DEFAULT_RISK

    @staticmethod
    def _rule_fields(rule: TomlValue) -> tuple[str | None, str]:
        """A rule's ``(prefix, risk)``; ``prefix`` is ``None`` and ``risk`` is ``""`` when malformed."""
        if not isinstance(rule, Mapping):
            return None, ""
        prefix = rule.get("prefix")
        risk = rule.get("risk")
        return (prefix if isinstance(prefix, str) else None, risk if isinstance(risk, str) else "")
