from __future__ import annotations

import unittest
from datetime import UTC, datetime

from callbrief.deduplication import DuplicateKind, find_duplicate
from callbrief.domain import Opportunity, OpportunityStatus, OpportunityType


NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _call(
    *,
    identifier: str,
    source: str,
    call_id: str | None = "COMPETE2030-2026-7",
    title: str = "Transferência do conhecimento científico e tecnológico",
    authority: str = "COMPETE 2030",
    deadline: datetime | None = None,
    url: str | None = None,
) -> Opportunity:
    return Opportunity(
        id=identifier,
        source_id=source,
        source_record_id=f"record-{identifier}",
        programme="Portugal 2030",
        call_id=call_id,
        title=title,
        authority=authority,
        canonical_url=url,
        status=OpportunityStatus.OPEN,
        opportunity_type=OpportunityType.GRANT,
        deadline=deadline,
        source_retrieved_at=NOW,
    )


class DeduplicationTests(unittest.TestCase):
    def test_same_programme_and_call_identifier_is_a_strong_duplicate(self) -> None:
        result = find_duplicate(
            _call(identifier="a", source="portal-a"),
            (_call(identifier="b", source="portal-b"),),
        )

        self.assertEqual(result.kind, DuplicateKind.EXACT)
        self.assertEqual(result.canonical_opportunity_id, "b")
        self.assertIn("call_id", result.matched_by)

    def test_equivalent_canonical_urls_ignore_tracking_parameters(self) -> None:
        result = find_duplicate(
            _call(
                identifier="a",
                source="portal-a",
                call_id=None,
                url="https://example.gov/call/1?utm_source=mail",
            ),
            (
                _call(
                    identifier="b",
                    source="portal-b",
                    call_id=None,
                    url="https://example.gov/call/1",
                ),
            ),
        )

        self.assertEqual(result.kind, DuplicateKind.EXACT)
        self.assertIn("canonical_url", result.matched_by)

    def test_similar_titles_are_flagged_without_silent_merge(self) -> None:
        result = find_duplicate(
            _call(identifier="a", source="portal-a", call_id=None),
            (
                _call(
                    identifier="b",
                    source="portal-b",
                    call_id=None,
                    title="Transferência de conhecimento científico e tecnológico",
                    deadline=NOW,
                ),
            ),
        )

        self.assertEqual(result.kind, DuplicateKind.POSSIBLE)
        self.assertIsNone(result.canonical_opportunity_id)

    def test_different_call_codes_are_not_merged_even_when_titles_match(self) -> None:
        result = find_duplicate(
            _call(identifier="a", source="portal-a", call_id="CALL-A"),
            (_call(identifier="b", source="portal-b", call_id="CALL-B"),),
        )

        self.assertEqual(result.kind, DuplicateKind.NONE)

    def test_different_known_call_codes_are_not_merged_by_a_shared_url(self) -> None:
        result = find_duplicate(
            _call(
                identifier="a",
                source="portal-a",
                call_id="CALL-2026-01",
                url="https://example.gov/calls/current",
            ),
            (
                _call(
                    identifier="b",
                    source="portal-b",
                    call_id="CALL-2026-02",
                    url="https://example.gov/calls/current",
                ),
            ),
        )

        self.assertEqual(result.kind, DuplicateKind.NONE)


if __name__ == "__main__":
    unittest.main()
