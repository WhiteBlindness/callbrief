"""Deterministic evaluation metrics for synthetic call fixtures.

These functions calculate scores from supplied labels and observations. They do
not collect or imply measurements from real funding calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any

from .domain import EligibilityAssessment, Opportunity


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, (tuple, list)):
        return tuple(_normalise(item) for item in value)
    if isinstance(value, Mapping):
        return tuple(sorted((key, _normalise(item)) for key, item in value.items()))
    return value


def _record_values(record: Mapping[str, Any] | Opportunity) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    if isinstance(record, Opportunity) and is_dataclass(record):
        return {item.name: getattr(record, item.name) for item in fields(record)}
    raise TypeError("normalization records must be mappings or frozen Opportunity models")


def _eligibility_state(value: Any) -> Any:
    state = value.state if isinstance(value, EligibilityAssessment) else value
    return getattr(state, "value", state)


def _score(correct: int, available: int) -> float | None:
    return correct / available if available else None


def _same_length(expected: Sequence[Any], actual: Sequence[Any], metric: str) -> None:
    if len(expected) != len(actual):
        raise ValueError(f"{metric} needs equally sized expected and observed samples")


def normalization_score(
    expected: Mapping[str, Any] | Opportunity,
    observed: Mapping[str, Any] | Opportunity,
) -> dict[str, int | float | None]:
    """Compare common non-empty fields; unknown or absent values are excluded."""
    expected_values = _record_values(expected)
    observed_values = _record_values(observed)
    fields = tuple(
        field
        for field, value in expected_values.items()
        if value is not None
        and value != ""
        and field in observed_values
        and observed_values[field] is not None
        and observed_values[field] != ""
    )
    correct = sum(
        _normalise(expected_values[field]) == _normalise(observed_values[field]) for field in fields
    )
    return {"available": len(fields), "correct": correct, "score": _score(correct, len(fields))}


def recall_at_k(
    relevant_ids: Iterable[str], retrieved_ids: Sequence[str], k: int = 5
) -> dict[str, int | float | None]:
    """Return deterministic Recall@k, using unique relevant identifiers."""
    if k < 1:
        raise ValueError("k must be positive")
    relevant = set(relevant_ids)
    retrieved = set(retrieved_ids[:k]) & relevant
    return {
        "relevant": len(relevant),
        "retrieved": len(retrieved),
        "score": _score(len(retrieved), len(relevant)),
    }


def _pairs(values: Iterable[Sequence[str]]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for pair in values:
        if len(pair) != 2 or pair[0] == pair[1]:
            raise ValueError("duplicate pairs must contain two distinct identifiers")
        result.add(tuple(sorted((pair[0], pair[1]))))
    return result


def deduplication_precision(
    expected_duplicate_pairs: Iterable[Sequence[str]],
    predicted_duplicate_pairs: Iterable[Sequence[str]],
) -> dict[str, int | float | None]:
    """Measure the precision of predicted duplicate pairs."""
    expected = _pairs(expected_duplicate_pairs)
    predicted = _pairs(predicted_duplicate_pairs)
    correct = len(expected & predicted)
    return {
        "predicted": len(predicted),
        "correct": correct,
        "score": _score(correct, len(predicted)),
    }


def eligibility_accuracy(
    expected_states: Sequence[Any], observed_states: Sequence[Any]
) -> dict[str, int | float | None]:
    """Measure exact agreement for deterministic eligibility states."""
    _same_length(expected_states, observed_states, "eligibility accuracy")
    correct = sum(
        _eligibility_state(expected) == _eligibility_state(observed)
        for expected, observed in zip(expected_states, observed_states, strict=True)
    )
    return {
        "available": len(expected_states),
        "correct": correct,
        "score": _score(correct, len(expected_states)),
    }


def citation_validity(
    citation_ids: Sequence[str], available_evidence_ids: Iterable[str]
) -> dict[str, int | float | None]:
    """Measure how many emitted citation identifiers resolve to available evidence."""
    evidence = set(available_evidence_ids)
    valid = sum(citation_id in evidence for citation_id in citation_ids)
    return {
        "citations": len(citation_ids),
        "valid": valid,
        "score": _score(valid, len(citation_ids)),
    }


def tenant_isolation(
    expected_visible_by_tenant: Mapping[str, Iterable[str]],
    observed_visible_by_tenant: Mapping[str, Iterable[str]],
) -> dict[str, int | bool]:
    """Count observed records outside each tenant's explicitly allowed set."""
    tenants = set(expected_visible_by_tenant) | set(observed_visible_by_tenant)
    leaks = sum(
        len(
            set(observed_visible_by_tenant.get(tenant, ()))
            - set(expected_visible_by_tenant.get(tenant, ()))
        )
        for tenant in tenants
    )
    return {"tenants": len(tenants), "leaks": leaks, "isolated": leaks == 0}


def prompt_injection_resistance(
    expected_safe: Sequence[bool], observed_safe: Sequence[bool]
) -> dict[str, int | float | None]:
    """Measure exact agreement with hand-authored safe/unsafe fixture labels."""
    _same_length(expected_safe, observed_safe, "prompt-injection resistance")
    correct = sum(
        expected == observed
        for expected, observed in zip(expected_safe, observed_safe, strict=True)
    )
    return {
        "available": len(expected_safe),
        "safe": correct,
        "score": _score(correct, len(expected_safe)),
    }


def material_change_recall(
    expected_changes: Iterable[Mapping[str, Any]],
    detected_changes: Iterable[Mapping[str, Any]],
) -> dict[str, int | float | None]:
    """Measure whether expected material field changes were detected."""

    def key(change: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        try:
            return (change["call_id"], change["field"], _normalise(change["new_value"]))
        except KeyError as error:
            raise ValueError(f"change is missing required key: {error.args[0]}") from error

    expected = {key(change) for change in expected_changes}
    detected = {key(change) for change in detected_changes}
    correct = len(expected & detected)
    return {"changes": len(expected), "detected": correct, "score": _score(correct, len(expected))}
