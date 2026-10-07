"""Evaluate the manually curated official-call sample without network access.

The reference labels are a single-pass manual transcription, not an independently
adjudicated human evaluation. The excerpts are short source quotations; raw page
responses and official-page payload hashes are not included.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from callbrief.corpus import Corpus, RetrievalStatus
from callbrief.deduplication import DuplicateKind, find_duplicate
from callbrief.domain import (
    EvidenceProvenance,
    FitAssessment,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
    SourceDocument,
)
from callbrief.eligibility import assess_eligibility
from callbrief.evaluation import deduplication_precision, recall_at_k
from callbrief.normalization import normalize_source_document
from callbrief.storage import SqliteStore
from callbrief.sources import load_source_registry

from .retrieval_benchmark import _legacy_rank

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "real_opportunities.json"
DEDUPLICATION_LABELS_PATH = ROOT / "evals" / "cross_source_deduplication_labels.json"
ELIGIBILITY_EXAMPLES_PATH = ROOT / "evals" / "portugal2030_eligibility_examples.json"
RETRIEVED_AT = datetime(2026, 10, 7, tzinfo=UTC)


def _load_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    if not isinstance(dataset, dict) or not isinstance(dataset.get("records"), list):
        raise ValueError("real opportunity dataset has an invalid shape")
    records = dataset["records"]
    identifiers = [item.get("id") for item in records if isinstance(item, dict)]
    if len(records) != 30 or len(set(identifiers)) != 30:
        raise ValueError("real opportunity dataset must contain 30 unique official records")
    return dataset


def _document(record: dict[str, Any]) -> SourceDocument:
    source_record_id = record["id"]
    if record["source_id"] == "cinea_life":
        source_record_id = (
            "CINEA-" + hashlib.sha256(record["source_url"].encode("utf-8")).hexdigest()[:24]
        )
    registry_source_id = "cinea" if record["source_id"] == "cinea_life" else record["source_id"]
    definition = next(item for item in load_source_registry() if item.source_id == registry_source_id)
    metadata = [("record_id", source_record_id)]
    metadata.extend(
        (name, value)
        for name, value in (
            ("source_family", definition.source_family),
            ("source_role", definition.source_role),
            ("canonical_source", definition.canonical_source),
            ("authority_relationship", definition.authority_relationship),
        )
        if value is not None
    )
    source_url = (
        record["listing_url"] if record["source_id"] == "cinea_life" else record["source_url"]
    )
    source_record: dict[str, Any] = {
        "record_id": source_record_id,
        "callIdentifier": record["id"],
        "programme": record["programme"],
        "title": record["title"],
        "authority": definition.authority,
        "_transcribed_excerpt": record["evidence_excerpt"],
    }
    if record.get("status_source"):
        source_record["status"] = record["status_source"]
    if record.get("opening_date"):
        source_record["openingDate"] = record["opening_date"]
    if record.get("deadline"):
        source_record["deadlineDate"] = record["deadline"]
    for claim in record.get("source_claims", []):
        if claim.get("field") == "funding_size":
            source_record["Dotação Global"] = claim["excerpt"].split("Dotação Global", 1)[-1].strip()
    if record["source_id"] == "cinea_life":
        source_record["topicCode"] = record["id"]
    discovery_links = (record["source_url"],)
    if record["source_id"] == "cinea_life":
        mirror_url = record.get("mirror_source_url")
        discovery_links = tuple(link for link in (mirror_url, record["source_url"]) if link)
    return SourceDocument(
        source_id=record["source_id"],
        source_url=source_url,
        retrieved_at=RETRIEVED_AT,
        content_type="application/json",
        title=record["title"],
        text=json.dumps(source_record, ensure_ascii=False, sort_keys=True, indent=2),
        metadata=tuple(metadata),
        discovered_links=discovery_links,
        provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
    )


def _expected_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _normalization_report(
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[Opportunity]]:
    measured_fields = (
        "call_id",
        "programme",
        "title",
        "status",
        "opening_date",
        "deadline",
        "budget_total",
    )
    unavailable_fields = (
        "authority",
        "opportunity_type",
        "geography",
        "beneficiary_type",
    )
    fields = {
        name: {"available": 0, "correct": 0, "wrong": 0, "unknown": 0}
        for name in (*measured_fields, *unavailable_fields)
    }
    opportunities: list[Opportunity] = []
    qualification_reports: list[dict[str, object]] = []
    citation_count = 0
    citation_valid = 0
    missing_payload_hashes = 0
    for record in records:
        document = _document(record)
        opportunity = normalize_source_document(document)
        qualification_evidence = []
        for claim in record.get("source_qualifications", []):
            if not isinstance(claim, dict):
                raise ValueError("source qualification must be an object")
            excerpt = claim.get("excerpt")
            source_url = claim.get("source_url")
            location = claim.get("evidence_location")
            field_name = claim.get("field")
            if not all(isinstance(value, str) and value.strip() for value in (
                excerpt, source_url, location, field_name
            )):
                raise ValueError("source qualification is missing its quote, URL, or location")
            qualification_document = SourceDocument(
                source_id=record["source_id"],
                source_url=source_url,
                retrieved_at=RETRIEVED_AT,
                content_type="application/pdf",
                title=record["title"],
                text=excerpt,
                provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
            )
            evidence = qualification_document.evidence(
                0, len(qualification_document.text), section=f"source qualification: {field_name}"
            )
            qualification_evidence.append(evidence)
            qualification_reports.append(
                {
                    "record_id": record["id"],
                    "field": field_name,
                    "value": claim.get("value"),
                    "excerpt": evidence.excerpt,
                    "source_url": evidence.url,
                    "evidence_location": location,
                    "evidence_id": evidence.evidence_id,
                    "valid_exact_excerpt_span": (
                        qualification_document.text[evidence.start : evidence.end]
                        == evidence.excerpt
                    ),
                    "source_payload_hash_available": False,
                }
            )
        if qualification_evidence:
            opportunity = replace(
                opportunity, evidence=(*opportunity.evidence, *qualification_evidence)
            )
            qualification_reports_start = len(qualification_reports) - len(
                qualification_evidence
            )
            for claim_report in qualification_reports[qualification_reports_start:]:
                claim_report["preserved_on_opportunity"] = any(
                    evidence.evidence_id == claim_report["evidence_id"]
                    for evidence in opportunity.evidence
                )
        opportunities.append(opportunity)
        reference = dict(record["reference_fields"])
        reference.setdefault("call_id", record["id"])
        reference.setdefault("programme", record["programme"])
        if record.get("opening_date"):
            reference.setdefault("opening_date", record["opening_date"])
        for claim in record.get("source_claims", []):
            if claim.get("field") == "funding_size":
                reference["budget_total"] = "182500000.00"
        observed: dict[str, object] = {
            "call_id": opportunity.call_id,
            "programme": opportunity.programme,
            "title": opportunity.title,
            "status": opportunity.status.value,
            "opening_date": opportunity.opening_date.date().isoformat()
            if opportunity.opening_date is not None
            else None,
            "deadline": opportunity.deadline.date().isoformat()
            if opportunity.deadline is not None
            else None,
            "budget_total": str(opportunity.budget_total)
            if opportunity.budget_total is not None
            else None,
        }
        evidence_by_section = {item.section: item for item in opportunity.evidence}
        for field_name, expected in reference.items():
            if field_name not in fields:
                continue
            counter = fields[field_name]
            counter["available"] += 1
            actual = observed[field_name]
            if actual is None:
                counter["unknown"] += 1
            elif field_name in {"opening_date", "deadline"} and isinstance(expected, str):
                if actual == _expected_date(expected):
                    counter["correct"] += 1
                else:
                    counter["wrong"] += 1
            elif actual == expected:
                counter["correct"] += 1
            else:
                counter["wrong"] += 1

            citation_count += 1
            citation_sections = {
                "call_id": ("callIdentifier", "topicCode"),
                "programme": ("programme",),
                "title": ("title",),
                "status": ("status",),
                "opening_date": ("openingDate",),
                "deadline": ("deadlineDate",),
                "budget_total": ("Dotação Global",),
            }.get(field_name, (field_name,))
            citation = next(
                (evidence_by_section.get(section) for section in citation_sections
                 if evidence_by_section.get(section) is not None),
                None,
            )
            span_valid = (
                citation is not None
                and document.text[citation.start : citation.end] == citation.excerpt
                and citation.url
                == (
                    record["listing_url"]
                    if record["source_id"] == "cinea_life"
                    else record["source_url"]
                )
                and citation.source_hash == document.content_hash
                and bool(citation.excerpt)
            )
            expected_value = expected
            if field_name in {"opening_date", "deadline"} and isinstance(expected, str):
                expected_value = _expected_date(expected)
            elif field_name == "budget_total":
                expected_value = str(Decimal(str(expected)))
            if span_valid and actual == expected_value:
                citation_valid += 1
        if record.get("source_payload_sha256") is None:
            missing_payload_hashes += 1

    available_fields = sum(item["available"] for item in fields.values())
    correct_fields = sum(item["correct"] for item in fields.values())
    wrong_fields = sum(item["wrong"] for item in fields.values())
    unknown_fields = sum(item["unknown"] for item in fields.values())
    return (
        {
            "fields": fields,
            "available": available_fields,
            "correct": correct_fields,
            "wrong": wrong_fields,
            "unknown": unknown_fields,
            "score": correct_fields / available_fields if available_fields else None,
            "labeling_note": "Single-pass manual transcription; no independent human adjudication.",
            "not_available_in_labelled_corpus": list(unavailable_fields),
            "citation_structure": {
                "cited_facts": citation_count,
                "valid_exact_excerpt_spans": citation_valid,
                "score": citation_valid / citation_count if citation_count else None,
                "official_payload_hashes_missing": missing_payload_hashes,
                "provenance_counts": {EvidenceProvenance.MANUALLY_TRANSCRIBED.value: citation_count},
                "scope": (
                    "Exact spans in manually transcribed structured records, with source URLs; "
                    "these spans do not prove that an original HTTP response was archived."
                ),
            },
            "source_qualifications": {
                "claims": qualification_reports,
                "available": len(qualification_reports),
                "valid_exact_excerpt_spans": sum(
                    item["valid_exact_excerpt_span"] is True
                    for item in qualification_reports
                ),
                "preserved_on_opportunity": sum(
                    item["preserved_on_opportunity"] is True
                    for item in qualification_reports
                ),
                "scope": (
                    "Exact spans and source links are preserved in the local sample and "
                    "derived opportunity evidence; the official PDF body and hash were not archived."
                ),
            },
        },
        opportunities,
    )


def _retrieval_report(
    records: list[dict[str, Any]], queries: list[dict[str, Any]]
) -> dict[str, Any]:
    documents = {
        record["id"]: "\n".join(
            (
                record["id"],
                record["title"],
                record["programme"],
                record["evidence_excerpt"],
            )
        )
        for record in records
    }
    development = [item for item in queries if item.get("partition") == "development"]
    holdout = [item for item in queries if item.get("partition", "holdout") == "holdout"]
    if not development or not holdout:
        raise ValueError("Retrieval benchmark needs separate development and holdout queries")
    if not any(item["relevant_ids"] for item in development) or not any(
        not item["relevant_ids"] for item in development
    ):
        raise ValueError("Development queries must include answerable and no-answer examples")
    baseline_elapsed_ms: list[float] = []
    current_elapsed_ms: list[float] = []
    abstention_elapsed_ms: list[float] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for identifier, text in documents.items():
            (root / f"{identifier}.txt").write_text(text, encoding="utf-8")
        corpus = Corpus.load(root)

        development_confidence = [
            (
                corpus.search_decision(
                    item["query"], min_confidence=0.0, min_term_coverage=0.0
                ).confidence,
                bool(item["relevant_ids"]),
            )
            for item in development
        ]
        abstention_threshold, calibration_score = _calibrate_abstention_threshold(
            development_confidence
        )

        baseline_recall: list[float] = []
        current_recall: list[float] = []
        baseline_reciprocal_rank: list[float] = []
        current_reciprocal_rank: list[float] = []
        baseline_no_answer_predictions = 0
        current_no_answer_predictions = 0
        baseline_no_answer_true_positives = 0
        current_no_answer_true_positives = 0
        no_answer_count = 0
        answerable_count = 0
        current_abstentions_on_answerable = 0
        current_supported_recall: list[float] = []
        current_supported_reciprocal_rank: list[float] = []
        for item in holdout:
            baseline_started = time.perf_counter()
            baseline = _legacy_rank(item["query"], documents)[:5]
            baseline_elapsed_ms.append((time.perf_counter() - baseline_started) * 1000)
            current_started = time.perf_counter()
            ranked_current = tuple(
                Path(result.source).stem for result in corpus.search(item["query"], max_results=5)
            )
            current_elapsed_ms.append((time.perf_counter() - current_started) * 1000)
            abstention_started = time.perf_counter()
            decision = corpus.search_decision(
                item["query"],
                max_results=5,
                min_confidence=abstention_threshold,
                min_term_coverage=0.0,
            )
            abstention_elapsed_ms.append((time.perf_counter() - abstention_started) * 1000)
            supported = tuple(Path(result.source).stem for result in decision.evidence)
            relevant = item["relevant_ids"]
            if relevant:
                answerable_count += 1
                baseline_recall.append(float(recall_at_k(relevant, baseline, 5)["score"] or 0.0))
                current_recall.append(
                    float(recall_at_k(relevant, ranked_current, 5)["score"] or 0.0)
                )
                baseline_reciprocal_rank.append(_reciprocal_rank(relevant, baseline))
                current_reciprocal_rank.append(_reciprocal_rank(relevant, ranked_current))
                current_supported_recall.append(
                    float(recall_at_k(relevant, supported, 5)["score"] or 0.0)
                )
                current_supported_reciprocal_rank.append(_reciprocal_rank(relevant, supported))
                current_abstentions_on_answerable += (
                    decision.status is RetrievalStatus.NO_SUPPORTED_RESULT
                )
            else:
                no_answer_count += 1
                baseline_abstained = not baseline
                current_abstained = decision.status is RetrievalStatus.NO_SUPPORTED_RESULT
                baseline_no_answer_true_positives += baseline_abstained
                current_no_answer_true_positives += current_abstained
            baseline_no_answer_predictions += not baseline
            current_no_answer_predictions += (
                decision.status is RetrievalStatus.NO_SUPPORTED_RESULT
            )

    baseline_no_answer_false_negatives = no_answer_count - baseline_no_answer_true_positives
    current_no_answer_false_negatives = no_answer_count - current_no_answer_true_positives
    return {
        "queries_total": len(queries),
        "development_queries": len(development),
        "development_answerable_queries": sum(bool(item["relevant_ids"]) for item in development),
        "development_no_answer_queries": sum(not item["relevant_ids"] for item in development),
        "holdout_queries": len(holdout),
        "answerable_queries": answerable_count,
        "no_answer_queries": no_answer_count,
        "abstention_calibration": {
            "partition": "development",
            "queries": len(development),
            "method": "threshold maximizing balanced accuracy for answerable versus no-answer queries",
            "confidence_threshold": abstention_threshold,
            "development_balanced_accuracy": calibration_score,
            "runtime_threshold_rounding": "ceiling to six decimal places",
            "confidence_rule": "0.7 * top-passage query-term coverage + 0.3 * BM25 score strength",
        },
        "query_latency_ms": {
            "queries_measured": len(holdout),
            "baseline_mean": round(_mean(baseline_elapsed_ms) or 0.0, 3),
            "baseline_total": round(sum(baseline_elapsed_ms), 3),
            "current_mean": round(_mean(current_elapsed_ms) or 0.0, 3),
            "current_total": round(sum(current_elapsed_ms), 3),
            "abstention_mean": round(_mean(abstention_elapsed_ms) or 0.0, 3),
            "abstention_total": round(sum(abstention_elapsed_ms), 3),
            "scope": "Single local run over 30 in-memory text records; excludes source I/O.",
        },
        "baseline": {
            "algorithm": "legacy term-only BM25",
            "recall_at_5": _mean(baseline_recall),
            "mrr_at_5": _mean(baseline_reciprocal_rank),
            "no_answer_precision": _ratio(
                baseline_no_answer_true_positives, baseline_no_answer_predictions
            ),
            "no_answer_recall": _ratio(baseline_no_answer_true_positives, no_answer_count),
            "no_answer_miss_rate": _ratio(
                baseline_no_answer_false_negatives, no_answer_count
            ),
        },
        "current": {
            "algorithm": "CallBrief normalized BM25",
            "recall_at_5": _mean(current_recall),
            "mrr_at_5": _mean(current_reciprocal_rank),
        },
        "abstention": {
            "algorithm": "Deterministic confidence threshold calibrated on development queries",
            "no_answer_precision": _ratio(
                current_no_answer_true_positives, current_no_answer_predictions
            ),
            "no_answer_recall": _ratio(current_no_answer_true_positives, no_answer_count),
            "no_answer_miss_rate": _ratio(
                current_no_answer_false_negatives, no_answer_count
            ),
            "answerable_abstention_rate": _ratio(
                current_abstentions_on_answerable, answerable_count
            ),
            "supported_recall_at_5": _mean(current_supported_recall),
            "supported_mrr_at_5": _mean(current_supported_reciprocal_rank),
        },
        "annotation_note": (
            "Single-pass manual relevance labels; no independent annotator adjudication. "
            "Only the development partition selects the abstention threshold."
        ),
    }


def _calibrate_abstention_threshold(confidence_labels: list[tuple[float, bool]]) -> tuple[float, float]:
    """Choose a deterministic threshold from development labels only."""
    if not confidence_labels or any(not 0 <= score <= 1 for score, _ in confidence_labels):
        raise ValueError("Calibration needs finite confidence values between zero and one")
    no_answer_count = sum(not answerable for _, answerable in confidence_labels)
    answerable_count = len(confidence_labels) - no_answer_count
    if not no_answer_count or not answerable_count:
        raise ValueError("Calibration requires both answerable and no-answer queries")
    candidates = {0.0, 1.0}
    candidates.update(min(1.0, score + 1e-9) for score, _ in confidence_labels)
    ranked_candidates = []
    for threshold in sorted(candidates):
        true_no_answer = sum(
            score < threshold and not answerable for score, answerable in confidence_labels
        )
        correct_answerable = sum(
            score >= threshold and answerable for score, answerable in confidence_labels
        )
        balanced_accuracy = 0.5 * (
            true_no_answer / no_answer_count + correct_answerable / answerable_count
        )
        ranked_candidates.append((balanced_accuracy, -threshold, threshold))
    best_score, _, selected = max(ranked_candidates)
    runtime_threshold = math.ceil(selected * 1_000_000) / 1_000_000
    return runtime_threshold, best_score


def _reciprocal_rank(relevant: list[str], ranked: tuple[str, ...]) -> float:
    return next(
        (
            1.0 / (index + 1)
            for index, identifier in enumerate(ranked)
            if identifier in relevant
        ),
        0.0,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _funding_tenders_mirror(record: dict[str, Any]) -> Opportunity:
    mirror_url = record.get("mirror_source_url")
    if not isinstance(mirror_url, str):
        raise ValueError("CINEA record is missing its Funding & Tenders mirror URL")
    parsed_url = urlparse(mirror_url)
    call_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "ec.europa.eu"
        or not call_id.startswith("LIFE-")
    ):
        raise ValueError("CINEA record has an invalid Funding & Tenders mirror URL")
    document = SourceDocument(
        source_id="eu_funding_tenders",
        source_url=mirror_url,
        retrieved_at=RETRIEVED_AT,
        content_type="text/uri-list",
        title=call_id,
        text=mirror_url,
        metadata=(("record_id", call_id), ("callIdentifier", call_id), ("programme", "LIFE")),
        provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
    )
    return normalize_source_document(document)


def _fixture_opportunity(record: dict[str, Any]) -> Opportunity:
    deadline_value = record.get("deadline")
    deadline = (
        datetime.fromisoformat(deadline_value).replace(tzinfo=UTC)
        if isinstance(deadline_value, str)
        else None
    )
    return Opportunity(
        id=record["id"],
        source_id=record["source_id"],
        source_record_id=record["id"],
        programme=record.get("programme"),
        call_id=record.get("call_id"),
        title=record.get("title"),
        authority=record.get("authority"),
        status=OpportunityStatus.UNKNOWN,
        opportunity_type=OpportunityType.UNKNOWN,
        deadline=deadline,
        source_retrieved_at=RETRIEVED_AT,
    )


def _deduplication_report(
    records: list[dict[str, Any]], opportunities: list[Opportunity]
) -> dict[str, Any]:
    labels = json.loads(DEDUPLICATION_LABELS_PATH.read_text(encoding="utf-8"))
    normalized_by_dataset_id = {
        record["id"]: opportunity
        for record, opportunity in zip(records, opportunities, strict=True)
    }
    expected_pairs: list[tuple[str, str]] = []
    predicted_pairs: list[tuple[str, str]] = []
    original_pair_results: list[dict[str, Any]] = []
    probable_review_candidates = 0
    possible_review_candidates = 0
    for label in labels["positive_pairs"]:
        record = next(
            item for item in records if item["id"] == label["cinea_record_id"]
        )
        from_cinea = normalized_by_dataset_id[label["cinea_record_id"]]
        from_ft = _funding_tenders_mirror(record)
        prediction = find_duplicate(from_ft, (from_cinea,))
        pair = tuple(sorted((from_cinea.id, from_ft.id)))
        expected_pairs.append(pair)
        if prediction.kind is DuplicateKind.EXACT:
            predicted_pairs.append(pair)
        elif prediction.kind is DuplicateKind.PROBABLE:
            probable_review_candidates += 1
        elif prediction.kind is DuplicateKind.POSSIBLE:
            possible_review_candidates += 1
        original_pair_results.append(
            {
                "topic_id": label["topic_id"],
                "programme_id": label["programme_id"],
                "cinea_title": label["title"],
                "cinea_source_url": record["source_url"],
                "funding_tenders_topic_url": label["canonical_topic_url"],
                "opening_date": label["opening_date"],
                "deadline": label["deadline"],
                "action": label["action"],
                "cinea_authority": label["cinea_authority"],
                "canonical_authority": label["canonical_authority"],
                "authority_relationship": "agency discovery presentation to canonical programme record",
                "result": prediction.kind.value,
                "matched_by": list(prediction.matched_by),
                "detected": prediction.kind is DuplicateKind.EXACT,
            }
        )

    labelled_pairs = list(expected_pairs)
    false_positive_pairs: list[tuple[str, str]] = []
    negative_results: list[dict[str, Any]] = []
    for negative in labels["negative_pairs"]:
        left_id, right_id = negative["left_id"], negative["right_id"]
        left = normalized_by_dataset_id[left_id]
        right = normalized_by_dataset_id[right_id]
        pair = tuple(sorted((left.id, right.id)))
        labelled_pairs.append(pair)
        prediction = find_duplicate(left, (right,))
        if prediction.kind is DuplicateKind.EXACT:
            false_positive_pairs.append(pair)
            predicted_pairs.append(pair)
        elif prediction.kind is DuplicateKind.PROBABLE:
            probable_review_candidates += 1
        elif prediction.kind is DuplicateKind.POSSIBLE:
            possible_review_candidates += 1
        negative_results.append(
            {
                "left_id": left_id,
                "right_id": right_id,
                "case": negative["case"],
                "result": prediction.kind.value,
                "matched_by": list(prediction.matched_by),
            }
        )
    for negative in labels.get("fixture_negative_pairs", []):
        left = _fixture_opportunity(negative["left"])
        right = _fixture_opportunity(negative["right"])
        pair = tuple(sorted((left.id, right.id)))
        labelled_pairs.append(pair)
        prediction = find_duplicate(left, (right,))
        if prediction.kind is DuplicateKind.EXACT:
            false_positive_pairs.append(pair)
            predicted_pairs.append(pair)
        elif prediction.kind is DuplicateKind.PROBABLE:
            probable_review_candidates += 1
        elif prediction.kind is DuplicateKind.POSSIBLE:
            possible_review_candidates += 1
        negative_results.append(
            {
                "left_id": left.id,
                "right_id": right.id,
                "case": negative["case"],
                "fixture_only": True,
                "result": prediction.kind.value,
                "matched_by": list(prediction.matched_by),
            }
        )
    metric = deduplication_precision(expected_pairs, predicted_pairs)
    return {
        "labelled_pairs": len(labelled_pairs),
        "positive_cross_source_pairs": len(expected_pairs),
        "negative_similar_call_pairs": len(labels["negative_pairs"]),
        "fixture_negative_pairs": len(labels.get("fixture_negative_pairs", [])),
        "predicted_duplicate_pairs": metric["predicted"],
        "correct_duplicate_pairs": metric["correct"],
        "precision": metric["score"],
        "recall": metric["correct"] / len(expected_pairs) if expected_pairs else None,
        "missed_duplicate_pairs": len(expected_pairs) - metric["correct"],
        "false_positive_pairs": len(false_positive_pairs),
        "probable_review_candidates": probable_review_candidates,
        "possible_review_candidates": possible_review_candidates,
        "automatic_merge_policy": "EXACT only; PROBABLE and POSSIBLE remain review candidates",
        "source_family_model": {
            "family": "eu_direct_funding",
            "canonical_source": "eu_funding_tenders",
            "discovery_source": "cinea_life",
            "authority_equality_required": False,
        },
        "original_five_pair_results": original_pair_results,
        "negative_pair_results": negative_results,
        "note": (
            "The CINEA page presents each LIFE topic as an agency discovery record and links "
            "to the canonical Funding & Tenders topic. Equal official topic identifiers and "
            "programme values provide exact evidence even when the source authorities differ. "
            "Title similarity alone never triggers an automatic merge."
            " One recurring-annual-call negative is a controlled fixture, outside the 30-record sample."
        ),
    }


def _profile_demo() -> dict[str, Any]:
    examples = json.loads(ELIGIBILITY_EXAMPLES_PATH.read_text(encoding="utf-8"))
    registry_source_id = examples["source_id"]
    definition = next(
        item for item in load_source_registry() if item.source_id == registry_source_id
    )
    normalized_rows: list[tuple[dict[str, Any], Opportunity]] = []
    source_field_quality = {
        field: {"available": 0, "correct": 0, "wrong": 0, "valid_exact_excerpt_spans": 0}
        for field in ("eligible_applicant_types", "eligible_regions")
    }
    page_context = examples["source_page_context"]
    for row in examples["records"]:
        record = row["fields"]
        context_document = SourceDocument(
            source_id=registry_source_id,
            source_url=examples["source_page"],
            retrieved_at=RETRIEVED_AT,
            content_type="text/html",
            title="Portugal 2030 Annual Plan eligibility filters",
            text=page_context["excerpt"],
            provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
        )
        context_evidence = context_document.evidence(
            0,
            len(context_document.text),
            section=f"{page_context['section']}: {row['case_id']}",
        )
        document = SourceDocument(
            source_id=registry_source_id,
            source_url=examples["source_file"],
            retrieved_at=RETRIEVED_AT,
            content_type="application/json",
            title=record["Designação do aviso"],
            text=json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2),
            metadata=(
                ("record_id", row["case_id"]),
                ("source_family", definition.source_family or ""),
                ("source_role", definition.source_role or ""),
                ("canonical_source", definition.canonical_source or ""),
                ("authority_relationship", definition.authority_relationship or ""),
            ),
            discovered_links=(examples["source_page"],),
            provenance_status=EvidenceProvenance.MANUALLY_TRANSCRIBED,
        )
        normalized = normalize_source_document(document)
        opportunity = replace(
            normalized,
            eligibility_rules=tuple(
                replace(
                    rule,
                    evidence_ids=(*rule.evidence_ids, context_evidence.evidence_id),
                )
                for rule in normalized.eligibility_rules
            ),
            evidence=(*normalized.evidence, context_evidence),
        )
        normalized_rows.append((row, opportunity))
        for field, source_field in (
            ("eligible_applicant_types", "Tipo Ent. Beneficiária"),
            ("eligible_regions", "NUTS II"),
        ):
            expected = (record[source_field],)
            quality = source_field_quality[field]
            quality["available"] += 1
            if getattr(opportunity, field) == expected:
                quality["correct"] += 1
            else:
                quality["wrong"] += 1
            evidence = next(
                (
                    item
                    for item in opportunity.evidence
                    if item.section is not None and item.section.endswith(f": {source_field}")
                ),
                None,
            )
            if (
                evidence is not None
                and document.text[evidence.start : evidence.end] == evidence.excerpt
            ):
                quality["valid_exact_excerpt_spans"] += 1

    private_opportunity = normalized_rows[0][1]
    profiles = (
        OrganisationProfile(
            id="demo-client-private",
            workspace_id="demo-consultancy",
            legal_name="Empresa privada fictícia",
            country="PT",
            regions=("RAM",),
            applicant_types=("Privada",),
        ),
        OrganisationProfile(
            id="demo-client-public",
            workspace_id="demo-consultancy",
            legal_name="Entidade pública fictícia",
            country="PT",
            regions=("RAM",),
            applicant_types=("Pública",),
        ),
        OrganisationProfile(
            id="demo-client-outside-region",
            workspace_id="demo-consultancy",
            legal_name="Empresa privada fictícia fora da região indicada",
            country="PT",
            regions=("Alentejo",),
            applicant_types=("Privada",),
        ),
        OrganisationProfile(
            id="demo-client-missing-region",
            workspace_id="demo-consultancy",
            legal_name="Empresa privada fictícia sem região indicada",
            country="PT",
            applicant_types=("Privada",),
        ),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteStore(Path(directory) / "demo.sqlite3")
        store.create_workspace("demo-consultancy", "Consultoria de demonstração")
        store.save_opportunity(private_opportunity, checked_at=RETRIEVED_AT)
        public_opportunity = normalized_rows[1][1]
        store.save_opportunity(public_opportunity, checked_at=RETRIEVED_AT)
        assessments: list[dict[str, Any]] = []
        profile_opportunities = tuple((profile, private_opportunity) for profile in profiles)
        for profile, opportunity in profile_opportunities:
            store.save_organisation(profile)
            eligibility = assess_eligibility(
                opportunity, profile, assessed_at=RETRIEVED_AT
            )
            fit = FitAssessment(
                organisation_id=profile.id,
                opportunity_id=opportunity.id,
                overall=None,
                components=(),
                explanation="Sem sinais de adequação fornecidos para a demonstração.",
                assessed_at=RETRIEVED_AT,
            )
            store.save_assessment("demo-consultancy", profile.id, eligibility, fit)
            stored = store.get_assessment("demo-consultancy", profile.id, opportunity.id)
            cited_ids = {
                evidence_id
                for finding in eligibility.findings
                for evidence_id in finding.evidence_ids
            }
            assessments.append(
                {
                    "organisation_id": profile.id,
                    "opportunity_id": opportunity.id,
                    "opportunity_title": opportunity.title or "",
                    "eligibility": eligibility.state.value,
                    "stored_for_same_profile": stored is not None
                    and stored[0]["organisation_id"] == profile.id,
                    "source_evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "section": item.section,
                            "excerpt": item.excerpt,
                            "url": item.url,
                            "provenance_status": item.provenance_status.value,
                            "source_payload_sha256": item.source_payload_sha256,
                        }
                        for item in opportunity.evidence
                        if item.evidence_id in cited_ids
                    ],
                }
            )
        no_cross_workspace_result = store.get_assessment(
            "not-the-demo-workspace", profiles[0].id, opportunity.id
        )
        store.close()
    return {
        "profiles": len(profiles),
        "opportunities": [
            {
                "title": private_opportunity.title,
                "source_page": examples["source_page"],
                "source_file": examples["source_file"],
                "provenance": EvidenceProvenance.MANUALLY_TRANSCRIBED.value,
                "applicant_types": list(private_opportunity.eligible_applicant_types or ()),
                "territory_field": list(private_opportunity.eligible_regions or ()),
                "source_payload_sha256": private_opportunity.evidence[0].source_payload_sha256,
            },
            {
                "title": public_opportunity.title,
                "source_url": examples["source_file"],
                "provenance": EvidenceProvenance.MANUALLY_TRANSCRIBED.value,
                "applicant_types": list(public_opportunity.eligible_applicant_types or ()),
                "territory_field": list(public_opportunity.eligible_regions or ()),
                "source_payload_sha256": public_opportunity.evidence[0].source_payload_sha256,
            },
        ],
        "source_specific_normalization": {
            "source": "portugal2030_annual_plan",
            "provenance": EvidenceProvenance.MANUALLY_TRANSCRIBED.value,
            "source_page_context": examples["source_page"],
            "context_excerpt": context_evidence.excerpt,
            "fields": source_field_quality,
        },
        "assessments": assessments,
        "no_cross_workspace_result": no_cross_workspace_result is None,
        "remediable_example": {
            "supported_by_source": False,
            "reason": (
                "The annual-plan rows state beneficiary type and region, but provide no "
                "corrective action that would justify a remediable result."
            ),
        },
        "limitation": (
            "The annual-plan beneficiary-type and NUTS II fields support deterministic "
            "entity-type and region checks. These are forecasts, and the manually "
            "transcribed rows have no archived source-payload hash."
        ),
    }


def run_benchmark() -> dict[str, Any]:
    started = time.perf_counter()
    dataset = _load_dataset()
    records = dataset["records"]
    queries = dataset["queries"]
    normalization, opportunities = _normalization_report(records)
    normalized_by_id = {item.source_record_id: item for item in opportunities}
    return {
        "dataset_checked_on": dataset["dataset"]["checked_on"],
        "unique_real_opportunities": len(records),
        "status_counts": dict(Counter(record["status"] for record in records)),
        "source_counts": dict(Counter(record["source_id"] for record in records)),
        "manual_reference_labels": dataset["dataset"]["independent_human_adjudication"] is False,
        "retrieval": _retrieval_report(records, queries),
        "normalization": normalization,
        "deduplication": _deduplication_report(records, opportunities),
        "multi_client_demo": _profile_demo(),
        "local_benchmark_runtime_ms": round((time.perf_counter() - started) * 1000, 2),
        "network_requests": 0,
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2))
