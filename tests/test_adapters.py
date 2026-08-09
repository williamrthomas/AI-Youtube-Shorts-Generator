"""Adapter fixture tests: parse saved pages with no network (§19.2)."""

from __future__ import annotations

import pytest

from mpvf.acquisition.dom import embedded_json_blocks, parse_html
from mpvf.adapters.base import SelectorFailure
from mpvf.adapters.brokerage import SourceDomains, extract_jsonld_listing
from mpvf.adapters.fixture import FixtureAdapter
from mpvf.adapters.zillow import parser
from mpvf.models.domain import DiscoveryQuery


class TestDom:
    def test_selects_by_tag_class_and_attribute(self):
        document = parse_html(
            '<div class="a b"><span data-test="x">hello</span><span>world</span></div>'
        )
        assert document.select_one("span[data-test=x]").text() == "hello"
        assert len(document.select("div.a span")) == 2
        assert document.select_one("div.missing") is None

    def test_ignores_script_text(self):
        document = parse_html("<div>visible<script>var hidden = 1;</script></div>")
        assert "hidden" not in document.text()

    def test_extracts_embedded_json(self, zillow_html):
        blocks = embedded_json_blocks(zillow_html("listing-active.html"))
        assert "__NEXT_DATA__" in blocks


class TestZillowSearchParsing:
    def test_extracts_every_visible_card(self, zillow_html):
        cards = parser.extract_cards(zillow_html("search-results.html"), "https://zillow.test/me/")
        assert len(cards) == 6

    def test_card_fields_survive_parsing(self, zillow_html):
        cards = parser.extract_cards(zillow_html("search-results.html"), "https://zillow.test/me/")
        observation = parser.parse_card(cards[0])
        assert observation.price == 895000
        assert observation.city == "Boothbay Harbor"
        assert observation.beds == 3
        assert observation.square_feet == 2140
        assert observation.status == "active"
        assert observation.source_property_id == "2077310001"
        assert "Sotheby" in (observation.brokerage_name or "")

    def test_land_and_pending_cards_are_parsed_not_dropped(self, zillow_html):
        """Filtering is the selection stage's job, not the adapter's."""

        cards = parser.extract_cards(zillow_html("search-results.html"), "https://zillow.test/me/")
        observations = [parser.parse_card(card) for card in cards]
        statuses = {observation.status for observation in observations}
        types = {observation.property_type for observation in observations}
        assert "pending" in statuses
        assert "land" in types

    def test_empty_results_page_is_not_a_failure(self, zillow_html):
        assert parser.extract_cards(zillow_html("empty-results.html"), "https://zillow.test/") == []

    def test_changed_layout_raises_selector_failure(self, zillow_html):
        with pytest.raises(SelectorFailure):
            parser.extract_cards(zillow_html("changed-layout.html"), "https://zillow.test/")

    def test_challenge_page_is_detected_and_never_bypassed(self, zillow_html):
        html = zillow_html("challenge.html")
        assert parser.detect_challenge(html) is not None
        with pytest.raises(SelectorFailure) as excinfo:
            parser.extract_cards(html, "https://zillow.test/")
        assert "challenge" in excinfo.value.selector


class TestZillowListingParsing:
    def test_reads_hydration_json_in_preference_to_dom(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-active.html"),
            "https://zillow.test/homedetails/2077310001_zpid/",
        )
        assert observation.price == 895000
        assert observation.year_built == 1901
        assert observation.mls_number == "1588421"
        assert observation.agent_name == "Sarah Whitten"
        assert observation.latitude == pytest.approx(43.8489)

    def test_extracts_waterfront_facts_from_prose(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-active.html"), "https://zillow.test/x_zpid/"
        )
        assert observation.waterfront.owned_frontage_feet == 190
        assert observation.waterfront.water_body == "Linekin Bay"
        assert observation.seasonal is False

    def test_pending_listing_keeps_its_status(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-pending.html"), "https://zillow.test/y_zpid/"
        )
        assert observation.status == "pending"

    def test_sold_listing_keeps_its_status(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-sold.html"), "https://zillow.test/z_zpid/"
        )
        assert observation.status == "sold"

    def test_land_listing_is_typed_as_land(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-land.html"), "https://zillow.test/l_zpid/"
        )
        assert observation.property_type == "land"
        assert observation.acres == 12.5

    def test_missing_fields_do_not_crash_the_parser(self, zillow_html):
        observation = parser.parse_listing_html(
            zillow_html("listing-missing-fields.html"), "https://zillow.test/m_zpid/"
        )
        assert observation.price is None
        assert observation.beds is None


class TestBrokerageAdapter:
    def test_domain_allow_list_is_enforced(self):
        domains = SourceDomains(brokerages=["legacysir.com"], portals=[], blocked=["spam.test"])
        assert domains.is_allowed("https://www.legacysir.com/listing/1")
        assert not domains.is_allowed("https://other.test/listing/1")
        assert not domains.is_allowed("https://spam.test/listing/1")

    def test_reads_schema_org_json_ld(self):
        html = """
        <script type="application/ld+json">
        {"@type":"SingleFamilyResidence","name":"18 Linekin Rd",
         "address":{"streetAddress":"18 Linekin Rd","addressLocality":"Boothbay Harbor",
                    "addressRegion":"ME","postalCode":"04538"},
         "yearBuilt":1901,
         "offers":{"price":895000,"availability":"https://schema.org/InStock"}}
        </script>"""
        fields = extract_jsonld_listing(html)
        assert fields["price"] == 895000
        assert fields["city"] == "Boothbay Harbor"
        assert fields["year_built"] == 1901


class TestFixtureAdapter:
    def test_discovers_every_maine_listing(self, fixture_dir):
        adapter = FixtureAdapter(fixture_dir / "listings")
        cards = adapter.discover(DiscoveryQuery(template_slug="t", search_urls=[]))
        assert len(cards) == 8

    def test_resolves_verification_by_mls(self, fixture_dir, observations):
        adapter = FixtureAdapter(fixture_dir / "listings")
        resolved = adapter.resolve(observations[0])
        assert resolved is not None
        assert resolved.mls_number == observations[0].mls_number

    def test_healthcheck_reports_fixture_count(self, fixture_dir):
        health = FixtureAdapter(fixture_dir / "listings").healthcheck()
        assert health.available
        assert "json fixtures" in health.detail
