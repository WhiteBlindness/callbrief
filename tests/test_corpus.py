from __future__ import annotations

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
