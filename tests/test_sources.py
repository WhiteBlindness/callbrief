from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from callbrief.deduplication import DuplicateKind, find_duplicate
from callbrief.domain import EvidenceProvenance, SourceDocument
from callbrief.normalization import normalize_source_document
from callbrief.sources import (
    AgentReachPage,
    AgentReachSourceAdapter,
    CineaLifeAdapter,
    FundingTendersAdapter,
    HttpBinaryResponse,
    HttpJsonResponse,
    HttpTextResponse,
    Portugal2030AnnualPlanAdapter,
    SourceAdapter,
    SourceError,
    TedSearchAdapter,
    create_adapter_registry,
    load_source_registry,
)

FIXTURE_RESPONSE: dict[str, object] = {
    "totalResults": 2,
    "results": [
        {
            "id": "HORIZON-CL4-2025-01",
            "content": {
                "title": "Digital industry call",
                "topicCode": "HORIZON-CL4-2025-01",
                "url": (
                    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
                    "opportunities/topic-details/example"
                ),
                "status": "Open",
            },
        },
        {
            "id": "DIGITAL-2025-02",
            "content": '{"title":"Digital Europe call","topicCode":"DIGITAL-2025-02"}',
        },
    ],
}


def _annual_plan_xlsx() -> bytes:
    values = [
        "ID",
        "Tipo Ent. Beneficiária",
        "Natureza Aviso",
        "Designacao do Aviso",
        "Programa",
        "Objetivo Específico",
        "Fundo",
        "Dotação Fundo",
        "Data Inicio Prevista",
        "Data Fim Prevista",
        "Quadrimestre",
        "NUTS II",
        "Modalidade Apresentação Candidatura",
        "2026-0001",
        "Privada | Pública",
        "Concurso",
        "Descarbonização de PME",
        "COMPETE 2030",
        "RSO2.1",
        "FEDER",
        "1 500 000 €",
        "15/10/2099",
        "30/11/2099",
        "Q4",
        "Norte",
        "Individual",
    ]
    strings = "".join(f"<si><t>{value}</t></si>" for value in values)
    rows = []
    index = 0
    for row_number, count in ((1, 13), (2, 13)):
        cells = []
        for column in range(1, count + 1):
            column_name = chr(ord("A") + column - 1)
            cells.append(f'<c r="{column_name}{row_number}" t="s"><v>{index}</v></c>')
            index += 1
        rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    payload = BytesIO()
    with ZipFile(payload, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{strings}</sst>",
        )
        workbook.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows)}</sheetData></worksheet>",
        )
    return payload.getvalue()


def _replace_xlsx_member(payload: bytes, name: str, old: bytes, new: bytes) -> bytes:
    rewritten = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(rewritten, "w", ZIP_DEFLATED) as target:
        for member in source.namelist():
            contents = source.read(member)
            if member == name:
                contents = contents.replace(old, new, 1)
            target.writestr(member, contents)
    return rewritten.getvalue()


class SourceAdapterTests(unittest.TestCase):
    def test_funding_tenders_adapter_normalizes_records_from_injected_transport(self) -> None:
        calls: list[tuple[str, dict[str, str], dict[str, object], float]] = []

        def transport(endpoint, params, payload, timeout):
            calls.append((endpoint, dict(params), dict(payload), timeout))
            return FIXTURE_RESPONSE

        adapter = FundingTendersAdapter(transport=transport)
        documents = adapter.fetch("digital", limit=2)

        self.assertEqual(len(documents), 2)
        first = documents[0]
        self.assertIsInstance(first, SourceDocument)
        self.assertEqual(first.source_id, "eu_funding_tenders")
        self.assertEqual(first.title, "Digital industry call")
        self.assertTrue(first.source_url.startswith("https://ec.europa.eu/"))
        self.assertEqual(first.discovered_links, (first.source_url,))
        self.assertIn('"status": "Open"', first.text)
        self.assertIs(first.retrieved_at.tzinfo, UTC)
        self.assertEqual(dict(first.metadata)["record_id"], "HORIZON-CL4-2025-01")
        self.assertEqual(documents[1].title, "Digital Europe call")
        self.assertEqual(
            calls,
            [
                (
                    FundingTendersAdapter.endpoint,
                    {
                        "apiKey": "SEDIA",
                        "text": "digital",
                        "pageSize": "2",
                        "pageNumber": "1",
                        "language": "en",
                    },
                    {
                        "bool": {
                            "must": [{"terms": {"status": ["31094501", "31094502", "31094503"]}}]
                        }
                    },
                    20,
                )
            ],
        )
        report = adapter.fetch_with_report("digital", limit=2)
        self.assertEqual(report.http_status, 200)
        self.assertEqual(report.total_results, 2)
        self.assertEqual(report.pagination_state, "complete")

    def test_live_funding_tenders_response_hash_reaches_field_evidence(self) -> None:
        raw_response = json.dumps(FIXTURE_RESPONSE, ensure_ascii=False).encode("utf-8")
        payload_hash = hashlib.sha256(raw_response).hexdigest()
        transport_response = HttpJsonResponse(
            FIXTURE_RESPONSE,
            200,
            len(raw_response),
            source_payload_sha256=payload_hash,
            provenance_status=EvidenceProvenance.LIVE_SOURCE_VERIFIED,
        )

        document = FundingTendersAdapter(transport=lambda *_: transport_response).fetch()[0]
        opportunity = normalize_source_document(document)
        evidence = next(item for item in opportunity.evidence if item.section == "title")

        self.assertEqual(document.provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED)
        self.assertEqual(document.source_payload_sha256, payload_hash)
        self.assertEqual(evidence.provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED)
        self.assertEqual(evidence.source_payload_sha256, payload_hash)
        self.assertIsNone(evidence.normalized_snapshot)

    def test_funding_tenders_reports_safe_schema_diagnostics_for_rejected_rows(self) -> None:
        response = {
            "totalResults": 1,
            "results": [
                {"recordIdentifier": "not-an-adapter-id", "content": {"titleText": "Fixture"}}
            ],
        }
        raw_response = json.dumps(response, ensure_ascii=False).encode("utf-8")
        payload_hash = hashlib.sha256(raw_response).hexdigest()
        transport_response = HttpJsonResponse(
            response,
            200,
            len(raw_response),
            last_modified="Tue, 06 Oct 2026 10:00:00 GMT",
            source_payload_sha256=payload_hash,
            provenance_status=EvidenceProvenance.LIVE_SOURCE_VERIFIED,
        )

        result = FundingTendersAdapter(transport=lambda *_: transport_response).fetch_with_report()

        self.assertEqual(result.total_results, 1)
        self.assertEqual(result.rejected_rows, 1)
        self.assertEqual(result.response_schema_fields, ("results", "totalResults"))
        self.assertIn(("results", "list"), result.response_field_types)
        self.assertIn(("totalResults", "int"), result.response_field_types)
        self.assertEqual(result.response_array_lengths, (("results", 1),))
        self.assertEqual(
            result.record_schema_fields,
            ("content", "recordIdentifier", "titleText"),
        )
        self.assertEqual(result.rejection_reasons, (("missing_stable_id", 1),))
        self.assertEqual(result.source_payload_sha256, payload_hash)
        self.assertEqual(result.source_timestamp, "Tue, 06 Oct 2026 10:00:00 GMT")
        self.assertEqual(
            result.response_provenance_status,
            EvidenceProvenance.LIVE_SOURCE_VERIFIED,
        )

    def test_adapter_rejects_invalid_query_and_limit_before_transport(self) -> None:
        def unexpected_transport(*_args):
            self.fail("must not make a request")

        adapter = FundingTendersAdapter(transport=unexpected_transport)
        with self.assertRaisesRegex(ValueError, "query"):
            adapter.fetch("x" * 501)
        with self.assertRaisesRegex(ValueError, "limit"):
            adapter.fetch(limit=True)
        with self.assertRaisesRegex(ValueError, "limit"):
            adapter.fetch(limit=101)

    def test_adapter_rejects_invalid_response_shape(self) -> None:
        for response, message in (
            ({"results": "invalid"}, "results list"),
            ({"unexpected_schema": []}, "recognized results list"),
            ({"results": {"unexpected_schema": []}}, "nested results wrapper"),
        ):
            with self.subTest(response=response):
                adapter = FundingTendersAdapter(transport=lambda *_, response=response: response)
                with self.assertRaisesRegex(SourceError, message):
                    adapter.fetch()

        empty_adapter = FundingTendersAdapter(transport=lambda *_: {"results": []})
        self.assertEqual(empty_adapter.fetch(), ())

    def test_registry_marks_unverified_sources_inactive(self) -> None:
        registry = {item.source_id: item for item in load_source_registry()}
        self.assertEqual(registry["eu_funding_tenders"].status, "active")
        self.assertEqual(registry["ted_eu_procurement"].status, "active")
        self.assertEqual(registry["cinea"].status, "active")
        self.assertEqual(registry["portugal2030_annual_plan"].status, "active")
        self.assertFalse(registry["eu_funding_tenders"].commercial_redistribution)
        self.assertTrue(registry["ted_eu_procurement"].commercial_redistribution)
        self.assertEqual(registry["cinea"].source_role, "discovery")
        self.assertEqual(registry["cinea"].canonical_source, "eu_funding_tenders")
        self.assertEqual(registry["eu_funding_tenders"].source_role, "canonical")
        self.assertEqual(
            registry["portugal2030_annual_plan"].legal_status,
            "terms_need_confirmation",
        )
        self.assertFalse(registry["portugal2030_annual_plan"].commercial_redistribution)
        self.assertEqual(registry["compete2030"].status, "pending_policy")
        self.assertEqual(registry["portugal2030"].status, "pending_policy")
        self.assertEqual(
            registry["portugal2030_annual_plan"].legal_status,
            "terms_need_confirmation",
        )
        self.assertEqual(registry["pt2030_open_dataset"].status, "pending_policy")
        self.assertFalse(registry["pt2030_open_dataset"].commercial_redistribution)
        self.assertEqual(registry["cordis"].status, "discovery_only")
        self.assertIn("autorização", registry["compete2030"].legal_notes.casefold())
        self.assertIn("licença", registry["pt2030_open_dataset"].legal_notes.casefold())
        self.assertGreaterEqual(len(registry), 20)

    def test_adapter_registry_only_constructs_verified_sources(self) -> None:
        adapter_map = create_adapter_registry(transport=lambda *_: FIXTURE_RESPONSE)
        self.assertEqual(
            set(adapter_map),
            {
                "eu_funding_tenders",
                "ted_eu_procurement",
                "cinea",
                "portugal2030_annual_plan",
            },
        )
        self.assertIsInstance(adapter_map["eu_funding_tenders"], SourceAdapter)
        self.assertIsInstance(adapter_map["ted_eu_procurement"], SourceAdapter)
        self.assertIsInstance(adapter_map["cinea"], SourceAdapter)
        self.assertIsInstance(adapter_map["portugal2030_annual_plan"], SourceAdapter)

    def test_portugal2030_annual_plan_maps_forecast_rows_with_cell_evidence(self) -> None:
        payload = _annual_plan_xlsx()
        adapter = Portugal2030AnnualPlanAdapter(
            transport=lambda *_: HttpBinaryResponse(
                payload, 200, len(payload), "Tue, 06 Oct 2026 10:00:00 GMT"
            )
        )

        result = adapter.fetch_with_report(limit=10)
        document = result.documents[0]
        opportunity = normalize_source_document(document)

        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.total_results, 1)
        self.assertEqual(result.rows_received, 1)
        self.assertEqual(result.rejected_rows, 0)
        self.assertEqual(opportunity.source_record_id, "2026-0001")
        self.assertEqual(opportunity.call_id, "2026-0001")
        self.assertEqual(opportunity.title, "Descarbonização de PME")
        self.assertEqual(opportunity.programme, "COMPETE 2030")
        self.assertEqual(opportunity.status.value, "upcoming")
        self.assertEqual(opportunity.deadline.isoformat(), "2099-11-30T00:00:00+00:00")
        self.assertEqual(str(opportunity.budget_total), "1500000")
        self.assertEqual(opportunity.eligible_regions, ("Norte",))
        self.assertEqual(opportunity.eligible_applicant_types, ("Privada", "Pública"))
        eligibility_rules = {item.profile_field: item for item in opportunity.eligibility_rules}
        self.assertIn("regions", eligibility_rules)
        self.assertIn("applicant_types", eligibility_rules)
        evidence_by_id = {item.evidence_id: item for item in opportunity.evidence}
        region_evidence = evidence_by_id[eligibility_rules["regions"].evidence_ids[0]]
        self.assertIn("NUTS II", region_evidence.section or "")
        self.assertIsNone(opportunity.canonical_url)
        self.assertIn(
            "https://portugal2030.pt/plano-anual-de-avisos/",
            document.discovered_links,
        )
        self.assertTrue(
            any("Descarbonização de PME" in item.excerpt for item in opportunity.evidence)
        )
        self.assertTrue(any("30/11/2099" in item.excerpt for item in opportunity.evidence))
        self.assertEqual(
            dict(document.metadata)["source_payload_sha256"],
            hashlib.sha256(payload).hexdigest(),
        )
        opportunity_evidence = next(
            item for item in opportunity.evidence if "Descarboniza" in item.excerpt
        )
        self.assertEqual(opportunity_evidence.provenance_status, "CAPTURED_FIXTURE")
        self.assertEqual(
            opportunity_evidence.source_payload_sha256,
            hashlib.sha256(payload).hexdigest(),
        )

    def test_portugal2030_annual_plan_rejects_malformed_workbook(self) -> None:
        adapter = Portugal2030AnnualPlanAdapter(
            transport=lambda *_: HttpBinaryResponse(b"not an xlsx", 200, 11)
        )
        with self.assertRaisesRegex(SourceError, "valid XLSX"):
            adapter.fetch()

    def test_portugal2030_annual_plan_rejects_negative_shared_string_indexes(self) -> None:
        payload = _replace_xlsx_member(
            _annual_plan_xlsx(),
            "xl/worksheets/sheet1.xml",
            b"<v>0</v>",
            b"<v>-1</v>",
        )
        adapter = Portugal2030AnnualPlanAdapter(
            transport=lambda *_: HttpBinaryResponse(payload, 200, len(payload))
        )
        with self.assertRaisesRegex(SourceError, "invalid shared string index"):
            adapter.fetch()

    def test_portugal2030_annual_plan_rejects_columns_past_xfd(self) -> None:
        from callbrief.sources import _xlsx_column_index

        self.assertEqual(_xlsx_column_index("XFD1"), 16383)
        with self.assertRaisesRegex(SourceError, "outside the XLSX limit"):
            _xlsx_column_index("XFE1")

    def test_portugal2030_annual_plan_maps_id_header_variants_without_common_canonical_url(
        self,
    ) -> None:
        payload = _replace_xlsx_member(
            _annual_plan_xlsx(),
            "xl/sharedStrings.xml",
            b"<si><t>ID</t></si>",
            b"<si><t>ID Aviso</t></si>",
        )
        adapter = Portugal2030AnnualPlanAdapter(
            transport=lambda *_: HttpBinaryResponse(payload, 200, len(payload))
        )
        document = adapter.fetch()[0]
        opportunity = normalize_source_document(document)

        self.assertEqual(opportunity.call_id, "2026-0001")
        self.assertIsNone(opportunity.canonical_url)
        self.assertEqual(document.discovered_links, (adapter.listing_url,))

    def test_portugal2030_annual_plan_normalizes_supported_title_and_programme_headers(
        self,
    ) -> None:
        payload = _annual_plan_xlsx()
        payload = _replace_xlsx_member(
            payload,
            "xl/sharedStrings.xml",
            b"<si><t>Designacao do Aviso</t></si>",
            b"<si><t>Designacao</t></si>",
        )
        payload = _replace_xlsx_member(
            payload,
            "xl/sharedStrings.xml",
            b"<si><t>Programa</t></si>",
            b"<si><t>Programa operacional</t></si>",
        )
        adapter = Portugal2030AnnualPlanAdapter(
            transport=lambda *_: HttpBinaryResponse(payload, 200, len(payload))
        )

        opportunity = normalize_source_document(adapter.fetch()[0])

        self.assertEqual(opportunity.title, "Descarbonização de PME")
        self.assertEqual(opportunity.programme, "COMPETE 2030")

    def test_portugal2030_annual_plan_never_confirms_open_status(self) -> None:
        record = {
            "ID": "2026-OLD",
            "Designacao do Aviso": "Aviso anterior",
            "Programa": "COMPETE 2030",
            "Data Inicio Prevista": "01/01/2020",
            "Data Fim Prevista": "30/11/2099",
            "status": "Open",
        }
        document = SourceDocument(
            source_id="portugal2030_annual_plan",
            source_url=Portugal2030AnnualPlanAdapter.endpoint,
            retrieved_at=datetime.now(UTC),
            content_type="application/json",
            title="Aviso anterior",
            text=json.dumps(record, ensure_ascii=False),
            discovered_links=(Portugal2030AnnualPlanAdapter.listing_url,),
        )
        opportunity = normalize_source_document(document)
        self.assertEqual(opportunity.status.value, "unknown")

    def test_portugal2030_annual_plan_evidence_ids_are_unique_per_row(self) -> None:
        opportunities = []
        for record_id in ("2026-0001", "2026-0002"):
            record = {
                "ID": record_id,
                "Designacao do Aviso": f"Aviso {record_id}",
                "Programa": "COMPETE 2030",
                "Data Inicio Prevista": "01/01/2099",
                "Data Fim Prevista": "30/11/2099",
            }
            document = SourceDocument(
                source_id="portugal2030_annual_plan",
                source_url=Portugal2030AnnualPlanAdapter.endpoint,
                retrieved_at=datetime.now(UTC),
                content_type="application/json",
                title=record["Designacao do Aviso"],
                text=json.dumps(record, ensure_ascii=False),
                metadata=(("record_id", record_id),),
                discovered_links=(Portugal2030AnnualPlanAdapter.listing_url,),
            )
            opportunities.append(normalize_source_document(document))

        evidence_ids = [
            item.evidence_id for opportunity in opportunities for item in opportunity.evidence
        ]
        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))

    def test_cinea_adapter_extracts_visible_deadline_evidence_and_reports_page_health(self) -> None:
        safe_link = (
            "https://eur03.safelinks.protection.outlook.com/?url="
            "https%3A%2F%2Fcinea.ec.europa.eu%2Ffunding-opportunities%2Fcalls-proposals%2Fwrapped_en"
        )
        html = f"""<!doctype html><html><body>
        <h1>LIFE Calls for proposals 2026</h1>
        <a href="/funding-opportunities/calls-proposals/life-2026-sap-env-gov_en">
          Standard Action Projects (SAPs) for Environmental Governance
        </a>
        <p>Deadline date: 22 September 2026</p>
        <a href="{safe_link}&amp;data=abc">
          Standard Action Projects (SAPs) Climate Governance and Information
        </a>
        <p>Deadline date: 22 September 2026</p>
        <a href="/funding-opportunities/calls-proposals/invalid-date_en">
          Standard Action Projects (SAPs) for a malformed deadline
        </a>
        <p>Deadline date: 22 September 2026</p>
        <a href="https://untrusted.example/call">
          Standard Action Projects (SAPs) for a rejected record
        </a>
        <p>Deadline date: 22 September 2026</p>
        </body></html>"""
        raw_response = html.encode("utf-8")

        def detail_page(reference: str, title: str) -> str:
            topic_slug = reference.casefold()
            return f"""<!doctype html><html><body>
            <h1>{title}</h1>
            <h2>Details</h2>
            <p>Status</p><p>Closed</p>
            <p>Reference</p><p>{reference}</p>
            <p>Publication date</p><p>21 April 2026</p>
            <p>Opening date</p><p>21 April 2026</p>
            <p>Deadline date</p><p>22 September 2026, 17:00 (CEST)</p>
            <p>Funding programme</p>
            <p>Programme for the Environment and Climate Action (LIFE) (2021/2027)</p>
            <p>Learn more and apply via the
              <a href="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{topic_slug}">
                Funding &amp; Tender opportunity portal
              </a>
            </p></body></html>"""

        invalid_date_url = (
            "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/invalid-date_en"
        )
        detail_pages = {
            "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/"
            "life-2026-sap-env-gov_en": detail_page(
                "LIFE-2026-SAP-ENV-GOV",
                "Standard Action Projects (SAPs) for Environmental Governance",
            ),
            "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/"
            "wrapped_en": detail_page(
                "LIFE-2026-SAP-CLIMA-GOV",
                "Climate Governance and Information",
            ),
            invalid_date_url: detail_page(
                "LIFE-2026-SAP-INVALID-DATE",
                "Standard Action Projects (SAPs) for a malformed deadline",
            ).replace("22 September 2026, 17:00 (CEST)", "31 February 2026, 17:00 (CEST)"),
        }

        def transport(endpoint: str, timeout: float) -> HttpTextResponse:
            body = html if endpoint == CineaLifeAdapter.endpoint else detail_pages[endpoint]
            encoded = body.encode("utf-8")
            return HttpTextResponse(
                body,
                200,
                len(encoded),
                "Tue, 06 Oct 2026 10:00:00 GMT",
                hashlib.sha256(encoded).hexdigest(),
                EvidenceProvenance.LIVE_SOURCE_VERIFIED,
            )

        adapter = CineaLifeAdapter(transport=transport)

        result = adapter.fetch_with_report(limit=10)

        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.rows_received, 4)
        self.assertEqual(result.rejected_rows, 2)
        self.assertEqual(
            result.rejection_reasons,
            (("invalid_detail_url", 1), ("unrecognized_detail_page", 1)),
        )
        self.assertEqual(
            result.response_bytes,
            len(raw_response) + sum(len(value.encode("utf-8")) for value in detail_pages.values()),
        )
        self.assertEqual(result.pagination_state, "complete")
        self.assertEqual(len(result.documents), 2)
        self.assertEqual(result.source_payload_sha256, hashlib.sha256(raw_response).hexdigest())
        self.assertEqual(result.source_timestamp, "Tue, 06 Oct 2026 10:00:00 GMT")
        self.assertEqual(
            result.response_provenance_status,
            EvidenceProvenance.LIVE_SOURCE_VERIFIED,
        )
        self.assertIn("22 September 2026", result.documents[0].text)
        self.assertTrue(dict(result.documents[0].metadata)["record_id"].startswith("CINEA-"))
        self.assertEqual(
            result.documents[0].provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED
        )
        expected_detail_hash = hashlib.sha256(
            detail_pages[result.documents[0].source_url].encode("utf-8")
        ).hexdigest()
        self.assertEqual(result.documents[0].source_payload_sha256, expected_detail_hash)
        self.assertEqual(dict(result.documents[0].metadata)["source_family"], "eu_direct_funding")
        self.assertEqual(
            dict(result.documents[0].metadata)["canonical_source"], "eu_funding_tenders"
        )
        deadline_evidence = result.documents[0].evidence(
            0, len(result.documents[0].text), section="deadline"
        )
        self.assertEqual(
            deadline_evidence.provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED
        )
        self.assertEqual(deadline_evidence.source_payload_sha256, expected_detail_hash)
        self.assertIsNone(deadline_evidence.normalized_snapshot)
        self.assertEqual(
            {document.source_url for document in result.documents},
            {
                "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/life-2026-sap-env-gov_en",
                "https://cinea.ec.europa.eu/funding-opportunities/calls-proposals/wrapped_en",
            },
        )
        normalized = normalize_source_document(result.documents[0])
        self.assertEqual(normalized.call_id, "LIFE-2026-SAP-ENV-GOV")
        self.assertEqual(normalized.topic_id, "LIFE-2026-SAP-ENV-GOV")
        self.assertEqual(normalized.programme, "LIFE")
        self.assertEqual(normalized.opening_date.date().isoformat(), "2026-04-21")
        self.assertEqual(normalized.deadline.date().isoformat(), "2026-09-22")
        self.assertEqual(
            normalized.canonical_url,
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
            "screen/opportunities/topic-details/life-2026-sap-env-gov",
        )
        climate_document = next(
            document
            for document in result.documents
            if dict(document.metadata)["reference"] == "LIFE-2026-SAP-CLIMA-GOV"
        )
        climate_opportunity = normalize_source_document(climate_document)
        self.assertEqual(climate_opportunity.title, "Climate Governance and Information")
        self.assertEqual(climate_opportunity.deadline.hour, 17)
        self.assertEqual(climate_opportunity.deadline.utcoffset().total_seconds(), 2 * 60 * 60)
        climate_source_record = json.loads(climate_document.text)
        self.assertEqual(climate_source_record["deadlineSource"], "22 September 2026, 17:00 (CEST)")

        query_result = adapter.fetch_with_report("LIFE 2026", limit=10)
        self.assertEqual(len(query_result.documents), 2)

    def test_five_cinea_topic_links_reconcile_with_api_shaped_fixtures(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        corpus = json.loads(
            (repository_root / "evals" / "real_opportunities.json").read_text(encoding="utf-8")
        )
        labels = json.loads(
            (repository_root / "evals" / "cross_source_deduplication_labels.json").read_text(
                encoding="utf-8"
            )
        )
        records = {record["id"]: record for record in corpus["records"]}
        detail_pages: dict[str, str] = {}
        listing_rows: list[str] = []
        api_rows: list[dict[str, str]] = []
        for label in labels["positive_pairs"]:
            record = records[label["cinea_record_id"]]
            detail_url = record["source_url"]
            canonical_url = label["canonical_topic_url"]
            call_id = label["topic_id"]
            title = label["title"]
            listing_title = label.get("listing_title", title)
            listing_rows.append(
                f'<a href="{detail_url}">{listing_title}</a><p>Deadline date: 22 September 2026</p>'
            )
            detail_pages[detail_url] = f"""<!doctype html><html><body>
            <h1>{title}</h1>
            <p>Status</p><p>Closed</p>
            <p>Reference</p><p>{call_id}</p>
            <p>Publication date</p><p>21 April 2026</p>
            <p>Opening date</p><p>21 April 2026</p>
            <p>Deadline date</p><p>22 September 2026, 17:00 (CEST)</p>
            <p>Funding programme</p>
            <p>Programme for the Environment and Climate Action (LIFE) (2021/2027)</p>
            <a href="{canonical_url}">Funding &amp; Tender opportunity portal</a>
            </body></html>"""
            api_rows.append(
                {
                    "id": call_id,
                    "topicCode": call_id,
                    "callIdentifier": call_id,
                    "programme": "LIFE (2021/2027)",
                    "title": title,
                    "status": "Closed",
                    "openingDate": label["opening_date"],
                    "deadlineDate": label.get("deadline_datetime", label["deadline"]),
                    "url": canonical_url,
                    "authority": "European Commission",
                }
            )
        listing_html = (
            "<!doctype html><html><body><h1>LIFE Calls for proposals 2026</h1>"
            + "".join(listing_rows)
            + "</body></html>"
        )

        def cinea_transport(endpoint: str, timeout: float) -> str:
            return listing_html if endpoint == CineaLifeAdapter.endpoint else detail_pages[endpoint]

        cinea_documents = CineaLifeAdapter(transport=cinea_transport).fetch(
            limit=len(labels["positive_pairs"])
        )
        funding_documents = FundingTendersAdapter(
            transport=lambda *args: {"total": len(api_rows), "results": api_rows}
        ).fetch(limit=len(api_rows))
        cinea_calls = {
            normalize_source_document(document).call_id: normalize_source_document(document)
            for document in cinea_documents
        }
        funding_calls = {
            normalize_source_document(document).call_id: normalize_source_document(document)
            for document in funding_documents
        }

        self.assertEqual(len(cinea_calls), 5)
        self.assertEqual(len(funding_calls), 5)
        for label in labels["positive_pairs"]:
            cinea = cinea_calls[label["topic_id"]]
            funding = funding_calls[label["topic_id"]]
            result = find_duplicate(cinea, (funding,))
            self.assertEqual(result.kind, DuplicateKind.EXACT)
            self.assertIn("canonical_url", result.matched_by)
            self.assertNotEqual(cinea.authority, funding.authority)
            self.assertEqual(cinea.opening_date, funding.opening_date)
            self.assertEqual(cinea.deadline, funding.deadline)

    def test_ted_adapter_maps_public_notices_and_reports_pagination(self) -> None:
        captured: list[tuple[str, dict[str, str], dict[str, object], float]] = []
        response = {
            "total": 15,
            "results": [
                {
                    "publication-number": "123456-2026",
                    "notice-title": "Digital services framework",
                    "publication-date": "20261006",
                    "deadline": "20261030",
                    "buyer-name": "European authority",
                    "buyer-country": "PT",
                }
            ],
        }

        def transport(endpoint, params, payload, timeout):
            captured.append((endpoint, dict(params), dict(payload), timeout))
            return response

        report = TedSearchAdapter(transport=transport).fetch_with_report(limit=10)
        self.assertEqual(report.http_status, 200)
        self.assertEqual(report.total_results, 15)
        self.assertEqual(report.pagination_state, "more_available")
        self.assertIn(("results", "list"), report.response_field_types)
        self.assertIn(("total", "int"), report.response_field_types)
        self.assertEqual(report.response_array_lengths, (("results", 1),))
        self.assertEqual(len(report.documents), 1)
        item = report.documents[0]
        self.assertEqual(item.source_id, "ted_eu_procurement")
        self.assertEqual(item.title, "Digital services framework")
        self.assertIn("123456-2026", item.source_url)
        self.assertIn('"publication-date": "20261006"', item.text)
        self.assertEqual(captured[0][0], TedSearchAdapter.endpoint)
        self.assertEqual(captured[0][1], {})
        self.assertRegex(captured[0][2]["query"], r"^publication-date >= \d{8}$")
        self.assertEqual(captured[0][2]["limit"], 10)
        self.assertIn("publication-number", captured[0][2]["fields"])

    def test_ted_reports_empty_response_shape_and_string_count_safely(self) -> None:
        response = {
            "iterationNextToken": None,
            "notices": [],
            "timedOut": False,
            "totalNoticeCount": "0",
        }

        report = TedSearchAdapter(transport=lambda *_: response).fetch_with_report()

        self.assertEqual(report.total_results, 0)
        self.assertEqual(report.rows_received, 0)
        self.assertEqual(report.response_array_lengths, (("notices", 0),))
        self.assertEqual(report.response_boolean_flags, (("timedOut", False),))
        self.assertIn(("totalNoticeCount", "str"), report.response_field_types)

    def test_ted_retains_only_a_bounded_normalized_snapshot_with_live_hash(self) -> None:
        response = {
            "total": 1,
            "results": [
                {
                    "publication-number": "123456-2026",
                    "notice-title": "Digital services framework",
                    "publication-date": "20261006",
                    "deadline": "20261030",
                }
            ],
        }
        raw_response = json.dumps(response, ensure_ascii=False).encode("utf-8")
        payload_hash = hashlib.sha256(raw_response).hexdigest()
        transport_response = HttpJsonResponse(
            response,
            200,
            len(raw_response),
            source_payload_sha256=payload_hash,
            provenance_status=EvidenceProvenance.LIVE_SOURCE_VERIFIED,
        )

        document = TedSearchAdapter(transport=lambda *_: transport_response).fetch()[0]
        opportunity = normalize_source_document(document)

        self.assertEqual(document.provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED)
        self.assertEqual(document.source_payload_sha256, payload_hash)
        self.assertIsNotNone(document.normalized_snapshot)
        self.assertLessEqual(len(document.normalized_snapshot.encode("utf-8")), 16 * 1024)
        self.assertTrue(opportunity.evidence)
        self.assertEqual(
            opportunity.evidence[0].provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED
        )
        self.assertEqual(opportunity.evidence[0].source_payload_sha256, payload_hash)
        self.assertEqual(opportunity.evidence[0].normalized_snapshot, document.normalized_snapshot)

    def test_agent_reach_adapter_maps_only_approved_official_pages(self) -> None:
        class Bridge:
            def fetch(self, query, *, limit):
                return (
                    AgentReachPage(
                        url="https://portugal2030.pt/avisos/1",
                        title="Aviso",
                        text="Metadados capturados",
                        links=(
                            "https://portugal2030.pt/avisos/2",
                            "https://untrusted.example/item",
                        ),
                    ),
                    AgentReachPage(
                        url="https://127.0.0.1/internal",
                        title="Internal",
                        text="Should be discarded",
                    ),
                )

        adapter = AgentReachSourceAdapter(Bridge(), allowed_hosts=("portugal2030.pt",))
        documents = adapter.fetch("avisos", limit=5)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].source_id, "agent_reach")
        self.assertEqual(documents[0].discovered_links, ("https://portugal2030.pt/avisos/2",))

    def test_agent_reach_fake_service_flows_into_callbrief_normalization(self) -> None:
        record = {
            "id": "PT2030-TEST-1",
            "title": "Apoio a empresas privadas",
            "programme": "COMPETE 2030",
            "status": "Open",
            "deadline": "2026-12-31",
        }
        source_payload_sha256 = hashlib.sha256(
            json.dumps(record, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        class Bridge:
            def fetch(self, query, *, limit):
                return (
                    AgentReachPage(
                        url="https://portugal2030.pt/avisos/1",
                        title=record["title"],
                        text=json.dumps(record, ensure_ascii=False),
                        content_type="application/json",
                        source_payload_sha256=source_payload_sha256,
                        provenance_status=EvidenceProvenance.LIVE_SOURCE_VERIFIED,
                    ),
                )

        documents = AgentReachSourceAdapter(Bridge(), allowed_hosts=("portugal2030.pt",)).fetch(
            "PT2030", limit=1
        )
        opportunity = normalize_source_document(documents[0])

        self.assertEqual(opportunity.source_record_id, "PT2030-TEST-1")
        self.assertEqual(opportunity.title, record["title"])
        self.assertEqual(opportunity.programme, record["programme"])
        self.assertEqual(opportunity.deadline.date().isoformat(), "2026-12-31")
        self.assertTrue(opportunity.evidence)
        self.assertEqual(
            opportunity.evidence[0].provenance_status, EvidenceProvenance.LIVE_SOURCE_VERIFIED
        )
        self.assertEqual(opportunity.evidence[0].source_payload_sha256, source_payload_sha256)

    def test_agent_reach_bridge_is_injected_without_scraping_implementation(self) -> None:
        class AgentReachBridge:
            source_id = "agent_reach"

            def fetch(self, query: str = "", *, limit: int = 50) -> tuple[SourceDocument, ...]:
                return ()

        adapters = create_adapter_registry(agent_reach_adapter=AgentReachBridge())
        self.assertIn("agent_reach", adapters)


if __name__ == "__main__":
    unittest.main()
