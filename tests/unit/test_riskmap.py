"""Tests for the structural risk computation and the versioned risk-map artefact (task C-2).

The contract is fourfold: ``structural_risk`` takes the *highest* matching level across all changed
paths (under-classifying a risky change is the expensive error US-4 cannot tolerate) and names the
rule it matched; an unmatched path falls to ``default``; the template emits byte-stably through
``tomlw`` (the artefact is versioned, so a nondeterministic template breaks it); and a risk level
outside {low, medium, high} is rejected rather than silently accepted.
"""

import tomllib

from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import TomlValue


def _risk_map(rules: list[dict[str, str]], *, default: str = "medium", version: str = "v1") -> dict[str, TomlValue]:
    """A parsed risk map with the given rules — the shape ``tomllib`` produces from ``[[rule]]``."""
    rule_tables: list[TomlValue] = [{"prefix": rule["prefix"], "risk": rule["risk"]} for rule in rules]
    return {"version": version, "default": default, "rule": rule_tables}


def test_highest_matching_risk_wins() -> None:
    """A change touching a high-risk and a low-risk path resolves to high, not the first match.

    Catches a risky change under-classified because a low-risk path happened to match first.
    """
    risk_map = _risk_map([{"prefix": "docs/", "risk": "low"}, {"prefix": "src/payments/", "risk": "high"}])
    risk, matched_rule = RiskMap.structural_risk(["docs/x.md", "src/payments/charge.py"], risk_map)
    assert risk == "high"
    assert matched_rule == "src/payments/ → high"


def test_unmatched_path_falls_to_default() -> None:
    """A path matching no rule uses the map's ``default`` (the monorepo ``cdk/src/`` case)."""
    risk_map = _risk_map([{"prefix": "src/payments/", "risk": "high"}], default="medium")
    risk, matched_rule = RiskMap.structural_risk(["cdk/src/app.py"], risk_map)
    assert risk == "medium"
    assert matched_rule == "default → medium"


def test_matched_rule_recorded() -> None:
    """The returned matched-rule string names the prefix and the level (the ``risk_structural_rule``
    audit trail, FR-20) — not a bare level with the prefix lost."""
    risk_map = _risk_map([{"prefix": "src/payments/", "risk": "high"}])
    _, matched_rule = RiskMap.structural_risk(["src/payments/charge.py"], risk_map)
    assert matched_rule == "src/payments/ → high"


def test_template_emitted_byte_stable() -> None:
    """The template bytes are identical across two emits and parse back to the same document."""
    first = RiskMap.emit_template()
    second = RiskMap.emit_template()
    assert first == second
    round_tripped = tomllib.loads(first.decode("utf-8"))
    assert round_tripped == RiskMap.template_document()
    assert RiskMap.validate(round_tripped) == []


def test_invalid_risk_level_rejected() -> None:
    """A rule with a risk outside {low, medium, high} is reported as a violation, not accepted."""
    risk_map = _risk_map([{"prefix": "src/", "risk": "critical"}])
    violations = RiskMap.validate(risk_map)
    assert violations
    assert any("critical" in violation for violation in violations)


def test_low_risk_only_change_resolves_low() -> None:
    """A change touching only a low-risk path resolves to low (the hand-verify's second case)."""
    risk_map = _risk_map([{"prefix": "src/payments/", "risk": "high"}, {"prefix": "docs/", "risk": "low"}])
    risk, matched_rule = RiskMap.structural_risk(["docs/guide.md"], risk_map)
    assert risk == "low"
    assert matched_rule == "docs/ → low"


def test_no_rules_falls_to_default() -> None:
    """An all-default map (no rules) sends every path to ``default`` without error."""
    risk, matched_rule = RiskMap.structural_risk(["src/payments/charge.py"], _risk_map([], default="high"))
    assert risk == "high"
    assert matched_rule == "default → high"


def test_valid_template_has_no_violations() -> None:
    """The shipped template document is itself well-formed against ``validate``."""
    assert RiskMap.validate(RiskMap.template_document()) == []


def test_parse_reads_versioned_rules() -> None:
    """``parse`` turns TOML text into the versioned value tree ``structural_risk`` consumes."""
    text = 'version = "v2"\ndefault = "low"\n\n[[rule]]\nprefix = "src/payments/"\nrisk = "high"\n'
    parsed = RiskMap.parse(text)
    assert parsed["version"] == "v2"
    assert RiskMap.structural_risk(["src/payments/charge.py"], parsed) == ("high", "src/payments/ → high")
