from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from callbrief.deduplication import DuplicateKind
from callbrief.domain import SourceDocument
from callbrief.pipeline import run_discovery
from callbrief.storage import SqliteStore


NOW = datetime(2026, 10, 6, tzinfo=UTC)


class FixtureAdapter:
    def __init__(self, source_id: str, source_url: str) -> None:
        self.source_id = source_id
        self.source_url = source_url

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        self.query = query
        self.limit = limit
        record = {
            "id": f"{self.source_id}-record",
            "topicCode": "PROGRAMME-2026-01",
            "programmeName": "Example programme",
            "title": "Funding call for innovation",
            "authority": "Public authority",
            "url": self.source_url,
            "status": "Open",
        }
        return (
            SourceDocument(
                source_id=self.source_id,
                source_url=self.source_url,
                retrieved_at=NOW,
                content_type="application/json",
                title=record["title"],
                text=json.dumps(record, ensure_ascii=False, indent=2),
            ),
        )


class DiscoveryPipelineTests(unittest.TestCase):
    def test_discovers_normalizes_persists_and_keeps_duplicate_as_source_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("workspace-1", "Consultancy")
            first_adapter = FixtureAdapter("official-a", "https://official-a.gov/call/1")
            second_adapter = FixtureAdapter("official-b", "https://official-b.gov/call/1")

            first = run_discovery(first_adapter, store, query="innovation", limit=1)
            second = run_discovery(second_adapter, store, query="innovation", limit=1)
            canonical = store.list_opportunities()
            variants = store.list_opportunities(include_duplicates=True)
            source_variants = store.list_source_variants(canonical[0].id)
            first_snapshots = store.list_source_snapshots("official-a")
            store.close()

            self.assertEqual(first_adapter.query, "innovation")
            self.assertEqual(first_adapter.limit, 1)
            self.assertEqual(first.new_snapshots, 1)
            self.assertEqual(second.exact_duplicates, 1)
            self.assertEqual(len(canonical), 1)
            self.assertEqual(len(variants), 2)
            self.assertEqual(len(source_variants), 1)
            self.assertEqual(source_variants[0].duplicate_of, canonical[0].id)
            self.assertEqual(len(first_snapshots), 1)
            self.assertEqual(source_variants[0].programme, "Example programme")
            self.assertEqual(second.items[0].duplicate.kind, DuplicateKind.EXACT)


if __name__ == "__main__":
    unittest.main()
