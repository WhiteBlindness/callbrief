from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from callbrief.corpus import Corpus, CorpusError


class CorpusTests(unittest.TestCase):
    def test_loads_supported_files_and_excludes_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "# Applicant rules\n\nPortuguese SMEs may apply for research grants.",
                encoding="utf-8",
            )
            (root / "ignored.env").write_text("SECRET=value", encoding="utf-8")

            corpus = Corpus.load(root)
            results = corpus.search("Portuguese SME research grant")

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].source, "call.md")
            self.assertIn("Portuguese SMEs", results[0].excerpt)

    def test_search_is_ranked_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.md").write_text(
                "Portugal supports companies.\n\nSME grant research Portugal.",
                encoding="utf-8",
            )
            (root / "b.txt").write_text(
                "Research grant eligibility for a Portuguese SME in Portugal.",
                encoding="utf-8",
            )
            corpus = Corpus.load(root)

            first = corpus.search("Portuguese SME grant eligibility", max_results=2)
            second = corpus.search("Portuguese SME grant eligibility", max_results=2)

            self.assertEqual(first, second)
            self.assertEqual(first[0].source, "b.txt")

    def test_search_evidence_has_exact_character_span_and_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_text = "# Elegibilidade\n\nEmpresas PME podem candidatar-se.\n"
            (root / "call.md").write_text(source_text, encoding="utf-8")
            corpus = Corpus.load(root)

            evidence = corpus.search("SME applicant eligibility", max_results=1)[0]

            self.assertEqual(source_text[evidence.start : evidence.end], evidence.excerpt)
            self.assertEqual(evidence.section, "Elegibilidade")
            self.assertEqual(
                evidence.source_hash,
                hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            )
            self.assertIn("line 1", evidence.location)

    def test_evidence_id_survives_unrelated_title_edit_while_hash_and_span_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "call.md"
            path.write_text(
                "# Aviso inicial\n\nTexto introdutório.\n\n"
                "## Elegibilidade\n\nAs PME podem candidatar-se ao apoio.",
                encoding="utf-8",
            )
            before = Corpus.load(root).search("PME apoio", max_results=1)[0]
            path.write_text(
                "# Aviso inicial com um título mais longo\n\nTexto introdutório.\n\n"
                "## Elegibilidade\n\nAs PME podem candidatar-se ao apoio.",
                encoding="utf-8",
            )
            after = Corpus.load(root).search("PME apoio", max_results=1)[0]

            self.assertEqual(before.evidence_id, after.evidence_id)
            self.assertNotEqual(before.source_hash, after.source_hash)
            self.assertNotEqual(before.start, after.start)
            self.assertEqual(after.section, "Elegibilidade")

    def test_long_paragraph_snippet_keeps_a_match_after_an_earlier_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_paragraph = ("contexto neutro " * 180) + "termoexclusivo elegibilidade"
            source_text = ("introdução " * 80) + "\n\n" + target_paragraph
            (root / "call.md").write_text(source_text, encoding="utf-8")
            corpus = Corpus.load(root)

            results = corpus.search("termoexclusivo", max_results=1)

            self.assertEqual(len(results), 1)
            self.assertIn("termoexclusivo", results[0].excerpt)
            self.assertEqual(source_text[results[0].start : results[0].end], results[0].excerpt)

    def test_retrieval_expands_supported_pt_en_funding_terms_and_rd_acronyms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "Apoio a investigação e desenvolvimento (I&D) para PME.", encoding="utf-8"
            )
            corpus = Corpus.load(root)

            results = corpus.search("R&D funding for SME", max_results=1)

            self.assertEqual(len(results), 1)
            self.assertIn("I&D", results[0].excerpt)

    def test_retrieval_expands_portuguese_connected_autonomous_mobility_terms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "Flagship-pilot demonstrations of Cooperative, Connected and Automated Mobility (CCAM).",
                encoding="utf-8",
            )
            corpus = Corpus.load(root)

            results = corpus.search(
                "testar veículos autónomos conectados", max_results=1
            )

            self.assertEqual(len(results), 1)
            self.assertGreater(results[0].term_coverage, 0.5)

    def test_retrieval_normalises_sme_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "Small and medium enterprises can apply for research support.", encoding="utf-8"
            )
            corpus = Corpus.load(root)

            results = corpus.search("PME investigação e desenvolvimento", max_results=1)

            self.assertEqual(len(results), 1)
            self.assertIn("Small and medium enterprises", results[0].excerpt)

    def test_search_decision_can_return_no_supported_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "Portugal funds business research and innovation projects.",
                encoding="utf-8",
            )
            corpus = Corpus.load(root)

            outcome = corpus.search_decision(
                "grant for a lunar submarine orchestra",
                max_results=5,
                min_confidence=0.65,
                min_term_coverage=0.3,
            )

            self.assertEqual(outcome.status, "no_supported_result")
            self.assertEqual(outcome.evidence, ())
            self.assertLess(outcome.confidence, 0.65)

    def test_search_decision_keeps_supported_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text(
                "## Eligibility\n\nSmall and medium enterprises may receive funding "
                "for applied research and innovation projects.",
                encoding="utf-8",
            )
            corpus = Corpus.load(root)

            outcome = corpus.search_decision(
                "funding for applied research and innovation in SMEs",
                max_results=5,
                min_confidence=0.2,
                min_term_coverage=0.25,
            )

            self.assertEqual(outcome.status, "supported")
            self.assertTrue(outcome.evidence)
            self.assertGreaterEqual(outcome.confidence, 0.2)

    def test_rejects_empty_or_unsupported_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "notes.csv").write_text("not supported", encoding="utf-8")
            with self.assertRaises(CorpusError):
                Corpus.load(root)

    def test_rejects_a_non_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            file_path = Path(temporary) / "call.md"
            file_path.write_text("A call.", encoding="utf-8")
            with self.assertRaises(CorpusError):
                Corpus.load(file_path)

    def test_validates_search_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "call.md").write_text("Research grant in Portugal.", encoding="utf-8")
            corpus = Corpus.load(root)

            with self.assertRaises(CorpusError):
                corpus.search("  ")
            with self.assertRaises(CorpusError):
                corpus.search("grant", max_results=0)


if __name__ == "__main__":
    unittest.main()
