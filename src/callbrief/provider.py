"""Small OpenAI-compatible chat-completions adapter using the standard library."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .settings import Settings

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 2_000_000


class ProviderError(RuntimeError):
    """Raised when the configured model endpoint cannot return a valid response."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelReply:
    tool_calls: tuple[ToolCall, ...]
    content: str | None = None


class OpenAICompatibleClient:
    def __init__(self, settings: Settings, *, max_retries: int = 2) -> None:
        if not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        self.settings = settings
        self.max_retries = max_retries
        self.route_name = settings.base_url

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        endpoint = f"{self.settings.base_url}/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        logger.info(
            "model_request",
            extra={
                "event": "model_request",
                "route": self.route_name,
                "model": self.settings.model,
            },
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError("Model response exceeds the 2 MB limit")
                result = json.loads(raw.decode("utf-8"))
                reply = self._parse_reply(result)
                logger.info(
                    "model_response",
                    extra={"event": "model_response", "tool_count": len(reply.tool_calls)},
                )
                return reply
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if retryable and attempt < self.max_retries:
                    logger.warning(
                        "model_retry", extra={"event": "model_retry", "status": exc.code}
                    )
                    time.sleep(min(2**attempt, 4))
                    continue
                logger.error("model_http_error", extra={"event": "model_error", "status": exc.code})
                raise ProviderError(f"Model endpoint returned HTTP {exc.code}") from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    logger.warning(
                        "model_retry", extra={"event": "model_retry", "reason": type(exc).__name__}
                    )
                    time.sleep(min(2**attempt, 4))
                    continue
                logger.error(
                    "model_connection_error",
                    extra={"event": "model_error", "reason": type(exc).__name__},
                )
                raise ProviderError("Could not connect to the configured model endpoint") from None
            except (UnicodeDecodeError, ValueError, TypeError, KeyError, IndexError) as exc:
                logger.error(
                    "model_invalid_response",
                    extra={"event": "model_error", "reason": type(exc).__name__},
                )
                raise ProviderError("Model endpoint returned a malformed response") from None
        raise ProviderError("Model request failed")

    @staticmethod
    def _parse_reply(payload: Any) -> ModelReply:
        if not isinstance(payload, dict):
            raise TypeError("response must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("response has no choice")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise TypeError("response message is malformed")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TypeError("response content is malformed")
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list) or len(raw_calls) > 4:
            raise ValueError("tool call list is malformed")
        calls: list[ToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict) or not isinstance(raw_call.get("function"), dict):
                raise TypeError("tool call is malformed")
            function = raw_call["function"]
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            name = function.get("name")
            call_id = raw_call.get("id")
            if not isinstance(name, str) or not isinstance(call_id, str):
                raise TypeError("tool name and id must be text")
            calls.append(ToolCall(call_id, name, arguments))
        return ModelReply(tuple(calls), content)
