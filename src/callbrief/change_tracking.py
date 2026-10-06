"""Material opportunity change detection and structured notification events."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from .domain import ChangeRecord, NotificationEvent, Opportunity, OpportunityStatus

_TRACKED_FIELDS = (
    "programme",
    "title",
    "authority",
    "canonical_url",
    "publication_date",
    "opening_date",
    "deadline",
    "additional_deadlines",
    "budget_total",
    "funding_min",
    "funding_max",
    "aid_intensity",
    "project_cost_min",
    "project_cost_max",
    "trl_min",
    "trl_max",
    "consortium_rules",
    "project_duration",
    "documents",
    "call_id",
    "opportunity_type",
    "eligibility_rules",
    "eligible_applicant_types",
    "eligible_company_sizes",
    "geography",
    "eligible_sectors",
    "eligible_regions",
    "eligible_activities",
    "excluded_activities",
    "financial_requirements",
    "other_hard_requirements",
    "status",
)
_ELIGIBILITY_FIELDS = frozenset(
    {
        "eligibility_rules",
        "eligible_applicant_types",
        "eligible_company_sizes",
        "geography",
        "eligible_sectors",
        "eligible_regions",
        "eligible_activities",
        "excluded_activities",
        "financial_requirements",
        "other_hard_requirements",
    }
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _serialise(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _serialise_field(field_name: str, value: Any) -> str | None:
    if field_name == "eligibility_rules" and value is not None:
        semantic_rules = tuple(
            (
                rule.rule_id,
                rule.profile_field,
                rule.operator,
                rule.expected,
                rule.reason,
                rule.hard_gate,
                rule.remediation_if_any,
            )
            for rule in value
        )
        return _serialise(semantic_rules)
    return _serialise(value)


def detect_changes(
    before: Opportunity,
    after: Opportunity,
    *,
    detected_at: datetime | None = None,
) -> tuple[ChangeRecord, ...]:
    if before.id != after.id:
        raise ValueError("Opportunity ids must match when tracking changes")
    timestamp = detected_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("detected_at must include a timezone")
    evidence_ids = tuple(item.evidence_id for item in after.evidence)
    records: list[ChangeRecord] = []
    for field_name in _TRACKED_FIELDS:
        old_value = _serialise_field(field_name, getattr(before, field_name))
        new_value = _serialise_field(field_name, getattr(after, field_name))
        if old_value == new_value:
            continue
        change_digest = hashlib.sha256(
            f"{after.id}\0{field_name}\0{old_value}\0{new_value}\0{timestamp.isoformat()}".encode()
        ).hexdigest()[:24]
        records.append(
            ChangeRecord(
                change_id=f"chg-{change_digest}",
                opportunity_id=after.id,
                field=field_name,
                old_value=old_value,
                new_value=new_value,
                detected_at=timestamp,
                source_id=after.source_id,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(records)


def notification_types(change: ChangeRecord) -> tuple[str, ...]:
    if change.field == "status":
        if change.new_value == '"open"':
            return ("call_opened", "important_call_change")
        if change.new_value == '"closed"':
            return ("call_closed", "important_call_change")
    if change.field in _ELIGIBILITY_FIELDS:
        return ("eligibility_requirement_changed", "important_call_change")
    return ("important_call_change",)


def deadline_approaching(
    opportunity: Opportunity,
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(days=30),
) -> NotificationEvent | None:
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("now must include a timezone")
    if window <= timedelta(0) or opportunity.deadline is None:
        return None
    if opportunity.status is not OpportunityStatus.OPEN:
        return None
    deadline = opportunity.deadline
    if deadline.tzinfo is None or not timestamp <= deadline <= timestamp + window:
        return None
    event_seed = f"{opportunity.id}\0{deadline.isoformat()}\0{int(window.total_seconds())}"
    event_id = hashlib.sha256(event_seed.encode()).hexdigest()[:24]
    return NotificationEvent(
        event_id=f"evt-{event_id}",
        event_type="deadline_approaching",
        opportunity_id=opportunity.id,
        created_at=timestamp,
        payload=(("deadline", deadline.isoformat()),),
    )
