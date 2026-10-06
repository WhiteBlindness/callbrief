"""Command-line interface for local funding-call assessments."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .agent import AgentError, AgentRunner
from .corpus import Corpus, CorpusError
from .domain import FitSignals, OpportunityType, OrganisationProfile
from .eligibility import assess_eligibility
from .filtering import FilterState, OpportunityFilter, filter_opportunity
from .pipeline import run_discovery
from .provider import OpenAICompatibleClient, ProviderError
from .report import (
    EvidenceDetail,
    ReportError,
    render_html,
    render_html_document,
    render_markdown,
    render_opportunity_markdown,
    write_report,
)
from .scoring import FitWeights, score_fit
from .settings import Settings, SettingsError
from .sources import SourceError, create_adapter_registry, load_source_registry
from .storage import SqliteStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="callbrief",
        description="Cria uma avaliação preliminar, com fontes, de um aviso de financiamento.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="regista eventos técnicos sem incluir documentos ou credenciais",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    assess = commands.add_parser(
        "assess", help="avalia os documentos locais de um aviso e de uma entidade"
    )
    assess.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="pasta com ficheiros .md, .txt e, opcionalmente, .pdf",
    )
    assess.add_argument(
        "--output",
        type=Path,
        default=Path("callbrief-report.md"),
        help="caminho do relatório Markdown",
    )
    assess.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="ficheiro local opcional de configuração",
    )
    assess.add_argument(
        "--allow-remote",
        action="store_true",
        help="autoriza o envio de excertos para um serviço remoto",
    )
    assess.add_argument("--force", action="store_true", help="substitui o relatório existente")
    assess.add_argument(
        "--format",
        choices=("markdown", "html"),
        default=None,
        help="formato do relatório; por omissão, segue a extensão do ficheiro",
    )

    source = commands.add_parser("source", help="consulta o registo declarativo de fontes")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_list = source_commands.add_parser("list", help="lista fontes e respetivo estado")
    source_list.add_argument("--registry", type=Path, default=None)

    discover = commands.add_parser("discover", help="pesquisa e guarda avisos numa fonte ativa")
    discover.add_argument("--source", required=True, help="identificador de fonte ativa")
    discover.add_argument("--query", required=True, help="termos a pesquisar no aviso")
    discover.add_argument("--limit", type=int, default=20, help="máximo de registos, entre 1 e 100")
    discover.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))

    organisation = commands.add_parser("organisation", help="gere perfis locais de organizações")
    organisation_commands = organisation.add_subparsers(dest="organisation_command", required=True)
    add_organisation = organisation_commands.add_parser("add", help="cria ou atualiza um perfil")
    add_organisation.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    add_organisation.add_argument("--workspace-id", required=True)
    add_organisation.add_argument("--workspace-name", required=True)
    add_organisation.add_argument("--id", required=True, dest="organisation_id")
    add_organisation.add_argument("--name", dest="legal_name")
    add_organisation.add_argument("--vat-number", dest="vat_number")
    add_organisation.add_argument("--country")
    add_organisation.add_argument("--company-size")
    add_organisation.add_argument("--employees", type=int)
    add_organisation.add_argument("--region", action="append", default=[])
    add_organisation.add_argument("--applicant-type", action="append", default=[])
    add_organisation.add_argument("--sector", action="append", default=[])
    add_organisation.add_argument("--preferred-geography", action="append", default=None)
    add_organisation.add_argument(
        "--preferred-type",
        action="append",
        choices=tuple(
            item.value for item in OpportunityType if item is not OpportunityType.UNKNOWN
        ),
        default=None,
    )
    add_organisation.add_argument(
        "--excluded-type",
        action="append",
        choices=tuple(
            item.value for item in OpportunityType if item is not OpportunityType.UNKNOWN
        ),
        default=None,
    )

    show_organisation = organisation_commands.add_parser("show", help="mostra um perfil guardado")
    show_organisation.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    show_organisation.add_argument("--workspace-id", required=True)
    show_organisation.add_argument("--id", required=True, dest="organisation_id")

    opportunities = commands.add_parser(
        "opportunity", help="consulta oportunidades guardadas e aplica filtros locais"
    )
    opportunity_commands = opportunities.add_subparsers(dest="opportunity_command", required=True)
    list_opportunities = opportunity_commands.add_parser("list", help="lista avisos canónicos")
    list_opportunities.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    list_opportunities.add_argument("--workspace-id")
    list_opportunities.add_argument("--organisation-id")
    list_opportunities.add_argument(
        "--type",
        action="append",
        choices=tuple(
            item.value for item in OpportunityType if item is not OpportunityType.UNKNOWN
        ),
        default=None,
        dest="opportunity_types",
    )
    list_opportunities.add_argument("--applicant-type", action="append", default=None)
    list_opportunities.add_argument("--region", action="append", default=None)
    list_opportunities.add_argument("--geography", action="append", default=None)
    list_opportunities.add_argument("--sector", action="append", default=None)
    list_opportunities.add_argument("--minimum-funding", type=Decimal)
    list_opportunities.add_argument("--deadline-within-days", type=int)
    list_opportunities.add_argument("--include-excluded", action="store_true")

    changes = commands.add_parser("changes", help="lista alterações materiais detetadas")
    changes.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    changes.add_argument("--opportunity-id")

    notifications = commands.add_parser("notification", help="consulta eventos na fila local")
    notification_commands = notifications.add_subparsers(dest="notification_command", required=True)
    notification_list = notification_commands.add_parser(
        "list", help="lista eventos visíveis no perfil"
    )
    notification_list.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    notification_list.add_argument("--workspace-id", required=True)
    notification_list.add_argument("--organisation-id")

    assess_call = commands.add_parser(
        "assess-opportunity", help="avalia regras e adequação de um aviso normalizado"
    )
    assess_call.add_argument("--database", type=Path, default=Path("callbrief.sqlite3"))
    assess_call.add_argument("--workspace-id", required=True)
    assess_call.add_argument("--organisation-id", required=True)
    assess_call.add_argument("--opportunity-id", required=True)
    assess_call.add_argument("--output", type=Path, default=Path("callbrief-opportunity.md"))
    assess_call.add_argument("--format", choices=("markdown", "html"), default=None)
    assess_call.add_argument("--weights-file", type=Path, default=None)
    assess_call.add_argument("--force", action="store_true")
    for option, label in (
        ("strategic-fit", "adequação estratégica analisada"),
        ("probability-of-winning", "probabilidade de sucesso avaliada"),
        ("funding-attractiveness", "atratividade do financiamento"),
        ("application-effort", "adequação ao esforço de candidatura"),
        ("time-to-deadline", "tempo até ao prazo"),
        ("company-capabilities", "capacidades da organização"),
        ("consortium-readiness", "preparação do consórcio"),
        ("maturity", "maturidade do projeto"),
        ("evidence-completeness", "completude da evidência"),
    ):
        assess_call.add_argument(
            f"--{option}",
            type=int,
            choices=range(0, 101),
            dest=option.replace("-", "_"),
            help=f"pontuação revista pelo utilizador para {label}, entre 0 e 100",
        )
    return parser


def _report_format(path: Path, selected: str | None) -> str:
    return selected or ("html" if path.suffix.casefold() == ".html" else "markdown")


def _load_weights(path: Path | None) -> FitWeights:
    if path is None:
        return FitWeights()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("O ficheiro de pesos tem de ser local, regular e inferior a 64 KiB.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Não foi possível ler a configuração de pesos.") from exc
    if not isinstance(data, dict) or set(data) - set(FitWeights.names()):
        raise ValueError("A configuração contém pesos desconhecidos ou não é um objeto JSON.")
    defaults = FitWeights().as_mapping()
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"O peso '{name}' tem de ser numérico.")
        defaults[name] = float(value)
    return FitWeights(**defaults)


def _source_list(registry_path: Path | None) -> int:
    definitions = load_source_registry(registry_path) if registry_path else load_source_registry()
    state_labels = {
        "active": "ativa",
        "pending_policy": "aguarda revisão de reutilização",
        "pending_verification": "por verificar",
        "discovery_only": "apenas descoberta",
    }
    cadence_labels = {
        "daily": "diária",
        "twice_weekly": "duas vezes por semana",
        "weekly": "semanal",
        "manual": "manual",
    }
    for item in definitions:
        state = state_labels[item.status]
        address = item.base_url or "URL por verificar"
        cadence = cadence_labels[item.check_cadence]
        print(f"{item.source_id}\t{state}\t{cadence}\t{address}")
    return 0


def _discover(args: argparse.Namespace) -> int:
    adapter = create_adapter_registry().get(args.source)
    if adapter is None:
        raise SourceError("A fonte não está ativa ou não dispõe de um adaptador verificado.")
    if not 1 <= args.limit <= 100:
        raise ValueError("O limite tem de estar entre 1 e 100.")
    with SqliteStore(args.database) as store:
        result = run_discovery(adapter, store, query=args.query, limit=args.limit)
    print(
        f"Registos recebidos: {len(result.items)}; novas capturas: {result.new_snapshots}; "
        f"duplicados exatos: {result.exact_duplicates}; correspondências para revisão: "
        f"{result.possible_matches}."
    )
    for item in result.items:
        title = item.opportunity.title or item.opportunity.id
        if item.duplicate.kind.value == "exact":
            suffix = f"variante de {item.duplicate.canonical_opportunity_id}"
        elif item.duplicate.kind.value == "possible":
            suffix = "correspondência possível, sem união automática"
        else:
            suffix = item.opportunity.id
        print(f"- {title}: {suffix}")
    return 0


def _manage_organisation(args: argparse.Namespace) -> int:
    with SqliteStore(args.database) as store:
        if args.organisation_command == "show":
            profile = store.get_organisation(args.workspace_id, args.organisation_id)
            if profile is None:
                raise ValueError(
                    "Não foi encontrado um perfil com esse identificador neste espaço de trabalho."
                )
            print(json.dumps(asdict(profile), ensure_ascii=False, default=str, indent=2))
            return 0

        store.create_workspace(args.workspace_id, args.workspace_name)
        previous = store.get_organisation(args.workspace_id, args.organisation_id)
        profile = previous or OrganisationProfile(
            id=args.organisation_id,
            workspace_id=args.workspace_id,
        )
        changes: dict[str, Any] = {}
        for name in ("legal_name", "vat_number", "country", "company_size", "employees"):
            value = getattr(args, name)
            if value is not None:
                changes[name] = value
        for argument, field_name in (
            ("region", "regions"),
            ("applicant_type", "applicant_types"),
            ("sector", "sectors"),
            ("preferred_geography", "preferred_geographies"),
        ):
            values = getattr(args, argument)
            if values:
                changes[field_name] = tuple(values)
        for argument, field_name in (
            ("preferred_type", "preferred_opportunity_types"),
            ("excluded_type", "excluded_opportunity_types"),
        ):
            values = getattr(args, argument)
            if values is not None:
                changes[field_name] = tuple(OpportunityType(value) for value in values)
        saved = replace(profile, **changes)
        store.save_organisation(saved)
    print(f"Perfil guardado: {saved.id} ({saved.workspace_id}).")
    return 0


def _assess_opportunity(args: argparse.Namespace) -> int:
    with SqliteStore(args.database) as store:
        profile = store.get_organisation(args.workspace_id, args.organisation_id)
        if profile is None:
            raise ValueError(
                "Não foi encontrado um perfil com esse identificador neste espaço de trabalho."
            )
        opportunity = store.get_opportunity(args.opportunity_id)
        if opportunity is None:
            raise ValueError("O aviso indicado não existe na base local.")
        eligibility = assess_eligibility(opportunity, profile)
        signal_values = {
            name: getattr(args, name)
            for name in FitWeights.names()
            if name != "eligibility" and getattr(args, name, None) is not None
        }
        fit = score_fit(
            eligibility,
            FitSignals(**signal_values),
            weights=_load_weights(args.weights_file),
        )
        store.save_assessment(args.workspace_id, args.organisation_id, eligibility, fit)
        variants = store.list_source_variants(opportunity.id)
    markdown = render_opportunity_markdown(opportunity, eligibility, fit, variants=variants)
    if _report_format(args.output, args.format) == "html":
        content = render_html_document(opportunity.title or opportunity.id, markdown)
    else:
        content = markdown
    write_report(args.output, content, force=args.force)
    print(f"Avaliação criada: {args.output}")
    return 0


def _list_opportunities(args: argparse.Namespace) -> int:
    if (args.workspace_id is None) != (args.organisation_id is None):
        raise ValueError("Indica o espaço de trabalho e a organização em conjunto.")
    with SqliteStore(args.database) as store:
        profile = None
        if args.workspace_id is not None:
            profile = store.get_organisation(args.workspace_id, args.organisation_id)
            if profile is None:
                raise ValueError(
                    "Não foi encontrado um perfil com esse identificador neste espaço de trabalho."
                )
        selected = OpportunityFilter(
            opportunity_types=(
                tuple(OpportunityType(item) for item in args.opportunity_types)
                if args.opportunity_types is not None
                else None
            ),
            applicant_types=tuple(args.applicant_type) if args.applicant_type else None,
            regions=tuple(args.region) if args.region else None,
            geographies=tuple(args.geography) if args.geography else None,
            sectors=tuple(args.sector) if args.sector else None,
            minimum_funding=args.minimum_funding,
            deadline_window_days=args.deadline_within_days,
        )
        for opportunity in store.list_opportunities():
            result = filter_opportunity(opportunity, selected, profile=profile)
            if result.state is FilterState.EXCLUDED and not args.include_excluded:
                continue
            title = opportunity.title or opportunity.id
            reasons = "; ".join(result.reasons)
            state_label = {
                FilterState.INCLUDED: "incluída",
                FilterState.EXCLUDED: "excluída",
                FilterState.NEEDS_REVIEW: "a rever",
            }[result.state]
            deadline = (
                opportunity.deadline.strftime("%d/%m/%Y")
                if opportunity.deadline
                else "Prazo desconhecido"
            )
            print(f"{state_label}\t{title}\t{deadline}")
            if reasons:
                print(f"  {reasons}")
    return 0


def _list_changes(args: argparse.Namespace) -> int:
    with SqliteStore(args.database) as store:
        records = store.list_changes(args.opportunity_id)
    for change in records:
        detected = (
            datetime.fromisoformat(change["detected_at"])
            .astimezone(UTC)
            .strftime("%d/%m/%Y %H:%M UTC")
        )
        print(
            f"{detected}\t{change['opportunity_id']}\t{change['field']}\t"
            f"{change['old_value']} -> {change['new_value']}\t{change['source_id']}"
        )
    if not records:
        print("Não foram detetadas alterações materiais.")
    return 0


def _list_notifications(args: argparse.Namespace) -> int:
    if args.organisation_id is not None:
        with SqliteStore(args.database) as store:
            profile = store.get_organisation(args.workspace_id, args.organisation_id)
            if profile is None:
                raise ValueError(
                    "Não foi encontrado um perfil com esse identificador neste espaço de trabalho."
                )
    with SqliteStore(args.database) as store:
        events = store.list_notifications(args.workspace_id, args.organisation_id)
    for event in events:
        created = (
            datetime.fromisoformat(event["created_at"])
            .astimezone(UTC)
            .strftime("%d/%m/%Y %H:%M UTC")
        )
        labels = {
            "new_opportunity": "novo aviso",
            "new_high_fit_opportunity": "novo aviso com adequação elevada",
            "deadline_approaching": "prazo próximo",
            "important_call_change": "alteração importante",
            "call_opened": "aviso aberto",
            "call_closed": "aviso encerrado",
            "eligibility_requirement_changed": "requisito de elegibilidade alterado",
        }
        event_label = labels.get(event["event_type"], event["event_type"])
        print(f"{created}\t{event_label}\t{event['opportunity_id']}")
    if not events:
        print("A fila de notificações está vazia.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    try:
        if args.command == "source":
            return _source_list(args.registry)
        if args.command == "discover":
            return _discover(args)
        if args.command == "organisation":
            return _manage_organisation(args)
        if args.command == "opportunity":
            return _list_opportunities(args)
        if args.command == "changes":
            return _list_changes(args)
        if args.command == "notification":
            return _list_notifications(args)
        if args.command == "assess-opportunity":
            return _assess_opportunity(args)

        settings = Settings.from_sources(env_file=args.env_file)
        if settings.base_url.startswith("https://") and not args.allow_remote:
            raise SettingsError(
                "O serviço de modelo configurado é remoto. Revê a política de dados e usa "
                "--allow-remote para enviar excertos documentais."
            )
        corpus = Corpus.load(args.corpus)
        result = AgentRunner(OpenAICompatibleClient(settings), max_turns=settings.max_turns).run(
            corpus
        )
        evidence_map = {
            item.evidence_id: EvidenceDetail(
                source=item.source,
                location=item.location,
                excerpt=item.excerpt,
                start=item.start,
                end=item.end,
                source_hash=item.source_hash,
                retrieved_at=item.retrieved_at.isoformat(),
                section=item.section,
            )
            for item in result.evidence
        }
        if _report_format(args.output, args.format) == "html":
            content = render_html(result.brief, evidence_map)
        else:
            content = render_markdown(result.brief, evidence_map)
        write_report(args.output, content, force=args.force)
        print(f"Relatório criado: {args.output}")
        return 0
    except (
        AgentError,
        CorpusError,
        ProviderError,
        ReportError,
        SettingsError,
        SourceError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
