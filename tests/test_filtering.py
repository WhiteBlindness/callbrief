from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from callbrief.domain import Opportunity, OpportunityStatus, OpportunityType, OrganisationProfile
from callbrief.filtering import FilterState, OpportunityFilter, filter_opportunity

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _opportunity(**changes: object) -> Opportunity:
    fields: dict[str, object] = {
        "id": "call-1",
        "source_id": "official-source",
        "source_record_id": "record-1",
        "programme": "Example programme",
        "title": "Example call",
        "authority": "Example authority",
        "status": OpportunityStatus.OPEN,
        "opportunity_type": OpportunityType.GRANT,
        "geography": ("Portugal",),
        "eligible_regions": ("Norte",),
        "eligible_applicant_types": ("SME",),
        "eligible_sectors": ("Manufacturing",),
        "funding_max": Decimal("500000"),
        "deadline": NOW + timedelta(days=20),
        "source_retrieved_at": NOW,
    }
    fields.update(changes)
    return Opportunity(**fields)


class OpportunityFilterTests(unittest.TestCase):
    def test_profile_preferences_include_matching_opportunities(self) -> None:
        profile = OrganisationProfile(
            id="client-a",
            workspace_id="consultancy",
            applicant_types=("SME",),
            sectors=("Manufacturing",),
            regions=("Norte",),
            preferred_opportunity_types=(OpportunityType.GRANT,),
            preferred_geographies=("Portugal",),
        )

        result = filter_opportunity(_opportunity(), OpportunityFilter(), profile=profile, now=NOW)

        self.assertEqual(result.state, FilterState.INCLUDED)
        self.assertEqual(result.reasons, ())

    def test_unknown_filter_field_needs_review_instead_of_silent_exclusion(self) -> None:
        result = filter_opportunity(
            _opportunity(opportunity_type=OpportunityType.UNKNOWN, geography=None),
            OpportunityFilter(
                opportunity_types=(OpportunityType.GRANT,),
                geographies=("Portugal",),
            ),
            now=NOW,
        )

        self.assertEqual(result.state, FilterState.NEEDS_REVIEW)
        self.assertEqual(len(result.reasons), 2)

    def test_explicit_mismatch_and_minimum_funding_exclude_call(self) -> None:
        result = filter_opportunity(
            _opportunity(eligible_regions=("Sul",)),
            OpportunityFilter(regions=("Norte",), minimum_funding=Decimal("600000")),
            now=NOW,
        )

        self.assertEqual(result.state, FilterState.EXCLUDED)
        self.assertEqual(len(result.reasons), 2)

    def test_region_filter_uses_eligible_regions_and_geography_is_separate(self) -> None:
        result = filter_opportunity(
            _opportunity(geography=("Portugal",), eligible_regions=("Norte",)),
            OpportunityFilter(regions=("Norte",), geographies=("Portugal",)),
            now=NOW,
        )

        self.assertEqual(result.state, FilterState.INCLUDED)
        self.assertEqual(result.reasons, ())

    def test_deadline_window_is_configurable_and_unknown_dates_need_review(self) -> None:
        late = filter_opportunity(
            _opportunity(), OpportunityFilter(deadline_window_days=10), now=NOW
        )
        unknown = filter_opportunity(
            _opportunity(deadline=None), OpportunityFilter(deadline_window_days=10), now=NOW
        )

        self.assertEqual(late.state, FilterState.EXCLUDED)
        self.assertEqual(unknown.state, FilterState.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
