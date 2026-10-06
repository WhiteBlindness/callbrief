"""Conservative field normalization for structured source documents."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import (
    EligibilityRule,
    EvidenceReference,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    RuleOperator,
    SourceDocument,
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "record_id": ("record_id", "id", "topicCode", "callIdentifier", "identifier"),
    "programme": ("programme", "programmeName", "frameworkProgramme", "programName"),
    "title": ("title", "title_en", "titleEN", "name", "subject"),
    "authority": ("authority", "managingAuthority", "organisation", "organization", "agency"),
    "call_id": ("topicCode", "callIdentifier", "callId", "identifier"),
    "consortium_rules": ("consortiumRules", "consortiumRequirements"),
    "project_duration": ("projectDuration", "duration"),
    "source_updated_at": ("sourceUpdatedAt", "updatedAt", "lastUpdated", "modificationDate"),
}
_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "geography": ("geography", "countries", "eligibleCountries"),
    "eligible_applicant_types": ("eligibleApplicantTypes", "applicantTypes", "beneficiaries"),
    "eligible_company_sizes": ("eligibleCompanySizes", "companySizes"),
    "eligible_sectors": ("eligibleSectors", "sectors"),
    "eligible_regions": ("eligibleRegions", "regions"),
    "eligible_activities": ("eligibleActivities", "activities"),
    "excluded_activities": ("excludedActivities",),
    "financial_requirements": ("financialRequirements",),
    "other_hard_requirements": ("otherHardRequirements", "requirements"),
    "documents": ("documents", "documentsToProvide"),
}
_DECIMAL_FIELDS: dict[str, tuple[str, ...]] = {
    "budget_total": ("budgetTotal", "totalBudget", "callBudget"),
    "funding_min": ("fundingMin", "minFunding", "minimumGrant"),
    "funding_max": ("fundingMax", "maxFunding", "maximumGrant"),
    "aid_intensity": ("aidIntensity", "fundingRate", "coFundingRate"),
    "project_cost_min": ("projectCostMin", "minimumProjectCost"),
    "project_cost_max": ("projectCostMax", "maximumProjectCost"),
}
_INTEGER_FIELDS: dict[str, tuple[str, ...]] = {
    "trl_min": ("trlMin", "technologyReadinessLevelMin"),
    "trl_max": ("trlMax", "technologyReadinessLevelMax"),
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _record(document: SourceDocument) -> dict[str, Any]:
    try:
        value = json.loads(document.text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    return dict(document.metadata)


def _lookup(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, Any] | None:
    keyed = {_key(name): (name, value) for name, value in record.items()}
    for alias in aliases:
        found = keyed.get(_key(alias))
        if found is not None:
            return found
    return None


def _text(record: dict[str, Any], aliases: tuple[str, ...]) -> str | None:
    found = _lookup(record, aliases)
    if found is None:
        return None
    value = found[1]
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _date(record: dict[str, Any], aliases: tuple[str, ...]) -> datetime | None:
    value = _text(record, aliases)
    if value is None:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _dates(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[datetime, ...] | None:
    found = _lookup(record, aliases)
    if found is None or not isinstance(found[1], list):
        return None
    parsed = tuple(_date({"value": item}, ("value",)) for item in found[1] if isinstance(item, str))
    if len(parsed) != len(found[1]) or any(item is None for item in parsed):
        return None
    return tuple(item for item in parsed if item is not None)


def _decimal(record: dict[str, Any], aliases: tuple[str, ...]) -> Decimal | None:
    found = _lookup(record, aliases)
    if found is None or isinstance(found[1], bool):
        return None
    try:
        result = Decimal(str(found[1]))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _integer(record: dict[str, Any], aliases: tuple[str, ...]) -> int | None:
    found = _lookup(record, aliases)
    value = found[1] if found is not None else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 9 else None


def _strings(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, ...] | None:
    found = _lookup(record, aliases)
    if found is None or not isinstance(found[1], list):
        return None
    value = found[1]
    if not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return tuple(item.strip() for item in value)


def _status(record: dict[str, Any]) -> OpportunityStatus:
    value = _text(record, ("status", "topicStatus", "callStatus"))
    if value is None:
        return OpportunityStatus.UNKNOWN
    normalized = " ".join(value.casefold().split())
    if normalized in {"open", "open for submission", "submission open", "ongoing"}:
        return OpportunityStatus.OPEN
    if normalized in {"forthcoming", "upcoming", "planned"}:
        return OpportunityStatus.UPCOMING
    if normalized in {"closed", "closed for submission", "deadline passed"}:
        return OpportunityStatus.CLOSED
    return OpportunityStatus.UNKNOWN


def _opportunity_type(record: dict[str, Any]) -> OpportunityType:
    value = _text(record, ("opportunityType", "typeName", "fundingType", "type"))
    if value is None:
        return OpportunityType.UNKNOWN
    normalized = _key(value)
    known = {
        "grant": OpportunityType.GRANT,
        "grants": OpportunityType.GRANT,
        "loan": OpportunityType.LOAN,
        "guarantee": OpportunityType.GUARANTEE,
        "taxincentive": OpportunityType.TAX_INCENTIVE,
        "financialinstrument": OpportunityType.FINANCIAL_INSTRUMENT,
        "publicprogramme": OpportunityType.PUBLIC_PROGRAMME,
        "tender": OpportunityType.TENDER,
        "repayableincentive": OpportunityType.REPAYABLE_INCENTIVE,
        "reimbursableadvance": OpportunityType.REPAYABLE_INCENTIVE,
    }
    return known.get(normalized, OpportunityType.UNKNOWN)


def _field_evidence(
    document: SourceDocument, raw_key: str, raw_value: Any
) -> EvidenceReference | None:
    key_token = json.dumps(raw_key, ensure_ascii=False)
    key_start = document.text.find(key_token)
    if key_start < 0:
        return None
    colon = document.text.find(":", key_start + len(key_token))
    if colon < 0:
        return None
    value_start = colon + 1
    while value_start < len(document.text) and document.text[value_start].isspace():
        value_start += 1
    try:
        decoded, value_length = json.JSONDecoder().raw_decode(document.text[value_start:])
    except json.JSONDecodeError:
        return None
    if decoded != raw_value or value_start - key_start > 4000:
        return None
    end = value_start + value_length
    if end - key_start > 4000:
        return None
    return document.evidence(key_start, end, section=raw_key)


def _has_evidence(
    document: SourceDocument, record: dict[str, Any], aliases: tuple[str, ...]
) -> bool:
    found = _lookup(record, aliases)
    return found is not None and _field_evidence(document, found[0], found[1]) is not None


def _unparsed_eligibility_rule(
    index: int, item: Any, evidence: EvidenceReference
) -> EligibilityRule:
    detail = item if isinstance(item, dict) else {}
    identifier = detail.get("ruleId", detail.get("id", f"eligibility-{index}"))
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 100:
        identifier = f"eligibility-{index}"
    reason = detail.get("reason", detail.get("requirement"))
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        reason = "Critério de elegibilidade não interpretado; exige revisão manual."
    return EligibilityRule(
        rule_id=identifier,
        profile_field="__unsupported__",
        operator=RuleOperator.PRESENT,
        expected=None,
        reason=reason,
        evidence_ids=(evidence.evidence_id,),
        hard_gate=False,
    )


def _eligibility_rules(
    document: SourceDocument, record: dict[str, Any]
) -> tuple[EligibilityRule, ...]:
    found = _lookup(record, ("eligibilityRules", "hardEligibilityRules"))
    if found is None or not isinstance(found[1], list):
        return ()
    evidence = _field_evidence(document, found[0], found[1])
    if evidence is None:
        return ()
    rules: list[EligibilityRule] = []
    for index, item in enumerate(found[1], start=1):
        detail = item if isinstance(item, dict) else {}
        rule_id = detail.get("ruleId", detail.get("id", f"eligibility-{index}"))
        profile_field = detail.get("profileField", detail.get("field"))
        operator = detail.get("operator")
        expected = detail.get("expected")
        reason = detail.get("reason", detail.get("requirement"))
        remediation = detail.get("remediationIfAny")
        hard_gate = detail.get("hardGate", True)
        if not isinstance(rule_id, str) or not rule_id.strip() or len(rule_id) > 100:
            rule_id = f"eligibility-{index}"
            profile_field = "__unsupported__"
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            reason = "Requisito estruturado por interpretar; exige revisão manual."
            profile_field = "__unsupported__"
        if not isinstance(profile_field, str) or not isinstance(operator, str):
            profile_field = "__unsupported__"
            operator = RuleOperator.PRESENT.value
            expected = None
        try:
            selected_operator = RuleOperator(operator)
        except ValueError:
            selected_operator = RuleOperator.PRESENT
            profile_field = "__unsupported__"
            expected = None
        if isinstance(expected, list) and all(isinstance(value, str) for value in expected):
            normalized_expected: str | Decimal | tuple[str, ...] | None = tuple(expected)
            if not expected or any(not value.strip() for value in expected):
                profile_field = "__unsupported__"
        elif isinstance(expected, str):
            normalized_expected = expected
            if not expected.strip():
                profile_field = "__unsupported__"
        elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
            try:
                normalized_expected = Decimal(str(expected))
            except InvalidOperation:
                normalized_expected = None
                profile_field = "__unsupported__"
        elif expected is None:
            normalized_expected = None
        else:
            normalized_expected = None
            profile_field = "__unsupported__"
        if remediation is not None and (
            not isinstance(remediation, str) or not remediation.strip() or len(remediation) > 1000
        ):
            remediation = None
            profile_field = "__unsupported__"
        if not isinstance(hard_gate, bool):
            hard_gate = False
            profile_field = "__unsupported__"
        try:
            rules.append(
                EligibilityRule(
                    rule_id=rule_id,
                    profile_field=profile_field,
                    operator=selected_operator,
                    expected=normalized_expected,
                    reason=reason,
                    evidence_ids=(evidence.evidence_id,),
                    hard_gate=hard_gate,
                    remediation_if_any=remediation,
                )
            )
        except ValueError:
            rules.append(_unparsed_eligibility_rule(index, item, evidence))
    return tuple(rules)


def normalize_source_document(document: SourceDocument) -> Opportunity:
    """Map fields with known names and types; leave unsupported facts unknown."""
    record = _record(document)
    metadata = dict(document.metadata)
    record_id = _text(record, _FIELD_ALIASES["record_id"]) or metadata.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        record_id = hashlib.sha256(document.text.encode("utf-8")).hexdigest()[:24]

    call_id = (
        _text(record, _FIELD_ALIASES["call_id"])
        if _has_evidence(document, record, _FIELD_ALIASES["call_id"])
        else None
    )
    programme = (
        _text(record, _FIELD_ALIASES["programme"])
        if _has_evidence(document, record, _FIELD_ALIASES["programme"])
        else None
    )
    authority = (
        _text(record, _FIELD_ALIASES["authority"])
        if _has_evidence(document, record, _FIELD_ALIASES["authority"])
        else None
    )
    title = (
        _text(record, _FIELD_ALIASES["title"]) or document.title.strip() or None
        if _has_evidence(document, record, _FIELD_ALIASES["title"])
        else None
    )
    evidence = []
    evidence_keys = (
        *_FIELD_ALIASES.values(),
        ("status", "topicStatus", "callStatus"),
        ("opportunityType", "typeName", "fundingType", "type"),
        ("publicationDate", "publishedAt"),
        ("openingDate", "startDate"),
        ("deadline", "deadlineDate", "submissionDeadline"),
        ("additionalDeadlines", "submissionDeadlines"),
        ("sourceUpdatedAt", "updatedAt", "lastUpdated", "modificationDate"),
        ("trlMin", "technologyReadinessLevelMin"),
        ("trlMax", "technologyReadinessLevelMax"),
        ("eligibilityRules", "hardEligibilityRules"),
        *_LIST_FIELDS.values(),
        *_DECIMAL_FIELDS.values(),
        ("url", "webUrl", "topicUrl", "link", "urlEN", "permalink"),
    )
    for aliases in evidence_keys:
        found = _lookup(record, aliases)
        if found:
            reference = _field_evidence(document, found[0], found[1])
            if reference is not None:
                evidence.append(reference)
    for aliases in (*_LIST_FIELDS.values(), *_DECIMAL_FIELDS.values()):
        found = _lookup(record, aliases)
        if found:
            reference = _field_evidence(document, found[0], found[1])
            if reference is not None:
                evidence.append(reference)
    for aliases in _INTEGER_FIELDS.values():
        found = _lookup(record, aliases)
        if found:
            reference = _field_evidence(document, found[0], found[1])
            if reference is not None:
                evidence.append(reference)

    canonical_url = document.source_url
    if "/search-api/" in canonical_url or not _has_evidence(
        document, record, ("url", "webUrl", "topicUrl", "link", "urlEN", "permalink")
    ):
        canonical_url = None
    opportunity_id = hashlib.sha256(
        f"{document.source_id}\0{record_id}".encode()
    ).hexdigest()[:24]
    normalized_fields: dict[str, Any] = {}
    for name, aliases in _LIST_FIELDS.items():
        found = _lookup(record, aliases)
        has_reference = (
            found is not None and _field_evidence(document, found[0], found[1]) is not None
        )
        normalized_fields[name] = _strings(record, aliases) if has_reference else None
    for name, aliases in _DECIMAL_FIELDS.items():
        value = _decimal(record, aliases) if _has_evidence(document, record, aliases) else None
        if name == "aid_intensity" and value is not None and value > 1:
            value = None
        normalized_fields[name] = value
    for name, aliases in _INTEGER_FIELDS.items():
        normalized_fields[name] = (
            _integer(record, aliases) if _has_evidence(document, record, aliases) else None
        )
    additional_deadlines = (
        _dates(record, ("additionalDeadlines", "submissionDeadlines"))
        if _has_evidence(document, record, ("additionalDeadlines", "submissionDeadlines"))
        else None
    )
    consortium_rules = (
        _text(record, _FIELD_ALIASES["consortium_rules"])
        if _has_evidence(document, record, _FIELD_ALIASES["consortium_rules"])
        else None
    )
    project_duration = (
        _text(record, _FIELD_ALIASES["project_duration"])
        if _has_evidence(document, record, _FIELD_ALIASES["project_duration"])
        else None
    )
    source_updated_at = (
        _date(record, _FIELD_ALIASES["source_updated_at"])
        if _has_evidence(document, record, _FIELD_ALIASES["source_updated_at"])
        else None
    )
    return Opportunity(
        id=f"opp-{opportunity_id}",
        source_id=document.source_id,
        source_record_id=record_id,
        programme=programme,
        call_id=call_id,
        title=title,
        authority=authority,
        canonical_url=canonical_url,
        status=(
            _status(record)
            if _has_evidence(document, record, ("status", "topicStatus", "callStatus"))
            else OpportunityStatus.UNKNOWN
        ),
        opportunity_type=(
            _opportunity_type(record)
            if _has_evidence(
                document, record, ("opportunityType", "typeName", "fundingType", "type")
            )
            else OpportunityType.UNKNOWN
        ),
        publication_date=(
            _date(record, ("publicationDate", "publishedAt"))
            if _has_evidence(document, record, ("publicationDate", "publishedAt"))
            else None
        ),
        opening_date=(
            _date(record, ("openingDate", "startDate"))
            if _has_evidence(document, record, ("openingDate", "startDate"))
            else None
        ),
        deadline=(
            _date(record, ("deadline", "deadlineDate", "submissionDeadline"))
            if _has_evidence(document, record, ("deadline", "deadlineDate", "submissionDeadline"))
            else None
        ),
        additional_deadlines=additional_deadlines,
        consortium_rules=consortium_rules,
        project_duration=project_duration,
        eligibility_rules=_eligibility_rules(document, record),
        source_updated_at=source_updated_at,
        source_retrieved_at=document.retrieved_at,
        last_checked_at=document.retrieved_at,
        raw_source_reference=document.source_url,
        evidence=tuple({item.evidence_id: item for item in evidence}.values()),
        **normalized_fields,
    )
