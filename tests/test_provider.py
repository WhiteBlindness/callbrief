from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from callbrief.provider import OpenAICompatibleClient, ProviderError
from callbrief.settings import Settings


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def tool_response() -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "search_documents",
                                "arguments": '{"query":"eligible applicant"}',
                            },
                        }
                    ],
                }
            }
        ]
    }


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            base_url="https://provider.example/v1",
            model="example-model",
            api_key="private-test-key",
            timeout_seconds=12,
            max_turns=6,
        )

    @patch("callbrief.provider.urlopen", return_value=FakeResponse(tool_response()))
    def test_sends_a_structured_tool_request(self, urlopen: object) -> None:
        client = OpenAICompatibleClient(self.settings)

        reply = client.complete(
            [{"role": "user", "content": "Assess the call."}],
            [{"type": "function", "function": {"name": "search_documents"}}],
        )

        self.assertEqual(reply.tool_calls[0].name, "search_documents")
        request = urlopen.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(request.full_url, "https://provider.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-test-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "example-model")
        self.assertEqual(body["tool_choice"], "auto")

    @patch(
        "callbrief.provider.urlopen",
        side_effect=HTTPError(
            "https://provider.example/v1/chat/completions",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"private-test-key must not appear in errors"),
        ),
    )
    def test_provider_error_does_not_include_response_body(self, _: object) -> None:
        client = OpenAICompatibleClient(self.settings, max_retries=0)

        with self.assertRaises(ProviderError) as context:
            client.complete([], [])

        self.assertNotIn("private-test-key", str(context.exception))
        self.assertNotIn("must not appear", str(context.exception))

    @patch("callbrief.provider.time.sleep")
    @patch(
        "callbrief.provider.urlopen",
        side_effect=[
            HTTPError(
                "https://provider.example/v1/chat/completions",
                429,
                "rate limited",
                {},
                io.BytesIO(b"temporary"),
            ),
            FakeResponse(tool_response()),
        ],
    )
    def test_retries_one_rate_limit_then_succeeds(self, urlopen: object, sleep: object) -> None:
        client = OpenAICompatibleClient(self.settings, max_retries=1)

        reply = client.complete([], [])

        self.assertEqual(reply.tool_calls[0].name, "search_documents")
        self.assertEqual(urlopen.call_count, 2)  # type: ignore[attr-defined]
        sleep.assert_called_once()  # type: ignore[attr-defined]

    @patch("callbrief.provider.urlopen", return_value=FakeResponse({"choices": []}))
    def test_rejects_malformed_provider_responses(self, _: object) -> None:
        client = OpenAICompatibleClient(self.settings, max_retries=0)

        with self.assertRaises(ProviderError):
            client.complete([], [])


if __name__ == "__main__":
    unittest.main()
