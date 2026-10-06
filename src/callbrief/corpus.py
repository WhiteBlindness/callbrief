"""Safe loading and deterministic lexical retrieval for a local document corpus."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
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
    start: int
    end: int
    source_hash: str
    retrieved_at: datetime
    section: str | None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end - self.start != len(self.excerpt):
            raise ValueError("Evidence span must match the exact source excerpt")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("Evidence retrieval time must include a timezone")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_hash):
            raise ValueError("Evidence source_hash must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class _Paragraph:
    line: int
    start: int
    end: int
    section: str | None


@dataclass(frozen=True, slots=True)
class _Document:
    source: str
    text: str
    source_hash: str
    retrieved_at: datetime
    paragraphs: tuple[_Paragraph, ...]


_SYNONYMS = {
    "pme": "sme",
    "sme": "pme",
    "investigacao": "research",
    "research": "investigacao",
    "desenvolvimento": "development",
    "development": "desenvolvimento",
    "inovacao": "innovation",
    "innovation": "inovacao",
    "financiamento": "funding",
    "funding": "financiamento",
}


def _normalise(text: str) -> str:
    folded = text.casefold()
    folded = re.sub(
        r"\b(?:pequenas?\s+e\s+m[eé]dias?\s+(?:empresas|dimens[aã]o)|small\s+and\s+medium\s+enterprises)\b",
        " PME ",
        folded,
    )
    expanded = re.sub(r"\b(?:i|r)\s*(?:&|\+|e)\s*d\b", "investigacao desenvolvimento", folded)
    decomposed = unicodedata.normalize("NFKD", expanded)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_TOKEN.findall(plain))


def _paragraphs(text: str) -> tuple[_Paragraph, ...]:
    groups: list[_Paragraph] = []
    section: str | None = None
    pending_heading: tuple[int, int] | None = None
    paragraph_start: int | None = None
    paragraph_line = 1
    paragraph_end = 0
    offset = 0

    def flush() -> None:
        nonlocal paragraph_start, paragraph_end
        if paragraph_start is not None and paragraph_end > paragraph_start:
            groups.append(_Paragraph(paragraph_line, paragraph_start, paragraph_end, section))
        paragraph_start = None
        paragraph_end = 0

    for line_number, raw in enumerate(text.splitlines(keepends=True), start=1):
        line = raw.strip()
        line_end = offset + len(raw.rstrip("\r\n"))
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            flush()
            section = heading.group(1).strip()
            pending_heading = (line_number, offset)
        elif not line:
            if paragraph_start is not None:
                flush()
        else:
            if paragraph_start is None:
                paragraph_line, paragraph_start = pending_heading or (line_number, offset)
                pending_heading = None
            paragraph_end = line_end
        offset += len(raw)
    flush()
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
                documents.append(
                    _Document(
                        source=relative,
                        text=text,
                        source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        retrieved_at=datetime.now(UTC),
                        paragraphs=paragraphs,
                    )
                )
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
        raw_terms = tuple(term for term in _normalise(query).split() if term not in _STOP_WORDS)
        expanded_terms = set(raw_terms)
        expanded_terms.update(_SYNONYMS[term] for term in raw_terms if term in _SYNONYMS)
        terms = tuple(sorted(expanded_terms))
        if not terms:
            raise CorpusError("Search query does not contain searchable terms")

        rows: list[tuple[float, _Document, _Paragraph]] = []
        document_frequency = {term: 0 for term in set(terms)}
        normalized_paragraphs: list[
            tuple[_Document, _Paragraph, tuple[str, ...], tuple[str, ...]]
        ] = []
        for document in self._documents:
            for paragraph in document.paragraphs:
                paragraph_text = document.text[paragraph.start : paragraph.end]
                tokens = tuple(_normalise(paragraph_text).split())
                section_tokens = tuple(_normalise(paragraph.section or "").split())
                normalized_paragraphs.append((document, paragraph, tokens, section_tokens))
                for term in document_frequency:
                    if term in tokens or term in section_tokens:
                        document_frequency[term] += 1
        total = max(1, len(normalized_paragraphs))
        for document, paragraph, tokens, section_tokens in normalized_paragraphs:
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
                    if term in section_tokens:
                        score += inverse_frequency * 0.75
                    if term in _normalise(Path(document.source).stem).split():
                        score += inverse_frequency * 0.5
            if score:
                rows.append((score, document, paragraph))
        rows.sort(key=lambda item: (-item[0], item[1].source.casefold(), item[2].line))

        results: list[Evidence] = []
        for _, document, paragraph in rows[:max_results]:
            matching_positions = [
                match.start()
                for match in _TOKEN.finditer(document.text, paragraph.start, paragraph.end)
                if _normalise(match.group()) in expanded_terms
            ]
            snippet_start = paragraph.start
            if paragraph.end - paragraph.start > 1200:
                center = matching_positions[0] if matching_positions else paragraph.start
                snippet_start = max(paragraph.start, center - 400)
                snippet_start = min(snippet_start, paragraph.end - 1200)
            snippet_end = min(paragraph.end, snippet_start + 1200)
            excerpt = document.text[snippet_start:snippet_end]
            line = paragraph.line + document.text[paragraph.start : snippet_start].count("\n")
            location = f"line {line}"
            if paragraph.section:
                location = f"{paragraph.section}, {location}"
            digest = hashlib.sha256(
                f"{document.source}\0{paragraph.section or ''}\0{excerpt}".encode()
            ).hexdigest()[:12]
            results.append(
                Evidence(
                    evidence_id=f"ev-{digest}",
                    source=document.source,
                    location=location,
                    excerpt=excerpt,
                    start=snippet_start,
                    end=snippet_end,
                    source_hash=document.source_hash,
                    retrieved_at=document.retrieved_at,
                    section=paragraph.section,
                )
            )
        logger.info("corpus_search", extra={"event": "corpus_search", "result_count": len(results)})
        return tuple(results)
