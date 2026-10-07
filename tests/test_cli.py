from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from callbrief.cli import main
from callbrief.domain import (
    EligibilityRule,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
    RuleOperator,
    SourceDocument,
)
from callbrief.sources import SourceError, SourceFetchResult, load_source_registry
from callbrief.storage import SqliteStore


class CliTests(unittest.TestCase):
    def test_source_check_all_active_emits_compact_machine_readable_summary(self) -> None:
        definitions = tuple(
            item for item in load_source_registry() if item.enabled and item.status == "active"
        )

        class Adapter:
            def __init__(self, source_id: str) -> None:
                self.source_id = source_id

            def fetch_with_report(self, query: str = "", *, limit: int = 20):
                document = SourceDocument(
                    source_id=self.source_id,
                    source_url="https://example.org/call",
                    retrieved_at=datetime(2026, 10, 7, tzinfo=UTC),
                    content_type="application/json",
                    title="A funded project call",
                    text=json.dumps(
                        {
                            "id": f"{self.source_id}-one",
                            "title": "A funded project call",
                            "programme": "Example programme",
                            "status": "Open",
                        }
                    ),
                )
                return SourceFetchResult(
                    source_id=self.source_id,
                    documents=(document,),
                    http_status=200,
                    response_bytes=128,
                    total_results=1,
                    pagination_state="complete",
                    rows_received=1,
                )

        adapters = {item.source_id: Adapter(item.source_id) for item in definitions}
        output = io.StringIO()
        with (
            patch("callbrief.cli.load_source_registry", return_value=definitions),
            patch("callbrief.cli.create_adapter_registry", return_value=adapters),
            redirect_stdout(output),
        ):
            code = main(["source", "check", "--all-active", "--json"])

        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(report["sources"]), 4)
        self.assertTrue(all(item["http_success"] for item in report["sources"]))
        self.assertTrue(all(item["rows_accepted"] == 1 for item in report["sources"]))
        for item in report["sources"]:
            self.assertIn("source_id", item)
            self.assertIn("response_bytes", item)
            self.assertIn("pagination_state", item)
            self.assertIn("schema_status", item)
            self.assertIn("source_timestamp", item)
            self.assertIn("parser_result", item)
            self.assertGreaterEqual(item["elapsed_ms"], 0)
        self.assertNotIn("A funded project call", output.getvalue())

    def test_source_list_displays_active_and_pending_sources(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            code = main(["source", "list"])

        self.assertEqual(code, 0)
        self.assertIn("eu_funding_tenders\tativa", output.getvalue())
        self.assertIn("aguarda revisão", output.getvalue())
        self.assertIn("por verificar", output.getvalue())

    def test_source_check_reports_fetch_and_schema_metrics_without_saving(self) -> None:
        record = {
            "publication-number": "123456-2026",
            "notice-title": "Digital services framework",
            "publication-date": "20261005",
            "deadline": "20261030",
        }
        document = SourceDocument(
            source_id="ted_eu_procurement",
            source_url="https://ted.europa.eu/en/notice/123456-2026",
            retrieved_at=datetime(2026, 10, 6, tzinfo=UTC),
            content_type="application/json",
            title="Digital services framework",
            text=json.dumps(record, ensure_ascii=False),
        )

        class Adapter:
            def fetch_with_report(self, query: str = "", *, limit: int = 20) -> SourceFetchResult:
                return SourceFetchResult(
                    source_id="ted_eu_procurement",
                    documents=(document,),
                    http_status=200,
                    response_bytes=512,
                    total_results=1,
                    pagination_state="complete",
                    rows_received=1,
                )

        output = io.StringIO()
        with (
            patch(
                "callbrief.cli.create_adapter_registry",
                return_value={"ted_eu_procurement": Adapter()},
            ),
            redirect_stdout(output),
        ):
            code = main(["source", "check", "ted_eu_procurement", "--limit", "20"])

        self.assertEqual(code, 0)
        self.assertIn("HTTP: 200", output.getvalue())
        self.assertIn("recebidos 1; aceites 1; rejeitados 0", output.getvalue())
        self.assertIn("512 bytes", output.getvalue())
        self.assertIn("Intervalo de datas estruturadas", output.getvalue())
        self.assertIn("05/10/2026 a 30/10/2026", output.getvalue())
        self.assertIn("Paginação: complete", output.getvalue())

    def test_source_check_reports_unavailable_metadata_after_transport_failure(self) -> None:
        class Adapter:
            def fetch_with_report(self, query: str = "", *, limit: int = 20) -> SourceFetchResult:
                raise SourceError("Source API request failed: URLError")

        output = io.StringIO()
        with (
            patch(
                "callbrief.cli.create_adapter_registry",
                return_value={"eu_funding_tenders": Adapter()},
            ),
            redirect_stdout(output),
        ):
            code = main(["source", "check", "eu_funding_tenders"])

        self.assertEqual(code, 1)
        self.assertIn("HTTP: não disponível", output.getvalue())
        self.assertIn("Última atualização declarada pela fonte: desconhecida", output.getvalue())
        self.assertIn("Paginação: não determinada", output.getvalue())

    def test_organisation_add_and_show_stay_within_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "calls.sqlite3"
            common = ["--database", str(database)]
            output = io.StringIO()
            with redirect_stdout(output):
                added = main(
                    [
                        "organisation",
                        "add",
                        *common,
                        "--workspace-id",
                        "consultancy",
                        "--workspace-name",
                        "Consultoria",
                        "--id",
                        "client-a",
                        "--name",
                        "Cliente A",
                        "--country",
                        "PT",
                        "--preferred-type",
                        "grant",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                shown = main(
                    [
                        "organisation",
                        "show",
                        *common,
                        "--workspace-id",
                        "consultancy",
                        "--id",
                        "client-a",
                    ]
                )
            self.assertEqual(added, 0)
            self.assertEqual(shown, 0)
            self.assertIn('"legal_name": "Cliente A"', output.getvalue())
            self.assertIn('"workspace_id": "consultancy"', output.getvalue())

    def test_assess_opportunity_writes_html_report_and_persists_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "calls.sqlite3"
            output_file = Path(temporary) / "report.html"
            now = datetime(2026, 10, 6, tzinfo=UTC)
            with SqliteStore(database) as store:
                store.create_workspace("consultancy", "Consultoria")
                store.save_organisation(
                    OrganisationProfile(
                        id="client-a", workspace_id="consultancy", legal_name="Cliente A"
                    )
                )
                canonical = Opportunity(
                    id="call-a",
                    source_id="official-source",
                    source_record_id="record-a",
                    programme=None,
                    call_id=None,
                    title="Aviso de exemplo",
                    authority=None,
                    canonical_url=None,
                    status=OpportunityStatus.OPEN,
                    opportunity_type=OpportunityType.GRANT,
                    deadline=now + timedelta(days=20),
                    eligibility_rules=(
                        EligibilityRule(
                            rule_id="country",
                            profile_field="country",
                            operator=RuleOperator.IN,
                            expected=("PT",),
                            reason="A entidade deve estar estabelecida em Portugal.",
                            evidence_ids=("ev-canonical",),
                        ),
                    ),
                    source_retrieved_at=now,
                )
                store.save_opportunity(canonical)
                store.save_opportunity(
                    replace(
                        canonical,
                        id="call-a-variant",
                        source_id="second-official-source",
                        source_record_id="record-a-variant",
                        canonical_url="https://second.gov/calls/call-a",
                        deadline=now + timedelta(days=30),
                        eligible_company_sizes=("large",),
                        eligibility_rules=(
                            EligibilityRule(
                                rule_id="country",
                                profile_field="country",
                                operator=RuleOperator.IN,
                                expected=("PT",),
                                reason="A entidade deve estar estabelecida em Portugal.",
                                evidence_ids=("ev-variant",),
                            ),
                        ),
                        duplicate_of="call-a",
                    )
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "assess-opportunity",
                        "--database",
                        str(database),
                        "--workspace-id",
                        "consultancy",
                        "--organisation-id",
                        "client-a",
                        "--opportunity-id",
                        "call-a",
                        "--output",
                        str(output_file),
                        "--probability-of-winning",
                        "0",
                    ]
                )
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("Avaliação criada", stdout.getvalue())
            report = output_file.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", report)
            self.assertIn("elegibilidade está incerto", report)
            self.assertIn("Não há evidência de origem", report)
            self.assertIn("Outras fontes do mesmo aviso", report)
            self.assertIn("Divergência, canónico / variante: prazo", report)
            self.assertIn("Divergência, canónico / variante: dimensão da entidade", report)
            self.assertNotIn("Divergência, canónico / variante: regras de elegibilidade", report)
            with SqliteStore(database) as store:
                saved = store.get_assessment("consultancy", "client-a", "call-a")
                self.assertIsNotNone(saved)
                if saved is not None:
                    self.assertEqual(
                        next(
                            component
                            for component in saved[1]["components"]
                            if component["name"] == "probability_of_winning"
                        )["score"],
                        0,
                    )


if __name__ == "__main__":
    unittest.main()
