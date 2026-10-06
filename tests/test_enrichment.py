from __future__ import annotations

import unittest
from datetime import UTC, datetime

from callbrief.domain import EvidenceReference, OrganisationProfile
from callbrief.enrichment import EnrichmentFact, apply_enrichment, enrich_organisation

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _fact(profile_field: str, value: str) -> EnrichmentFact:
    excerpt = "Example public company description."
    return EnrichmentFact(
        profile_field=profile_field,
        value=value,
        provider_id="fixture-provider",
        retrieved_at=NOW,
        evidence=EvidenceReference(
            evidence_id=f"ev-{profile_field}",
            source_id="official-register",
            url="https://example.gov/company/1",
            retrieved_at=NOW,
            section="Company profile",
            start=0,
            end=len(excerpt),
            excerpt=excerpt,
            source_hash="b" * 64,
        ),
    )


class FixtureProvider:
    provider_id = "fixture-provider"

    def __init__(self, facts: tuple[EnrichmentFact, ...]) -> None:
        self.facts = facts
        self.received_vat: str | None = None

    def enrich(self, vat_number: str) -> tuple[EnrichmentFact, ...]:
        self.received_vat = vat_number
        return self.facts


class EnrichmentTests(unittest.TestCase):
    def test_provider_receives_only_vat_and_unknown_fields_gain_provenance(self) -> None:
        profile = OrganisationProfile(
            id="client-a",
            workspace_id="consultancy",
            vat_number="PT123456789",
            legal_name=None,
        )
        provider = FixtureProvider((_fact("legal_name", "Example Company"),))

        enriched = enrich_organisation(profile, provider)

        self.assertEqual(provider.received_vat, "PT123456789")
        self.assertEqual(enriched.legal_name, "Example Company")
        self.assertEqual(enriched.evidence[0].evidence_id, "ev-legal_name")

    def test_enrichment_does_not_replace_known_profile_facts(self) -> None:
        profile = OrganisationProfile(
            id="client-a",
            workspace_id="consultancy",
            legal_name="User-provided name",
        )

        enriched = apply_enrichment(profile, (_fact("legal_name", "Different public name"),))

        self.assertEqual(enriched.legal_name, "User-provided name")
        self.assertEqual(enriched.evidence, ())

    def test_unknown_field_or_provider_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "não pode ser enriquecido"):
            apply_enrichment(
                OrganisationProfile(id="client-a", workspace_id="consultancy"),
                (_fact("workspace_id", "other-workspace"),),
            )
        provider = FixtureProvider((_fact("legal_name", "Example Company"),))
        provider.provider_id = "different-provider"
        with self.assertRaisesRegex(ValueError, "proveniência"):
            enrich_organisation(
                OrganisationProfile(
                    id="client-a",
                    workspace_id="consultancy",
                    vat_number="PT123456789",
                ),
                provider,
            )


if __name__ == "__main__":
    unittest.main()
