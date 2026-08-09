"""Deduplication and canonical property construction (FR-021 .. FR-023)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from mpvf.models.domain import ListingObservation, PropertyRecord
from mpvf.normalization.address import NormalizedAddress, addresses_match, parse_address

STATUS_PRIORITY = {
    "active": 0,
    "active_under_contract": 1,
    "contingent": 2,
    "pending": 3,
    "sold": 4,
    "off_market": 5,
    "unknown": 6,
}

SOURCE_PRIORITY_DEFAULT = 50


@dataclass
class _Cluster:
    address: NormalizedAddress
    observations: list[ListingObservation] = field(default_factory=list)

    def keys(self) -> set[str]:
        keys: set[str] = set()
        for observation in self.observations:
            if observation.mls_number:
                keys.add(f"mls:{observation.mls_number.strip().lower()}")
            if observation.source_property_id:
                keys.add(f"{observation.source}:{observation.source_property_id}")
            if observation.listing_url:
                keys.add(f"url:{observation.listing_url.split('?')[0].rstrip('/').lower()}")
        return keys


def _address_of(observation: ListingObservation) -> NormalizedAddress:
    return parse_address(
        observation.raw_address,
        street=observation.street,
        city=observation.city,
        state=observation.state,
        postal_code=observation.postal_code,
    )


def cluster_observations(observations: list[ListingObservation]) -> list[list[ListingObservation]]:
    """Group observations that describe the same physical property.

    Matching order follows FR-021: source property id, MLS number, canonical
    URL, exact normalized address, then fuzzy address match.
    """

    clusters: list[_Cluster] = []
    index: dict[str, _Cluster] = {}

    for observation in observations:
        address = _address_of(observation)
        candidate_keys = set()
        if observation.mls_number:
            candidate_keys.add(f"mls:{observation.mls_number.strip().lower()}")
        if observation.source_property_id:
            candidate_keys.add(f"{observation.source}:{observation.source_property_id}")
        if observation.listing_url:
            candidate_keys.add(f"url:{observation.listing_url.split('?')[0].rstrip('/').lower()}")
        if address.canonical:
            candidate_keys.add(f"addr:{address.key}")

        target: _Cluster | None = None
        for key in candidate_keys:
            if key in index:
                target = index[key]
                break
        if target is None:
            for cluster in clusters:
                if address.canonical and addresses_match(cluster.address, address):
                    target = cluster
                    break
        if target is None:
            target = _Cluster(address=address)
            clusters.append(target)

        target.observations.append(observation)
        for key in candidate_keys | target.keys():
            index[key] = target
        if not target.address.canonical and address.canonical:
            target.address = address

    return [cluster.observations for cluster in clusters]


def _merge_observations(observations: list[ListingObservation]) -> ListingObservation:
    """Build the canonical observation: best status, richest facts.

    Verification-source values win over discovery values for the same field
    because verification is the authoritative read (FR-030).
    """

    ordered = sorted(
        observations,
        key=lambda obs: (
            STATUS_PRIORITY.get(obs.status, 9),
            0 if obs.source != "zillow" else 1,
            -(obs.captured_at.timestamp()),
        ),
    )
    canonical = ordered[0].model_copy(deep=True)

    scalar_fields = (
        "mls_number",
        "raw_address",
        "street",
        "city",
        "postal_code",
        "latitude",
        "longitude",
        "price",
        "previous_price",
        "beds",
        "baths",
        "square_feet",
        "acres",
        "year_built",
        "hoa_fee",
        "seasonal",
        "brokerage_name",
        "agent_name",
        "description_text",
        "listed_at",
        "listing_url",
    )
    for observation in ordered[1:]:
        for name in scalar_fields:
            if getattr(canonical, name, None) in (None, "") and getattr(observation, name, None):
                setattr(canonical, name, getattr(observation, name))
        for url in observation.image_urls:
            if url not in canonical.image_urls:
                canonical.image_urls.append(url)
        for key, value in observation.facts.items():
            canonical.facts.setdefault(key, value)
        if canonical.waterfront.frontage_type in ("unknown", "none"):
            if observation.waterfront.frontage_type not in ("unknown", "none"):
                canonical.waterfront = observation.waterfront
        elif canonical.waterfront.owned_frontage_feet is None:
            canonical.waterfront.owned_frontage_feet = observation.waterfront.owned_frontage_feet
    return canonical


def build_property_records(observations: list[ListingObservation]) -> list[PropertyRecord]:
    """Collapse observations into canonical property records."""

    records: list[PropertyRecord] = []
    for group in cluster_observations(observations):
        canonical = _merge_observations(group)
        address = _address_of(canonical)
        records.append(
            PropertyRecord(
                property_key=address.key,
                normalized_address=address.canonical,
                city=address.city.title() or None,
                state=address.state,
                postal_code=address.postal_code,
                latitude=canonical.latitude,
                longitude=canonical.longitude,
                observations=group,
                canonical=canonical,
                first_seen_at=min(obs.captured_at for obs in group),
                last_seen_at=max(obs.captured_at for obs in group),
            )
        )
    return records


def price_history(record: PropertyRecord) -> list[tuple[str, int]]:
    """Ordered ``(captured_at, price)`` history preserved across relists (FR-023)."""

    history: list[tuple[str, int]] = []
    seen: set[int] = set()
    for observation in sorted(record.observations, key=lambda obs: obs.captured_at):
        if observation.price and observation.price not in seen:
            history.append((observation.captured_at.isoformat(), observation.price))
            seen.add(observation.price)
    return history


def group_by_town(records: list[PropertyRecord]) -> dict[str, list[PropertyRecord]]:
    grouped: dict[str, list[PropertyRecord]] = defaultdict(list)
    for record in records:
        grouped[record.town].append(record)
    return dict(grouped)
