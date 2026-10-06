"""Provider boundary for optional organisation facts discovered from a VAT number."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .domain import EvidenceReference, OrganisationProfile


class OrganisationEnrichmentProvider(Protocol):
    """A provider receives only the organisation's VAT number."""

    @property
    def provider_id(self) -> str: ...

    def enrich(self, vat_number: str) -> tuple[EnrichmentFact, ...]: ...


@dataclass(frozen=True, slots=True)
class EnrichmentFact:
    profile_field: str
    value: str | int | Decimal | tuple[str, ...]
    provider_id: str
    retrieved_at: datetime
    evidence: EvidenceReference

    def __post_init__(self) -> None:
        if not self.profile_field.strip() or len(self.profile_field) > 100:
            raise ValueError("O campo enriquecido tem um identificador inválido.")
        if not self.provider_id.strip() or len(self.provider_id) > 100:
            raise ValueError("O fornecedor de enriquecimento tem um identificador inválido.")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("A data de obtenção do dado tem de incluir o fuso horário.")
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise ValueError("O valor financeiro enriquecido tem de ser finito.")


_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "legal_name": (str,),
    "country": (str,),
    "regions": (tuple,),
    "applicant_types": (tuple,),
    "company_size": (str,),
    "employees": (int,),
    "turnover": (Decimal,),
    "balance_sheet": (Decimal,),
    "financial_autonomy": (Decimal,),
    "sectors": (tuple,),
    "activities": (tuple,),
    "technologies": (tuple,),
    "products_services": (tuple,),
    "certifications": (tuple,),
    "export_activity": (str,),
    "internationalisation": (str,),
}


def apply_enrichment(
    profile: OrganisationProfile,
    facts: tuple[EnrichmentFact, ...],
) -> OrganisationProfile:
    """Fill unknown profile fields only and retain evidence for every accepted fact."""
    changes: dict[str, Any] = {}
    evidence = {item.evidence_id: item for item in profile.evidence}
    for fact in facts:
        expected_types = _FIELD_TYPES.get(fact.profile_field)
        if expected_types is None:
            raise ValueError(f"O campo '{fact.profile_field}' não pode ser enriquecido.")
        if not isinstance(fact.value, expected_types) or isinstance(fact.value, bool):
            raise ValueError(f"O valor de '{fact.profile_field}' tem um tipo incompatível.")
        current = changes.get(fact.profile_field, getattr(profile, fact.profile_field))
        if current is None:
            if isinstance(fact.value, tuple) and not all(
                isinstance(item, str) and item.strip() for item in fact.value
            ):
                raise ValueError("Os valores de lista enriquecidos têm de ser texto não vazio.")
            changes[fact.profile_field] = fact.value
            evidence[fact.evidence.evidence_id] = fact.evidence
        elif current == fact.value:
            evidence[fact.evidence.evidence_id] = fact.evidence
    changes["evidence"] = tuple(evidence.values())
    return replace(profile, **changes)


def enrich_organisation(
    profile: OrganisationProfile,
    provider: OrganisationEnrichmentProvider,
) -> OrganisationProfile:
    """Call an explicitly supplied provider with the VAT number, never the full profile."""
    if not profile.vat_number:
        raise ValueError("O perfil não tem número de identificação fiscal para enriquecimento.")
    facts = provider.enrich(profile.vat_number)
    if any(fact.provider_id != provider.provider_id for fact in facts):
        raise ValueError("O fornecedor devolveu dados com uma proveniência incompatível.")
    return apply_enrichment(profile, facts)
