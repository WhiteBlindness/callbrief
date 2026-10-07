"""Immutable funding-domain models with explicit unknown values and provenance."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlparse


class DomainValidationError(ValueError):
    """Raised when a funding-domain record violates its data contract."""


class OpportunityStatus(StrEnum):
    UPCOMING = "upcoming"
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class OpportunityType(StrEnum):
    GRANT = "grant"
    REPAYABLE_INCENTIVE = "repayable_incentive"
    LOAN = "loan"
    GUARANTEE = "guarantee"
    TAX_INCENTIVE = "tax_incentive"
    FINANCIAL_INSTRUMENT = "financial_instrument"
    PUBLIC_PROGRAMME = "public_programme"
    TENDER = "tender"
    UNKNOWN = "unknown"


class EligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    REMEDIABLE = "remediable"
    UNCERTAIN = "uncertain"


class RuleOperator(StrEnum):
    EQUALS = "equals"
    IN = "in"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    INTERSECTS = "intersects"
    PRESENT = "present"


class Cadence(StrEnum):
    DAILY = "daily"
    TWICE_WEEKLY = "twice_weekly"
    WEEKLY = "weekly"
    MANUAL = "manual"


class EvidenceProvenance(StrEnum):
    LIVE_SOURCE_VERIFIED = "LIVE_SOURCE_VERIFIED"
    CAPTURED_FIXTURE = "CAPTURED_FIXTURE"
    MANUALLY_TRANSCRIBED = "MANUALLY_TRANSCRIBED"


def _required_text(value: str, name: str, maximum: int = 500) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{name} must contain between 1 and {maximum} characters")


def _optional_decimal(value: Decimal | None, name: str) -> None:
    if value is not None and (not value.is_finite() or value < 0):
        raise DomainValidationError(f"{name} must be a finite, non-negative decimal")


def _https_url(value: str, name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DomainValidationError(f"{name} must be an HTTPS URL without embedded credentials")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    source_id: str
    url: str
    retrieved_at: datetime
    section: str | None
    start: int
    end: int
    excerpt: str
    source_hash: str
    provenance_status: EvidenceProvenance = EvidenceProvenance.CAPTURED_FIXTURE
    source_payload_sha256: str | None = None
    normalized_snapshot: str | None = None

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.evidence_id, "evidence_id", 80),
            (self.source_id, "source_id", 100),
            (self.excerpt, "excerpt", 4000),
        ):
            _required_text(value, name, maximum)
        _https_url(self.url, "url")
        if self.start < 0 or self.end < self.start or self.end - self.start != len(self.excerpt):
            raise DomainValidationError("Evidence span must match the exact excerpt boundaries")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_hash):
            raise DomainValidationError("source_hash must be a SHA-256 hex digest")
        if self.source_payload_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.source_payload_sha256
        ):
            raise DomainValidationError("source_payload_sha256 must be a SHA-256 hex digest")
        if not isinstance(self.provenance_status, EvidenceProvenance):
            try:
                object.__setattr__(
                    self, "provenance_status", EvidenceProvenance(self.provenance_status)
                )
            except ValueError as exc:
                raise DomainValidationError("Unsupported evidence provenance status") from exc
        if self.provenance_status is EvidenceProvenance.LIVE_SOURCE_VERIFIED:
            if self.source_payload_sha256 is None:
                raise DomainValidationError("Live-source evidence requires its response SHA-256")
        if (
            self.normalized_snapshot is not None
            and len(self.normalized_snapshot.encode("utf-8")) > 16 * 1024
        ):
            raise DomainValidationError("normalized_snapshot cannot exceed 16 KiB")
        if self.retrieved_at.tzinfo is None:
            raise DomainValidationError("retrieved_at must include a timezone")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    source_url: str
    retrieved_at: datetime
    content_type: str
    title: str
    text: str
    metadata: tuple[tuple[str, str], ...] = ()
    discovered_links: tuple[str, ...] = ()
    etag: str | None = None
    last_modified: str | None = None
    provenance_status: EvidenceProvenance = EvidenceProvenance.CAPTURED_FIXTURE
    source_payload_sha256: str | None = None
    normalized_snapshot: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.source_id, "source_id", 100)
        _required_text(self.content_type, "content_type", 100)
        _required_text(self.title, "title", 300)
        _https_url(self.source_url, "source_url")
        if not isinstance(self.text, str) or len(self.text.encode("utf-8")) > 5 * 1024 * 1024:
            raise DomainValidationError("text must be UTF-8 text no larger than 5 MiB")
        if self.retrieved_at.tzinfo is None:
            raise DomainValidationError("retrieved_at must include a timezone")
        if len(self.metadata) > 100 or len(self.discovered_links) > 500:
            raise DomainValidationError("source metadata or discovered links exceed their limits")
        if not isinstance(self.provenance_status, EvidenceProvenance):
            try:
                object.__setattr__(
                    self, "provenance_status", EvidenceProvenance(self.provenance_status)
                )
            except ValueError as exc:
                raise DomainValidationError("Unsupported source provenance status") from exc
        if self.source_payload_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.source_payload_sha256
        ):
            raise DomainValidationError("source_payload_sha256 must be a SHA-256 hex digest")
        if self.provenance_status is EvidenceProvenance.LIVE_SOURCE_VERIFIED:
            if self.source_payload_sha256 is None:
                raise DomainValidationError("Live source documents require their response SHA-256")
        if (
            self.normalized_snapshot is not None
            and len(self.normalized_snapshot.encode("utf-8")) > 16 * 1024
        ):
            raise DomainValidationError("normalized_snapshot cannot exceed 16 KiB")
        for link in self.discovered_links:
            _https_url(link, "discovered_links item")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def evidence(self, start: int, end: int, *, section: str | None = None) -> EvidenceReference:
        if not 0 <= start < end <= len(self.text):
            raise DomainValidationError("Evidence span is outside the source document")
        excerpt = self.text[start:end]
        digest = hashlib.sha256(
            f"{self.source_id}\0{self.source_url}\0{section or ''}\0{excerpt}".encode()
        ).hexdigest()
        return EvidenceReference(
            evidence_id=f"ev-{digest[:20]}",
            source_id=self.source_id,
            url=self.source_url,
            retrieved_at=self.retrieved_at,
            section=section,
            start=start,
            end=end,
            excerpt=excerpt,
            source_hash=self.content_hash,
            provenance_status=self.provenance_status,
            source_payload_sha256=self.source_payload_sha256,
            normalized_snapshot=self.normalized_snapshot,
        )


@dataclass(frozen=True, slots=True)
class EligibilityRule:
    rule_id: str
    profile_field: str
    operator: RuleOperator
    expected: str | Decimal | tuple[str, ...] | None
    reason: str
    evidence_ids: tuple[str, ...]
    hard_gate: bool = True
    remediation_if_any: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.rule_id, "rule_id", 100)
        _required_text(self.profile_field, "profile_field", 100)
        _required_text(self.reason, "reason", 1000)
        if len(self.evidence_ids) > 12 or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise DomainValidationError("evidence_ids must be unique and contain at most 12 items")
        if self.remediation_if_any is not None:
            _required_text(self.remediation_if_any, "remediation_if_any", 1000)


@dataclass(frozen=True, slots=True)
class Opportunity:
    id: str
    source_id: str
    source_record_id: str
    programme: str | None
    title: str | None
    authority: str | None
    source_retrieved_at: datetime
    call_id: str | None = None
    canonical_url: str | None = None
    status: OpportunityStatus = OpportunityStatus.UNKNOWN
    opportunity_type: OpportunityType = OpportunityType.UNKNOWN
    publication_date: datetime | None = None
    opening_date: datetime | None = None
    deadline: datetime | None = None
    additional_deadlines: tuple[datetime, ...] | None = None
    geography: tuple[str, ...] | None = None
    eligible_applicant_types: tuple[str, ...] | None = None
    eligible_company_sizes: tuple[str, ...] | None = None
    eligible_sectors: tuple[str, ...] | None = None
    eligible_regions: tuple[str, ...] | None = None
    eligible_activities: tuple[str, ...] | None = None
    excluded_activities: tuple[str, ...] | None = None
    budget_total: Decimal | None = None
    funding_min: Decimal | None = None
    funding_max: Decimal | None = None
    aid_intensity: Decimal | None = None
    project_cost_min: Decimal | None = None
    project_cost_max: Decimal | None = None
    trl_min: int | None = None
    trl_max: int | None = None
    consortium_rules: str | None = None
    project_duration: str | None = None
    financial_requirements: tuple[str, ...] | None = None
    other_hard_requirements: tuple[str, ...] | None = None
    documents: tuple[str, ...] | None = None
    eligibility_rules: tuple[EligibilityRule, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    source_updated_at: datetime | None = None
    last_checked_at: datetime | None = None
    raw_source_reference: str | None = None
    duplicate_of: str | None = None
    source_family: str | None = None
    source_role: str | None = None
    canonical_source: str | None = None
    authority_relationship: str | None = None

    def __post_init__(self) -> None:
        for required_value, required_name in (
            (self.id, "id"),
            (self.source_id, "source_id"),
            (self.source_record_id, "source_record_id"),
        ):
            _required_text(required_value, required_name)
        for optional_value, optional_name in (
            (self.programme, "programme"),
            (self.title, "title"),
            (self.authority, "authority"),
            (self.source_family, "source_family"),
            (self.source_role, "source_role"),
            (self.canonical_source, "canonical_source"),
            (self.authority_relationship, "authority_relationship"),
        ):
            if optional_value is not None:
                _required_text(optional_value, optional_name)
        if self.canonical_url is not None:
            _https_url(self.canonical_url, "canonical_url")
        if self.source_retrieved_at.tzinfo is None:
            raise DomainValidationError("source_retrieved_at must include a timezone")
        for name in (
            "budget_total",
            "funding_min",
            "funding_max",
            "aid_intensity",
            "project_cost_min",
            "project_cost_max",
        ):
            _optional_decimal(getattr(self, name), name)
        if self.trl_min is not None and not 0 <= self.trl_min <= 9:
            raise DomainValidationError("trl_min must be between 0 and 9")
        if self.trl_max is not None and not 0 <= self.trl_max <= 9:
            raise DomainValidationError("trl_max must be between 0 and 9")
        if self.aid_intensity is not None and self.aid_intensity > 1:
            raise DomainValidationError("aid_intensity must be a fraction between 0 and 1")


@dataclass(frozen=True, slots=True)
class OrganisationProfile:
    id: str
    workspace_id: str
    legal_name: str | None = None
    vat_number: str | None = None
    country: str | None = None
    regions: tuple[str, ...] | None = None
    applicant_types: tuple[str, ...] | None = None
    company_size: str | None = None
    employees: int | None = None
    turnover: Decimal | None = None
    balance_sheet: Decimal | None = None
    financial_autonomy: Decimal | None = None
    sectors: tuple[str, ...] | None = None
    activities: tuple[str, ...] | None = None
    technologies: tuple[str, ...] | None = None
    products_services: tuple[str, ...] | None = None
    rd_activity: str | None = None
    previous_rd_projects: tuple[str, ...] | None = None
    certifications: tuple[str, ...] | None = None
    export_activity: str | None = None
    internationalisation: str | None = None
    investment_plans: tuple[str, ...] | None = None
    project_ideas: tuple[str, ...] | None = None
    consortium_capability: str | None = None
    preferred_opportunity_types: tuple[OpportunityType, ...] | None = None
    excluded_opportunity_types: tuple[OpportunityType, ...] | None = None
    preferred_geographies: tuple[str, ...] | None = None
    risk_tolerance: str | None = None
    effort_tolerance: str | None = None
    evidence: tuple[EvidenceReference, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.id, "id", 100)
        _required_text(self.workspace_id, "workspace_id", 100)
        if self.legal_name is not None:
            _required_text(self.legal_name, "legal_name", 300)
        _optional_decimal(self.turnover, "turnover")
        _optional_decimal(self.balance_sheet, "balance_sheet")
        _optional_decimal(self.financial_autonomy, "financial_autonomy")
        if self.financial_autonomy is not None and self.financial_autonomy > 1:
            raise DomainValidationError("financial_autonomy must be a fraction between 0 and 1")
        if self.employees is not None and self.employees < 0:
            raise DomainValidationError("employees must be non-negative")


@dataclass(frozen=True, slots=True)
class EligibilityFinding:
    rule_id: str
    state: EligibilityState
    requirement: str
    reason: str
    evidence_ids: tuple[str, ...]
    remediation_if_any: str | None = None


@dataclass(frozen=True, slots=True)
class EligibilityAssessment:
    organisation_id: str
    opportunity_id: str
    state: EligibilityState
    findings: tuple[EligibilityFinding, ...]
    assessed_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.organisation_id, "organisation_id", 100)
        _required_text(self.opportunity_id, "opportunity_id", 100)
        if self.assessed_at.tzinfo is None:
            raise DomainValidationError("assessed_at must include a timezone")


@dataclass(frozen=True, slots=True)
class FitSignals:
    strategic_fit: int | None = None
    probability_of_winning: int | None = None
    funding_attractiveness: int | None = None
    application_effort: int | None = None
    time_to_deadline: int | None = None
    company_capabilities: int | None = None
    consortium_readiness: int | None = None
    maturity: int | None = None
    evidence_completeness: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "strategic_fit",
            "probability_of_winning",
            "funding_attractiveness",
            "application_effort",
            "time_to_deadline",
            "company_capabilities",
            "consortium_readiness",
            "maturity",
            "evidence_completeness",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not 0 <= value <= 100):
                raise DomainValidationError(f"{name} must be between 0 and 100 or unknown")


@dataclass(frozen=True, slots=True)
class FitComponent:
    name: str
    score: int | None
    weight: float
    explanation: str

    def __post_init__(self) -> None:
        _required_text(self.name, "name", 100)
        if self.score is not None and (isinstance(self.score, bool) or not 0 <= self.score <= 100):
            raise DomainValidationError("Fit component score must be between 0 and 100 or unknown")
        if not math.isfinite(self.weight) or not 0 <= self.weight <= 1:
            raise DomainValidationError("Fit component weight must be finite and between 0 and 1")
        _required_text(self.explanation, "explanation", 1000)


@dataclass(frozen=True, slots=True)
class FitAssessment:
    organisation_id: str
    opportunity_id: str
    overall: int | None
    components: tuple[FitComponent, ...]
    explanation: str
    assessed_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.organisation_id, "organisation_id", 100)
        _required_text(self.opportunity_id, "opportunity_id", 100)
        if self.overall is not None and (
            isinstance(self.overall, bool) or not 0 <= self.overall <= 100
        ):
            raise DomainValidationError("Overall fit score must be between 0 and 100 or unknown")
        if self.assessed_at.tzinfo is None:
            raise DomainValidationError("assessed_at must include a timezone")
        _required_text(self.explanation, "explanation", 1000)


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    change_id: str
    opportunity_id: str
    field: str
    old_value: str | None
    new_value: str | None
    detected_at: datetime
    source_id: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    event_type: str
    opportunity_id: str
    created_at: datetime
    workspace_id: str | None = None
    organisation_id: str | None = None
    payload: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.event_type, "event_type"),
            (self.opportunity_id, "opportunity_id"),
        ):
            _required_text(value, name, 100)
        if self.created_at.tzinfo is None:
            raise DomainValidationError("created_at must include a timezone")
        if (self.workspace_id is None) != (self.organisation_id is None):
            raise DomainValidationError(
                "workspace_id and organisation_id must both be present or both be unknown"
            )
