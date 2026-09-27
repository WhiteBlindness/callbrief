from __future__ import annotations

import json
import unittest
from pathlib import Path

from callbrief.agent import AgentRunner
from callbrief.corpus import Corpus
from callbrief.provider import ModelReply, ToolCall


ROOT = Path(__file__).resolve().parents[1]


class ScenarioClient:
    route_name = "offline-evaluation"

    def __init__(self, scenario: dict[str, object]) -> None:
        self.scenario = scenario
        self.selected_queries: list[str] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        _: list[dict[str, object]],
    ) -> ModelReply:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            query = str(self.scenario["query"])
            self.selected_queries.append(query)
            return ModelReply(
                tool_calls=(
                    ToolCall(
                        call_id="eval-search",
                        name="search_documents",
                        arguments={"query": query, "max_results": 6},
                    ),
                )
            )

        search_result = json.loads(str(tool_messages[-1]["content"]))
        results = search_result["results"]
        if not results:
            raise AssertionError("The evaluation query returned no evidence")
        evidence_ids = [item["evidence_id"] for item in results]
        brief = {
            "title": str(self.scenario["title"]),
            "summary": {
                "statement": "The synthetic call is relevant to the fictional applicant.",
                "evidence_ids": evidence_ids,
            },
            "fit_band": str(self.scenario["expected_fit_band"]),
            "fit_rationale": {
                "statement": "The applicant type matches, with one unresolved funding detail.",
                "evidence_ids": evidence_ids,
            },
            "requirements": [
                {
                    "requirement": "Applicant type and location",
                    "status": "cumpre",
                    "note": "The fictional applicant is a Portuguese SME.",
                    "evidence_ids": evidence_ids,
                },
                {
                    "requirement": "Matching funds",
                    "status": str(self.scenario["expected_matching_funds_status"]),
                    "note": "The profile does not document available matching funds.",
                    "evidence_ids": evidence_ids,
                },
            ],
            "deadlines": [
                {
                    "statement": "The fictional call closes on 30 June 2027.",
                    "evidence_ids": evidence_ids,
                }
            ],
            "risks": [],
            "open_questions": ["Can the applicant provide the matching funds?"],
            "next_steps": ["Confirm the funding plan with the applicant."],
        }
        return ModelReply(
            tool_calls=(
                ToolCall(call_id="eval-submit", name="submit_brief", arguments=brief),
            )
        )


class EvaluationScenarioTests(unittest.TestCase):
    def test_offline_scenarios_check_retrieval_tool_use_and_output_contract(self) -> None:
        corpus = Corpus.load(ROOT / "examples" / "corpus")
        scenarios = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))

        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                client = ScenarioClient(scenario)
                result = AgentRunner(client).run(corpus)

                self.assertEqual(client.selected_queries, [scenario["query"]])
                self.assertEqual(result.brief.fit_band.value, scenario["expected_fit_band"])
                self.assertEqual(
                    result.brief.requirements[1].status.value,
                    scenario["expected_matching_funds_status"],
                )
                self.assertEqual(
                    result.tool_calls,
                    ("search_documents", "submit_brief"),
                )
                retrieved = " ".join(item.excerpt for item in result.evidence).casefold()
                for phrase in scenario["expected_evidence_phrases"]:
                    self.assertIn(str(phrase).casefold(), retrieved)


if __name__ == "__main__":
    unittest.main()
