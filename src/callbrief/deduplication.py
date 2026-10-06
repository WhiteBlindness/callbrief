"""Conservative opportunity matching that separates duplicates from review candidates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .domain import Opportunity

_TITLE_STOP_WORDS = frozenset(
    "a ao aos as da das de do dos e em na nas no nos o os para por com sem um uma uns umas".split()
)


class DuplicateKind(StrEnum):
    EXACT = "exact"
    POSSIBLE = "possible"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DuplicateResult:
    kind: DuplicateKind
    canonical_opportunity_id: str | None
    matched_by: tuple[str, ...]


def _normalise_text(value: str | None) -> str:
    if value is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))


def _normalise_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"gclid", "fbclid", "ref"}
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, urlencode(query), "")
    )


def _strong_match(candidate: Opportunity, existing: Opportunity) -> tuple[str, ...]:
    if candidate.call_id and existing.call_id:
        if _normalise_text(candidate.call_id) != _normalise_text(existing.call_id):
            return ()
    matches: list[str] = []
    if _normalise_url(candidate.canonical_url) == _normalise_url(existing.canonical_url):
        if candidate.canonical_url is not None:
            matches.append("canonical_url")
    if candidate.call_id and existing.call_id:
        same_programme = (
            candidate.programme is not None
            and existing.programme is not None
            and _normalise_text(candidate.programme) == _normalise_text(existing.programme)
        )
        same_call = _normalise_text(candidate.call_id) == _normalise_text(existing.call_id)
        if same_programme and same_call:
            matches.append("call_id")
    return tuple(matches)


def _possible_match(candidate: Opportunity, existing: Opportunity) -> bool:
    if any(
        value is None
        for value in (
            candidate.programme,
            existing.programme,
            candidate.authority,
            existing.authority,
            candidate.title,
            existing.title,
        )
    ):
        return False
    if candidate.call_id and existing.call_id:
        if _normalise_text(candidate.call_id) != _normalise_text(existing.call_id):
            return False
    if _normalise_text(candidate.programme) != _normalise_text(existing.programme):
        return False
    if _normalise_text(candidate.authority) != _normalise_text(existing.authority):
        return False
    if candidate.deadline and existing.deadline and candidate.deadline != existing.deadline:
        return False
    left = set(_normalise_text(candidate.title).split()) - _TITLE_STOP_WORDS
    right = set(_normalise_text(existing.title).split()) - _TITLE_STOP_WORDS
    if not left or not right:
        return False
    overlap = len(left & right) / len(left | right)
    return overlap >= 0.8


def find_duplicate(
    candidate: Opportunity,
    existing_opportunities: tuple[Opportunity, ...],
) -> DuplicateResult:
    """Return the strongest match; weak matches remain unmerged for human review."""
    ordered = tuple(sorted(existing_opportunities, key=lambda item: item.id))
    for existing in ordered:
        matched_by = _strong_match(candidate, existing)
        if matched_by:
            return DuplicateResult(DuplicateKind.EXACT, existing.id, matched_by)
    possible = tuple(item for item in ordered if _possible_match(candidate, item))
    if possible:
        return DuplicateResult(DuplicateKind.POSSIBLE, None, ("normalized_title_context",))
    return DuplicateResult(DuplicateKind.NONE, None, ())
