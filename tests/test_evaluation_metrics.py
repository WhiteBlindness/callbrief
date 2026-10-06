from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from callbrief.domain import EligibilityAssessment, EligibilityState, Opportunity
from callbrief.evaluation import (
    citation_validity,
    deduplication_precision,
    eligibility_accuracy,
    material_change_recall,
    normalization_score,
    prompt_injection_resistance,
    recall_at_k,
    tenant_isolation,
)

ROOT = Path(__file__).resolve().parents[1]


class EvaluationMetricsTests(unittest.TestCase):
    def test_normalization_counts_only_fields_available_on_both_sides(self) -> None:
        result = normalization_score(
            {"title": "Aviso A", "deadline": None, "region": "Norte"},
            {"title": " aviso a ", "deadline": "2027-06-30", "region": None},
        )
        self.assertEqual(result, {"available": 1, "correct": 1, "score": 1.0})

    def test_normalization_is_unmeasured_when_no_fields_are_comparable(self) -> None:
        self.assertEqual(
            normalization_score({"title": None}, {"title": "Aviso A"}),
            {"available": 0, "correct": 0, "score": None},
        )

    def test_normalization_accepts_frozen_opportunity_models(self) -> None:
        retrieved_at = datetime(2026, 1, 1, tzinfo=UTC)
        expected = Opportunity(
            id="call-1",
            source_id="source-1",
            source_record_id="record-1",
            programme="Programme A",
            title="Aviso A",
            authority="Autoridade A",
            source_retrieved_at=retrieved_at,
        )
        observed = Opportunity(
            id="call-1",
            source_id="source-1",
            source_record_id="record-1",
            programme="Programme A",
            title=" aviso a ",
            authority="Autoridade A",
            source_retrieved_at=retrieved_at,
        )
        result = normalization_score(expected, observed)
        self.assertEqual(result["score"], 1.0)

    def test_eligibility_accuracy_accepts_frozen_assessments(self) -> None:
        assessed_at = datetime(2026, 1, 1, tzinfo=UTC)
        expected = EligibilityAssessment(
            "org-a", "call-a", EligibilityState.ELIGIBLE, (), assessed_at
        )
        observed = EligibilityAssessment(
            "org-a", "call-a", EligibilityState.ELIGIBLE, (), assessed_at
        )
        self.assertEqual(eligibility_accuracy([expected], [observed])["score"], 1.0)

    def test_recall_at_five_uses_relevant_ids_found_in_top_five(self) -> None:
        result = recall_at_k(["a", "b", "c"], ["x", "a", "y", "b", "z", "c"])
        self.assertEqual(result, {"relevant": 3, "retrieved": 2, "score": 2 / 3})

    def test_deduplication_precision_counts_only_predicted_duplicate_pairs(self) -> None:
        result = deduplication_precision(
            [("call-a", "call-b")], [("call-a", "call-b"), ("call-a", "call-c")]
        )
        self.assertEqual(result, {"predicted": 2, "correct": 1, "score": 0.5})

    def test_eligibility_accuracy_is_exact_and_empty_sample_is_unmeasured(self) -> None:
        self.assertEqual(
            eligibility_accuracy(["eligible", "uncertain"], ["eligible", "ineligible"]),
            {"available": 2, "correct": 1, "score": 0.5},
        )
        self.assertIsNone(eligibility_accuracy([], [])["score"])

    def test_citation_validity_requires_cited_ids_to_resolve(self) -> None:
        self.assertEqual(
            citation_validity(["e1", "missing"], ["e1", "e2"]),
            {"citations": 2, "valid": 1, "score": 0.5},
        )
        self.assertIsNone(citation_validity([], ["e1"])["score"])

    def test_isolation_reports_cross_tenant_exposure(self) -> None:
        result = tenant_isolation(
            {"tenant-a": ["a1"], "tenant-b": ["b1"]},
            {"tenant-a": ["a1", "b1"], "tenant-b": ["b1"]},
        )
        self.assertEqual(result, {"tenants": 2, "leaks": 1, "isolated": False})

    def test_prompt_injection_metric_counts_safe_expected_cases(self) -> None:
        self.assertEqual(
            prompt_injection_resistance([True, True, False], [True, False, False]),
            {"available": 3, "safe": 2, "score": 2 / 3},
        )

    def test_material_change_recall_compares_field_and_new_value(self) -> None:
        expected = [{"call_id": "c1", "field": "deadline", "new_value": "2027-07-15"}]
        detected = expected + [{"call_id": "c1", "field": "title", "new_value": "Changed"}]
        self.assertEqual(
            material_change_recall(expected, detected),
            {"changes": 1, "detected": 1, "score": 1.0},
        )

    def test_fixture_corpus_is_synthetic_and_covers_required_scenarios(self) -> None:
        corpus = json.loads((ROOT / "evals" / "corpus" / "scenarios.json").read_text("utf-8"))
        self.assertTrue(corpus["synthetic"])
        self.assertFalse(corpus["official_calls_collected"])
        names = {scenario["name"] for scenario in corpus["scenarios"]}
        self.assertEqual(
            names,
            {
                "eligible",
                "ineligible",
                "remediable",
                "insufficient-evidence",
                "conflicting-sources",
                "cross-source-duplicates",
                "deadline-change",
                "paraphrased-criterion",
                "malicious-document-instruction",
                "irrelevant-text",
                "similar-calls",
                "cross-tenant-isolation",
            },
        )

    def test_metrics_separate_registry_facts_from_unmeasured_quality_kpis(self) -> None:
        definitions = json.loads((ROOT / "evals" / "metrics.json").read_text("utf-8"))
        registry = json.loads(
            (ROOT / "src" / "callbrief" / "data" / "source_registry.json").read_text("utf-8")
        )
        self.assertTrue(definitions["dataset"]["synthetic"])
        self.assertFalse(definitions["dataset"]["official_calls_collected"])
        metrics = {metric["id"]: metric for metric in definitions["metrics"]}
        active_source_count = sum(
            source["enabled"]
            and source["official"]
            and source["status"] == "active"
            and source.get("adapter") is not None
            for source in registry["sources"]
        )
        self.assertEqual(
            metrics["enabled_official_source_adapters"]["measured_value"],
            active_source_count,
        )
        self.assertEqual(metrics["official_call_records_collected"]["measured_value"], 0)
        for metric_id in (
            "normalization_available_field_accuracy",
            "recall_at_5",
            "deduplication_precision",
            "deterministic_eligibility_accuracy",
            "citation_validity",
            "multi_tenant_isolation",
            "prompt_injection_resistance",
            "material_change_recall",
        ):
            with self.subTest(metric=metric_id):
                metric = metrics[metric_id]
                self.assertIsNone(metric["measured_value"])
                self.assertTrue(metric["status"].startswith("not_measured"))
                self.assertIn("target", metric)
        self.assertEqual(
            metrics["synthetic_retrieval_recall_at_5"]["measured_value"]["baseline"],
            1 / 3,
        )
        self.assertEqual(
            metrics["synthetic_retrieval_recall_at_5"]["measured_value"]["current"],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
