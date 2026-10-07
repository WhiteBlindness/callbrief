"""Evaluate the manually curated official-call sample without network access.

The reference labels are a single-pass manual transcription, not an independently
adjudicated human evaluation. The excerpts are short source quotations; raw page
responses and official-page payload hashes are not included.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from callbrief.corpus import Corpus
from callbrief.deduplication import DuplicateKind, find_duplicate
from callbrief.domain import (
    FitAssessment,
    Opportunity,
    OrganisationProfile,
    SourceDocument,
)
from callbrief.eligibility import assess_eligibility
from callbrief.evaluation import deduplication_precision, recall_at_k
from callbrief.normalization import normalize_source_document
from callbrief.storage import SqliteStore

from .retrieval_benchmark import _legacy_rank

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "real_opportunities.json"
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
    metadata: list[tuple[str, str]] = [
        ("record_id", source_record_id),
        ("title", record["title"]),
    ]
    if record.get("status_source"):
        metadata.append(("status", record["status_source"]))
    if record.get("deadline_source"):
        metadata.append(("deadline", record["deadline_source"]))
    if record["source_id"] != "cinea_life":
        metadata.append(("callIdentifier", record["id"]))
    else:
        metadata.append(("programme", record["programme"]))
    source_url = (
        record["listing_url"] if record["source_id"] == "cinea_life" else record["source_url"]
    )
    evidence_text = record["evidence_excerpt"]
    if record["source_id"] == "cinea_life" and "LIFE" not in evidence_text:
        evidence_text = f"LIFE calls for proposals 2026\n{evidence_text}"
    return SourceDocument(
        source_id=record["source_id"],
        source_url=source_url,
        retrieved_at=RETRIEVED_AT,
        content_type="text/plain; charset=utf-8",
        title=record["title"],
        text=evidence_text,
        metadata=tuple(metadata),
        discovered_links=(record["source_url"],) if record["source_id"] == "cinea_life" else (),
    )


def _expected_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _normalization_report(
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[Opportunity]]:
    fields = {
        name: {"available": 0, "correct": 0, "wrong": 0, "unknown": 0}
        for name in ("title", "status", "deadline")
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
        reference = record["reference_fields"]
        observed: dict[str, object] = {
            "title": opportunity.title,
            "status": opportunity.status.value,
            "deadline": opportunity.deadline.date().isoformat()
            if opportunity.deadline is not None
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
            elif field_name == "deadline" and isinstance(expected, str):
                if actual == _expected_date(expected):
                    counter["correct"] += 1
                else:
                    counter["wrong"] += 1
            elif actual == expected:
                counter["correct"] += 1
            else:
                counter["wrong"] += 1

            citation_count += 1
            citation = evidence_by_section.get(field_name)
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
            expected_value = (
                _expected_date(expected)
                if field_name == "deadline" and isinstance(expected, str)
                else expected
            )
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
            "citation_structure": {
                "cited_facts": citation_count,
                "valid_exact_excerpt_spans": citation_valid,
                "score": citation_valid / citation_count if citation_count else None,
                "official_payload_hashes_missing": missing_payload_hashes,
                "scope": (
                    "Stored short excerpts and official URLs; upstream response bodies "
                    "were not saved."
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
    baseline_recall: list[float] = []
    current_recall: list[float] = []
    baseline_reciprocal_rank: list[float] = []
    current_reciprocal_rank: list[float] = []
    baseline_false_positives = 0
    current_false_positives = 0
    no_answer_count = 0
    baseline_elapsed_ms: list[float] = []
    current_elapsed_ms: list[float] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for identifier, text in documents.items():
            (root / f"{identifier}.txt").write_text(text, encoding="utf-8")
        corpus = Corpus.load(root)
        for item in queries:
            baseline_started = time.perf_counter()
            baseline = _legacy_rank(item["query"], documents)[:5]
            baseline_elapsed_ms.append((time.perf_counter() - baseline_started) * 1000)
            current_started = time.perf_counter()
            current = tuple(
                Path(result.source).stem
                for result in corpus.search(item["query"], max_results=5)
            )
            current_elapsed_ms.append((time.perf_counter() - current_started) * 1000)
            relevant = item["relevant_ids"]
            if relevant:
                baseline_recall.append(float(recall_at_k(relevant, baseline, 5)["score"] or 0.0))
                current_recall.append(float(recall_at_k(relevant, current, 5)["score"] or 0.0))
                baseline_reciprocal_rank.append(_reciprocal_rank(relevant, baseline))
                current_reciprocal_rank.append(_reciprocal_rank(relevant, current))
            else:
                no_answer_count += 1
                baseline_false_positives += bool(baseline)
                current_false_positives += bool(current)
    return {
        "queries_total": len(queries),
        "answerable_queries": len(baseline_recall),
        "no_answer_queries": no_answer_count,
        "query_latency_ms": {
            "queries_measured": len(queries),
            "baseline_mean": round(_mean(baseline_elapsed_ms) or 0.0, 3),
            "baseline_total": round(sum(baseline_elapsed_ms), 3),
            "current_mean": round(_mean(current_elapsed_ms) or 0.0, 3),
            "current_total": round(sum(current_elapsed_ms), 3),
            "scope": "Single local run over 30 in-memory text records; excludes source I/O.",
        },
        "baseline": {
            "algorithm": "legacy term-only BM25",
            "recall_at_5": _mean(baseline_recall),
            "mrr_at_5": _mean(baseline_reciprocal_rank),
            "no_answer_false_positive_rate": _ratio(baseline_false_positives, no_answer_count),
        },
        "current": {
            "algorithm": "CallBrief normalized BM25",
            "recall_at_5": _mean(current_recall),
            "mrr_at_5": _mean(current_reciprocal_rank),
            "no_answer_false_positive_rate": _ratio(current_false_positives, no_answer_count),
        },
        "annotation_note": (
            "Single-pass manual relevance labels; no independent annotator adjudication."
        ),
    }


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
    )
    return normalize_source_document(document)


def _deduplication_report(
    records: list[dict[str, Any]], opportunities: list[Opportunity]
) -> dict[str, Any]:
    life_records = [record for record in records if record["source_id"] == "cinea_life"]
    normalized_by_dataset_id = {
        record["id"]: opportunity
        for record, opportunity in zip(records, opportunities, strict=True)
    }
    expected_pairs: list[tuple[str, str]] = []
    predicted_pairs: list[tuple[str, str]] = []
    for record in life_records:
        from_cinea = normalized_by_dataset_id[record["id"]]
        from_ft = _funding_tenders_mirror(record)
        prediction = find_duplicate(from_ft, (from_cinea,))
        pair = tuple(sorted((from_cinea.id, from_ft.id)))
        expected_pairs.append(pair)
        if prediction.kind is DuplicateKind.EXACT:
            predicted_pairs.append(pair)

    negative_ids = (
        ("LIFE-2026-SAP-ENV-ENVIRONMENT", "LIFE-2026-SAP-ENV-GOV"),
        ("LIFE-2026-SAP-ENV-GOV", "LIFE-2026-SAP-CLIMA-GOV"),
        ("LIFE-2026-SAP-NAT-GOV", "LIFE-2026-SAP-NAT-NATURE"),
        ("MPr-2026-8", "MPr-2026-5"),
        ("MPr-2026-4", "MPr-2026-7"),
        ("HORIZON-CL5-2026-10-D6-01", "HORIZON-CL5-2026-10-D6-03"),
        ("HORIZON-CL5-2026-10-D6-02", "HORIZON-CL5-2026-10-D6-03"),
        ("HORIZON-CL5-2026-10-D6-06", "HORIZON-CL5-2026-10-D6-10"),
        ("DIGITAL-ECCC-2027-DEPLOY-CYBER-11-AI4SME", "DIGITAL-ECCC-2027-DEPLOY-CYBER-11-CYBERAI"),
        ("DIGITAL-ECCC-2027-DEPLOY-CYBER-11-DUALUSE", "DIGITAL-ECCC-2027-DEPLOY-CYBER-11-EULEG"),
    )
    labelled_pairs = list(expected_pairs)
    false_positive_pairs: list[tuple[str, str]] = []
    for left_id, right_id in negative_ids:
        left = normalized_by_dataset_id[left_id]
        right = normalized_by_dataset_id[right_id]
        pair = tuple(sorted((left.id, right.id)))
        labelled_pairs.append(pair)
        if find_duplicate(left, (right,)).kind is DuplicateKind.EXACT:
            false_positive_pairs.append(pair)
            predicted_pairs.append(pair)
    metric = deduplication_precision(expected_pairs, predicted_pairs)
    return {
        "labelled_pairs": len(labelled_pairs),
        "positive_cross_source_pairs": len(expected_pairs),
        "negative_similar_call_pairs": len(negative_ids),
        "predicted_duplicate_pairs": metric["predicted"],
        "correct_duplicate_pairs": metric["correct"],
        "precision": metric["score"],
        "recall": metric["correct"] / len(expected_pairs) if expected_pairs else None,
        "missed_duplicate_pairs": len(expected_pairs) - metric["correct"],
        "false_positive_pairs": len(false_positive_pairs),
        "note": (
            "CINEA records and Funding & Tenders mirror URLs are normalized separately. "
            "The mirror URL exposes an official call identifier, but the CINEA listing "
            "does not; no identifier is copied between sources. Five known mirror pairs "
            "are missed. Precision is undefined because no duplicates were predicted."
        ),
    }


def _profile_demo(opportunity: Opportunity) -> dict[str, Any]:
    profiles = (
        OrganisationProfile(
            id="demo-manufacturing-sme",
            workspace_id="demo-consultancy",
            legal_name="Fábrica Atlântica, Lda. (fictícia)",
            country="PT",
            regions=("Norte",),
            company_size="sme",
            sectors=("manufacturing",),
            activities=("industrial innovation",),
        ),
        OrganisationProfile(
            id="demo-technology-startup",
            workspace_id="demo-consultancy",
            legal_name="Tecnologia Verde, Lda. (fictícia)",
            country="PT",
            regions=("Centro",),
            company_size="small",
            sectors=("information technology",),
            activities=("research and development",),
        ),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteStore(Path(directory) / "demo.sqlite3")
        store.create_workspace("demo-consultancy", "Consultoria de demonstração")
        store.save_opportunity(opportunity, checked_at=RETRIEVED_AT)
        assessments: list[dict[str, str]] = []
        for profile in profiles:
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
            assessments.append(
                {
                    "organisation_id": profile.id,
                    "opportunity_id": opportunity.id,
                    "eligibility": eligibility.state.value,
                    "stored_for_same_profile": stored is not None
                    and stored[0]["organisation_id"] == profile.id,
                }
            )
        no_cross_workspace_result = store.get_assessment(
            "not-the-demo-workspace", profiles[0].id, opportunity.id
        )
        store.close()
    return {
        "profiles": len(profiles),
        "canonical_opportunity_id": opportunity.id,
        "assessments": assessments,
        "no_cross_workspace_result": no_cross_workspace_result is None,
        "limitation": (
            "As regras de elegibilidade não constam da amostra; os dois resultados "
            "permanecem incertos."
        ),
    }


def run_benchmark() -> dict[str, Any]:
    started = time.perf_counter()
    dataset = _load_dataset()
    records = dataset["records"]
    queries = dataset["queries"]
    normalization, opportunities = _normalization_report(records)
    demo_record = next(record for record in records if record["id"] == "MPr-2026-7")
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
        "multi_client_demo": _profile_demo(normalized_by_id[demo_record["id"]]),
        "local_benchmark_runtime_ms": round((time.perf_counter() - started) * 1000, 2),
        "network_requests": 0,
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2))
