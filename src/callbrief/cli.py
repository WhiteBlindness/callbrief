"""Command-line interface for local funding-call assessments."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agent import AgentError, AgentRunner
from .corpus import Corpus, CorpusError
from .provider import OpenAICompatibleClient, ProviderError
from .report import ReportError, render_markdown, write_report
from .settings import Settings, SettingsError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="callbrief", description="Cria uma avaliação preliminar, com fontes, de um aviso de financiamento.")
    parser.add_argument("--verbose", action="store_true", help="regista eventos técnicos sem incluir documentos ou credenciais")
    commands = parser.add_subparsers(dest="command", required=True)
    assess = commands.add_parser("assess", help="avalia os documentos locais de um aviso e de uma entidade")
    assess.add_argument("--corpus", type=Path, required=True, help="pasta com ficheiros .md, .txt e, opcionalmente, .pdf")
    assess.add_argument("--output", type=Path, default=Path("callbrief-report.md"), help="caminho do relatório Markdown")
    assess.add_argument("--env-file", type=Path, default=Path(".env"), help="ficheiro local opcional de configuração")
    assess.add_argument("--allow-remote", action="store_true", help="autoriza o envio de excertos para um serviço remoto")
    assess.add_argument("--force", action="store_true", help="substitui o relatório existente")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    try:
        settings = Settings.from_sources(env_file=args.env_file)
        if settings.base_url.startswith("https://") and not args.allow_remote:
            raise SettingsError("The configured model endpoint is remote. Review your data policy and pass --allow-remote to send document excerpts.")
        corpus = Corpus.load(args.corpus)
        result = AgentRunner(OpenAICompatibleClient(settings), max_turns=settings.max_turns).run(corpus)
        evidence_map = {
            item.evidence_id: (item.source, item.location, item.excerpt)
            for item in result.evidence
        }
        write_report(args.output, render_markdown(result.brief, evidence_map), force=args.force)
        print(f"Relatório criado: {args.output}")
        return 0
    except (AgentError, CorpusError, ProviderError, ReportError, SettingsError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
