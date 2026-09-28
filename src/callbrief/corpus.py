"""Safe loading and deterministic lexical retrieval for a local document corpus."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf"})
IGNORED_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_CORPUS_FILES = 250
MAX_CORPUS_CHARACTERS = 2_000_000
_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_STOP_WORDS = frozenset(
    """
    a ao aos as com da das de do dos e em entre for from i na nas no nos o os ou para por que se
    the um uma uns umas is of and or to this that may can do does applicant applicants entidade
    empresa empresas apoio aviso candidatura candidaturas
    """.split()
)


class CorpusError(ValueError):
    """Raised when a document corpus is missing or unsafe to process."""


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    source: str
    location: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class _Document:
    source: str
    paragraphs: tuple[tuple[int, str], ...]


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_TOKEN.findall(plain))


def _paragraphs(text: str) -> tuple[tuple[int, str], ...]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    groups: list[tuple[int, str]] = []
    current: list[str] = []
    start = 1
    pending_heading: tuple[int, str] | None = None
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if line:
            if not current and re.match(r"^#{1,6}\s+", line):
                pending_heading = (line_number, line.lstrip("# "))
                continue
            if not current:
                start = pending_heading[0] if pending_heading else line_number
                if pending_heading:
                    current.append(pending_heading[1])
                    pending_heading = None
            current.append(line)
        elif current:
            groups.append((start, " ".join(current)))
            current = []
    if current:
        groups.append((start, " ".join(current)))
    return tuple(groups)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise CorpusError(
            "PDF support requires the optional 'pdf' extra: pip install callbrief[pdf]"
        ) from exc
    try:
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as exc:
        raise CorpusError(f"Could not extract text from PDF: {path.name}") from exc


@dataclass(frozen=True, slots=True)
class Corpus:
    _documents: tuple[_Document, ...]

    @classmethod
    def load(cls, root: Path) -> Corpus:
        path = Path(root)
        if not path.is_dir() or path.is_symlink():
            raise CorpusError("Corpus path must be a real directory")
        selected: list[Path] = []
        for candidate in path.rglob("*"):
            if candidate.is_symlink():
                continue
            if any(
                part in IGNORED_DIRECTORIES or part.startswith(".")
                for part in candidate.relative_to(path).parts[:-1]
            ):
                continue
            if candidate.is_file() and candidate.suffix.casefold() in SUPPORTED_SUFFIXES:
                selected.append(candidate)
        selected.sort(key=lambda item: item.relative_to(path).as_posix().casefold())
        if not selected:
            raise CorpusError("No supported .md, .txt or .pdf files were found")
        if len(selected) > MAX_CORPUS_FILES:
            raise CorpusError(f"Corpus exceeds the limit of {MAX_CORPUS_FILES} files")

        documents: list[_Document] = []
        total_characters = 0
        for source_path in selected:
            if source_path.stat().st_size > MAX_FILE_BYTES:
                raise CorpusError(f"File exceeds the 5 MiB limit: {source_path.name}")
            try:
                text = (
                    _read_pdf(source_path)
                    if source_path.suffix.casefold() == ".pdf"
                    else source_path.read_text(encoding="utf-8")
                )
            except UnicodeDecodeError as exc:
                raise CorpusError(f"File is not valid UTF-8 text: {source_path.name}") from exc
            relative = source_path.relative_to(path).as_posix()
            total_characters += len(text)
            if total_characters > MAX_CORPUS_CHARACTERS:
                raise CorpusError("Corpus exceeds the two-million-character limit")
            paragraphs = _paragraphs(text)
            if paragraphs:
                documents.append(_Document(relative, paragraphs))
        if not documents:
            raise CorpusError("No readable text was found in the supported files")
        logger.info("corpus_loaded", extra={"event": "corpus_loaded", "documents": len(documents)})
        return cls(tuple(documents))

    def search(self, query: str, max_results: int = 6) -> tuple[Evidence, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise CorpusError("Search query must contain between 1 and 500 characters")
        if (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or not 1 <= max_results <= 12
        ):
            raise CorpusError("max_results must be between 1 and 12")
        terms = tuple(term for term in _normalise(query).split() if term not in _STOP_WORDS)
        if not terms:
            raise CorpusError("Search query does not contain searchable terms")

        rows: list[tuple[float, str, int, str]] = []
        document_frequency = {term: 0 for term in set(terms)}
        normalized_paragraphs: list[tuple[_Document, int, str, tuple[str, ...]]] = []
        for document in self._documents:
            for line, paragraph in document.paragraphs:
                tokens = tuple(_normalise(paragraph).split())
                normalized_paragraphs.append((document, line, paragraph, tokens))
                for term in document_frequency:
                    if term in tokens:
                        document_frequency[term] += 1
        total = max(1, len(normalized_paragraphs))
        for document, line, paragraph, tokens in normalized_paragraphs:
            frequencies = {token: tokens.count(token) for token in set(tokens)}
            score = 0.0
            for term in set(terms):
                frequency = frequencies.get(term, 0)
                if frequency:
                    inverse_frequency = math.log(
                        1
                        + (total - document_frequency[term] + 0.5)
                        / (document_frequency[term] + 0.5)
                    )
                    score += inverse_frequency * (frequency * 2.2) / (frequency + 1.2)
            if score:
                rows.append((score, document.source, line, paragraph))
        rows.sort(key=lambda item: (-item[0], item[1].casefold(), item[2]))

        results: list[Evidence] = []
        for _, source, line, paragraph in rows[:max_results]:
            digest = hashlib.sha256(f"{source}\0{line}\0{paragraph}".encode()).hexdigest()[:12]
            excerpt = paragraph[:1200]
            results.append(
                Evidence(
                    evidence_id=f"ev-{digest}",
                    source=source,
                    location=f"line {line}",
                    excerpt=excerpt,
                )
            )
        logger.info("corpus_search", extra={"event": "corpus_search", "result_count": len(results)})
        return tuple(results)
