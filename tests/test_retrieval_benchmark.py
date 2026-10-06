from __future__ import annotations

import unittest

from evals.retrieval_benchmark import run_benchmark


class RetrievalBenchmarkTests(unittest.TestCase):
    def test_current_ranker_improves_the_fixed_synthetic_recall_suite(self) -> None:
        result = run_benchmark()

        self.assertTrue(result["synthetic"])
        self.assertEqual(result["sample_size"], 3)
        self.assertAlmostEqual(result["baseline"], 1 / 3)
        self.assertEqual(result["current"], 1.0)
        self.assertAlmostEqual(result["change"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
