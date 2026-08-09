"""Zillow selectors, version 1 (FR-013).

Every site-specific string lives here so that a layout change is a one-file
edit plus a fixture, not a hunt through the pipeline. Selectors are ordered
fallback chains: the first one that yields text wins.
"""

from __future__ import annotations

VERSION = "zillow-selectors-v1"

# Search results page ------------------------------------------------------
RESULT_LIST = ["ul[class*=photo-cards]", "div#grid-search-results", "div[id=search-page-list]"]
RESULT_CARD = ["article[data-test=property-card]", "article[class*=property-card]", "li article"]

CARD_LINK = ["a[data-test=property-card-link]", "a[class*=property-card-link]", "a[href]"]
CARD_PRICE = [
    "span[data-test=property-card-price]",
    "div[class*=PropertyCardPrice] span",
    "span[class*=price]",
]
CARD_ADDRESS = [
    "address[data-test=property-card-addr]",
    "address",
    "a[data-test=property-card-link] address",
]
CARD_DETAILS = [
    "ul[class*=StyledPropertyCardHomeDetailsList] li",
    "ul[class*=property-card-details] li",
]
CARD_STATUS = [
    "div[class*=StatusText]",
    "span[data-test=property-status]",
    "div[class*=property-card-status]",
]
CARD_BROKER = [
    "div[class*=StyledPropertyCardDataArea--broker]",
    "div[class*=broker]",
    "span[class*=attribution]",
]
CARD_IMAGE = ["img[src]", "source[srcset]"]

# Listing detail page ------------------------------------------------------
DETAIL_PRICE = ["span[data-testid=price]", "div[data-testid=price] span", "span[class*=Price]"]
DETAIL_ADDRESS = ["h1[class*=address]", "h1", "div[data-testid=home-details-chip] h1"]
DETAIL_STATUS = ["span[data-testid=home-status]", "div[class*=home-status]"]
DETAIL_FACTS = ["div[data-testid=bed-bath-sqft-fact-container] span", "ul[class*=fact-list] li"]
DETAIL_DESCRIPTION = ["div[data-testid=description] div", "div[class*=Text-c11n] span", "article"]
DETAIL_BROKER = ["div[data-testid=attribution-LISTING_AGENT]", "span[class*=listing-attribution]"]
DETAIL_IMAGES = ["ul[class*=media-stream] img[src]", "picture source[srcset]", "img[src]"]
DETAIL_HOA = ["span[data-testid=hoa-fee]", "li[class*=hoa]"]

# Hydration payloads are preferred over rendered DOM when present.
JSON_SCRIPT_IDS = (
    "__NEXT_DATA__",
    "__NEXT_DATA__ ",
    "hdpApolloPreloadedData",
    "__LOADABLE_LOADED_CHUNKS__",
)

# Signals that the page is a challenge/verification wall rather than content.
CHALLENGE_MARKERS = (
    "captcha",
    "px-captcha",
    "perimeterx",
    "please verify you're a human",
    "press & hold",
    "unusual traffic",
    "are you a robot",
    "access to this page has been denied",
    "sign in to continue",
)

# Marker text that means "zero results", which is a valid outcome, not a break.
EMPTY_RESULT_MARKERS = (
    "no matching results",
    "we couldn't find any results",
    "0 results",
)
