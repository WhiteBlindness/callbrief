"""Acquisition adapters for verified official funding sources."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import posixpath
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .domain import SourceDocument

REGISTRY_PATH = Path(__file__).with_name("data") / "source_registry.json"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_XLSX_XML_BYTES = 20 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_XLSX_ROWS = 10_000
MAX_XLSX_COLUMNS = 256
PORTUGAL2030_ANNUAL_PLAN_URL = (
    "https://portugal2030.pt/wp-content/uploads/sites/3/2026/09/"
    "PlanoAnualAvisos_download_140926-1.xlsx"
)
PORTUGAL2030_ANNUAL_PLAN_PAGE_URL = "https://portugal2030.pt/plano-anual-de-avisos/"


class SourceError(RuntimeError):
    """Raised when an upstream source cannot provide a valid response."""


@runtime_checkable
class SourceAdapter(Protocol):
    """Stable contract for adapters that return normalized source documents."""

    @property
    def source_id(self) -> str: ...

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]: ...


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    body: object
    status_code: int
    response_bytes: int
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class HttpTextResponse:
    body: str
    status_code: int
    response_bytes: int
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class HttpBinaryResponse:
    body: bytes
    status_code: int
    response_bytes: int
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class SourceFetchResult:
    source_id: str
    documents: tuple[SourceDocument, ...]
    http_status: int
    response_bytes: int
    total_results: int | None
    pagination_state: str
    rows_received: int = 0
    rejected_rows: int = 0


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
    legal_status: str = "policy_unverified"
    commercial_redistribution: bool = False

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
        if self.legal_status not in {
            "documented_reuse",
            "terms_need_confirmation",
            "permission_required",
            "license_unspecified",
            "policy_unverified",
        }:
            raise SourceError(f"Unsupported source legal_status: {self.legal_status}")
        if not isinstance(self.commercial_redistribution, bool):
            raise SourceError("commercial_redistribution must be a boolean")
        if self.commercial_redistribution and self.legal_status != "documented_reuse":
            raise SourceError("Commercial redistribution needs documented reuse terms")


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
TextTransport = Callable[[str, float], object]
BinaryTransport = Callable[[str, float], object]


def _post_json(
    endpoint: str,
    params: Mapping[str, str],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> HttpJsonResponse:
    """POST JSON to a public endpoint; kept replaceable for fixture tests."""
    url = f"{endpoint}?{urlencode(params)}" if params else endpoint
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status_code = int(getattr(response, "status", 200))
            last_modified = response.headers.get("Last-Modified")
    except HTTPError as exc:
        raise SourceError(f"Source API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Source API request failed: {type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("Source API response exceeds 5 MiB")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("Source API returned invalid JSON") from exc
    return HttpJsonResponse(value, status_code, len(body), last_modified)


def _result_rows(response: object) -> list[Mapping[str, object]]:
    if isinstance(response, HttpJsonResponse):
        response = response.body
    if isinstance(response, list):
        rows = response
    elif isinstance(response, dict):
        result_key = next(
            (key for key in ("results", "notices", "items", "data") if key in response),
            None,
        )
        if result_key is None:
            raise SourceError("Source API response is missing a recognized results list")
        rows = response[result_key]
        if isinstance(rows, dict):
            nested_key = next((key for key in ("results", "items") if key in rows), None)
            if nested_key is None:
                raise SourceError("Source API response has an invalid nested results wrapper")
            rows = rows[nested_key]
    else:
        raise SourceError("Source API returned an unexpected response")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceError("Source API response has an invalid results list")
    return rows


def _total_results(response: object) -> int | None:
    if isinstance(response, HttpJsonResponse):
        response = response.body
    if not isinstance(response, dict):
        return None
    for key in ("totalResults", "total", "totalElements", "numberOfResults"):
        value = response.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _response_details(response: object) -> tuple[object, int, int, str | None]:
    if isinstance(response, HttpJsonResponse):
        return response.body, response.status_code, response.response_bytes, response.last_modified
    try:
        response_bytes = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        response_bytes = 0
    return response, 200, response_bytes, None


def _get_html(endpoint: str, timeout_seconds: float) -> HttpTextResponse:
    """GET a bounded public HTML page and preserve its response metadata."""
    request = Request(endpoint, headers={"Accept": "text/html"}, method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status_code = int(getattr(response, "status", 200))
            last_modified = response.headers.get("Last-Modified")
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise SourceError(f"Source page returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Source page request failed: {type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("Source page response exceeds 5 MiB")
    if content_type != "text/html":
        raise SourceError("Source page returned an unexpected content type")
    try:
        value = body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        raise SourceError("Source page returned invalid text") from None
    return HttpTextResponse(value, status_code, len(body), last_modified)


def _get_binary(endpoint: str, timeout_seconds: float) -> HttpBinaryResponse:
    """GET a bounded official binary file and preserve response metadata."""
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme != "https" or parsed_endpoint.hostname != "portugal2030.pt":
        raise SourceError("Binary source URL must use the official Portugal 2030 host")
    request = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status_code = int(getattr(response, "status", 200))
            last_modified = response.headers.get("Last-Modified")
            content_type = response.headers.get_content_type()
            final_url = urlparse(response.geturl())
    except HTTPError as exc:
        raise SourceError(f"Source file returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Source file request failed: {type(exc).__name__}") from None
    if (
        final_url.scheme != "https"
        or final_url.hostname != "portugal2030.pt"
        or final_url.username
        or final_url.password
        or final_url.port not in {None, 443}
    ):
        raise SourceError("Source file redirected outside the official Portugal 2030 host")
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("Source file response exceeds 5 MiB")
    if content_type not in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
        "application/vnd.ms-excel",
    }:
        raise SourceError("Source file returned an unexpected content type")
    return HttpBinaryResponse(body, status_code, len(body), last_modified)


def _binary_response_details(response: object) -> HttpBinaryResponse:
    if not isinstance(response, HttpBinaryResponse) or not isinstance(response.body, bytes):
        raise SourceError("Source file returned an unexpected response")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise SourceError("Source file response exceeds 5 MiB")
    return response


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XLSX_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_ANNUAL_PLAN_RETAINED_HEADERS = {
    "tipoentbeneficiaria",
    "tipodeentidadebeneficiaria",
    "naturezaaviso",
    "objetivoespecifico",
    "fundo",
    "dotacaofundo",
    "dotacaodofundo",
    "datainicioprevista",
    "datadeinicioprevista",
    "datafimprevista",
    "datadefimprevista",
    "quadrimestre",
    "nutsii",
    "modalidadeapresentacaocandidatura",
    "modalidadedeapresentacaodecandidatura",
}


def _xlsx_xml(archive: ZipFile, name: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise SourceError("Annual plan workbook is missing a required XML part") from exc
    if info.file_size > MAX_XLSX_XML_BYTES:
        raise SourceError("Annual plan workbook contains an oversized XML part")
    try:
        contents = archive.read(name)
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise SourceError("Annual plan workbook contains an invalid ZIP member") from exc
    try:
        xml_text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("Annual plan workbook XML must use UTF-8") from exc
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        raise SourceError("Annual plan workbook XML cannot contain DTD or entity declarations")
    try:
        return ElementTree.fromstring(contents)
    except ElementTree.ParseError as exc:
        raise SourceError("Annual plan workbook contains malformed XML") from exc


def _xlsx_sheet_path(archive: ZipFile) -> str:
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        candidates = sorted(
            name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        if candidates:
            return candidates[0]
        raise SourceError("Annual plan workbook has no worksheet")

    workbook = _xlsx_xml(archive, "xl/workbook.xml")
    sheet = workbook.find(f"{_XLSX_NS}sheets/{_XLSX_NS}sheet")
    if sheet is None:
        raise SourceError("Annual plan workbook has no worksheet")
    relationship_id = sheet.attrib.get(f"{_XLSX_DOC_REL_NS}id")
    if not relationship_id:
        raise SourceError("Annual plan workbook has an invalid worksheet reference")
    relationships = _xlsx_xml(archive, "xl/_rels/workbook.xml.rels")
    target = next(
        (
            item.attrib.get("Target")
            for item in relationships.findall(f"{_XLSX_PACKAGE_REL_NS}Relationship")
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if not target:
        raise SourceError("Annual plan workbook has a broken worksheet reference")
    path = (
        posixpath.normpath(target.lstrip("/"))
        if target.startswith("/")
        else posixpath.normpath(posixpath.join("xl", target))
    )
    if not path.startswith("xl/worksheets/") or path not in names:
        raise SourceError("Annual plan workbook worksheet path is invalid")
    return path


def _xlsx_column_index(reference: str) -> int | None:
    match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", reference)
    if not match:
        return None
    if int(match.group(2)) > 1_048_576:
        raise SourceError("Annual plan worksheet row is outside the XLSX limit")
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - ord("A") + 1
    if column > 16_384:
        raise SourceError("Annual plan worksheet column is outside the XLSX limit")
    return column - 1


def _xlsx_rows(payload: bytes) -> tuple[tuple[str, ...], ...]:
    try:
        archive = ZipFile(BytesIO(payload))
    except (BadZipFile, OSError) as exc:
        raise SourceError("Annual plan response is not a valid XLSX workbook") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) != len({info.filename for info in infos}):
            raise SourceError("Annual plan workbook contains duplicate ZIP member names")
        if any(info.flag_bits & 0x1 for info in infos):
            raise SourceError("Encrypted annual plan workbooks are not supported")
        if sum(info.file_size for info in infos) > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise SourceError("Annual plan workbook expands beyond the 40 MiB limit")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = _xlsx_xml(archive, "xl/sharedStrings.xml")
            shared_strings = [
                "".join(part.text or "" for part in item.iter(f"{_XLSX_NS}t"))
                for item in shared_root.findall(f"{_XLSX_NS}si")
            ]

        worksheet = _xlsx_xml(archive, _xlsx_sheet_path(archive))
        sheet_data = worksheet.find(f"{_XLSX_NS}sheetData")
        if sheet_data is None:
            raise SourceError("Annual plan worksheet has no tabular data")
        rows: list[tuple[str, ...]] = []
        worksheet_rows = sheet_data.findall(f"{_XLSX_NS}row")
        if len(worksheet_rows) > MAX_XLSX_ROWS:
            raise SourceError("Annual plan worksheet exceeds the 10,000-row limit")
        for row in worksheet_rows:
            cells: dict[int, str] = {}
            for cell in row.findall(f"{_XLSX_NS}c"):
                reference = cell.attrib.get("r", "")
                column_index = _xlsx_column_index(reference)
                if column_index is None:
                    continue
                if column_index >= MAX_XLSX_COLUMNS:
                    raise SourceError("Annual plan worksheet exceeds the 256-column limit")
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    inline = cell.find(f"{_XLSX_NS}is")
                    value = (
                        "".join(part.text or "" for part in inline.iter(f"{_XLSX_NS}t"))
                        if inline is not None
                        else ""
                    )
                else:
                    value_element = cell.find(f"{_XLSX_NS}v")
                    value = value_element.text or "" if value_element is not None else ""
                    if cell_type == "s" and value:
                        try:
                            shared_index = int(value)
                        except ValueError as exc:
                            raise SourceError(
                                "Annual plan workbook contains an invalid shared string index"
                            ) from exc
                        if not 0 <= shared_index < len(shared_strings):
                            raise SourceError(
                                "Annual plan workbook contains an invalid shared string index"
                            )
                        value = shared_strings[shared_index]
                cells[column_index] = value
            if cells:
                rows.append(tuple(cells.get(index, "") for index in range(max(cells) + 1)))
        if not rows:
            raise SourceError("Annual plan worksheet has no rows")
        return tuple(rows)


def _xlsx_header_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in normalized
        if character.isalnum() and not unicodedata.combining(character)
    )


def _annual_plan_header_indexes(header: tuple[str, ...]) -> tuple[int, int, int]:
    fields = {
        "record_id": {
            "id",
            "idaviso",
            "iddoaviso",
            "codigoaviso",
            "codigodoaviso",
            "identificador",
            "numeroaviso",
            "numerodoaviso",
        },
        "title": {
            "designacaodoaviso",
            "designacao",
            "nomedoaviso",
        },
        "programme": {"programa", "programafinanciador", "programaoperacional"},
    }
    normalized = [_xlsx_header_key(value) for value in header]
    indexes: list[int] = []
    for field in ("record_id", "title", "programme"):
        found = next(
            (index for index, value in enumerate(normalized) if value in fields[field]),
            None,
        )
        if found is None:
            raise SourceError("Annual plan worksheet is missing a required header")
        indexes.append(found)
    non_empty = [value for value in normalized if value]
    if len(non_empty) != len(set(non_empty)):
        raise SourceError("Annual plan worksheet contains duplicate column headers")
    return indexes[0], indexes[1], indexes[2]


def _html_response_details(response: object) -> tuple[str, int, int, str | None]:
    if isinstance(response, HttpTextResponse):
        return response.body, response.status_code, response.response_bytes, response.last_modified
    if not isinstance(response, str):
        raise SourceError("Source page returned an unexpected response")
    return response, 200, len(response.encode("utf-8")), None


def _source_metadata(total: int | None, returned: int, limit: int) -> tuple[tuple[str, str], ...]:
    state = (
        "more_available"
        if total is not None and total > returned
        else "limit_reached_unknown"
        if returned == limit and total is None
        else "complete"
    )
    return (
        ("source_total_results", str(total) if total is not None else ""),
        ("source_returned_results", str(returned)),
        ("source_pagination_state", state),
    )


@dataclass(frozen=True, slots=True)
class _HtmlAnchor:
    href: str
    text: str
    start: int
    end: int


class _VisibleTextParser(HTMLParser):
    """Retain visible text offsets and links from a small official listing page."""

    _BLOCK_TAGS = frozenset({"br", "div", "li", "p", "h1", "h2", "h3", "section"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._visible_length = 0
        self._ends_with_newline = False
        self.anchors: list[_HtmlAnchor] = []
        self._anchor_href: str | None = None
        self._anchor_start = 0
        self._anchor_parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def _append(self, value: str) -> None:
        self._parts.append(value)
        self._visible_length += len(value)
        self._ends_with_newline = value.endswith("\n") if value else self._ends_with_newline

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS and self._visible_length and not self._ends_with_newline:
            self._append("\n")
        if tag == "a":
            href = next((value for name, value in attrs if name == "href"), None)
            if isinstance(href, str):
                self._anchor_href = href
                self._anchor_start = self._visible_length
                self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            title = " ".join("".join(self._anchor_parts).split())
            self.anchors.append(
                _HtmlAnchor(self._anchor_href, title, self._anchor_start, self._visible_length)
            )
            self._anchor_href = None
            self._anchor_parts = []
        if tag in self._BLOCK_TAGS and self._visible_length and not self._ends_with_newline:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        self._append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)


class CineaLifeAdapter:
    """Extract LIFE call titles and deadlines from CINEA's official listing."""

    source_id = "cinea_life"
    endpoint = "https://cinea.ec.europa.eu/life-calls-proposals-2026_en"
    _DEADLINE = re.compile(r"Deadline\s+date\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE)
    _MONTHS = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    def __init__(
        self,
        *,
        transport: TextTransport = _get_html,
        timeout_seconds: float = 20,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self._transport = transport
        self.timeout_seconds = timeout_seconds

    @classmethod
    def _parse_deadline(cls, value: str) -> date | None:
        parts = value.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
            return None
        month = cls._MONTHS.get(parts[1].casefold())
        if month is None:
            return None
        try:
            return date(int(parts[2]), month, int(parts[0]))
        except ValueError:
            return None

    @staticmethod
    def _official_link(base_url: str, href: str) -> str | None:
        target = urljoin(base_url, href)
        parsed = urlparse(target)
        hostname = parsed.hostname.casefold().rstrip(".") if parsed.hostname else ""
        if hostname == "safelinks.protection.outlook.com" or hostname.endswith(
            ".safelinks.protection.outlook.com"
        ):
            wrapped_targets = {
                key.casefold(): values for key, values in parse_qs(parsed.query).items()
            }.get("url", [])
            if not wrapped_targets:
                return None
            target = wrapped_targets[0]
            parsed = urlparse(target)
            hostname = parsed.hostname.casefold().rstrip(".") if parsed.hostname else ""
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or hostname not in {"cinea.ec.europa.eu", "ec.europa.eu"}
        ):
            return None
        return target

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        return self.fetch_with_report(query, limit=limit).documents

    def fetch_with_report(self, query: str = "", *, limit: int = 50) -> SourceFetchResult:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        response = self._transport(self.endpoint, self.timeout_seconds)
        html, http_status, response_bytes, last_modified = _html_response_details(response)
        parser = _VisibleTextParser()
        try:
            parser.feed(html)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise SourceError("CINEA page contains malformed HTML") from exc
        text = parser.text
        candidates = [
            anchor
            for anchor in parser.anchors
            if "standard action projects (saps)" in anchor.text.casefold()
        ]
        retrieved_at = datetime.now(UTC)
        selected_query = query.strip().casefold()
        documents: list[SourceDocument] = []
        rejected = 0
        for index, anchor in enumerate(candidates):
            context_end = candidates[index + 1].start if index + 1 < len(candidates) else len(text)
            context = text[anchor.end : context_end]
            match = self._DEADLINE.search(context)
            detail_url = self._official_link(self.endpoint, anchor.href)
            if match is None or detail_url is None:
                rejected += 1
                continue
            due_date = self._parse_deadline(match.group(1))
            if due_date is None:
                rejected += 1
                continue
            excerpt_end = anchor.end + match.end()
            excerpt = text[anchor.start : excerpt_end].strip()
            heading = re.search(
                r"\bLIFE\s+calls\s+for\s+proposals\s+2026\b",
                text,
                re.IGNORECASE,
            )
            if heading is not None:
                excerpt = f"{heading.group(0)}\n{excerpt}"
            title = anchor.text.strip()
            searchable_text = f"{title} {excerpt} LIFE 2026".casefold()
            if selected_query and selected_query not in searchable_text:
                continue
            record_id = f"CINEA-{hashlib.sha256(detail_url.encode('utf-8')).hexdigest()[:24]}"
            documents.append(
                SourceDocument(
                    source_id=self.source_id,
                    source_url=self.endpoint,
                    retrieved_at=retrieved_at,
                    content_type="text/html; charset=utf-8",
                    title=title[:300],
                    text=excerpt,
                    metadata=(
                        ("record_id", record_id),
                        ("title", title[:300]),
                        ("deadline", match.group(1)),
                        ("programme", "LIFE"),
                        ("source_page_sha256", hashlib.sha256(html.encode("utf-8")).hexdigest()),
                    ),
                    discovered_links=(detail_url,),
                    last_modified=last_modified,
                )
            )
            if len(documents) >= limit:
                break
        total = len(candidates) - rejected
        pagination = dict(_source_metadata(total, len(documents), limit))["source_pagination_state"]
        return SourceFetchResult(
            self.source_id,
            tuple(documents),
            http_status,
            response_bytes,
            total,
            pagination,
            len(candidates),
            rejected,
        )


def _first_text(record: Mapping[str, object], names: tuple[str, ...]) -> str | None:
    normalized = {
        "".join(char for char in key.casefold() if char.isalnum()): value
        for key, value in record.items()
    }
    for name in names:
        value = normalized.get("".join(char for char in name.casefold() if char.isalnum()))
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


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
        return self.fetch_with_report(query, limit=limit).documents

    def fetch_with_report(self, query: str = "", *, limit: int = 50) -> SourceFetchResult:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        # The API uses a JSON query body and public URL parameters. The default
        # query deliberately leaves filtering to its `text` parameter.
        transport_response = self._transport(
            self.endpoint,
            {
                "apiKey": self.api_key,
                "text": query.strip() or "*",
                "pageSize": str(limit),
                "pageNumber": "1",
                "language": "en",
            },
            {"bool": {"must": [{"terms": {"status": ["31094501", "31094502", "31094503"]}}]}},
            self.timeout_seconds,
        )
        response, http_status, response_bytes, last_modified = _response_details(transport_response)
        retrieved_at = datetime.now(UTC)
        rows = _result_rows(response)
        total = _total_results(response)
        documents: list[SourceDocument] = []
        rejected = 0
        for row in rows[:limit]:
            record = _flatten_record(row)
            title = next(
                (
                    str(record[key]).strip()
                    for key in ("title", "title_en", "titleEN", "name", "subject")
                    if isinstance(record.get(key), str) and str(record[key]).strip()
                ),
                "EU funding opportunity",
            )[:300]
            stable_id = record.get("id") or record.get("topicCode") or record.get("callIdentifier")
            if not isinstance(stable_id, (str, int)) or not str(stable_id).strip():
                rejected += 1
                continue
            record_id = str(stable_id)[:200]
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
                    metadata=(
                        ("record_id", record_id),
                        *_source_metadata(total, min(len(rows), limit), limit),
                        *scalar_metadata[:96],
                    ),
                    discovered_links=links,
                    last_modified=last_modified,
                )
            )
        returned = len(documents)
        pagination = dict(_source_metadata(total, returned, limit))["source_pagination_state"]
        return SourceFetchResult(
            self.source_id,
            tuple(documents),
            http_status,
            response_bytes,
            total,
            pagination,
            min(len(rows), limit),
            rejected,
        )


class TedSearchAdapter:
    """Search published procurement notices through the public TED Search API."""

    source_id = "ted_eu_procurement"
    endpoint = "https://api.ted.europa.eu/v3/notices/search"
    fields = (
        "publication-number",
        "notice-title",
        "publication-date",
        "deadline",
        "buyer-name",
        "buyer-country",
        "place-of-performance",
        "notice-type",
    )

    def __init__(
        self,
        *,
        transport: JsonTransport = _post_json,
        timeout_seconds: float = 20,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self._transport = transport
        self.timeout_seconds = timeout_seconds

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        return self.fetch_with_report(query, limit=limit).documents

    def fetch_with_report(self, query: str = "", *, limit: int = 50) -> SourceFetchResult:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cutoff = (datetime.now(UTC).date()).toordinal() - 90
        recent_date = date.fromordinal(cutoff).strftime("%Y%m%d")
        selected_query = query.strip() or f"PD>={recent_date}"
        transport_response = self._transport(
            self.endpoint,
            {},
            {
                "query": selected_query,
                "fields": list(self.fields),
                "page": 1,
                "limit": limit,
                "paginationMode": "PAGE_NUMBER",
                "checkQuerySyntax": True,
            },
            self.timeout_seconds,
        )
        response, http_status, response_bytes, last_modified = _response_details(transport_response)
        retrieved_at = datetime.now(UTC)
        rows = _result_rows(response)
        total = _total_results(response)
        documents: list[SourceDocument] = []
        rejected = 0
        for row in rows[:limit]:
            record_id = _first_text(row, ("publication-number", "notice-identifier", "ND", "id"))
            if record_id is None:
                rejected += 1
                continue
            title = _first_text(row, ("notice-title", "title", "TI"))
            canonical = f"https://ted.europa.eu/en/notice/-/detail/{record_id}"
            links = tuple(
                dict.fromkeys(
                    value
                    for key in ("url", "notice-url", "webUrl")
                    if isinstance((value := row.get(key)), str) and value.startswith("https://")
                )
            ) or (canonical,)
            record = dict(row)
            documents.append(
                SourceDocument(
                    source_id=self.source_id,
                    source_url=links[0],
                    retrieved_at=retrieved_at,
                    content_type="application/json",
                    title=(title or "TED procurement notice")[:300],
                    text=json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
                    metadata=(
                        ("record_id", record_id),
                        *_source_metadata(total, min(len(rows), limit), limit),
                        ("publication_number", record_id),
                    ),
                    discovered_links=links,
                    last_modified=last_modified,
                )
            )
        pagination = dict(_source_metadata(total, len(documents), limit))["source_pagination_state"]
        return SourceFetchResult(
            self.source_id,
            tuple(documents),
            http_status,
            response_bytes,
            total,
            pagination,
            min(len(rows), limit),
            rejected,
        )


class Portugal2030AnnualPlanAdapter:
    """Read forecast rows from the official Portugal 2030 annual-plan workbook."""

    source_id = "portugal2030_annual_plan"
    endpoint = PORTUGAL2030_ANNUAL_PLAN_URL
    listing_url = PORTUGAL2030_ANNUAL_PLAN_PAGE_URL

    def __init__(
        self,
        *,
        transport: BinaryTransport = _get_binary,
        timeout_seconds: float = 20,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self._transport = transport
        self.timeout_seconds = timeout_seconds

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        return self.fetch_with_report(query, limit=limit).documents

    def fetch_with_report(self, query: str = "", *, limit: int = 50) -> SourceFetchResult:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        response = _binary_response_details(self._transport(self.endpoint, self.timeout_seconds))
        rows = _xlsx_rows(response.body)
        header_position: int | None = None
        header_indexes: tuple[int, int, int] | None = None
        for index, row in enumerate(rows):
            try:
                candidate_indexes = _annual_plan_header_indexes(row)
            except SourceError:
                continue
            header_position = index
            header_indexes = candidate_indexes
            header = row
            break
        if header_position is None or header_indexes is None:
            raise SourceError("Annual plan workbook has no recognized header row")

        title_index, programme_index, record_id_index = (
            header_indexes[1],
            header_indexes[2],
            header_indexes[0],
        )
        retrieved_at = datetime.now(UTC)
        workbook_hash = hashlib.sha256(response.body).hexdigest()
        received_rows = 0
        rejected_rows = 0
        matched_rows: list[tuple[str, str, dict[str, str]]] = []
        query_terms = tuple(term for term in query.casefold().split() if term)
        for row in rows[header_position + 1 :]:
            if not any(value.strip() for value in row):
                continue
            received_rows += 1
            raw_record = {
                column: row[index] if index < len(row) else ""
                for index, column in enumerate(header)
                if column.strip()
            }
            record_id = raw_record.get(header[record_id_index], "").strip()
            title = raw_record.get(header[title_index], "").strip()
            programme = raw_record.get(header[programme_index], "").strip()
            if not record_id or not title or not programme:
                rejected_rows += 1
                continue
            search_text = " ".join((title, programme, *raw_record.values())).casefold()
            if query_terms and not all(term in search_text for term in query_terms):
                continue
            retained_headers = _ANNUAL_PLAN_RETAINED_HEADERS | {
                _xlsx_header_key(header[record_id_index]),
                _xlsx_header_key(header[title_index]),
                _xlsx_header_key(header[programme_index]),
            }
            record = {
                name: value
                for name, value in raw_record.items()
                if _xlsx_header_key(name) in retained_headers
            }
            matched_rows.append((record_id, title, record))

        documents = tuple(
            SourceDocument(
                source_id=self.source_id,
                source_url=self.endpoint,
                retrieved_at=retrieved_at,
                content_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                title=title[:300],
                text=json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
                metadata=(
                    ("record_id", record_id),
                    ("source_payload_sha256", workbook_hash),
                    ("forecast_only", "true"),
                ),
                discovered_links=(self.listing_url,),
                last_modified=response.last_modified,
            )
            for record_id, title, record in matched_rows[:limit]
        )
        total = len(matched_rows)
        pagination = "more_available" if total > len(documents) else "complete"
        return SourceFetchResult(
            self.source_id,
            documents,
            response.status_code,
            response.response_bytes,
            total,
            pagination,
            received_rows,
            rejected_rows,
        )


@dataclass(frozen=True, slots=True)
class AgentReachPage:
    """Small bridge payload for page results supplied by an Agent-Reach host tool."""

    url: str
    title: str
    text: str
    content_type: str = "text/plain"
    links: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    retrieved_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None


class AgentReachBridge(Protocol):
    """Host-owned bridge contract; Agent-Reach internals stay outside CallBrief."""

    def fetch(self, query: str, *, limit: int) -> Iterable[AgentReachPage]: ...


class AgentReachSourceAdapter:
    """Map approved Agent-Reach page results to CallBrief source documents."""

    source_id = "agent_reach"

    def __init__(self, bridge: AgentReachBridge, *, allowed_hosts: Iterable[str]) -> None:
        hosts = tuple(dict.fromkeys(host.casefold().strip(".") for host in allowed_hosts))
        if not hosts or any(not host for host in hosts):
            raise ValueError("allowed_hosts must contain at least one official hostname")
        self._bridge = bridge
        self._allowed_hosts = hosts

    def _approved_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.casefold().rstrip(".")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return False
        try:
            port = parsed.port
        except ValueError:
            return False
        if port not in {None, 443}:
            return False
        return any(
            hostname == host or hostname.endswith(f".{host}") for host in self._allowed_hosts
        )

    def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query must be text no longer than 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        documents: list[SourceDocument] = []
        for page in self._bridge.fetch(query.strip(), limit=limit):
            if not isinstance(page, AgentReachPage) or not self._approved_url(page.url):
                continue
            links = tuple(link for link in page.links if self._approved_url(link))
            documents.append(
                SourceDocument(
                    source_id=self.source_id,
                    source_url=page.url,
                    retrieved_at=page.retrieved_at or datetime.now(UTC),
                    content_type=page.content_type,
                    title=page.title,
                    text=page.text,
                    metadata=page.metadata,
                    discovered_links=links,
                    etag=page.etag,
                    last_modified=page.last_modified,
                )
            )
            if len(documents) >= limit:
                break
        return tuple(documents)


def create_adapter_registry(
    *,
    transport: JsonTransport = _post_json,
    html_transport: TextTransport | None = None,
    binary_transport: BinaryTransport | None = None,
    agent_reach_adapter: SourceAdapter | None = None,
    agent_reach_bridge: AgentReachBridge | None = None,
    agent_reach_allowed_hosts: Iterable[str] = (),
) -> dict[str, SourceAdapter]:
    """Construct registered adapters and optionally attach a host integration."""
    adapters: dict[str, SourceAdapter] = {
        definition.source_id: (
            FundingTendersAdapter(transport=transport)
            if definition.adapter == "funding_tenders"
            else TedSearchAdapter(transport=transport)
            if definition.adapter == "ted_search"
            else Portugal2030AnnualPlanAdapter(transport=binary_transport or _get_binary)
            if definition.adapter == "portugal2030_annual_plan_xlsx"
            else CineaLifeAdapter(transport=html_transport or _get_html)
        )
        for definition in load_source_registry()
        if definition.enabled
        and definition.status == "active"
        and definition.adapter
        in {
            "funding_tenders",
            "ted_search",
            "cinea_life_html",
            "portugal2030_annual_plan_xlsx",
        }
    }
    if agent_reach_bridge is not None:
        adapters["agent_reach"] = AgentReachSourceAdapter(
            agent_reach_bridge,
            allowed_hosts=agent_reach_allowed_hosts,
        )
    if agent_reach_adapter is not None:
        if not isinstance(agent_reach_adapter, SourceAdapter):
            raise TypeError("agent_reach_adapter must implement SourceAdapter")
        adapters[agent_reach_adapter.source_id] = agent_reach_adapter
    return adapters
