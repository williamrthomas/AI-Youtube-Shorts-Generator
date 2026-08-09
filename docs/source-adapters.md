# Source adapters

## Contract

```python
class ListingSourceAdapter(Protocol):
    name: str
    version: str
    def healthcheck(self) -> SourceHealth: ...
    def discover(self, query: DiscoveryQuery) -> list[RawListingCard]: ...
    def fetch_listing(self, url: str) -> RawListingPage: ...
    def parse_card(self, raw: RawListingCard) -> ListingObservation: ...
    def parse_listing(self, raw: RawListingPage) -> ListingObservation: ...
```

Adapters are the only code that knows about a specific website. Everything
downstream consumes `ListingObservation`.

## Rules every adapter follows

1. **Never bypass an access control.** A login wall, CAPTCHA or automated-access
   challenge causes the adapter to save the page, mark the source unavailable,
   and stop. The run tries a configured fallback source or skips the episode.
   There is no stealth-proxy path, and there will not be one.
2. **Selectors live in a versioned file.** `adapters/zillow/selectors_v1.py`
   holds every site-specific string as an ordered fallback chain. A layout
   change is a one-file edit plus a fixture.
3. **Prefer hydration JSON to rendered DOM.** Listing sites ship their data in a
   script tag; reading it is more stable and gentler than scraping.
4. **Parsing is pure.** `parser.py` never touches the network, so CI covers
   every path against saved fixtures.
5. **A structure change fails loudly.** `SelectorFailure` carries the selector
   that produced nothing and the path to the saved HTML.
6. **Rate limits and delays are configured, not hard-coded**, and the daily
   search scope is limited to what enabled templates actually need.

## Fixtures

`tests/fixtures/zillow/` holds a sanitized page for each case the spec requires:
search results, active listing, pending, sold, land, missing fields, changed
layout, an access challenge, and an empty result set. CI parses all of them with
no network. A separate manual smoke test checks live pages.

## Adding a source

1. Create `adapters/<source>/selectors_v1.py` and `parser.py`.
2. Implement the adapter class; keep browser or HTTP work out of `parser.py`.
3. Save fixtures for all nine cases above and write parse tests.
4. Register it in `adapters/registry.py`.
5. Name it in a template's `discovery.adapter` or `discovery.fallback_adapters`.

## Verification resolution

Verification prefers a *different* source from discovery. The ladder, in order,
with confidence falling at each step:

1. canonical or brokerage link exposed on the listing
2. exact MLS-number search
3. exact address plus brokerage name
4. exact address restricted to allow-listed brokerage domains
5. a configured alternate portal

`config/source-domains.yaml` is the allow list. A domain that is not on it is
never fetched.
