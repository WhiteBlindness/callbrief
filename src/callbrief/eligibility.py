"""Deterministic rule evaluation with explicit uncertainty and evidence checks."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import (
    EligibilityAssessment,
    EligibilityFinding,
    EligibilityRule,
    EligibilityState,
    Opportunity,
    OrganisationProfile,
    RuleOperator,
)

_PROFILE_FIELDS = frozenset(
    {
        "country",
        "regions",
        "applicant_types",
        "company_size",
        "employees",
        "turnover",
        "balance_sheet",
        "financial_autonomy",
        "sectors",
        "activities",
        "certifications",
        "consortium_capability",
    }
)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def _rule_matches(rule: EligibilityRule, actual: Any) -> bool | None:
    expected = rule.expected
    if rule.operator is RuleOperator.PRESENT:
        if actual is None:
            return None
        return actual != "" and actual != ()
    if actual is None or expected is None:
        return None
    if rule.operator is RuleOperator.EQUALS:
        if isinstance(actual, str) and isinstance(expected, str):
            return _normalise(actual) == _normalise(expected)
        left = _decimal(actual)
        right = _decimal(expected)
        if left is not None and right is not None:
            return left == right
        return None
    if rule.operator is RuleOperator.IN:
        candidates = expected if isinstance(expected, tuple) else (expected,)
        if isinstance(actual, str):
            if not all(isinstance(item, str) for item in candidates):
                return None
            return _normalise(actual) in {_normalise(item) for item in candidates}
        if isinstance(actual, tuple):
            if not all(isinstance(item, str) for item in (*actual, *candidates)):
                return None
            actual_values = {_normalise(item) for item in actual}
            expected_values = {_normalise(item) for item in candidates}
            return bool(actual_values & expected_values)
        return None
    if rule.operator is RuleOperator.INTERSECTS:
        actual_values = actual if isinstance(actual, (tuple, list)) else (actual,)
        expected_values = expected if isinstance(expected, tuple) else (expected,)
        if not all(isinstance(item, str) for item in (*actual_values, *expected_values)):
            return None
        return bool(
            {_normalise(item) for item in actual_values}
            & {_normalise(item) for item in expected_values}
        )
    left = _decimal(actual)
    right = _decimal(expected)
    if left is None or right is None:
        return None
    if rule.operator is RuleOperator.MINIMUM:
        return left >= right
    if rule.operator is RuleOperator.MAXIMUM:
        return left <= right
    return None


def _finding(
    rule: EligibilityRule, profile: OrganisationProfile, opportunity: Opportunity
) -> EligibilityFinding:
    evidence_by_id = {item.evidence_id: item for item in opportunity.evidence}
    valid_evidence = tuple(item for item in rule.evidence_ids if item in evidence_by_id)
    if not valid_evidence:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.UNCERTAIN,
            requirement=rule.reason,
            reason="A evidência de origem desta regra não existe ou não corresponde a este aviso.",
            evidence_ids=(),
        )
    if rule.profile_field not in _PROFILE_FIELDS:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.UNCERTAIN,
            requirement=rule.reason,
            reason="A regra usa um campo de perfil que não é suportado.",
            evidence_ids=valid_evidence,
        )

    actual = getattr(profile, rule.profile_field)
    matches = _rule_matches(rule, actual)
    if matches is None:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.UNCERTAIN,
            requirement=rule.reason,
            reason=(
                f"O valor do perfil para {rule.profile_field} é desconhecido "
                "ou não pode ser comparado."
            ),
            evidence_ids=valid_evidence,
        )
    if matches:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.ELIGIBLE,
            requirement=rule.reason,
            reason="O valor disponível no perfil cumpre esta regra documentada.",
            evidence_ids=valid_evidence,
        )
    if rule.remediation_if_any:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.REMEDIABLE,
            requirement=rule.reason,
            reason="O valor disponível no perfil não cumpre esta regra neste momento.",
            evidence_ids=valid_evidence,
            remediation_if_any=rule.remediation_if_any,
        )
    if rule.hard_gate:
        return EligibilityFinding(
            rule_id=rule.rule_id,
            state=EligibilityState.INELIGIBLE,
            requirement=rule.reason,
            reason="O valor disponível no perfil falha este requisito obrigatório documentado.",
            evidence_ids=valid_evidence,
        )
    return EligibilityFinding(
        rule_id=rule.rule_id,
        state=EligibilityState.UNCERTAIN,
        requirement=rule.reason,
        reason=(
            "O valor disponível no perfil não cumpre este requisito, mas o efeito "
            "não foi definido como impeditivo."
        ),
        evidence_ids=valid_evidence,
    )


def _overall_state(findings: tuple[EligibilityFinding, ...]) -> EligibilityState:
    states = {item.state for item in findings}
    if EligibilityState.INELIGIBLE in states:
        return EligibilityState.INELIGIBLE
    if EligibilityState.UNCERTAIN in states:
        return EligibilityState.UNCERTAIN
    if EligibilityState.REMEDIABLE in states:
        return EligibilityState.REMEDIABLE
    return EligibilityState.ELIGIBLE


def assess_eligibility(
    opportunity: Opportunity,
    profile: OrganisationProfile,
    *,
    assessed_at: datetime | None = None,
) -> EligibilityAssessment:
    """Evaluate only explicit rules supported by evidence attached to the call."""
    timestamp = assessed_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("assessed_at must include a timezone")
    if not opportunity.eligibility_rules:
        findings = (
            EligibilityFinding(
                rule_id="no-published-rules",
                state=EligibilityState.UNCERTAIN,
                requirement="Requisitos de elegibilidade publicados",
                reason="Esta fonte ainda não tem regras explícitas de elegibilidade normalizadas.",
                evidence_ids=(),
            ),
        )
    else:
        findings = tuple(
            _finding(rule, profile, opportunity) for rule in opportunity.eligibility_rules
        )
    return EligibilityAssessment(
        organisation_id=profile.id,
        opportunity_id=opportunity.id,
        state=_overall_state(findings),
        findings=findings,
        assessed_at=timestamp,
    )
