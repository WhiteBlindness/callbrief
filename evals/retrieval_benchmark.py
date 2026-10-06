"""Benchmark legacy lexical retrieval against the current BM25 pipeline.

The corpus is deliberately synthetic. Its results show fixture behaviour only,
not expected retrieval quality on official funding notices.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from callbrief.corpus import Corpus
from callbrief.evaluation import recall_at_k

TOKEN = re.compile(r"[\w]+", re.UNICODE)
STOP_WORDS = frozenset(
    "a ao aos as com da das de do dos e em entre for from i na nas no nos o os ou para por que se the um uma uns umas is of and or to this that may can do does applicant applicants entidade empresa empresas apoio aviso candidatura candidaturas".split()
)
SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "query": "SME research funding",
        "relevant": "pt_sme_rd",
    },
    {
        "query": "PME inovação",
        "relevant": "pt_sme_innovation",
    },
    {
        "query": "small and medium enterprises R&D grants",
        "relevant": "pt_sme_research",
    },
)
DOCUMENTS = {
    "pt_sme_rd": "Financiamento para pequenas e médias empresas em investigação e desenvolvimento experimental.",
    "pt_sme_innovation": "## Pequenas e médias empresas\n\nApoio à inovação para entidades com projetos de valorização tecnológica.",
    "pt_sme_research": "São apoiadas PME com projetos de investigação e desenvolvimento colaborativo.",
    "pt_large_research": "Financiamento para grandes empresas em investigação industrial.",
    "pt_training": "Formação profissional para pequenas e médias empresas.",
    "en_university": "Research funding for universities and public research organisations.",
    "pt_energy": "Incentivos à eficiência energética em edifícios públicos.",
    "pt_tender": "Concurso público para aquisição de equipamento informático.",
}


def _legacy_normalise(text: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return tuple(token for token in TOKEN.findall(plain) if token not in STOP_WORDS)


def _legacy_rank(query: str, documents: dict[str, str]) -> tuple[str, ...]:
    """Reproduce the pre-v0.2 term-only BM25 ranker for this plain-text corpus."""
    terms = set(_legacy_normalise(query))
    paragraphs = {
        identifier: tuple(
            _legacy_normalise(paragraph)
            for paragraph in text.split("\n\n")
            if paragraph.strip()
        )
        for identifier, text in documents.items()
    }
    flat = [(identifier, tokens) for identifier, groups in paragraphs.items() for tokens in groups]
    total = max(1, len(flat))
    frequency = {
        term: sum(term in tokens for _, tokens in flat)
        for term in terms
    }
    ranked: list[tuple[float, str]] = []
    for identifier, groups in paragraphs.items():
        score = 0.0
        for tokens in groups:
            counts = {token: tokens.count(token) for token in set(tokens)}
            for term in terms:
                count = counts.get(term, 0)
                if count:
                    inverse = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
                    score += inverse * (count * 2.2) / (count + 1.2)
        if score:
            ranked.append((score, identifier))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return tuple(identifier for _, identifier in ranked)


def run_benchmark() -> dict[str, Any]:
    """Return comparable Recall@5 scores for the fixed synthetic scenarios."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for identifier, text in DOCUMENTS.items():
            (root / f"{identifier}.txt").write_text(text, encoding="utf-8")
        corpus = Corpus.load(root)
        baseline_scores: list[float] = []
        current_scores: list[float] = []
        detail: list[dict[str, Any]] = []
        for scenario in SCENARIOS:
            relevant = (scenario["relevant"],)
            baseline = _legacy_rank(scenario["query"], DOCUMENTS)[:5]
            current = tuple(
                Path(item.source).stem for item in corpus.search(scenario["query"], max_results=5)
            )
            baseline_metric = recall_at_k(relevant, baseline, k=5)
            current_metric = recall_at_k(relevant, current, k=5)
            baseline_scores.append(float(baseline_metric["score"] or 0.0))
            current_scores.append(float(current_metric["score"] or 0.0))
            detail.append(
                {
                    "query": scenario["query"],
                    "relevant_id": scenario["relevant"],
                    "baseline_top_5": list(baseline),
                    "current_top_5": list(current),
                    "baseline_recall_at_5": baseline_metric["score"],
                    "current_recall_at_5": current_metric["score"],
                }
            )
        sample_count = len(SCENARIOS)
        return {
            "synthetic": True,
            "sample_size": sample_count,
            "metric": "Recall@5, averaged across three hand-labelled fixture queries",
            "baseline": sum(baseline_scores) / sample_count,
            "current": sum(current_scores) / sample_count,
            "change": sum(current_scores) / sample_count - sum(baseline_scores) / sample_count,
            "scenarios": detail,
        }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2))
