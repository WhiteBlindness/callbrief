from __future__ import annotations

import json
import unittest

from callbrief.domain import EvidenceProvenance
from callbrief.sources import AgentReachPage
from scripts.verify_agent_reach import verify_bridge


class AgentReachSmokeTests(unittest.TestCase):
    def test_fake_bridge_normalizes_official_page_without_exposing_bridge_internals(self) -> None:
        record = {
            "id": "PT2030-LOCAL-1",
            "title": "Apoio a empresas privadas",
            "programme": "Portugal 2030",
            "status": "Open",
            "deadline": "2027-03-31",
        }
        payload = json.dumps(record, ensure_ascii=False)

        class FakeBridge:
            def fetch(self, query: str, *, limit: int):
                return (
                    AgentReachPage(
                        url="https://portugal2030.pt/avisos/1",
                        title=record["title"],
                        text=payload,
                        content_type="application/json",
                        provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
                    ),
                )

        report = verify_bridge(
            FakeBridge(),
            allowed_hosts=("portugal2030.pt",),
            query="Portugal 2030",
            limit=1,
        )

        self.assertEqual(report["normalization_status"], "passed")
        self.assertEqual(report["documents_returned"], 1)
        self.assertEqual(report["opportunities_normalized"], 1)
        self.assertEqual(report["records"][0]["source_id"], "agent_reach")
        self.assertEqual(report["records"][0]["call_id"], "PT2030-LOCAL-1")
        self.assertEqual(report["records"][0]["provenance_status"], "MANUALLY_TRANSCRIBED")
        self.assertNotIn("bridge", json.dumps(report, ensure_ascii=False).casefold())


if __name__ == "__main__":
    unittest.main()
