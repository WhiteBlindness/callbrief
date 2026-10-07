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
    source_family: str | None = None,
    source_role: str | None = None,
    canonical_source: str | None = None,
    authority_relationship: str | None = None,
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
        source_family=source_family,
        source_role=source_role,
        canonical_source=canonical_source,
        authority_relationship=authority_relationship,
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

    def test_agency_discovery_relationship_is_reviewable_without_authority_equality(self) -> None:
        cinea = _call(
            identifier="cinea",
            source="cinea_life",
            call_id=None,
            authority="European Climate, Infrastructure and Environment Executive Agency",
            deadline=NOW,
            source_family="eu_direct_funding",
            source_role="discovery",
            canonical_source="eu_funding_tenders",
            authority_relationship="agency_presentation_to_canonical_topic",
        )
        funding_tenders = _call(
            identifier="funding-tenders",
            source="eu_funding_tenders",
            call_id=None,
            authority="European Commission",
            deadline=NOW,
            source_family="eu_direct_funding",
            source_role="canonical",
            canonical_source="eu_funding_tenders",
            authority_relationship="canonical_programme_record",
        )

        result = find_duplicate(cinea, (funding_tenders,))

        self.assertEqual(result.kind, DuplicateKind.PROBABLE)
        self.assertIsNone(result.canonical_opportunity_id)
        self.assertIn("known_source_relationship", result.matched_by)

    def test_same_title_in_recurring_annual_calls_is_not_a_duplicate(self) -> None:
        current = _call(
            identifier="current",
            source="portal-a",
            call_id="TRAINING-2026",
            title="Formação empresarial para pequenas empresas",
            deadline=datetime(2026, 12, 1, tzinfo=UTC),
        )
        previous = _call(
            identifier="previous",
            source="portal-b",
            call_id="TRAINING-2025",
            title="Formação empresarial para pequenas empresas",
            deadline=datetime(2025, 12, 1, tzinfo=UTC),
        )

        result = find_duplicate(current, (previous,))

        self.assertEqual(result.kind, DuplicateKind.NONE)


if __name__ == "__main__":
    unittest.main()
