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
from evals.real_corpus_benchmark import run_benchmark as run_real_corpus_benchmark

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

    def test_metrics_report_manual_real_sample_and_keep_unmeasured_kpis_separate(self) -> None:
        definitions = json.loads((ROOT / "evals" / "metrics.json").read_text("utf-8"))
        registry = json.loads(
            (ROOT / "src" / "callbrief" / "data" / "source_registry.json").read_text("utf-8")
        )
        self.assertFalse(definitions["dataset"]["synthetic"])
        self.assertTrue(definitions["dataset"]["official_calls_collected"])
        self.assertEqual(definitions["dataset"]["sample_size"], 30)
        self.assertFalse(definitions["dataset"]["independent_human_adjudication"])
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
        self.assertEqual(metrics["official_call_records_collected"]["measured_value"], 30)
        for metric_id in (
            "deterministic_eligibility_accuracy",
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
            metrics["normalization_available_field_accuracy"]["measured_value"]["score"],
            1.0,
        )
        self.assertEqual(
            metrics["normalization_available_field_accuracy"]["measured_value"]["available"],
            165,
        )
        specific_normalization = metrics["portugal2030_eligibility_field_normalization"][
            "measured_value"
        ]
        self.assertEqual(specific_normalization["eligible_applicant_types"]["correct"], 2)
        self.assertEqual(specific_normalization["eligible_regions"]["correct"], 2)
        self.assertEqual(
            metrics["recall_at_5"]["measured_value"]["current_recall_at_5"],
            0.9117647058823529,
        )
        self.assertEqual(metrics["recall_at_5"]["measured_value"]["total_queries"], 59)
        self.assertEqual(metrics["deduplication_precision"]["measured_value"]["precision"], 1.0)
        self.assertEqual(metrics["deduplication_precision"]["measured_value"]["recall"], 1.0)
        self.assertEqual(
            metrics["citation_validity"]["measured_value"]["valid_exact_excerpt_spans"],
            165,
        )
        self.assertEqual(
            metrics["source_qualification_evidence"]["measured_value"]["preserved_on_opportunity"],
            1,
        )
        self.assertEqual(
            metrics["retrieval_no_answer_false_positive_rate"]["measured_value"][
                "current_no_answer_miss_rate"
            ],
            0.16666666666666666,
        )
        self.assertEqual(
            metrics["synthetic_retrieval_recall_at_5"]["measured_value"]["baseline"],
            1 / 3,
        )
        self.assertEqual(
            metrics["synthetic_retrieval_recall_at_5"]["measured_value"]["current"],
            1.0,
        )

    def test_manual_real_corpus_benchmark_reports_scope_and_offline_metrics(self) -> None:
        report = run_real_corpus_benchmark()

        self.assertEqual(report["unique_real_opportunities"], 30)
        self.assertEqual(
            report["status_counts"],
            {
                "open": 24,
                "upcoming": 1,
                "closed": 5,
            },
        )
        self.assertEqual(report["retrieval"]["queries_total"], 59)
        self.assertEqual(report["retrieval"]["development_queries"], 19)
        self.assertEqual(report["retrieval"]["no_answer_queries"], 6)
        self.assertEqual(report["retrieval"]["baseline"]["recall_at_5"], 0.9117647058823529)
        self.assertEqual(report["retrieval"]["current"]["recall_at_5"], 0.9117647058823529)
        self.assertEqual(report["retrieval"]["query_latency_ms"]["queries_measured"], 40)
        self.assertGreaterEqual(report["retrieval"]["query_latency_ms"]["current_mean"], 0.0)
        dataset = json.loads((ROOT / "evals" / "real_opportunities.json").read_text("utf-8"))
        categories = {item["category"] for item in dataset["queries"]}
        self.assertIn("funding size", categories)
        self.assertIn("deadline", categories)
        no_answer_queries = [item for item in dataset["queries"] if not item["relevant_ids"]]
        self.assertEqual(len(no_answer_queries), 12)
        self.assertTrue(all(item.get("no_answer_reason") for item in no_answer_queries))
        titles = [record["title"].casefold() for record in dataset["records"]]
        self.assertFalse(
            any(
                title in query["query"].casefold()
                for title in titles
                for query in dataset["queries"]
            )
        )
        self.assertEqual(report["retrieval"]["no_answer_queries"], 6)
        self.assertEqual(report["retrieval"]["abstention"]["no_answer_recall"], 5 / 6)
        self.assertEqual(report["normalization"]["available"], 165)
        self.assertEqual(report["normalization"]["correct"], 165)
        self.assertEqual(report["normalization"]["citation_structure"]["cited_facts"], 165)
        self.assertEqual(
            report["normalization"]["citation_structure"]["valid_exact_excerpt_spans"],
            165,
        )
        qualification_evidence = report["normalization"]["source_qualifications"]
        self.assertEqual(qualification_evidence["available"], 1)
        self.assertEqual(qualification_evidence["valid_exact_excerpt_spans"], 1)
        self.assertEqual(qualification_evidence["preserved_on_opportunity"], 1)
        qualification = qualification_evidence["claims"][0]
        self.assertEqual(qualification["record_id"], "Mpr-2026-6")
        self.assertEqual(
            qualification["source_url"],
            "https://compete2030.gov.pt/wp-content/uploads/2026/06/AVISOM3-1.pdf",
        )
        self.assertEqual(
            qualification["evidence_location"],
            "PDF da republicação de 30/09/2026, página 1, secção «Republicação»",
        )
        self.assertEqual(report["deduplication"]["labelled_pairs"], 16)
        self.assertEqual(report["deduplication"]["precision"], 1.0)
        self.assertEqual(report["deduplication"]["recall"], 1.0)
        self.assertEqual(report["deduplication"]["missed_duplicate_pairs"], 0)
        self.assertEqual(len(report["deduplication"]["original_five_pair_results"]), 5)
        annual_pair = next(
            item
            for item in report["deduplication"]["negative_pair_results"]
            if item["case"] == "recurring_annual_call_new_identifier"
        )
        self.assertTrue(annual_pair["fixture_only"])
        self.assertEqual(annual_pair["result"], "none")
        self.assertTrue(report["multi_client_demo"]["no_cross_workspace_result"])
        self.assertEqual(
            {item["eligibility"] for item in report["multi_client_demo"]["assessments"]},
            {"eligible", "ineligible", "uncertain"},
        )
        self.assertEqual(report["multi_client_demo"]["profiles"], 4)
        source_fields = report["multi_client_demo"]["source_specific_normalization"]["fields"]
        self.assertEqual(source_fields["eligible_applicant_types"]["correct"], 2)
        self.assertEqual(source_fields["eligible_regions"]["correct"], 2)
        self.assertEqual(source_fields["eligible_regions"]["valid_exact_excerpt_spans"], 2)
        self.assertFalse(report["multi_client_demo"]["remediable_example"]["supported_by_source"])
        self.assertEqual(report["network_requests"], 0)


if __name__ == "__main__":
    unittest.main()
