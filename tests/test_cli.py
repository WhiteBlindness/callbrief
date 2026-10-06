from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from callbrief.cli import main
from callbrief.domain import (
    EligibilityRule,
    Opportunity,
    OpportunityStatus,
    OpportunityType,
    OrganisationProfile,
    RuleOperator,
)
from callbrief.storage import SqliteStore


class CliTests(unittest.TestCase):
    def test_source_list_displays_active_and_pending_sources(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            code = main(["source", "list"])

        self.assertEqual(code, 0)
        self.assertIn("eu_funding_tenders\tativa", output.getvalue())
        self.assertIn("aguarda revisão", output.getvalue())
        self.assertIn("por verificar", output.getvalue())

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
