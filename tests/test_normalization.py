from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from callbrief.change_tracking import detect_changes
from callbrief.domain import (
    EligibilityState,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
    SourceDocument,
)
from callbrief.eligibility import assess_eligibility
from callbrief.normalization import normalize_source_document

NOW = datetime(2026, 10, 6, tzinfo=UTC)


class NormalizationTests(unittest.TestCase):
    def test_structured_applicant_type_rule_uses_the_source_field_evidence(self) -> None:
        record = {
            "id": "2026-0001",
            "title": "Support for private companies",
            "programme": "Regional programme",
            "tipo ent. beneficiária": ["Privada"],
        }
        document = SourceDocument(
            source_id="portugal2030_annual_plan",
            source_url="https://portugal2030.pt/plan.xlsx",
            retrieved_at=NOW,
            content_type="application/json",
            title=record["title"],
            text=json.dumps(record, ensure_ascii=False),
        )

        opportunity = normalize_source_document(document)
        private = OrganisationProfile(
            id="private", workspace_id="consultancy", applicant_types=("Privada",)
        )
        public = OrganisationProfile(
            id="public", workspace_id="consultancy", applicant_types=("Pública",)
        )

        self.assertEqual(opportunity.eligible_applicant_types, ("Privada",))
        self.assertEqual(len(opportunity.eligibility_rules), 1)
        rule = opportunity.eligibility_rules[0]
        self.assertTrue(rule.evidence_ids)
        self.assertEqual(
            assess_eligibility(opportunity, private, assessed_at=NOW).state,
            EligibilityState.ELIGIBLE,
        )
        self.assertEqual(
            assess_eligibility(opportunity, public, assessed_at=NOW).state,
            EligibilityState.INELIGIBLE,
        )

    def test_structured_portugal2030_region_rule_uses_the_source_field_evidence(self) -> None:
        record = {
            "id": "annual-plan-region-1",
            "title": "Apoio regional",
            "programme": "Portugal 2030",
            "NUTS II": ["RAM"],
        }
        text = json.dumps(record, ensure_ascii=False)
        document = SourceDocument(
            source_id="portugal2030_annual_plan",
            source_url="https://portugal2030.pt/plan.xlsx",
            retrieved_at=NOW,
            content_type="application/json",
            title=record["title"],
            text=text,
        )

        opportunity = normalize_source_document(document)
        in_region = OrganisationProfile(
            id="in-region", workspace_id="consultancy", regions=("RAM",)
        )
        outside_region = OrganisationProfile(
            id="outside-region", workspace_id="consultancy", regions=("Alentejo",)
        )

        self.assertEqual(opportunity.eligible_regions, ("RAM",))
        self.assertEqual(len(opportunity.eligibility_rules), 1)
        rule = opportunity.eligibility_rules[0]
        self.assertEqual(rule.profile_field, "regions")
        self.assertTrue(rule.evidence_ids)
        evidence = {item.evidence_id: item for item in opportunity.evidence}
        self.assertIn("NUTS II", evidence[rule.evidence_ids[0]].section or "")
        self.assertEqual(
            assess_eligibility(opportunity, in_region, assessed_at=NOW).state,
            EligibilityState.ELIGIBLE,
        )
        self.assertEqual(
            assess_eligibility(opportunity, outside_region, assessed_at=NOW).state,
            EligibilityState.INELIGIBLE,
        )

    def test_normalizes_supported_values_and_attaches_exact_source_evidence(self) -> None:
        record = {
            "id": "topic-2026-1",
            "topicCode": "DIGITAL-2026-01",
            "programmeName": "Digital Europe",
            "title": "Digital transition call",
            "authority": "European Commission",
            "status": "Open",
            "typeName": "Grant",
            "deadline": "2027-03-30",
            "budgetTotal": 1200000,
            "eligibleApplicantTypes": ["SME", "research organisation"],
            "url": "https://ec.europa.eu/funding/call-1",
        }
        text = json.dumps(record, ensure_ascii=False, indent=2)
        document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url=record["url"],
            retrieved_at=NOW,
            content_type="application/json",
            title=record["title"],
            text=text,
        )

        opportunity = normalize_source_document(document)

        self.assertEqual(opportunity.programme, "Digital Europe")
        self.assertEqual(opportunity.call_id, "DIGITAL-2026-01")
        self.assertEqual(opportunity.status, OpportunityStatus.OPEN)
        self.assertEqual(opportunity.opportunity_type, OpportunityType.GRANT)
        self.assertEqual(opportunity.budget_total, Decimal("1200000"))
        self.assertEqual(opportunity.eligible_applicant_types, ("SME", "research organisation"))
        self.assertEqual(opportunity.deadline, datetime(2027, 3, 30, tzinfo=UTC))
        self.assertTrue(opportunity.evidence)
        for reference in opportunity.evidence:
            self.assertEqual(text[reference.start : reference.end], reference.excerpt)
            self.assertEqual(reference.source_hash, document.content_hash)

    def test_unavailable_or_unrecognized_fields_remain_unknown(self) -> None:
        record = {"id": "topic-2026-2", "title": "Another call", "status": "published"}
        text = json.dumps(record, ensure_ascii=False)
        document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url="https://api.tech.ec.europa.eu/search-api/item",
            retrieved_at=NOW,
            content_type="application/json",
            title="Another call",
            text=text,
        )

        opportunity = normalize_source_document(document)

        self.assertIsNone(opportunity.programme)
        self.assertIsNone(opportunity.authority)
        self.assertIsNone(opportunity.deadline)
        self.assertIsNone(opportunity.canonical_url)
        self.assertEqual(opportunity.status, OpportunityStatus.UNKNOWN)
        self.assertEqual(opportunity.opportunity_type, OpportunityType.UNKNOWN)

    def test_status_metadata_without_an_exact_source_span_remains_unknown(self) -> None:
        document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url="https://example.eu/call",
            retrieved_at=NOW,
            content_type="text/plain",
            title="Call without a status label",
            text="The source page does not state a status.",
            metadata=(("status", "open"),),
        )

        opportunity = normalize_source_document(document)

        self.assertEqual(opportunity.status, OpportunityStatus.UNKNOWN)
        self.assertEqual(opportunity.evidence, ())

    def test_normalizes_ted_notice_dates_and_classifies_procurement_as_tender(self) -> None:
        record = {
            "publication-number": "123456-2026",
            "notice-title": "Digital services framework",
            "publication-date": "20261005",
            "deadline": "20261030",
            "buyer-name": "European authority",
        }
        text = json.dumps(record, ensure_ascii=False, indent=2)
        document = SourceDocument(
            source_id="ted_eu_procurement",
            source_url="https://ted.europa.eu/en/notice/123456-2026",
            retrieved_at=NOW,
            content_type="application/json",
            title="Digital services framework",
            text=text,
        )

        opportunity = normalize_source_document(document)

        self.assertEqual(opportunity.source_record_id, "123456-2026")
        self.assertEqual(opportunity.call_id, "123456-2026")
        self.assertEqual(opportunity.title, "Digital services framework")
        self.assertEqual(opportunity.authority, "European authority")
        self.assertEqual(opportunity.publication_date, datetime(2026, 10, 5, tzinfo=UTC))
        self.assertEqual(opportunity.deadline, datetime(2026, 10, 30, tzinfo=UTC))
        self.assertEqual(opportunity.status, OpportunityStatus.OPEN)
        self.assertEqual(opportunity.opportunity_type, OpportunityType.TENDER)
        for reference in opportunity.evidence:
            self.assertEqual(text[reference.start : reference.end], reference.excerpt)
            self.assertEqual(reference.source_hash, document.content_hash)

    def test_normalizes_cinea_listing_deadline_from_the_visible_source_excerpt(self) -> None:
        text = (
            "LIFE calls for proposals 2026\n"
            "Standard Action Projects (SAPs) for Environmental Governance\n"
            "Deadline date: 22 September 2026"
        )
        document = SourceDocument(
            source_id="cinea_life",
            source_url="https://cinea.ec.europa.eu/life-calls-proposals-2026_en",
            retrieved_at=NOW,
            content_type="text/html; charset=utf-8",
            title="Standard Action Projects (SAPs) for Environmental Governance",
            text=text,
            metadata=(
                ("record_id", "LIFE-2026-environmental-governance"),
                ("title", "Standard Action Projects (SAPs) for Environmental Governance"),
                ("programme", "LIFE"),
                ("deadline", "22 September 2026"),
                ("status", "open"),
            ),
            discovered_links=(
                "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/"
                "environmental-governance_en",
            ),
        )

        opportunity = normalize_source_document(document)

        self.assertEqual(
            opportunity.title,
            "Standard Action Projects (SAPs) for Environmental Governance",
        )
        self.assertEqual(opportunity.deadline, datetime(2026, 9, 22, tzinfo=UTC))
        self.assertEqual(opportunity.status, OpportunityStatus.CLOSED)
        self.assertEqual(opportunity.programme, "LIFE")
        self.assertIsNone(opportunity.call_id)
        self.assertEqual(
            opportunity.canonical_url,
            "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/"
            "environmental-governance_en",
        )
        title_evidence = next(item for item in opportunity.evidence if item.section == "title")
        deadline_evidence = next(
            item for item in opportunity.evidence if item.section == "deadline"
        )
        self.assertEqual(
            title_evidence.excerpt,
            "Standard Action Projects (SAPs) for Environmental Governance",
        )
        self.assertEqual(deadline_evidence.excerpt, "22 September 2026")
        self.assertEqual(
            document.text[deadline_evidence.start : deadline_evidence.end],
            deadline_evidence.excerpt,
        )

    def test_cinea_future_deadline_does_not_imply_open_status(self) -> None:
        text = "LIFE calls for proposals 2026\nDeadline date: 22 September 2027"
        document = SourceDocument(
            source_id="cinea_life",
            source_url="https://cinea.ec.europa.eu/life-calls-proposals-2026_en",
            retrieved_at=NOW,
            content_type="text/html; charset=utf-8",
            title="LIFE call",
            text=text,
            metadata=(("deadline", "22 September 2027"), ("status", "open")),
        )

        opportunity = normalize_source_document(document)

        self.assertEqual(opportunity.status, OpportunityStatus.UNKNOWN)

    def test_title_only_source_update_does_not_change_eligibility_semantics(self) -> None:
        base_record = {
            "id": "stable-call-id",
            "topicCode": "CALL-2026-8",
            "programmeName": "Programa de exemplo",
            "title": "Aviso inicial",
            "eligibilityRules": [
                {
                    "ruleId": "country",
                    "profileField": "country",
                    "operator": "in",
                    "expected": ["PT"],
                    "reason": "A entidade deve estar estabelecida em Portugal.",
                }
            ],
        }
        revised_record = {**base_record, "title": "Aviso inicial com título atualizado"}
        before_document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url="https://ec.europa.eu/funding/call-8",
            retrieved_at=NOW,
            content_type="application/json",
            title=base_record["title"],
            text=json.dumps(base_record, ensure_ascii=False),
        )
        after_document = SourceDocument(
            source_id=before_document.source_id,
            source_url=before_document.source_url,
            retrieved_at=NOW,
            content_type=before_document.content_type,
            title=revised_record["title"],
            text=json.dumps(revised_record, ensure_ascii=False),
        )
        before = normalize_source_document(before_document)
        after = normalize_source_document(after_document)

        changes = detect_changes(before, after, detected_at=NOW)

        self.assertNotEqual(before_document.content_hash, after_document.content_hash)
        self.assertEqual(
            before.eligibility_rules[0].evidence_ids, after.eligibility_rules[0].evidence_ids
        )
        self.assertEqual({item.field for item in changes}, {"title"})

    def test_normalizes_structured_rules_and_additional_domain_fields(self) -> None:
        record = {
            "id": "topic-2026-3",
            "title": "Aviso de investigação",
            "eligibilityRules": [
                {
                    "ruleId": "country",
                    "profileField": "country",
                    "operator": "in",
                    "expected": ["PT"],
                    "reason": "A entidade deve estar estabelecida em Portugal.",
                }
            ],
            "additionalDeadlines": ["2027-04-30"],
            "projectDuration": "12 a 24 meses",
            "consortiumRules": "Consórcio de duas entidades.",
            "trlMin": 3,
            "trlMax": 6,
            "sourceUpdatedAt": "2026-10-01T12:00:00Z",
            "aidIntensity": 50,
        }
        text = json.dumps(record, ensure_ascii=False, indent=2)
        document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url="https://ec.europa.eu/funding/call-3",
            retrieved_at=NOW,
            content_type="application/json",
            title=record["title"],
            text=text,
        )

        opportunity = normalize_source_document(document)
        profile = OrganisationProfile(id="org-pt", workspace_id="workspace", country="PT")
        assessment = assess_eligibility(opportunity, profile, assessed_at=NOW)

        self.assertEqual(opportunity.trl_min, 3)
        self.assertEqual(opportunity.trl_max, 6)
        self.assertEqual(opportunity.additional_deadlines, (datetime(2027, 4, 30, tzinfo=UTC),))
        self.assertEqual(opportunity.project_duration, "12 a 24 meses")
        self.assertEqual(opportunity.consortium_rules, "Consórcio de duas entidades.")
        self.assertEqual(opportunity.source_updated_at, datetime(2026, 10, 1, 12, tzinfo=UTC))
        self.assertIsNone(opportunity.aid_intensity)
        self.assertEqual(assessment.state, EligibilityState.ELIGIBLE)
        evidence_by_id = {item.evidence_id: item for item in opportunity.evidence}
        rule_evidence = evidence_by_id[opportunity.eligibility_rules[0].evidence_ids[0]]
        self.assertEqual(rule_evidence.section, "eligibilityRules")
        self.assertEqual(rule_evidence.source_hash, document.content_hash)
        self.assertEqual(text[rule_evidence.start : rule_evidence.end], rule_evidence.excerpt)

    def test_uninterpretable_hard_rule_stays_uncertain(self) -> None:
        record = {
            "id": "topic-2026-4",
            "title": "Aviso com regra não suportada",
            "eligibilityRules": [
                {
                    "ruleId": "assets",
                    "profileField": "asset_count",
                    "operator": "less-than-or-equal",
                    "expected": 2,
                    "reason": "A entidade não pode ter mais de dois ativos relevantes.",
                },
                {
                    "ruleId": "malformed",
                    "profileField": "country",
                    "operator": "in",
                    "expected": ["PT"],
                    "reason": "x" * 1001,
                },
            ],
        }
        document = SourceDocument(
            source_id="eu_funding_tenders",
            source_url="https://ec.europa.eu/funding/call-4",
            retrieved_at=NOW,
            content_type="application/json",
            title=record["title"],
            text=json.dumps(record, ensure_ascii=False),
        )
        opportunity = normalize_source_document(document)
        profile = OrganisationProfile(id="org-a", workspace_id="workspace", country="PT")

        assessment = assess_eligibility(opportunity, profile, assessed_at=NOW)

        self.assertEqual(assessment.state, EligibilityState.UNCERTAIN)
        self.assertEqual(len(assessment.findings), 2)


if __name__ == "__main__":
    unittest.main()
