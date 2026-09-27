from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from callbrief.agent import AgentError, AgentRunner
from callbrief.corpus import Corpus
from callbrief.provider import ModelReply, ToolCall


class ScriptedClient:
    route_name = "test-fake"

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        self.calls.append((messages, tools))
        if not self.replies:
            raise AssertionError("Unexpected model request")
        return self.replies.pop(0)


def make_brief(evidence_id: str) -> dict[str, object]:
    reference = [evidence_id]
    return {
        "title": "Green Mobility Research Call",
        "summary": {
            "statement": "The call supports research by Portuguese SMEs.",
            "evidence_ids": reference,
        },
        "fit_band": "possível",
        "fit_rationale": {
            "statement": "The profile matches the applicant type, but funding is unclear.",
            "evidence_ids": reference,
        },
        "requirements": [
            {
                "requirement": "Applicant type and location",
                "status": "cumpre",
                "note": "The profile describes a Portuguese SME.",
                "evidence_ids": reference,
            }
        ],
        "deadlines": [],
        "risks": [],
        "open_questions": ["Does the applicant have the required matching funds?"],
        "next_steps": ["Confirm the available matching funds."],
    }


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "call.md").write_text(
            "Portuguese SMEs may apply for green mobility research. "
            "Applicants provide matching funds.",
            encoding="utf-8",
        )
        self.corpus = Corpus.load(root)
        self.evidence = self.corpus.search("Portuguese SME green mobility research", 1)[0]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runs_allowlisted_search_then_returns_cited_brief(self) -> None:
        client = ScriptedClient(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="search-1",
                            name="search_documents",
                            arguments={"query": "Portuguese SME green mobility research"},
                        ),
                    )
                ),
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="final-1",
                            name="submit_brief",
                            arguments=make_brief(self.evidence.evidence_id),
                        ),
                    )
                ),
            ]
        )

        result = AgentRunner(client).run(self.corpus)

        self.assertEqual(result.brief.title, "Green Mobility Research Call")
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(result.tool_calls, ("search_documents", "submit_brief"))
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(client.calls[0][1][0]["function"]["name"], "search_documents")

    def test_rejects_unlisted_tools_without_executing_them(self) -> None:
        client = ScriptedClient(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="unsafe-1",
                            name="shell.exec",
                            arguments={"command": "echo unsafe"},
                        ),
                    )
                )
            ]
        )

        with self.assertRaises(AgentError):
            AgentRunner(client).run(self.corpus)
        self.assertEqual(len(client.calls), 1)

    def test_rejects_citations_that_were_not_returned_by_search(self) -> None:
        client = ScriptedClient(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="search-1",
                            name="search_documents",
                            arguments={"query": "Portuguese SME green mobility research"},
                        ),
                    )
                ),
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="final-1",
                            name="submit_brief",
                            arguments=make_brief("fabricated-evidence-id"),
                        ),
                    )
                ),
            ]
        )

        with self.assertRaisesRegex(AgentError, "evidence"):
            AgentRunner(client).run(self.corpus)

    def test_rejects_a_final_brief_before_any_evidence_search(self) -> None:
        client = ScriptedClient(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="final-1",
                            name="submit_brief",
                            arguments=make_brief(self.evidence.evidence_id),
                        ),
                    )
                )
            ]
        )

        with self.assertRaises(AgentError):
            AgentRunner(client).run(self.corpus)

    def test_stops_when_the_model_never_submits_a_brief(self) -> None:
        search = ModelReply(
            tool_calls=(
                ToolCall(
                    call_id="search-1",
                    name="search_documents",
                    arguments={"query": "Portuguese SME green mobility research"},
                ),
            )
        )
        client = ScriptedClient([search, search])

        with self.assertRaisesRegex(AgentError, "maximum"):
            AgentRunner(client, max_turns=2).run(self.corpus)

    def test_rejects_multiple_tool_calls_in_one_turn(self) -> None:
        client = ScriptedClient(
            [
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            call_id="search-1",
                            name="search_documents",
                            arguments={"query": "Portuguese SME green mobility research"},
                        ),
                        ToolCall(
                            call_id="search-2",
                            name="search_documents",
                            arguments={"query": "matching funds"},
                        ),
                    )
                )
            ]
        )

        with self.assertRaises(AgentError):
            AgentRunner(client).run(self.corpus)


if __name__ == "__main__":
    unittest.main()
