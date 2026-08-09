"""Address normalization, field parsing and deduplication (§19.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mpvf.normalization.address import (
    addresses_match,
    display_address,
    parse_address,
    token_similarity,
)
from mpvf.normalization.dedup import build_property_records, cluster_observations, price_history
from mpvf.normalization.listing import (
    build_observation,
    detect_waterfront,
    is_seasonal,
    looks_like_auction,
    parse_acres,
    parse_hoa,
    parse_price,
    parse_property_type,
    parse_square_feet,
    parse_status,
    parse_year_built,
)


class TestAddress:
    def test_parses_a_single_line(self):
        address = parse_address("18 Linekin Rd, Boothbay Harbor, ME 04538")
        assert address.street == "18 linekin rd"
        assert address.city == "boothbay harbor"
        assert address.state == "ME"
        assert address.postal_code == "04538"

    def test_normalizes_suffixes_and_directions(self):
        left = parse_address("402 North Shore Road, Cape Elizabeth, ME")
        right = parse_address("402 N Shore Rd, Cape Elizabeth, ME")
        assert left.canonical == right.canonical

    def test_extracts_a_unit(self):
        address = parse_address("12 Ocean Ave Unit 3, Ogunquit, ME 03907")
        assert address.unit == "3"
        assert "unit 3" in address.canonical

    def test_unit_difference_defeats_a_match(self):
        left = parse_address("12 Ocean Ave Unit 3, Ogunquit, ME")
        right = parse_address("12 Ocean Ave Unit 4, Ogunquit, ME")
        assert not addresses_match(left, right)

    def test_same_property_across_sources_matches(self):
        left = parse_address("9 Granite Point, Stonington, ME 04681")
        right = parse_address(None, street="9 Granite Pt", city="Stonington", state="ME")
        assert addresses_match(left, right)

    def test_different_towns_never_match(self):
        left = parse_address("5 Main St, Camden, ME")
        right = parse_address("5 Main St, Rockport, ME")
        assert not addresses_match(left, right)

    def test_key_is_stable_and_short(self):
        address = parse_address("18 Linekin Rd, Boothbay Harbor, ME 04538")
        assert address.key == parse_address("18 LINEKIN ROAD, Boothbay Harbor, ME 04538").key
        assert len(address.key) == 20

    def test_display_is_human_readable(self):
        address = parse_address("18 linekin rd, boothbay harbor, me 04538")
        assert display_address(address) == "18 Linekin Rd, Boothbay Harbor, ME 04538"

    def test_similarity_is_bounded(self):
        assert token_similarity("18 linekin rd", "18 linekin rd") == 1.0
        assert token_similarity("", "anything") == 0.0


class TestFieldParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$895,000", 895000),
            ("895000", 895000),
            ("$1.25M", 1250000),
            ("850K", 850000),
            ("", None),
            ("Contact agent", None),
            (0, None),
        ],
    )
    def test_price(self, raw, expected):
        assert parse_price(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2,140 sqft", 2140), ("2140", 2140), ("12", None), ("900000", None), (None, None)],
    )
    def test_square_feet_rejects_implausible_values(self, raw, expected):
        assert parse_square_feet(raw) == expected

    def test_acres_converts_square_feet(self):
        assert parse_acres("43560 sq ft") == 1.0
        assert parse_acres("3.4 acres") == 3.4

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("House for sale", "active"),
            ("Pending", "pending"),
            ("Active under contract", "active_under_contract"),
            ("Accepting backup offers", "contingent"),
            ("Sold", "sold"),
            ("Off market", "off_market"),
            ("", "unknown"),
        ],
    )
    def test_status(self, raw, expected):
        assert parse_status(raw) == expected

    def test_property_type_prefers_the_most_specific_match(self):
        assert parse_property_type("Condominium") == "condo"
        assert parse_property_type("Single Family") == "single_family"
        assert (
            parse_property_type(None, None, "A seasonal camp on the pond") == "seasonal_residence"
        )
        assert parse_property_type("Lot / Land") == "land"

    def test_year_built_rejects_nonsense(self):
        assert parse_year_built("1901") == 1901
        assert parse_year_built("19") is None
        assert parse_year_built("3025") is None

    def test_hoa_converts_annual_to_monthly(self):
        assert parse_hoa("$5,400/yr") == 450.0
        assert parse_hoa("$465") == 465.0

    def test_seasonal_detection_is_three_valued(self):
        assert is_seasonal("Not winterized, seasonal use") is True
        assert is_seasonal("The property is winterized for year-round use") is False
        assert is_seasonal("A house on the water") is None

    def test_auction_detection(self):
        assert looks_like_auction("Auction: bidding closes Friday")
        assert not looks_like_auction("A quiet house")


class TestWaterfront:
    def test_reads_owned_frontage_and_body(self):
        facts = detect_waterfront(
            "Approximately 190 feet of owned water frontage on Linekin Bay with deeded access."
        )
        assert facts.owned_frontage_feet == 190
        assert facts.water_body == "Linekin Bay"
        assert facts.deeded_access is True

    def test_classifies_frontage_type(self):
        assert detect_waterfront("Direct oceanfront home").frontage_type == "ocean"
        assert detect_waterfront("Lakefront camp").frontage_type == "lake"
        assert detect_waterfront("A house in town").frontage_type == "unknown"

    def test_detects_a_view_without_frontage(self):
        facts = detect_waterfront("Ocean view from every room")
        assert facts.water_view is True
        assert facts.owned_frontage_feet is None


class TestDeduplication:
    def _observation(self, source: str, **fields):
        payload = {
            "address": "18 Linekin Rd, Boothbay Harbor, ME 04538",
            "price": 895000,
            "status": "For sale",
            **fields,
        }
        return build_observation(source, payload, "https://example.test")

    def test_same_mls_collapses_across_sources(self):
        left = self._observation("zillow", mls_number="1588421")
        right = self._observation(
            "brokerage", mls_number="1588421", address="18 Linekin Road, Boothbay Harbor ME"
        )
        clusters = cluster_observations([left, right])
        assert len(clusters) == 1

    def test_different_properties_stay_apart(self):
        left = self._observation("zillow", mls_number="1")
        right = self._observation(
            "zillow", mls_number="2", address="402 Shore Rd, Cape Elizabeth, ME"
        )
        assert len(cluster_observations([left, right])) == 2

    def test_canonical_prefers_the_active_verification_read(self):
        stale = self._observation("zillow", mls_number="9", status="Pending")
        fresh = self._observation("brokerage", mls_number="9", status="For sale", beds=3)
        records = build_property_records([stale, fresh])
        assert len(records) == 1
        assert records[0].canonical.status == "active"
        assert records[0].canonical.beds == 3

    def test_canonical_merges_missing_fields_and_images(self):
        sparse = self._observation("zillow", mls_number="7", image_urls=["https://a.test/1.jpg"])
        rich = self._observation(
            "brokerage", mls_number="7", year_built=1901, image_urls=["https://a.test/2.jpg"]
        )
        record = build_property_records([sparse, rich])[0]
        assert record.canonical.year_built == 1901
        assert record.canonical.usable_image_count() == 2

    def test_price_history_is_preserved_across_relists(self):
        first = self._observation("zillow", mls_number="5", price=1150000)
        first.captured_at = datetime.now(UTC) - timedelta(days=30)
        second = self._observation("zillow", mls_number="5", price=985000)
        record = build_property_records([first, second])[0]
        history = price_history(record)
        assert [price for _stamp, price in history] == [1150000, 985000]

    def test_builds_records_from_the_full_fixture(self, observations):
        records = build_property_records(observations)
        assert len(records) == len(observations)
        assert all(record.state == "ME" for record in records)
