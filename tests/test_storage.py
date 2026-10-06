from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from callbrief.domain import (
    EligibilityAssessment,
    EligibilityState,
    FitAssessment,
    FitComponent,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
)
from callbrief.storage import SqliteStore

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _profile(organisation_id: str, name: str) -> OrganisationProfile:
    return OrganisationProfile(
        id=organisation_id,
        workspace_id="consultancy-1",
        legal_name=name,
        country="PT",
    )


def _opportunity(deadline: datetime | None) -> Opportunity:
    return Opportunity(
        id="call-1",
        source_id="official-source",
        source_record_id="record-1",
        programme="Example programme",
        call_id="CALL-1",
        title="Example call",
        authority="Example authority",
        canonical_url="https://example.gov/call/1",
        status=OpportunityStatus.OPEN,
        opportunity_type=OpportunityType.GRANT,
        deadline=deadline,
        source_retrieved_at=NOW,
        last_checked_at=NOW,
    )


class StorageTests(unittest.TestCase):
    def test_organisation_queries_are_scoped_to_workspace_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            store.save_organisation(_profile("client-a", "Client A"))
            store.save_organisation(_profile("client-b", "Client B"))

            client_a = store.get_organisation("consultancy-1", "client-a")
            wrong_workspace = store.get_organisation("other-workspace", "client-a")
            client_b = store.get_organisation("consultancy-1", "client-b")

            self.assertEqual(client_a.legal_name if client_a else None, "Client A")
            self.assertIsNone(wrong_workspace)
            self.assertEqual(client_b.legal_name if client_b else None, "Client B")
            store.close()

    def test_deadline_change_is_snapshotted_and_emits_change_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            initial = _opportunity(NOW + timedelta(days=10))
            changed = _opportunity(NOW + timedelta(days=20))

            self.assertEqual(store.save_opportunity(initial), ())
            records = store.save_opportunity(changed)
            snapshots = store.list_snapshots("call-1")
            store.close()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].field, "deadline")
            self.assertEqual(len(snapshots), 2)

    def test_unchanged_opportunity_does_not_create_a_new_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            opportunity = _opportunity(NOW + timedelta(days=10))

            store.save_opportunity(opportunity)
            store.save_opportunity(opportunity)
            snapshots = store.list_snapshots("call-1")
            store.close()

            self.assertEqual(len(snapshots), 1)

    def test_assessments_are_scoped_to_the_selected_client_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            store.save_organisation(_profile("client-a", "Client A"))
            store.save_organisation(_profile("client-b", "Client B"))
            store.save_opportunity(_opportunity(NOW + timedelta(days=10)), checked_at=NOW)
            eligibility = EligibilityAssessment(
                organisation_id="client-a",
                opportunity_id="call-1",
                state=EligibilityState.UNCERTAIN,
                findings=(),
                assessed_at=NOW,
            )
            fit = FitAssessment(
                organisation_id="client-a",
                opportunity_id="call-1",
                overall=85,
                components=(
                    FitComponent(
                        name="strategic_fit",
                        score=85,
                        weight=0.17,
                        explanation="Pontuação revista para o ensaio.",
                    ),
                ),
                explanation="Synthetic test assessment",
                assessed_at=NOW,
            )

            store.save_assessment("consultancy-1", "client-a", eligibility, fit)
            client_a = store.get_assessment("consultancy-1", "client-a", "call-1")
            client_b = store.get_assessment("consultancy-1", "client-b", "call-1")
            cross_workspace = store.get_assessment("another-workspace", "client-a", "call-1")
            client_a_events = store.list_notifications("consultancy-1", "client-a")
            client_b_events = store.list_notifications("consultancy-1", "client-b")
            global_events = store.list_notifications("consultancy-1")
            store.close()

            self.assertIsNotNone(client_a)
            self.assertIsNone(client_b)
            self.assertIsNone(cross_workspace)
            self.assertTrue(any(item["event_type"] == "new_opportunity" for item in global_events))
            self.assertTrue(
                any(item["event_type"] == "deadline_approaching" for item in global_events)
            )
            self.assertTrue(
                any(item["event_type"] == "new_high_fit_opportunity" for item in client_a_events)
            )
            self.assertNotIn(
                "new_high_fit_opportunity", {item["event_type"] for item in client_b_events}
            )

    def test_eligibility_only_score_does_not_enqueue_a_high_fit_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            store.save_organisation(_profile("client-a", "Client A"))
            store.save_opportunity(_opportunity(NOW + timedelta(days=10)), checked_at=NOW)
            eligibility = EligibilityAssessment(
                organisation_id="client-a",
                opportunity_id="call-1",
                state=EligibilityState.ELIGIBLE,
                findings=(),
                assessed_at=NOW,
            )
            fit = FitAssessment(
                organisation_id="client-a",
                opportunity_id="call-1",
                overall=100,
                components=(
                    FitComponent(
                        name="eligibility",
                        score=100,
                        weight=0.16,
                        explanation="Regras determinísticas cumpridas.",
                    ),
                    FitComponent(
                        name="strategic_fit",
                        score=100,
                        weight=0.0,
                        explanation="Peso nulo no ensaio.",
                    ),
                ),
                explanation="Only eligibility was scored.",
                assessed_at=NOW,
            )

            store.save_assessment("consultancy-1", "client-a", eligibility, fit)
            events = store.list_notifications("consultancy-1", "client-a")
            store.close()

            self.assertNotIn("new_high_fit_opportunity", {item["event_type"] for item in events})

    def test_refetch_time_alone_does_not_create_an_opportunity_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteStore(Path(temporary) / "callbrief.sqlite3")
            store.create_workspace("consultancy-1", "Example consultancy")
            original = _opportunity(NOW + timedelta(days=10))
            refreshed = replace(
                original,
                source_retrieved_at=NOW + timedelta(hours=1),
                last_checked_at=NOW + timedelta(hours=1),
            )

            store.save_opportunity(original)
            store.save_opportunity(refreshed)
            snapshots = store.list_snapshots("call-1")
            store.close()

            self.assertEqual(len(snapshots), 1)


if __name__ == "__main__":
    unittest.main()
