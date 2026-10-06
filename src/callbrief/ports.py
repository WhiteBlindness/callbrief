"""Storage and acquisition ports kept independent of the local SQLite adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .domain import ChangeRecord, Opportunity, OrganisationProfile, SourceDocument


class DiscoveryRepository(Protocol):
    """Repository operations required by the acquisition pipeline."""

    def list_opportunities(
        self, *, include_duplicates: bool = False
    ) -> tuple[Opportunity, ...]: ...

    def save_source_document(self, document: SourceDocument) -> bool: ...

    def save_opportunity(
        self, opportunity: Opportunity, *, checked_at: datetime | None = None
    ) -> tuple[ChangeRecord, ...]: ...


class OrganisationRepository(Protocol):
    """Workspace-scoped profile operations for local or future service storage."""

    def get_organisation(
        self, workspace_id: str, organisation_id: str
    ) -> OrganisationProfile | None: ...

    def save_organisation(self, profile: OrganisationProfile) -> None: ...


class AssessmentRepository(Protocol):
    """Assessment reads and writes always carry both tenant identifiers."""

    def get_assessment(
        self, workspace_id: str, organisation_id: str, opportunity_id: str
    ) -> tuple[dict[str, object], dict[str, object]] | None: ...
