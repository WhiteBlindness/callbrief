from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from callbrief.change_tracking import _TRACKED_FIELDS, detect_changes, notification_types
from callbrief.domain import Opportunity, OpportunityStatus, OpportunityType

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _call() -> Opportunity:
    return Opportunity(
        id="call-1",
        source_id="official-source",
        source_record_id="record-1",
        programme="Example programme",
        title="Example call",
        authority="Example authority",
        status=OpportunityStatus.OPEN,
        opportunity_type=OpportunityType.GRANT,
        deadline=NOW + timedelta(days=20),
        source_retrieved_at=NOW,
    )


class ChangeTrackingTests(unittest.TestCase):
    def test_each_material_field_is_registered_once(self) -> None:
        self.assertEqual(len(_TRACKED_FIELDS), len(set(_TRACKED_FIELDS)))
        self.assertIn("status", _TRACKED_FIELDS)
        self.assertIn("eligibility_rules", _TRACKED_FIELDS)

    def test_deadline_and_status_changes_produce_notification_types(self) -> None:
        before = _call()
        after = replace(
            before,
            deadline=before.deadline + timedelta(days=10) if before.deadline else None,
            status=OpportunityStatus.CLOSED,
            source_retrieved_at=NOW + timedelta(hours=1),
        )

        changes = detect_changes(before, after, detected_at=NOW + timedelta(hours=1))
        event_types = {item for change in changes for item in notification_types(change)}

        self.assertEqual({item.field for item in changes}, {"deadline", "status"})
        self.assertIn("important_call_change", event_types)
        self.assertIn("call_closed", event_types)

    def test_retrieval_time_and_unmodeled_markup_do_not_create_material_changes(self) -> None:
        before = _call()
        after = replace(before, source_retrieved_at=NOW + timedelta(hours=1))

        changes = detect_changes(before, after, detected_at=NOW + timedelta(hours=1))

        self.assertEqual(changes, ())

    def test_budget_documents_and_eligibility_requirements_are_material_changes(self) -> None:
        before = _call()
        after = replace(
            before,
            budget_total=Decimal("1200000"),
            documents=("application.pdf",),
            other_hard_requirements=("Minimum financial autonomy: 20%",),
        )

        changes = detect_changes(before, after, detected_at=NOW + timedelta(hours=1))
        event_types = {event for item in changes for event in notification_types(item)}

        self.assertEqual(
            {item.field for item in changes},
            {"budget_total", "documents", "other_hard_requirements"},
        )
        self.assertIn("eligibility_requirement_changed", event_types)

    def test_deadline_approaching_notification_uses_current_time_and_window(self) -> None:
        from callbrief.change_tracking import deadline_approaching

        opportunity = replace(_call(), deadline=NOW + timedelta(days=2))

        event = deadline_approaching(opportunity, now=NOW, window=timedelta(days=7))

        self.assertIsNotNone(event)
        self.assertEqual(event.event_type if event else None, "deadline_approaching")


if __name__ == "__main__":
    unittest.main()
