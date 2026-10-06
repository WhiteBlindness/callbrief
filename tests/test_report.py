from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from callbrief.models import Brief, Claim, FitBand, Requirement, RequirementStatus
from callbrief.report import (
    EvidenceDetail,
    ReportError,
    render_html,
    render_markdown,
    write_report,
)


class ReportTests(unittest.TestCase):
    def test_render_includes_citations_and_preliminary_notice(self) -> None:
        brief = Brief(
            title="Call title",
            summary=Claim("The call supports Portuguese SMEs.", ("evidence-1",)),
            fit_band=FitBand.POSSIBLE,
            fit_rationale=Claim("Applicant type matches.", ("evidence-1",)),
            requirements=(
                Requirement(
                    "Applicant location",
                    RequirementStatus.MEETS,
                    "The applicant is based in Portugal.",
                    ("evidence-1",),
                ),
            ),
            deadlines=(),
            risks=(),
            open_questions=("Confirm the matching funds.",),
            next_steps=("Ask the finance lead for the funding plan.",),
        )
        report = render_markdown(brief, {"evidence-1": ("call.md", "lines 2-4", "Portuguese SMEs")})

        self.assertIn("avaliação preliminar", report.casefold())
        self.assertIn("evidence-1", report)
        self.assertIn("call.md", report)
        self.assertIn("Confirm the matching funds.", report)

    def test_render_shows_exact_evidence_provenance(self) -> None:
        brief = Brief(
            title="Call title",
            summary=Claim("The call supports SMEs.", ("evidence-1",)),
            fit_band=FitBand.POSSIBLE,
            fit_rationale=Claim("Applicant type may fit.", ("evidence-1",)),
            requirements=(),
            deadlines=(),
            risks=(),
            open_questions=(),
            next_steps=(),
        )
        detail = EvidenceDetail(
            source="call.md",
            location="line 3",
            excerpt="SMEs may apply.",
            start=20,
            end=35,
            source_hash="a" * 64,
            retrieved_at="2026-10-06T12:00:00+00:00",
            section="Eligibility",
        )

        report = render_markdown(brief, {"evidence-1": detail})

        self.assertIn("Caracteres 20:35", report)
        self.assertIn("a" * 64, report)
        self.assertIn("consultado em 2026-10-06T12:00:00+00:00", report)

    def test_html_report_escapes_untrusted_content_and_has_no_script_execution(self) -> None:
        brief = Brief(
            title="<img src=x onerror=alert(1)>",
            summary=Claim("Texto <script>alert(1)</script>.", ("evidence-1",)),
            fit_band=FitBand.POSSIBLE,
            fit_rationale=Claim("Adequação incerta.", ("evidence-1",)),
            requirements=(),
            deadlines=(),
            risks=(),
            open_questions=(),
            next_steps=(),
        )

        report = render_html(
            brief, {"evidence-1": ("call.md", "linha 1", "<script>alert(1)</script>")}
        )

        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertIn("Content-Security-Policy", report)

    def test_does_not_overwrite_without_explicit_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "brief.md"
            path.write_text("keep me", encoding="utf-8")

            with self.assertRaises(ReportError):
                write_report(path, "replace me", force=False)

            self.assertEqual(path.read_text(encoding="utf-8"), "keep me")

    def test_writes_atomically_when_overwrite_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "brief.md"
            path.write_text("old", encoding="utf-8")

            write_report(path, "new", force=True)

            self.assertEqual(path.read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
