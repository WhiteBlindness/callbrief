"""Acquisition adapters for verified official funding sources."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .domain import SourceDocument

REGISTRY_PATH = Path(__file__).with_name("data") / "source_registry.json"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SourceError(RuntimeError):
    """Raised when an upstream source cannot provide a valid response."""


@runtime_checkable
class SourceAdapter(Protocol):
    """Stable contract for adapters that return normalized source documents."""

    @property
    def source_id(self) -> str: ...

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]: ...


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    name: str
    status: str
    authority: str
    jurisdiction: str
    official: bool
    base_url: str | None
    discovery_method: str
    content_types: tuple[str, ...]
    opportunity_types: tuple[str, ...]
    check_cadence: str
    enabled: bool
    priority: int
    legal_notes: str
    last_verified: date | None
    adapter: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or len(self.source_id) > 100:
            raise SourceError("Source id must be non-empty and no longer than 100 characters")
        if self.status not in {
            "active",
            "pending_policy",
            "pending_verification",
            "discovery_only",
        }:
            raise SourceError(f"Unsupported source status: {self.status}")
        if self.jurisdiction not in {"portugal", "direct_eu", "unknown"}:
            raise SourceError(f"Unsupported source jurisdiction: {self.jurisdiction}")
        if self.check_cadence not in {"daily", "twice_weekly", "weekly", "manual"}:
            raise SourceError(f"Unsupported source cadence: {self.check_cadence}")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not 1 <= self.priority <= 5
        ):
            raise SourceError("Source priority must be an integer from 1 to 5")
        if self.enabled and (self.status != "active" or self.base_url is None):
            raise SourceError("Only active sources with a verified base URL can be enabled")
        if self.base_url is not None:
            parsed = urlparse(self.base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise SourceError("Source base_url must be a credential-free HTTPS URL")
        if not isinstance(self.official, bool) or not self.legal_notes.strip():
            raise SourceError("Source official flag and legal notes are required")
        if not isinstance(self.content_types, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.content_types
        ):
            raise SourceError("Source content_types must be a tuple of non-empty strings")
        if not isinstance(self.opportunity_types, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.opportunity_types
        ):
            raise SourceError("Source opportunity_types must be a tuple of non-empty strings")
        if self.last_verified is not None and not isinstance(self.last_verified, date):
            raise SourceError("Source last_verified must be a calendar date or unknown")
        if self.status == "active" and self.last_verified is None:
            raise SourceError("Active sources need a verification date")


def load_source_registry(path: Path = REGISTRY_PATH) -> tuple[SourceDefinition, ...]:
    """Load and validate the declarative source registry."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError("Could not read the source registry") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SourceError("Unsupported source registry format")
    if data.get("default_cadence") not in {"daily", "twice_weekly", "weekly", "manual"}:
        raise SourceError("Source registry default_cadence is invalid")
    items = data.get("sources")
    if not isinstance(items, list):
        raise SourceError("Source registry must contain a sources list")

    definitions: list[SourceDefinition] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise SourceError("Each source definition must be an object")
        try:
            normalized = dict(item)
            for key in ("content_types", "opportunity_types"):
                value = normalized.get(key)
                if isinstance(value, list):
                    normalized[key] = tuple(value)
            if isinstance(normalized.get("last_verified"), str):
                normalized["last_verified"] = date.fromisoformat(normalized["last_verified"])
            definition = SourceDefinition(**normalized)
        except TypeError as exc:
            raise SourceError("Source definition has missing or unexpected fields") from exc
        except ValueError as exc:
            raise SourceError("Source definition contains an invalid date or value") from exc
        if definition.source_id in seen:
            raise SourceError("Source IDs must be non-empty and unique")
        seen.add(definition.source_id)
        definitions.append(definition)
    return tuple(definitions)


JsonTransport = Callable[[str, Mapping[str, str], Mapping[str, object], float], object]


def _post_json(
    endpoint: str,
    params: Mapping[str, str],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> object:
    """POST JSON to a public endpoint; kept replaceable for fixture tests."""
    request = Request(
        f"{endpoint}?{urlencode(params)}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise SourceError(f"Funding & Tenders API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Funding & Tenders API request failed: {type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("Funding & Tenders API response exceeds 5 MiB")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("Funding & Tenders API returned invalid JSON") from exc


def _result_rows(response: object) -> list[Mapping[str, object]]:
    if isinstance(response, list):
        rows = response
    elif isinstance(response, dict):
        rows = next((response[key] for key in ("results", "items", "data") if key in response), [])
        if isinstance(rows, dict):
            rows = rows.get("results", rows.get("items", []))
    else:
        raise SourceError("Funding & Tenders API returned an unexpected response")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceError("Funding & Tenders API response has an invalid results list")
    return rows


def _flatten_record(row: Mapping[str, object]) -> dict[str, object]:
    content = row.get("content")
    if isinstance(content, dict):
        return {**row, **content}
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return dict(row)
        if isinstance(decoded, dict):
            return {**row, **decoded}
    return dict(row)


class FundingTendersAdapter:
    """Search public call records through the Commission Search API."""

    source_id = "eu_funding_tenders"
    endpoint = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"

    def __init__(
        self,
        *,
        transport: JsonTransport = _post_json,
        timeout_seconds: float = 20,
        api_key: str = "SEDIA",
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not api_key or len(api_key) > 100:
            raise ValueError("api_key must contain between 1 and 100 characters")
        self._transport = transport
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        # The API uses a JSON query body and public URL parameters. The default
        # query deliberately leaves filtering to its `text` parameter.
        response = self._transport(
            self.endpoint,
            {
                "apiKey": self.api_key,
                "text": query.strip() or "*",
                "pageSize": str(limit),
                "language": "en",
            },
            {"bool": {"must": []}},
            self.timeout_seconds,
        )
        retrieved_at = datetime.now(UTC)
        documents: list[SourceDocument] = []
        for row in _result_rows(response)[:limit]:
            record = _flatten_record(row)
            title = next(
                (
                    str(record[key]).strip()
                    for key in ("title", "title_en", "titleEN", "name", "subject")
                    if isinstance(record.get(key), str) and str(record[key]).strip()
                ),
                "EU funding opportunity",
            )[:300]
            record_id = str(record.get("id") or record.get("topicCode") or title)[:200]
            url_keys = ("url", "webUrl", "topicUrl", "link", "urlEN", "permalink")
            links = tuple(
                dict.fromkeys(
                    value
                    for key in url_keys
                    if isinstance((value := record.get(key)), str) and value.startswith("https://")
                )
            )
            scalar_metadata = tuple(
                (key, str(value)[:500])
                for key, value in record.items()
                if isinstance(value, (str, int, float, bool))
            )
            documents.append(
                SourceDocument(
                    source_id=self.source_id,
                    source_url=links[0] if links else self.endpoint,
                    retrieved_at=retrieved_at,
                    content_type="application/json",
                    title=title,
                    text=json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
                    metadata=(("record_id", record_id), *scalar_metadata[:99]),
                    discovered_links=links,
                )
            )
        return tuple(documents)


def create_adapter_registry(
    *,
    transport: JsonTransport = _post_json,
    agent_reach_adapter: SourceAdapter | None = None,
) -> dict[str, SourceAdapter]:
    """Construct registered adapters and optionally attach a host integration."""
    adapters: dict[str, SourceAdapter] = {
        definition.source_id: FundingTendersAdapter(transport=transport)
        for definition in load_source_registry()
        if definition.enabled
        and definition.status == "active"
        and definition.adapter == "funding_tenders"
    }
    if agent_reach_adapter is not None:
        if not isinstance(agent_reach_adapter, SourceAdapter):
            raise TypeError("agent_reach_adapter must implement SourceAdapter")
        adapters[agent_reach_adapter.source_id] = agent_reach_adapter
    return adapters
