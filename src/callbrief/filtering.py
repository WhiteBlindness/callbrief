"""Deterministic opportunity filters with explicit review for unknown fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .domain import Opportunity, OpportunityType, OrganisationProfile


class FilterState(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class OpportunityFilter:
    opportunity_types: tuple[OpportunityType, ...] | None = None
    excluded_types: tuple[OpportunityType, ...] = ()
    applicant_types: tuple[str, ...] | None = None
    regions: tuple[str, ...] | None = None
    geographies: tuple[str, ...] | None = None
    sectors: tuple[str, ...] | None = None
    minimum_funding: Decimal | None = None
    deadline_window_days: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_funding is not None and (
            not self.minimum_funding.is_finite() or self.minimum_funding < 0
        ):
            raise ValueError("minimum_funding must be a non-negative finite amount")
        if self.deadline_window_days is not None and self.deadline_window_days < 1:
            raise ValueError("deadline_window_days must be positive")


@dataclass(frozen=True, slots=True)
class FilterResult:
    opportunity_id: str
    state: FilterState
    reasons: tuple[str, ...]


def _normalized(values: tuple[str, ...]) -> set[str]:
    return {" ".join(item.casefold().split()) for item in values}


def filter_opportunity(
    opportunity: Opportunity,
    selected: OpportunityFilter,
    *,
    profile: OrganisationProfile | None = None,
    now: datetime | None = None,
) -> FilterResult:
    """Exclude explicit mismatches; keep unknown criteria visible for review."""
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("now must include a timezone")
    types = selected.opportunity_types or (
        tuple(profile.preferred_opportunity_types)
        if profile and profile.preferred_opportunity_types
        else None
    )
    excluded_types = set(selected.excluded_types)
    if profile and profile.excluded_opportunity_types:
        excluded_types.update(profile.excluded_opportunity_types)
    regions = selected.regions or (profile.regions if profile and profile.regions else None)
    geographies = selected.geographies or (
        profile.preferred_geographies if profile and profile.preferred_geographies else None
    )
    sectors = selected.sectors or (profile.sectors if profile and profile.sectors else None)
    applicants = selected.applicant_types or (
        profile.applicant_types if profile and profile.applicant_types else None
    )

    reasons: list[str] = []
    excluded = False
    if types:
        if opportunity.opportunity_type is OpportunityType.UNKNOWN:
            reasons.append("O tipo de financiamento não está identificado.")
        elif opportunity.opportunity_type not in types:
            excluded = True
            reasons.append("O tipo de financiamento não corresponde ao filtro selecionado.")
    if opportunity.opportunity_type in excluded_types:
        excluded = True
        reasons.append("O perfil exclui este tipo de financiamento.")
    elif excluded_types and opportunity.opportunity_type is OpportunityType.UNKNOWN:
        reasons.append("O tipo desconhecido pode corresponder a uma preferência excluída.")
    if regions:
        if opportunity.eligible_regions is None:
            reasons.append("As regiões elegíveis do aviso são desconhecidas.")
        elif not _normalized(tuple(regions)) & _normalized(opportunity.eligible_regions):
            excluded = True
            reasons.append(
                "A região da organização não consta entre as regiões elegíveis conhecidas."
            )
    if geographies:
        if opportunity.geography is None:
            reasons.append("A geografia do aviso é desconhecida.")
        elif not _normalized(tuple(geographies)) & _normalized(opportunity.geography):
            excluded = True
            reasons.append("A geografia do aviso não corresponde ao filtro selecionado.")
    if sectors:
        if opportunity.eligible_sectors is None:
            reasons.append("Os setores elegíveis do aviso são desconhecidos.")
        elif not _normalized(tuple(sectors)) & _normalized(opportunity.eligible_sectors):
            excluded = True
            reasons.append(
                "Os setores do perfil não constam entre os setores elegíveis conhecidos."
            )
    if applicants:
        if opportunity.eligible_applicant_types is None:
            reasons.append("Os tipos de beneficiário do aviso são desconhecidos.")
        elif not _normalized(tuple(applicants)) & _normalized(opportunity.eligible_applicant_types):
            excluded = True
            reasons.append("O tipo de organização não corresponde aos beneficiários conhecidos.")
    if selected.minimum_funding is not None:
        if opportunity.funding_max is None:
            reasons.append("O montante máximo de apoio é desconhecido.")
        elif opportunity.funding_max < selected.minimum_funding:
            excluded = True
            reasons.append("O montante máximo fica abaixo do mínimo selecionado.")
    if selected.deadline_window_days is not None:
        if opportunity.deadline is None:
            reasons.append("O prazo de candidatura é desconhecido.")
        elif (
            not timestamp
            <= opportunity.deadline
            <= timestamp + timedelta(days=selected.deadline_window_days)
        ):
            excluded = True
            reasons.append("O prazo não está dentro da janela selecionada.")

    if excluded:
        state = FilterState.EXCLUDED
    elif reasons:
        state = FilterState.NEEDS_REVIEW
    else:
        state = FilterState.INCLUDED
    return FilterResult(opportunity.id, state, tuple(reasons))
