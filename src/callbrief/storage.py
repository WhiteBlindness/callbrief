"""SQLite repositories for the local prototype, with workspace-scoped records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .change_tracking import deadline_approaching, detect_changes, notification_types
from .domain import (
    ChangeRecord,
    EligibilityAssessment,
    EligibilityFinding,
    EligibilityRule,
    EligibilityState,
    EvidenceReference,
    FitAssessment,
    FitComponent,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
    NotificationEvent,
    RuleOperator,
    SourceDocument,
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
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _dump(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime | None = None) -> str:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return instant.isoformat()


def _tuple_fields(data: dict[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        if data.get(name) is not None:
            data[name] = tuple(data[name])


def _evidence_from(data: dict[str, Any]) -> EvidenceReference:
    if isinstance(data.get("retrieved_at"), str):
        data["retrieved_at"] = datetime.fromisoformat(data["retrieved_at"])
    return EvidenceReference(**data)


def _rule_from(data: dict[str, Any]) -> EligibilityRule:
    if data.get("evidence_ids") is not None:
        data["evidence_ids"] = tuple(data["evidence_ids"])
    if isinstance(data.get("expected"), list):
        data["expected"] = tuple(data["expected"])
    data["operator"] = RuleOperator(data["operator"])
    if data["operator"] in {RuleOperator.MINIMUM, RuleOperator.MAXIMUM} and isinstance(
        data.get("expected"), str
    ):
        data["expected"] = Decimal(data["expected"])
    return EligibilityRule(**data)


def _opportunity_from(payload: str) -> Opportunity:
    data = json.loads(payload)
    for name in (
        "publication_date",
        "opening_date",
        "deadline",
        "source_retrieved_at",
        "source_updated_at",
        "last_checked_at",
    ):
        if data.get(name) is not None:
            data[name] = datetime.fromisoformat(data[name])
    if data.get("additional_deadlines") is not None:
        data["additional_deadlines"] = tuple(
            datetime.fromisoformat(value) for value in data["additional_deadlines"]
        )
    for name in (
        "budget_total",
        "funding_min",
        "funding_max",
        "aid_intensity",
        "project_cost_min",
        "project_cost_max",
    ):
        if data.get(name) is not None:
            data[name] = Decimal(data[name])
    _tuple_fields(
        data,
        (
            "geography",
            "eligible_applicant_types",
            "eligible_company_sizes",
            "eligible_sectors",
            "eligible_regions",
            "eligible_activities",
            "excluded_activities",
            "financial_requirements",
            "other_hard_requirements",
            "documents",
        ),
    )
    for name in ("eligibility_rules", "evidence"):
        data[name] = tuple(
            _rule_from(item) if name == "eligibility_rules" else _evidence_from(item)
            for item in data.get(name, [])
        )
    data["status"] = OpportunityStatus(data["status"])
    data["opportunity_type"] = OpportunityType(data["opportunity_type"])
    return Opportunity(**data)


def _organisation_from(payload: str) -> OrganisationProfile:
    data = json.loads(payload)
    for name in ("turnover", "balance_sheet", "financial_autonomy"):
        if data.get(name) is not None:
            data[name] = Decimal(data[name])
    _tuple_fields(
        data,
        (
            "regions",
            "applicant_types",
            "sectors",
            "activities",
            "technologies",
            "products_services",
            "previous_rd_projects",
            "certifications",
            "investment_plans",
            "project_ideas",
            "preferred_geographies",
        ),
    )
    for name in ("preferred_opportunity_types", "excluded_opportunity_types"):
        if data.get(name) is not None:
            data[name] = tuple(OpportunityType(item) for item in data[name])
    data["evidence"] = tuple(_evidence_from(item) for item in data.get("evidence", []))
    return OrganisationProfile(**data)


def _stable_snapshot_payload(opportunity: Opportunity) -> str:
    data = _jsonable(opportunity)
    for volatile in ("source_retrieved_at", "last_checked_at"):
        data.pop(volatile, None)
    for item in data.get("evidence", []):
        item.pop("retrieved_at", None)
    return _dump(data)


class SqliteStore:
    """A small repository layer; every client-facing read includes its workspace."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            destination = Path(self.path)
            if destination.exists() and destination.is_symlink():
                raise ValueError("Refusing to open a database through a symbolic link")
            destination.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        migration = Path(__file__).with_name("migrations") / "001_initial.sql"
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        )
        applied = self._connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 1"
        ).fetchone()
        if applied is None:
            self._connection.executescript(migration.read_text(encoding="utf-8"))
            self._connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (_timestamp(),),
            )
            self._connection.commit()

    def create_workspace(self, workspace_id: str, name: str) -> None:
        if not workspace_id.strip() or not name.strip():
            raise ValueError("Workspace id and name are required")
        self._connection.execute(
            "INSERT INTO workspaces(id, name, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (workspace_id, name, _timestamp()),
        )
        self._connection.commit()

    def save_organisation(self, profile: OrganisationProfile) -> None:
        payload = _dump(profile)
        self._connection.execute(
            "INSERT INTO organisations(workspace_id, id, payload_json, updated_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(workspace_id, id) DO UPDATE SET "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            (profile.workspace_id, profile.id, payload, _timestamp()),
        )
        self._connection.commit()

    def get_organisation(
        self, workspace_id: str, organisation_id: str
    ) -> OrganisationProfile | None:
        row = self._connection.execute(
            "SELECT payload_json FROM organisations WHERE workspace_id = ? AND id = ?",
            (workspace_id, organisation_id),
        ).fetchone()
        return _organisation_from(row["payload_json"]) if row else None

    def save_source_document(self, document: SourceDocument) -> bool:
        """Persist a source snapshot; return True only for newly observed content."""
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO source_snapshots "
            "(source_id, source_url, content_hash, content_type, title, text_content, metadata_json, "
            "discovered_links_json, etag, last_modified, first_seen_at, last_seen_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document.source_id,
                document.source_url,
                document.content_hash,
                document.content_type,
                document.title,
                document.text,
                _dump(document.metadata),
                _dump(document.discovered_links),
                document.etag,
                document.last_modified,
                document.retrieved_at.isoformat(),
                document.retrieved_at.isoformat(),
            ),
        )
        created = cursor.rowcount == 1
        if not created:
            self._connection.execute(
                "UPDATE source_snapshots SET last_seen_at = ?, source_url = ?, etag = ?, "
                "last_modified = ? WHERE source_id = ? AND content_hash = ?",
                (
                    document.retrieved_at.isoformat(),
                    document.source_url,
                    document.etag,
                    document.last_modified,
                    document.source_id,
                    document.content_hash,
                ),
            )
        self._connection.commit()
        return created

    def list_source_snapshots(self, source_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            "SELECT source_url, content_hash, content_type, title, text_content, metadata_json, "
            "discovered_links_json, first_seen_at, last_seen_at "
            "FROM source_snapshots WHERE source_id = ? ORDER BY snapshot_id",
            (source_id,),
        ).fetchall()
        return tuple(
            {
                "source_url": row["source_url"],
                "content_hash": row["content_hash"],
                "content_type": row["content_type"],
                "title": row["title"],
                "text": row["text_content"],
                "metadata": json.loads(row["metadata_json"]),
                "discovered_links": json.loads(row["discovered_links_json"]),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        )

    def save_opportunity(
        self, opportunity: Opportunity, *, checked_at: datetime | None = None
    ) -> tuple[ChangeRecord, ...]:
        timestamp = _timestamp()
        payload = _dump(opportunity)
        stable_payload = _stable_snapshot_payload(opportunity)
        stable_hash = hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()
        prior = self._connection.execute(
            "SELECT payload_json FROM opportunities WHERE id = ?", (opportunity.id,)
        ).fetchone()
        changes: tuple[ChangeRecord, ...] = ()
        if prior:
            before = _opportunity_from(prior["payload_json"])
            changes = detect_changes(before, opportunity, detected_at=checked_at)
        with self._connection:
            self._connection.execute(
                "INSERT INTO opportunities(id, source_id, duplicate_of, payload_json, payload_hash, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "source_id = excluded.source_id, duplicate_of = excluded.duplicate_of, "
                "payload_json = excluded.payload_json, "
                "payload_hash = excluded.payload_hash, updated_at = excluded.updated_at",
                (
                    opportunity.id,
                    opportunity.source_id,
                    opportunity.duplicate_of,
                    payload,
                    stable_hash,
                    timestamp,
                ),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO opportunity_snapshots "
                "(opportunity_id, payload_hash, payload_json, captured_at) VALUES(?, ?, ?, ?)",
                (opportunity.id, stable_hash, stable_payload, timestamp),
            )
            if prior is None and opportunity.duplicate_of is None:
                event_id = hashlib.sha256(
                    f"new_opportunity\0{opportunity.id}".encode()
                ).hexdigest()[:24]
                self._save_notification(
                    NotificationEvent(
                        event_id=f"evt-{event_id}",
                        event_type="new_opportunity",
                        opportunity_id=opportunity.id,
                        created_at=opportunity.source_retrieved_at,
                    )
                )
            approaching = (
                deadline_approaching(opportunity, now=checked_at)
                if opportunity.duplicate_of is None
                else None
            )
            if approaching is not None:
                self._save_notification(approaching)
            self._connection.execute(
                "DELETE FROM evidence WHERE opportunity_id = ?", (opportunity.id,)
            )
            for evidence in opportunity.evidence:
                self._connection.execute(
                    "INSERT INTO evidence(evidence_id, opportunity_id, source_hash, payload_json) "
                    "VALUES(?, ?, ?, ?)",
                    (evidence.evidence_id, opportunity.id, evidence.source_hash, _dump(evidence)),
                )
            for change in changes:
                self._save_change(change)
                for event_type in notification_types(change):
                    event_id = hashlib.sha256(
                        f"{change.change_id}\0{event_type}".encode()
                    ).hexdigest()[:24]
                    self._save_notification(
                        NotificationEvent(
                            event_id=f"evt-{event_id}",
                            event_type=event_type,
                            opportunity_id=opportunity.id,
                            created_at=change.detected_at,
                            payload=(("change_id", change.change_id), ("field", change.field)),
                        )
                    )
        return changes

    def _save_change(self, change: ChangeRecord) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO changes "
            "(change_id, opportunity_id, field, old_value, new_value, detected_at, source_id, "
            "evidence_ids_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                change.change_id,
                change.opportunity_id,
                change.field,
                change.old_value,
                change.new_value,
                change.detected_at.isoformat(),
                change.source_id,
                _dump(change.evidence_ids),
            ),
        )

    def _save_notification(self, event: NotificationEvent) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO notification_queue "
            "(event_id, event_type, opportunity_id, created_at, workspace_id, organisation_id, "
            "payload_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_type,
                event.opportunity_id,
                event.created_at.isoformat(),
                event.workspace_id,
                event.organisation_id,
                _dump(event.payload),
            ),
        )

    def list_notifications(
        self, workspace_id: str, organisation_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        if organisation_id is None:
            rows = self._connection.execute(
                "SELECT * FROM notification_queue WHERE "
                "(workspace_id IS NULL AND organisation_id IS NULL) OR "
                "(workspace_id = ? AND organisation_id IS NULL) ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM notification_queue WHERE "
                "(workspace_id IS NULL AND organisation_id IS NULL) OR "
                "(workspace_id = ? AND organisation_id = ?) ORDER BY created_at",
                (workspace_id, organisation_id),
            ).fetchall()
        return tuple(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "opportunity_id": row["opportunity_id"],
                "created_at": row["created_at"],
                "workspace_id": row["workspace_id"],
                "organisation_id": row["organisation_id"],
                "payload": json.loads(row["payload_json"]),
                "delivered_at": row["delivered_at"],
            }
            for row in rows
        )

    def list_changes(self, opportunity_id: str | None = None) -> tuple[dict[str, Any], ...]:
        query = (
            "SELECT change_id, opportunity_id, field, old_value, new_value, detected_at, "
            "source_id, evidence_ids_json FROM changes"
        )
        parameters: tuple[str, ...] = ()
        if opportunity_id is not None:
            query += " WHERE opportunity_id = ?"
            parameters = (opportunity_id,)
        query += " ORDER BY detected_at, change_id"
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(
            {
                "change_id": row["change_id"],
                "opportunity_id": row["opportunity_id"],
                "field": row["field"],
                "old_value": row["old_value"],
                "new_value": row["new_value"],
                "detected_at": row["detected_at"],
                "source_id": row["source_id"],
                "evidence_ids": json.loads(row["evidence_ids_json"]),
            }
            for row in rows
        )

    def mark_notification_delivered(
        self, event_id: str, *, delivered_at: datetime | None = None
    ) -> bool:
        cursor = self._connection.execute(
            "UPDATE notification_queue SET delivered_at = ? WHERE event_id = ? AND delivered_at IS NULL",
            (_timestamp(delivered_at), event_id),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def list_snapshots(self, opportunity_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            "SELECT payload_json FROM opportunity_snapshots "
            "WHERE opportunity_id = ? ORDER BY snapshot_id",
            (opportunity_id,),
        ).fetchall()
        return tuple(json.loads(row["payload_json"]) for row in rows)

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self._connection.execute(
            "SELECT payload_json FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone()
        return _opportunity_from(row["payload_json"]) if row else None

    def list_opportunities(self, *, include_duplicates: bool = False) -> tuple[Opportunity, ...]:
        query = "SELECT payload_json FROM opportunities"
        if not include_duplicates:
            query += " WHERE duplicate_of IS NULL"
        query += " ORDER BY id"
        rows = self._connection.execute(query).fetchall()
        return tuple(_opportunity_from(row["payload_json"]) for row in rows)

    def list_source_variants(self, canonical_opportunity_id: str) -> tuple[Opportunity, ...]:
        rows = self._connection.execute(
            "SELECT payload_json FROM opportunities WHERE duplicate_of = ? ORDER BY id",
            (canonical_opportunity_id,),
        ).fetchall()
        return tuple(_opportunity_from(row["payload_json"]) for row in rows)

    def save_assessment(
        self,
        workspace_id: str,
        organisation_id: str,
        eligibility: EligibilityAssessment,
        fit: FitAssessment,
    ) -> None:
        if eligibility.organisation_id != organisation_id or fit.organisation_id != organisation_id:
            raise ValueError("Assessment organisation does not match the requested profile")
        if eligibility.opportunity_id != fit.opportunity_id:
            raise ValueError("Eligibility and fit must refer to the same opportunity")
        assessed_at = _timestamp(max(eligibility.assessed_at, fit.assessed_at))
        with self._connection:
            profile = self._connection.execute(
                "SELECT 1 FROM organisations WHERE workspace_id = ? AND id = ?",
                (workspace_id, organisation_id),
            ).fetchone()
            if profile is None:
                raise PermissionError("Organisation is not available in this workspace")
            opportunity = self._connection.execute(
                "SELECT 1 FROM opportunities WHERE id = ?", (fit.opportunity_id,)
            ).fetchone()
            if opportunity is None:
                raise ValueError("Opportunity does not exist")
            self._connection.execute(
                "INSERT OR REPLACE INTO assessments "
                "(workspace_id, organisation_id, opportunity_id, assessed_at, eligibility_json, "
                "fit_json) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    organisation_id,
                    fit.opportunity_id,
                    assessed_at,
                    _dump(eligibility),
                    _dump(fit),
                ),
            )
            if (
                eligibility.state is not EligibilityState.INELIGIBLE
                and fit.overall is not None
                and fit.overall >= 80
                and any(
                    component.name != "eligibility"
                    and component.score is not None
                    and component.weight > 0
                    for component in fit.components
                )
            ):
                event_id = hashlib.sha256(
                    f"new_high_fit\0{workspace_id}\0{organisation_id}\0{fit.opportunity_id}".encode()
                ).hexdigest()[:24]
                self._save_notification(
                    NotificationEvent(
                        event_id=f"evt-{event_id}",
                        event_type="new_high_fit_opportunity",
                        opportunity_id=fit.opportunity_id,
                        created_at=fit.assessed_at,
                        workspace_id=workspace_id,
                        organisation_id=organisation_id,
                        payload=(("fit_score", str(fit.overall)),),
                    )
                )

    def get_assessment(
        self, workspace_id: str, organisation_id: str, opportunity_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = self._connection.execute(
            "SELECT eligibility_json, fit_json FROM assessments "
            "WHERE workspace_id = ? AND organisation_id = ? AND opportunity_id = ? "
            "ORDER BY assessed_at DESC LIMIT 1",
            (workspace_id, organisation_id, opportunity_id),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["eligibility_json"]), json.loads(row["fit_json"])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["SqliteStore"]
