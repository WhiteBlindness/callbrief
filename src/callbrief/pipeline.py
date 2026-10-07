"""Explicit discovery, normalization, and duplicate-review stages."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .deduplication import DuplicateKind, DuplicateResult, find_duplicate
from .domain import Opportunity, SourceDocument
from .normalization import normalize_source_document
from .ports import DiscoveryRepository
from .sources import SourceAdapter


@dataclass(frozen=True, slots=True)
class DiscoveryItem:
    document: SourceDocument
    opportunity: Opportunity
    duplicate: DuplicateResult
    new_source_snapshot: bool


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    source_id: str
    query: str
    items: tuple[DiscoveryItem, ...]

    @property
    def exact_duplicates(self) -> int:
        return sum(item.duplicate.kind is DuplicateKind.EXACT for item in self.items)

    @property
    def possible_matches(self) -> int:
        return sum(item.duplicate.kind is DuplicateKind.POSSIBLE for item in self.items)

    @property
    def probable_matches(self) -> int:
        return sum(item.duplicate.kind is DuplicateKind.PROBABLE for item in self.items)

    @property
    def review_candidates(self) -> int:
        return self.probable_matches + self.possible_matches

    @property
    def new_snapshots(self) -> int:
        return sum(item.new_source_snapshot for item in self.items)


def run_discovery(
    adapter: SourceAdapter,
    store: DiscoveryRepository,
    *,
    query: str,
    limit: int = 50,
) -> DiscoveryResult:
    """Acquire source documents, normalize them, then flag strong/weak duplicates."""
    documents = adapter.fetch(query, limit=limit)
    known_canonical = list(store.list_opportunities())
    items: list[DiscoveryItem] = []
    for document in documents:
        if document.source_id != adapter.source_id:
            raise ValueError("Adapter returned a document for a different source id")
        new_snapshot = store.save_source_document(document)
        candidate = normalize_source_document(document)
        existing = tuple(item for item in known_canonical if item.id != candidate.id)
        duplicate = find_duplicate(candidate, existing)
        if duplicate.kind is DuplicateKind.EXACT and duplicate.canonical_opportunity_id:
            candidate = replace(candidate, duplicate_of=duplicate.canonical_opportunity_id)
        store.save_opportunity(candidate, checked_at=document.retrieved_at)
        if candidate.duplicate_of is None:
            known_canonical.append(candidate)
        items.append(DiscoveryItem(document, candidate, duplicate, new_snapshot))
    return DiscoveryResult(adapter.source_id, query, tuple(items))
