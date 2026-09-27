"""Validated domain models used by the agent and report renderer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ModelValidationError(ValueError):
    """Raised when a model response does not match the brief contract."""


class FitBand(StrEnum):
    STRONG = "forte"
    POSSIBLE = "possível"
    WEAK = "fraco"
    INSUFFICIENT_EVIDENCE = "evidência insuficiente"


class RequirementStatus(StrEnum):
    MEETS = "cumpre"
    DOES_NOT_MEET = "não cumpre"
    UNCONFIRMED = "não confirmado"


@dataclass(frozen=True, slots=True)
class Claim:
    statement: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Requirement:
    requirement: str
    status: RequirementStatus
    note: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Brief:
    title: str
    summary: Claim
    fit_band: FitBand
    fit_rationale: Claim
    requirements: tuple[Requirement, ...]
    deadlines: tuple[Claim, ...]
    risks: tuple[Claim, ...]
    open_questions: tuple[str, ...]
    next_steps: tuple[str, ...]


def _text(value: Any, label: str, *, maximum: int = 1200) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{label} must be text")
    result = value.strip()
    if not result or len(result) > maximum:
        raise ModelValidationError(f"{label} must contain between 1 and {maximum} characters")
    return result


def _string_list(value: Any, label: str, *, maximum_items: int = 12) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ModelValidationError(f"{label} must be a list with at most {maximum_items} items")
    return tuple(_text(item, label, maximum=500) for item in value)


def _evidence_ids(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 12:
        raise ModelValidationError(f"{label} must cite between 1 and 12 evidence items")
    ids = tuple(_text(item, label, maximum=80) for item in value)
    if len(set(ids)) != len(ids):
        raise ModelValidationError(f"{label} contains duplicate evidence ids")
    return ids


def _claim(value: Any, label: str) -> Claim:
    if not isinstance(value, dict):
        raise ModelValidationError(f"{label} must be an object")
    return Claim(
        statement=_text(value.get("statement"), f"{label}.statement"),
        evidence_ids=_evidence_ids(value.get("evidence_ids"), f"{label}.evidence_ids"),
    )


def brief_from_payload(value: Any) -> Brief:
    """Convert untrusted model JSON into an immutable, bounded domain object."""
    if not isinstance(value, dict):
        raise ModelValidationError("brief must be an object")
    try:
        fit_band = FitBand(_text(value.get("fit_band"), "fit_band", maximum=40))
    except ValueError as exc:
        raise ModelValidationError("fit_band is not supported") from exc

    raw_requirements = value.get("requirements")
    if not isinstance(raw_requirements, list) or len(raw_requirements) > 30:
        raise ModelValidationError("requirements must be a list with at most 30 items")
    requirements: list[Requirement] = []
    for index, raw in enumerate(raw_requirements):
        if not isinstance(raw, dict):
            raise ModelValidationError(f"requirements[{index}] must be an object")
        try:
            status = RequirementStatus(_text(raw.get("status"), "requirement.status", maximum=40))
        except ValueError as exc:
            raise ModelValidationError(f"requirements[{index}].status is not supported") from exc
        requirements.append(
            Requirement(
                requirement=_text(raw.get("requirement"), "requirement.requirement", maximum=300),
                status=status,
                note=_text(raw.get("note"), "requirement.note", maximum=1000),
                evidence_ids=_evidence_ids(raw.get("evidence_ids"), "requirement.evidence_ids"),
            )
        )

    raw_deadlines = value.get("deadlines", [])
    raw_risks = value.get("risks", [])
    if not isinstance(raw_deadlines, list) or len(raw_deadlines) > 20:
        raise ModelValidationError("deadlines must be a list with at most 20 items")
    if not isinstance(raw_risks, list) or len(raw_risks) > 20:
        raise ModelValidationError("risks must be a list with at most 20 items")

    return Brief(
        title=_text(value.get("title"), "title", maximum=200),
        summary=_claim(value.get("summary"), "summary"),
        fit_band=fit_band,
        fit_rationale=_claim(value.get("fit_rationale"), "fit_rationale"),
        requirements=tuple(requirements),
        deadlines=tuple(_claim(item, "deadline") for item in raw_deadlines),
        risks=tuple(_claim(item, "risk") for item in raw_risks),
        open_questions=_string_list(value.get("open_questions", []), "open_questions"),
        next_steps=_string_list(value.get("next_steps", []), "next_steps"),
    )
