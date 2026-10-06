"""Readable, evidence-linked Markdown report output."""

from __future__ import annotations

import html
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from .domain import (
    EligibilityAssessment,
    EligibilityRule,
    EligibilityState,
    FitAssessment,
    Opportunity,
)
from .models import Brief, Claim


class ReportError(RuntimeError):
    """Raised when a report cannot be written safely."""


@dataclass(frozen=True, slots=True)
class EvidenceDetail:
    source: str
    location: str
    excerpt: str
    start: int
    end: int
    source_hash: str
    retrieved_at: str
    section: str | None = None


def _render_claim(claim: Claim) -> str:
    citations = ", ".join(f"`{evidence_id}`" for evidence_id in claim.evidence_ids)
    return f"{claim.statement} ({citations})"


def render_markdown(
    brief: Brief,
    evidence_map: Mapping[str, EvidenceDetail | tuple[str, str, str]],
) -> str:
    lines = [
        f"# {brief.title}",
        "",
        (
            "> Avaliação preliminar para apoiar uma decisão interna. Confirme os critérios, prazos "
            "e condições no aviso oficial; este relatório não é aconselhamento "
            "jurídico ou financeiro."
        ),
        "",
        "## Síntese",
        "",
        _render_claim(brief.summary),
        "",
        f"**Adequação preliminar:** {brief.fit_band.value}",
        "",
        _render_claim(brief.fit_rationale),
        "",
        "## Requisitos",
        "",
    ]
    if brief.requirements:
        for requirement in brief.requirements:
            citations = ", ".join(f"`{item}`" for item in requirement.evidence_ids)
            lines.append(
                f"- **{requirement.status.value}: {requirement.requirement}.** "
                f"{requirement.note} ({citations})"
            )
    else:
        lines.append("- Não foram identificados requisitos com evidência suficiente.")
    lines.extend(["", "## Prazos", ""])
    lines.extend(
        f"- {_render_claim(item)}" for item in brief.deadlines
    ) if brief.deadlines else lines.append(
        "- Não foi identificado um prazo nos excertos consultados."
    )
    lines.extend(["", "## Riscos e dúvidas", ""])
    lines.extend(
        f"- {_render_claim(item)}" for item in brief.risks
    ) if brief.risks else lines.append("- Não foram assinalados riscos documentais.")
    lines.extend(f"- Questão: {item}" for item in brief.open_questions)
    lines.extend(["", "## Próximos passos", ""])
    lines.extend(f"- {item}" for item in brief.next_steps) if brief.next_steps else lines.append(
        "- Rever os pontos não confirmados com a equipa responsável."
    )
    lines.extend(["", "## Evidência consultada", ""])
    for evidence_id, detail in evidence_map.items():
        if isinstance(detail, EvidenceDetail):
            source, location, excerpt = detail.source, detail.location, detail.excerpt
            provenance = (
                f" Caracteres {detail.start}:{detail.end}; hash SHA-256 `{detail.source_hash}`; "
                f"consultado em {detail.retrieved_at}."
            )
            if detail.section:
                provenance = f" Secção: {detail.section}." + provenance
        else:
            source, location, excerpt = detail
            provenance = ""
        safe_excerpt = " ".join(excerpt.split())
        lines.append(f"- `{evidence_id}`: `{source}`, {location}. “{safe_excerpt}”{provenance}")
    lines.append("")
    return "\n".join(lines)


def _inline_html(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https://[^\s)]+)\)",
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        escaped,
    )
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _markdown_body_to_html(content: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(list_items) + "</ul>")
            list_items.clear()

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h1>{_inline_html(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{_inline_html(stripped[3:])}</h2>")
        elif stripped.startswith("> "):
            flush_paragraph()
            flush_list()
            blocks.append(f'<p class="notice">{_inline_html(stripped[2:])}</p>')
        elif stripped.startswith("- "):
            flush_paragraph()
            list_items.append(f"<li>{_inline_html(stripped[2:])}</li>")
        else:
            flush_list()
            paragraph.append(stripped)
    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def render_html(
    brief: Brief,
    evidence_map: Mapping[str, EvidenceDetail | tuple[str, str, str]],
) -> str:
    """Render a self-contained, escaped HTML version of the Markdown brief."""
    markdown = render_markdown(brief, evidence_map)
    return render_html_document(brief.title, markdown)


def render_html_document(title: str, markdown: str) -> str:
    """Wrap escaped Markdown as a small, self-contained HTML report."""
    safe_title = html.escape(title, quote=True)
    body = _markdown_body_to_html(markdown)
    return f"""<!doctype html>
<html lang="pt-PT">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
  <title>{safe_title} | CallBrief</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #182630; background: #f2f5f7; }}
    body {{ max-width: 880px; margin: 0 auto; padding: 2rem 1.25rem 4rem; line-height: 1.65; }}
    main {{ background: #fff; padding: clamp(1.25rem, 5vw, 3rem); border: 1px solid #dbe3e8; border-radius: 12px; }}
    h1, h2 {{ line-height: 1.2; color: #14354a; }}
    h1 {{ font-size: clamp(1.8rem, 4vw, 2.5rem); }}
    h2 {{ margin-top: 2rem; font-size: 1.25rem; }}
    .notice {{ padding: 1rem; border-left: 4px solid #c38a26; background: #fbf6e8; }}
    li {{ margin: .4rem 0; }}
    code {{ padding: .1rem .3rem; background: #edf2f5; border-radius: 4px; overflow-wrap: anywhere; }}
    @media print {{ :root {{ background: #fff; }} body {{ max-width: none; padding: 0; }} main {{ border: 0; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>
"""


def render_opportunity_markdown(
    opportunity: Opportunity,
    eligibility: EligibilityAssessment,
    fit: FitAssessment,
    *,
    variants: tuple[Opportunity, ...] = (),
) -> str:
    """Render a normalized call assessment with reasons and evidence provenance."""
    state_labels = {
        EligibilityState.ELIGIBLE: "elegível",
        EligibilityState.INELIGIBLE: "não elegível",
        EligibilityState.REMEDIABLE: "remediável",
        EligibilityState.UNCERTAIN: "incerto",
    }
    status_labels = {
        "upcoming": "a abrir",
        "open": "aberto",
        "closed": "encerrado",
        "unknown": "desconhecido",
    }
    component_labels = {
        "eligibility": "Elegibilidade",
        "strategic_fit": "Adequação estratégica",
        "probability_of_winning": "Probabilidade de sucesso",
        "funding_attractiveness": "Atratividade do financiamento",
        "application_effort": "Esforço de candidatura",
        "time_to_deadline": "Tempo até ao prazo",
        "company_capabilities": "Capacidades da entidade",
        "consortium_readiness": "Preparação do consórcio",
        "maturity": "Maturidade do projeto",
        "evidence_completeness": "Completude da evidência",
    }
    opportunity_type_labels = {
        "grant": "subvenção não reembolsável",
        "repayable_incentive": "incentivo reembolsável",
        "loan": "empréstimo",
        "guarantee": "garantia",
        "tax_incentive": "incentivo fiscal",
        "financial_instrument": "instrumento financeiro",
        "public_programme": "programa público",
        "tender": "concurso público",
        "unknown": "desconhecido",
    }

    def amount(value: object) -> str:
        if value is None:
            return "Desconhecido"
        raw = format(value, "f")
        whole, separator, fraction = raw.partition(".")
        groups: list[str] = []
        while whole:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        formatted = " ".join(groups)
        return f"{formatted},{fraction}" if separator else formatted

    deadline = opportunity.deadline.strftime("%d/%m/%Y") if opportunity.deadline else "Desconhecido"
    status = status_labels[opportunity.status.value]
    title = opportunity.title or opportunity.id
    overall = "N/A" if fit.overall is None else f"{fit.overall}/100"
    effort = next(
        (item.score for item in fit.components if item.name == "application_effort"), None
    )
    effort_label = f"{effort}/100" if effort is not None else "Sem dados"
    aid_intensity = (
        f"{amount(opportunity.aid_intensity * 100)}%"
        if opportunity.aid_intensity is not None
        else "Desconhecida"
    )
    lines = [
        f"# {title}",
        "",
        f"**Programa:** {opportunity.programme or 'Desconhecido'}",
        f"**Entidade responsável:** {opportunity.authority or 'Desconhecida'}",
        f"**Prazo:** {deadline}",
        f"**Estado:** {status}",
        f"**Tipo de apoio:** {opportunity_type_labels[opportunity.opportunity_type.value]}",
        "",
        "## Elegibilidade",
        "",
        f"**Estado:** {state_labels[eligibility.state]}",
        "",
    ]
    for finding in eligibility.findings:
        citations = ", ".join(f"`{item}`" for item in finding.evidence_ids) or "sem evidência"
        lines.append(
            f"- **{state_labels[finding.state]}:** {finding.requirement}. {finding.reason} "
            f"({citations})"
        )
        if finding.remediation_if_any:
            lines.append(f"  - Remediação: {finding.remediation_if_any}")
    lines.extend(["", "## Adequação", "", f"**Pontuação global:** {overall}", ""])
    for component in fit.components:
        score = "Sem dados" if component.score is None else f"{component.score}/100"
        lines.append(
            f"- **{component_labels.get(component.name, component.name)}:** {score} "
            f"(peso {component.weight:.0%}). {component.explanation}"
        )
    lines.extend(["", "## Porque pode adequar-se", ""])
    positive = [
        item
        for item in fit.components
        if item.name != "eligibility" and item.score is not None and item.score >= 70
    ]
    if positive:
        lines.extend(
            f"- {component_labels.get(item.name, item.name)}: {item.score}/100."
            for item in positive
        )
    else:
        lines.append(
            "- Ainda não há componentes suficientes para confirmar uma correspondência forte."
        )
    lines.extend(["", "## Porque pode não adequar-se", ""])
    negative = [
        item
        for item in fit.components
        if item.name != "eligibility" and item.score is not None and item.score < 50
    ]
    if negative:
        lines.extend(
            f"- {component_labels.get(item.name, item.name)}: {item.score}/100."
            for item in negative
        )
    if eligibility.state is not EligibilityState.ELIGIBLE:
        lines.append(f"- A elegibilidade está {state_labels[eligibility.state]}.")
    if not negative and eligibility.state is EligibilityState.ELIGIBLE:
        lines.append("- Não há bloqueios determinísticos nos requisitos normalizados.")
    lines.extend(
        [
            "",
            "## Financiamento e esforço",
            "",
            f"- Montante máximo: {amount(opportunity.funding_max)}.",
            f"- Intensidade de apoio: {aid_intensity}.",
            f"- Esforço de candidatura: {effort_label}.",
            f"- Prazo: {deadline}.",
            "",
            "## Evidência",
            "",
        ]
    )
    if opportunity.evidence:
        for reference in opportunity.evidence:
            section = f", secção {reference.section}" if reference.section else ""
            lines.append(
                f"- `{reference.evidence_id}`: [{reference.source_id}]({reference.url}){section}, "
                f"caracteres {reference.start}:{reference.end}, consultado em "
                f"{reference.retrieved_at.isoformat()}, hash SHA-256 `{reference.source_hash}`. "
                f"“{' '.join(reference.excerpt.split())}”"
            )
    else:
        lines.append("- Não há evidência de origem ligada a este registo.")
    if variants:
        variant_fields = (
            ("title", "título"),
            ("programme", "programa"),
            ("authority", "entidade responsável"),
            ("call_id", "identificador do aviso"),
            ("status", "estado"),
            ("opportunity_type", "tipo de financiamento"),
            ("publication_date", "data de publicação"),
            ("opening_date", "data de abertura"),
            ("deadline", "prazo"),
            ("additional_deadlines", "prazos adicionais"),
            ("budget_total", "orçamento total"),
            ("funding_min", "apoio mínimo"),
            ("funding_max", "apoio máximo"),
            ("aid_intensity", "intensidade de apoio"),
            ("project_cost_min", "custo mínimo do projeto"),
            ("project_cost_max", "custo máximo do projeto"),
            ("trl_min", "TRL mínimo"),
            ("trl_max", "TRL máximo"),
            ("consortium_rules", "regras de consórcio"),
            ("project_duration", "duração do projeto"),
            ("documents", "documentos exigidos"),
            ("geography", "geografia"),
            ("eligible_regions", "regiões elegíveis"),
            ("eligible_applicant_types", "beneficiários"),
            ("eligible_company_sizes", "dimensão da entidade"),
            ("eligible_sectors", "setores elegíveis"),
            ("eligible_activities", "atividades elegíveis"),
            ("excluded_activities", "atividades excluídas"),
            ("financial_requirements", "requisitos financeiros"),
            ("other_hard_requirements", "outros requisitos obrigatórios"),
            ("eligibility_rules", "regras de elegibilidade"),
        )

        def display(value: object) -> str:
            if value is None:
                return "desconhecido"
            if isinstance(value, (date, datetime)):
                return value.strftime("%d/%m/%Y")
            if isinstance(value, tuple):
                return "; ".join(display(item) for item in value)
            if isinstance(value, EligibilityRule):
                operator = value.operator.value
                expected = display(value.expected)
                return f"{value.reason} ({value.profile_field} {operator} {expected})"
            if isinstance(value, Enum):
                return str(value.value)
            return str(value)

        def same_value(field_name: str, canonical: object, source_variant: object) -> bool:
            if field_name != "eligibility_rules":
                return canonical == source_variant

            def semantics(value: object) -> tuple[tuple[object, ...], ...]:
                if not isinstance(value, tuple):
                    return ()
                return tuple(
                    (
                        rule.rule_id,
                        rule.profile_field,
                        rule.operator,
                        rule.expected,
                        rule.reason,
                        rule.hard_gate,
                        rule.remediation_if_any,
                    )
                    for rule in value
                    if isinstance(rule, EligibilityRule)
                )

            return semantics(canonical) == semantics(source_variant)

        lines.extend(["", "## Outras fontes do mesmo aviso", ""])
        for variant in variants:
            variant_url = variant.canonical_url or (
                variant.evidence[0].url if variant.evidence else None
            )
            source_label = (
                f"[{variant.source_id}]({variant_url})" if variant_url else variant.source_id
            )
            lines.append(
                f"- {source_label}"
                f": {variant.title or variant.id}. Consultada em "
                f"{variant.source_retrieved_at.strftime('%d/%m/%Y')}."
            )
            differences = [
                f"{label}: {display(getattr(opportunity, name))} / "
                f"{display(getattr(variant, name))}"
                for name, label in variant_fields
                if not same_value(name, getattr(opportunity, name), getattr(variant, name))
            ]
            if differences:
                lines.extend(
                    f"  - Divergência, canónico / variante: {item}." for item in differences
                )
            else:
                lines.append(
                    "  - Não foram detetadas divergências nos campos materiais apresentados."
                )
            if variant.evidence:
                lines.append("  - Evidência da variante:")
                for reference in variant.evidence:
                    section = reference.section or "sem secção"
                    lines.append(
                        f"    - `{reference.evidence_id}`, {section}, caracteres "
                        f"{reference.start}:{reference.end}, hash SHA-256 "
                        f"`{reference.source_hash}`."
                    )
    lines.extend(["", "## Próximos passos", ""])
    if eligibility.state is EligibilityState.UNCERTAIN:
        lines.append("- Confirmar os requisitos que ainda não foram normalizados ou comprovados.")
    for finding in eligibility.findings:
        if finding.remediation_if_any:
            lines.append(f"- Avaliar a remediação: {finding.remediation_if_any}")
    lines.append("- Rever o aviso e as condições atuais na fonte oficial antes de decidir.")
    lines.extend(["", "## Questões em aberto", ""])
    uncertain = [item for item in eligibility.findings if item.state is EligibilityState.UNCERTAIN]
    lines.extend(f"- {item.requirement}: {item.reason}" for item in uncertain)
    if not uncertain:
        lines.append("- Não há questões de elegibilidade por esclarecer nas regras normalizadas.")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, content: str, *, force: bool = False) -> None:
    destination = Path(path)
    if destination.is_symlink():
        raise ReportError("Refusing to replace a symlink")
    if destination.exists() and not force:
        raise ReportError(f"Report already exists: {destination}; pass --force to replace it")
    if destination.exists() and not destination.is_file():
        raise ReportError("Report destination must be a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if destination.exists() and not force:
            raise ReportError(f"Report already exists: {destination}; pass --force to replace it")
        os.replace(temporary_name, destination)
    except OSError as exc:
        raise ReportError("Could not write the report") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
