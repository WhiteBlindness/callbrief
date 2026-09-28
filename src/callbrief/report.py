"""Readable, evidence-linked Markdown report output."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .models import Brief, Claim


class ReportError(RuntimeError):
    """Raised when a report cannot be written safely."""


def _render_claim(claim: Claim) -> str:
    citations = ", ".join(f"`{evidence_id}`" for evidence_id in claim.evidence_ids)
    return f"{claim.statement} ({citations})"


def render_markdown(brief: Brief, evidence_map: dict[str, tuple[str, str, str]]) -> str:
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
    for evidence_id, (source, location, excerpt) in evidence_map.items():
        safe_excerpt = " ".join(excerpt.split())
        lines.append(f"- `{evidence_id}`: `{source}`, {location}. “{safe_excerpt}”")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, content: str, *, force: bool = False) -> None:
    destination = Path(path)
    if destination.exists() and not force:
        raise ReportError(f"Report already exists: {destination}; pass --force to replace it")
    if destination.exists() and destination.is_symlink():
        raise ReportError("Refusing to replace a symlink")
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
