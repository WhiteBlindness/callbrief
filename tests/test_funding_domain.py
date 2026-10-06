from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from callbrief.domain import (
    EligibilityRule,
    EligibilityState,
    EvidenceReference,
    FitSignals,
    Opportunity,
    OrganisationProfile,
    RuleOperator,
    SourceDocument,
)
from callbrief.eligibility import assess_eligibility
from callbrief.scoring import FitWeights, score_fit


NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def _evidence(evidence_id: str = "ev-source") -> EvidenceReference:
    excerpt = "Portuguese SMEs may apply."
    return EvidenceReference(
        evidence_id=evidence_id,
        source_id="official-source",
        url="https://example.gov/call/1",
        retrieved_at=NOW,
        section="Eligible applicants",
        start=0,
        end=len(excerpt),
        excerpt=excerpt,
        source_hash="a" * 64,
    )


def _opportunity(*rules: EligibilityRule) -> Opportunity:
    return Opportunity(
        id="call-1",
        source_id="official-source",
        source_record_id="record-1",
        programme="Fictional programme",
        call_id="CALL-1",
        title="Fictional call",
        authority="Public authority",
        canonical_url="https://example.gov/call/1",
        eligibility_rules=tuple(rules),
        evidence=(_evidence(),),
        source_retrieved_at=NOW,
    )


def _profile(**changes: object) -> OrganisationProfile:
    values: dict[str, object] = {
        "id": "org-a",
        "workspace_id": "workspace-1",
        "legal_name": "Example company",
        "country": "PT",
        "applicant_types": ("SME",),
        "financial_autonomy": Decimal("0.15"),
    }
    values.update(changes)
    return OrganisationProfile(**values)  # type: ignore[arg-type]


class FundingDomainTests(unittest.TestCase):
    def test_missing_opportunity_fields_remain_unknown(self) -> None:
        opportunity = _opportunity()

        self.assertIsNone(opportunity.deadline)
        self.assertIsNone(opportunity.budget_total)
        self.assertIsNone(opportunity.eligible_company_sizes)
        self.assertIsNone(opportunity.geography)

    def test_source_document_hashes_text_and_keeps_exact_span_metadata(self) -> None:
        document = SourceDocument(
            source_id="official-source",
            source_url="https://example.gov/call/1",
            retrieved_at=NOW,
            content_type="text/html",
            title="Fictional call",
            text="Eligible applicants: Portuguese SMEs may apply.",
        )
        evidence = document.evidence(0, len(document.text), section="Eligibility")

        self.assertEqual(evidence.excerpt, document.text)
        self.assertEqual(evidence.start, 0)
        self.assertEqual(evidence.end, len(document.text))
        self.assertEqual(len(evidence.source_hash), 64)

    def test_explicit_geography_mismatch_is_a_hard_ineligibility_gate(self) -> None:
        rule = EligibilityRule(
            rule_id="country",
            profile_field="country",
            operator=RuleOperator.IN,
            expected=("PT",),
            reason="Applicants must be established in Portugal.",
            evidence_ids=("ev-source",),
        )

        result = assess_eligibility(_opportunity(rule), _profile(country="ES"))

        self.assertEqual(result.state, EligibilityState.INELIGIBLE)
        self.assertEqual(result.findings[0].evidence_ids, ("ev-source",))
        self.assertIsNone(score_fit(result, FitSignals()).overall)

    def test_unknown_required_profile_fact_produces_uncertain_state(self) -> None:
        rule = EligibilityRule(
            rule_id="country",
            profile_field="country",
            operator=RuleOperator.IN,
            expected=("PT",),
            reason="Applicants must be established in Portugal.",
            evidence_ids=("ev-source",),
        )

        result = assess_eligibility(_opportunity(rule), _profile(country=None))

        self.assertEqual(result.state, EligibilityState.UNCERTAIN)
        self.assertIn("desconhecido", result.findings[0].reason.casefold())

    def test_failed_rule_with_documented_remediation_is_remediable(self) -> None:
        rule = EligibilityRule(
            rule_id="financial-autonomy",
            profile_field="financial_autonomy",
            operator=RuleOperator.MINIMUM,
            expected=Decimal("0.20"),
            reason="Financial autonomy must be at least 20%.",
            evidence_ids=("ev-source",),
            remediation_if_any="A capital increase may meet the threshold.",
        )

        result = assess_eligibility(_opportunity(rule), _profile())

        self.assertEqual(result.state, EligibilityState.REMEDIABLE)
        self.assertEqual(
            result.findings[0].remediation_if_any,
            "A capital increase may meet the threshold.",
        )

    def test_rule_without_valid_source_evidence_cannot_confirm_eligibility(self) -> None:
        rule = EligibilityRule(
            rule_id="country",
            profile_field="country",
            operator=RuleOperator.IN,
            expected=("PT",),
            reason="Applicants must be established in Portugal.",
            evidence_ids=("missing-evidence",),
        )

        result = assess_eligibility(_opportunity(rule), _profile())

        self.assertEqual(result.state, EligibilityState.UNCERTAIN)

    def test_present_rule_does_not_treat_an_unknown_profile_field_as_a_failure(self) -> None:
        rule = EligibilityRule(
            rule_id="certification",
            profile_field="certifications",
            operator=RuleOperator.PRESENT,
            expected=None,
            reason="A entidade deve ter uma certificação.",
            evidence_ids=("ev-source",),
        )

        result = assess_eligibility(_opportunity(rule), _profile(certifications=None))

        self.assertEqual(result.state, EligibilityState.UNCERTAIN)

    def test_eligibility_without_fit_signals_does_not_create_a_global_score(self) -> None:
        rule = EligibilityRule(
            rule_id="country",
            profile_field="country",
            operator=RuleOperator.IN,
            expected=("PT",),
            reason="A entidade deve estar estabelecida em Portugal.",
            evidence_ids=("ev-source",),
        )
        eligibility = assess_eligibility(_opportunity(rule), _profile())

        result = score_fit(eligibility, FitSignals(), assessed_at=NOW)

        self.assertEqual(eligibility.state, EligibilityState.ELIGIBLE)
        self.assertIsNone(result.overall)
        self.assertIn("ainda não há componentes", result.explanation)

    def test_zero_weight_signal_does_not_create_a_score_from_eligibility_alone(self) -> None:
        rule = EligibilityRule(
            rule_id="country",
            profile_field="country",
            operator=RuleOperator.IN,
            expected=("PT",),
            reason="A entidade deve estar estabelecida em Portugal.",
            evidence_ids=("ev-source",),
        )
        eligibility = assess_eligibility(_opportunity(rule), _profile())
        weights = FitWeights(strategic_fit=0.0)

        result = score_fit(
            eligibility,
            FitSignals(strategic_fit=0),
            weights=weights,
            assessed_at=NOW,
        )

        self.assertEqual(eligibility.state, EligibilityState.ELIGIBLE)
        self.assertIsNone(result.overall)

    def test_fit_uses_configured_weights_and_leaves_win_probability_unknown(self) -> None:
        eligibility = assess_eligibility(
            _opportunity(
                EligibilityRule(
                    rule_id="country",
                    profile_field="country",
                    operator=RuleOperator.IN,
                    expected=("PT",),
                    reason="Applicants must be established in Portugal.",
                    evidence_ids=("ev-source",),
                )
            ),
            _profile(),
        )
        signals = FitSignals(strategic_fit=80, evidence_completeness=50)
        weights = FitWeights(
            eligibility=0.25,
            strategic_fit=0.5,
            probability_of_winning=0.25,
            evidence_completeness=0.0,
        )

        result = score_fit(eligibility, signals, weights=weights)

        self.assertEqual(result.overall, 87)
        probability = next(
            item for item in result.components if item.name == "probability_of_winning"
        )
        self.assertIsNone(probability.score)
        self.assertIn("não existe", probability.explanation.casefold())

    def test_profiles_carry_distinct_workspace_and_organisation_ids(self) -> None:
        profile_a = _profile(id="client-a", legal_name="Client A")
        profile_b = _profile(id="client-b", legal_name="Client B")

        self.assertNotEqual(profile_a.id, profile_b.id)
        self.assertEqual(profile_a.workspace_id, profile_b.workspace_id)
        self.assertEqual(profile_a.legal_name, "Client A")
        self.assertEqual(profile_b.legal_name, "Client B")


if __name__ == "__main__":
    unittest.main()
