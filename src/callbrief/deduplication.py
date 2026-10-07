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
    PROBABLE = "probable"
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


def _is_funding_topic_url(value: str | None) -> bool:
    if value is None:
        return False
    return "/topic-details/" in value.casefold()


def _conflicting_topic_identity(candidate: Opportunity, existing: Opportunity) -> bool:
    if candidate.topic_id and existing.topic_id:
        if _normalise_text(candidate.topic_id) != _normalise_text(existing.topic_id):
            return True
    same_topic_id = (
        candidate.topic_id is not None
        and existing.topic_id is not None
        and _normalise_text(candidate.topic_id) == _normalise_text(existing.topic_id)
    )
    return (
        not same_topic_id
        and _is_funding_topic_url(candidate.canonical_url)
        and _is_funding_topic_url(existing.canonical_url)
        and _normalise_url(candidate.canonical_url) != _normalise_url(existing.canonical_url)
    )


def _strong_match(candidate: Opportunity, existing: Opportunity) -> tuple[str, ...]:
    if _conflicting_topic_identity(candidate, existing):
        return ()
    same_topic_id = (
        candidate.topic_id is not None
        and existing.topic_id is not None
        and _normalise_text(candidate.topic_id) == _normalise_text(existing.topic_id)
    )
    if not same_topic_id and candidate.call_id and existing.call_id:
        if _normalise_text(candidate.call_id) != _normalise_text(existing.call_id):
            return ()
    matches: list[str] = []
    if _normalise_url(candidate.canonical_url) == _normalise_url(existing.canonical_url):
        if candidate.canonical_url is not None:
            matches.append("canonical_url")
    if same_topic_id:
        matches.append("topic_id")
    return tuple(matches)


def _review_match(
    candidate: Opportunity, existing: Opportunity
) -> tuple[DuplicateKind, tuple[str, ...]] | None:
    if _conflicting_topic_identity(candidate, existing):
        return None
    if any(
        value is None
        for value in (
            candidate.programme,
            existing.programme,
            candidate.title,
            existing.title,
        )
    ):
        return None
    if candidate.call_id and existing.call_id:
        if _normalise_text(candidate.call_id) != _normalise_text(existing.call_id):
            return None
    if _normalise_text(candidate.programme) != _normalise_text(existing.programme):
        return None
    if candidate.deadline and existing.deadline and candidate.deadline != existing.deadline:
        return None
    if candidate.opening_date and existing.opening_date:
        if candidate.opening_date != existing.opening_date:
            return None
    left = set(_normalise_text(candidate.title).split()) - _TITLE_STOP_WORDS
    right = set(_normalise_text(existing.title).split()) - _TITLE_STOP_WORDS
    if not left or not right:
        return None
    overlap = len(left & right) / len(left | right)
    if overlap < 0.8:
        return None
    known_canonical_relationship = (
        candidate.source_family is not None
        and candidate.source_family == existing.source_family
        and (
            candidate.canonical_source == existing.source_id
            or existing.canonical_source == candidate.source_id
        )
    )
    same_authority = (
        candidate.authority is not None
        and existing.authority is not None
        and _normalise_text(candidate.authority) == _normalise_text(existing.authority)
    )
    if not known_canonical_relationship and not same_authority:
        return None
    context: list[str] = ["programme", "normalized_title_context"]
    if candidate.deadline and existing.deadline:
        context.append("deadline")
    if candidate.opening_date and existing.opening_date:
        context.append("opening_date")
    if known_canonical_relationship:
        context.append("known_source_relationship")
        return DuplicateKind.PROBABLE, tuple(context)
    return DuplicateKind.POSSIBLE, tuple(context)


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
    review_matches = tuple((item, _review_match(candidate, item)) for item in ordered)
    probable = tuple(
        (item, match)
        for item, match in review_matches
        if match is not None and match[0] is DuplicateKind.PROBABLE
    )
    if probable:
        _, (kind, matched_by) = probable[0]
        return DuplicateResult(kind, None, matched_by)
    possible = tuple(
        (item, match)
        for item, match in review_matches
        if match is not None and match[0] is DuplicateKind.POSSIBLE
    )
    if possible:
        _, (kind, matched_by) = possible[0]
        return DuplicateResult(kind, None, matched_by)
    return DuplicateResult(DuplicateKind.NONE, None, ())
