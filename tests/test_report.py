from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from callbrief.models import Brief, Claim, FitBand, Requirement, RequirementStatus
from callbrief.report import ReportError, render_markdown, write_report


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
