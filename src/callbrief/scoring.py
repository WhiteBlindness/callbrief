"""Explainable fit scoring with configurable weights and no inferred win odds."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from .domain import (
    EligibilityAssessment,
    EligibilityState,
    FitAssessment,
    FitComponent,
    FitSignals,
)


@dataclass(frozen=True, slots=True)
class FitWeights:
    eligibility: float = 0.16
    strategic_fit: float = 0.17
    probability_of_winning: float = 0.17
    funding_attractiveness: float = 0.10
    application_effort: float = 0.08
    time_to_deadline: float = 0.07
    company_capabilities: float = 0.10
    consortium_readiness: float = 0.05
    maturity: float = 0.05
    evidence_completeness: float = 0.05

    def __post_init__(self) -> None:
        values = tuple(getattr(self, name) for name in self.names())
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
            raise ValueError("Fit weights must be finite values between 0 and 1")
        if not any(values):
            raise ValueError("At least one fit weight must be greater than zero")

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return (
            "eligibility",
            "strategic_fit",
            "probability_of_winning",
            "funding_attractiveness",
            "application_effort",
            "time_to_deadline",
            "company_capabilities",
            "consortium_readiness",
            "maturity",
            "evidence_completeness",
        )

    def as_mapping(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in self.names()}


_SIGNAL_EXPLANATIONS = {
    "strategic_fit": "Pontuação de adequação estratégica fornecida na configuração da avaliação.",
    "probability_of_winning": (
        "Não existe um modelo histórico validado nem uma estimativa "
        "revista que sustente esta pontuação."
    ),
    "funding_attractiveness": (
        "Pontuação de atratividade do financiamento fornecida na configuração."
    ),
    "application_effort": "Pontuação de adequação ao esforço fornecida na configuração.",
    "time_to_deadline": "Pontuação de tempo até ao prazo fornecida na configuração.",
    "company_capabilities": "Pontuação de capacidades fornecida na configuração.",
    "consortium_readiness": "Pontuação de preparação do consórcio fornecida na configuração.",
    "maturity": "Pontuação de maturidade do projeto fornecida na configuração.",
    "evidence_completeness": "Pontuação de completude da evidência fornecida na configuração.",
}


def _eligibility_score(state: EligibilityState) -> tuple[int, str]:
    if state is EligibilityState.ELIGIBLE:
        return 100, "Todas as regras normalizadas e ligadas a evidência foram cumpridas."
    if state is EligibilityState.REMEDIABLE:
        return 55, "Pelo menos um requisito documentado ainda exige uma medida corretiva."
    return 35, "Pelo menos um requisito de elegibilidade continua por confirmar."


def score_fit(
    eligibility: EligibilityAssessment,
    signals: FitSignals,
    *,
    weights: FitWeights | None = None,
    assessed_at: datetime | None = None,
) -> FitAssessment:
    """Combine available 0-100 components; omit unknown components from the total."""
    selected_weights = weights or FitWeights()
    timestamp = assessed_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("assessed_at must include a timezone")

    eligibility_score, eligibility_explanation = _eligibility_score(eligibility.state)
    values: dict[str, int | None] = {
        "eligibility": eligibility_score,
        **{name: getattr(signals, name) for name in FitWeights.names() if name != "eligibility"},
    }
    components = tuple(
        FitComponent(
            name=name,
            score=values[name],
            weight=selected_weights.as_mapping()[name],
            explanation=(
                eligibility_explanation if name == "eligibility" else _SIGNAL_EXPLANATIONS[name]
            ),
        )
        for name in FitWeights.names()
    )

    if eligibility.state is EligibilityState.INELIGIBLE:
        return FitAssessment(
            organisation_id=eligibility.organisation_id,
            opportunity_id=eligibility.opportunity_id,
            overall=None,
            components=components,
            explanation=(
                "Não aplicável: falhou uma regra determinística de "
                "elegibilidade obrigatória."
            ),
            assessed_at=timestamp,
        )

    configured_weights = selected_weights.as_mapping()
    if not any(
        value is not None and name != "eligibility" and configured_weights[name] > 0
        for name, value in values.items()
    ):
        return FitAssessment(
            organisation_id=eligibility.organisation_id,
            opportunity_id=eligibility.opportunity_id,
            overall=None,
            components=components,
            explanation=(
                "A elegibilidade foi avaliada, mas ainda não há componentes de adequação "
                "pontuados para calcular uma correspondência global."
            ),
            assessed_at=timestamp,
        )

    denominator = sum(
        component.weight
        for component in components
        if component.score is not None and component.weight > 0
    )
    numerator = sum(
        component.score * component.weight
        for component in components
        if component.score is not None and component.weight > 0
    )
    overall = int(math.floor(numerator / denominator + 0.5)) if denominator else None
    if overall is None:
        explanation = "Não há componentes pontuados disponíveis."
    else:
        explanation = (
            "Média ponderada dos componentes disponíveis; os componentes "
            "desconhecidos ficam de fora e os respetivos pesos são redistribuídos."
        )
    return FitAssessment(
        organisation_id=eligibility.organisation_id,
        opportunity_id=eligibility.opportunity_id,
        overall=overall,
        components=components,
        explanation=explanation,
        assessed_at=timestamp,
    )
