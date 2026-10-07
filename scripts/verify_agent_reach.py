"""Run a local contract check for an optional Agent-Reach bridge."""

from __future__ import annotations

import argparse
import importlib
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from callbrief.normalization import normalize_source_document
from callbrief.sources import AgentReachBridge, AgentReachSourceAdapter, SourceError

DEFAULT_FACTORY = "agent_reach_callbrief_bridge:create_bridge"


def _load_factory(specification: str) -> Callable[[], AgentReachBridge]:
    module_name, separator, attribute = specification.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("Factory must use module_name:callable_name format")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ValueError("Configured bridge factory is not callable")
    return factory


def verify_bridge(
    bridge: AgentReachBridge,
    *,
    allowed_hosts: Iterable[str],
    query: str,
    limit: int,
) -> dict[str, Any]:
    adapter = AgentReachSourceAdapter(bridge, allowed_hosts=allowed_hosts)
    documents = adapter.fetch(query, limit=limit)
    if not documents:
        raise ValueError("The bridge returned no approved official source documents")

    records: list[dict[str, Any]] = []
    rejected = 0
    for document in documents:
        try:
            opportunity = normalize_source_document(document)
        except (TypeError, ValueError):
            rejected += 1
            continue
        if (
            opportunity.source_id != adapter.source_id
            or not opportunity.title
            or not opportunity.evidence
            or not re.fullmatch(r"[a-f0-9]{64}", document.content_hash)
        ):
            rejected += 1
            continue
        records.append(
            {
                "source_id": opportunity.source_id,
                "source_record_id": opportunity.source_record_id,
                "call_id": opportunity.call_id,
                "title": opportunity.title,
                "status": opportunity.status.value,
                "source_url": document.source_url,
                "evidence_count": len(opportunity.evidence),
                "provenance_status": document.provenance_status.value,
                "source_payload_sha256": document.source_payload_sha256,
            }
        )

    if not records or rejected:
        raise ValueError(
            f"Bridge normalization failed for {rejected} of {len(documents)} returned documents"
        )
    return {
        "contract": "Agent-Reach -> SourceDocument -> CallBrief Opportunity",
        "query": query,
        "documents_returned": len(documents),
        "opportunities_normalized": len(records),
        "normalization_status": "passed",
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a local Agent-Reach bridge contract.")
    parser.add_argument("--factory", default=DEFAULT_FACTORY, help="module:factory callable")
    parser.add_argument("--allowed-host", action="append", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        if not args.query.strip() or not 1 <= args.limit <= 100:
            raise ValueError("Query and limit are invalid")
        bridge = _load_factory(args.factory)()
        report = verify_bridge(
            bridge,
            allowed_hosts=args.allowed_host,
            query=args.query,
            limit=args.limit,
        )
    except (ImportError, AttributeError, TypeError, ValueError, SourceError) as exc:
        print(
            json.dumps(
                {"normalization_status": "failed", "error_code": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
