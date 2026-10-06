from __future__ import annotations

import unittest
from datetime import UTC

from callbrief.domain import SourceDocument
from callbrief.sources import (
    FundingTendersAdapter,
    SourceAdapter,
    SourceError,
    create_adapter_registry,
    load_source_registry,
)

FIXTURE_RESPONSE: dict[str, object] = {
    "totalResults": 2,
    "results": [
        {
            "id": "HORIZON-CL4-2025-01",
            "content": {
                "title": "Digital industry call",
                "topicCode": "HORIZON-CL4-2025-01",
                "url": (
                    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
                    "opportunities/topic-details/example"
                ),
                "status": "Open",
            },
        },
        {
            "id": "DIGITAL-2025-02",
            "content": '{"title":"Digital Europe call","topicCode":"DIGITAL-2025-02"}',
        },
    ],
}


class SourceAdapterTests(unittest.TestCase):
    def test_funding_tenders_adapter_normalizes_records_from_injected_transport(self) -> None:
        calls: list[tuple[str, dict[str, str], dict[str, object], float]] = []

        def transport(endpoint, params, payload, timeout):
            calls.append((endpoint, dict(params), dict(payload), timeout))
            return FIXTURE_RESPONSE

        adapter = FundingTendersAdapter(transport=transport)
        documents = adapter.fetch("digital", limit=2)

        self.assertEqual(len(documents), 2)
        first = documents[0]
        self.assertIsInstance(first, SourceDocument)
        self.assertEqual(first.source_id, "eu_funding_tenders")
        self.assertEqual(first.title, "Digital industry call")
        self.assertTrue(first.source_url.startswith("https://ec.europa.eu/"))
        self.assertEqual(first.discovered_links, (first.source_url,))
        self.assertIn('"status": "Open"', first.text)
        self.assertIs(first.retrieved_at.tzinfo, UTC)
        self.assertEqual(dict(first.metadata)["record_id"], "HORIZON-CL4-2025-01")
        self.assertEqual(documents[1].title, "Digital Europe call")
        self.assertEqual(
            calls,
            [
                (
                    FundingTendersAdapter.endpoint,
                    {"apiKey": "SEDIA", "text": "digital", "pageSize": "2", "language": "en"},
                    {"bool": {"must": []}},
                    20,
                )
            ],
        )

    def test_adapter_rejects_invalid_query_and_limit_before_transport(self) -> None:
        def unexpected_transport(*_args):
            self.fail("must not make a request")

        adapter = FundingTendersAdapter(transport=unexpected_transport)
        with self.assertRaisesRegex(ValueError, "query"):
            adapter.fetch("x" * 501)
        with self.assertRaisesRegex(ValueError, "limit"):
            adapter.fetch(limit=True)
        with self.assertRaisesRegex(ValueError, "limit"):
            adapter.fetch(limit=101)

    def test_adapter_rejects_invalid_response_shape(self) -> None:
        adapter = FundingTendersAdapter(transport=lambda *_: {"results": "invalid"})
        with self.assertRaisesRegex(SourceError, "results list"):
            adapter.fetch()

    def test_registry_marks_unverified_sources_inactive(self) -> None:
        registry = {item.source_id: item for item in load_source_registry()}
        self.assertEqual(registry["eu_funding_tenders"].status, "active")
        self.assertEqual(registry["compete2030"].status, "pending_policy")
        self.assertEqual(registry["portugal2030"].status, "pending_policy")
        self.assertEqual(registry["pt2030_open_dataset"].status, "pending_policy")
        self.assertEqual(registry["cordis"].status, "discovery_only")
        self.assertEqual(registry["cinea"].status, "discovery_only")
        self.assertIn("autorização", registry["compete2030"].legal_notes.casefold())
        self.assertIn("licença", registry["pt2030_open_dataset"].legal_notes.casefold())
        self.assertGreaterEqual(len(registry), 20)

    def test_adapter_registry_only_constructs_verified_source(self) -> None:
        adapter_map = create_adapter_registry(transport=lambda *_: FIXTURE_RESPONSE)
        self.assertEqual(set(adapter_map), {"eu_funding_tenders"})
        self.assertIsInstance(adapter_map["eu_funding_tenders"], SourceAdapter)

    def test_agent_reach_bridge_is_injected_without_scraping_implementation(self) -> None:
        class AgentReachBridge:
            source_id = "agent_reach"

            def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
                return ()

        adapters = create_adapter_registry(agent_reach_adapter=AgentReachBridge())
        self.assertIn("agent_reach", adapters)


if __name__ == "__main__":
    unittest.main()
