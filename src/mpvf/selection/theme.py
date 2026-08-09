"""Rule-based theme evaluation (FR-042).

Theme fit is decided by rules over structured facts before any model is asked
for an opinion. "Coastal" means evidence of water, view, access or a recognized
coastal town — never a vibe.
"""

from __future__ import annotations

from mpvf.config.templates import ThemeRules
from mpvf.models.domain import PropertyRecord

# Recognized Maine coastal towns (§5.2A "verified coastal village").
COASTAL_TOWNS = {
    "kittery",
    "york",
    "ogunquit",
    "wells",
    "kennebunkport",
    "kennebunk",
    "biddeford",
    "saco",
    "old orchard beach",
    "scarborough",
    "cape elizabeth",
    "south portland",
    "portland",
    "falmouth",
    "cumberland",
    "yarmouth",
    "freeport",
    "brunswick",
    "harpswell",
    "phippsburg",
    "georgetown",
    "bath",
    "boothbay harbor",
    "boothbay",
    "southport",
    "bristol",
    "south bristol",
    "damariscotta",
    "friendship",
    "cushing",
    "st george",
    "saint george",
    "rockland",
    "rockport",
    "camden",
    "lincolnville",
    "belfast",
    "searsport",
    "stockton springs",
    "castine",
    "brooksville",
    "blue hill",
    "sedgwick",
    "brooklin",
    "deer isle",
    "stonington",
    "mount desert",
    "bar harbor",
    "southwest harbor",
    "tremont",
    "gouldsboro",
    "winter harbor",
    "milbridge",
    "jonesport",
    "machias",
    "cutler",
    "lubec",
    "eastport",
    "perry",
    "robbinston",
}

ISLAND_TOWNS = {
    "vinalhaven",
    "north haven",
    "islesboro",
    "swans island",
    "frenchboro",
    "monhegan",
    "matinicus",
    "cranberry isles",
    "isle au haut",
    "long island",
    "chebeague island",
    "peaks island",
    "great diamond island",
    "cliff island",
    "deer isle",
    "stonington",
    "isleboro",
}

SKI_TOWNS = {
    "carrabassett valley",
    "kingfield",
    "stratton",
    "eustis",
    "newry",
    "bethel",
    "greenwood",
    "rangeley",
    "dallas plantation",
    "sandy river plantation",
    "andover",
    "oquossoc",
    "upton",
    "gilead",
    "hanover",
    "rumford",
    "mexico",
    "byron",
}

LAKE_BODIES = {
    "moosehead",
    "sebago",
    "rangeley",
    "mooselookmeguntic",
    "great pond",
    "long pond",
    "messalonskee",
    "china lake",
    "damariscotta lake",
    "schoodic lake",
    "cobbosseecontee",
    "thompson lake",
    "panther pond",
    "highland lake",
    "kezar lake",
    "flagstaff lake",
    "chesuncook",
    "grand lake",
    "west grand lake",
    "east grand lake",
}

PORTLAND_WALKABLE = {"portland", "south portland"}

_UNIQUE_MARKERS = (
    "converted school",
    "former church",
    "converted church",
    "old mill",
    "firehouse",
    "lighthouse",
    "keeper's house",
    "a-frame",
    "geodesic",
    "yurt",
    "silo",
    "barn conversion",
    "architect-designed",
    "architect designed",
    "private island",
    "compound",
    "working farm",
    "post and beam",
    "timber frame",
    "round house",
    "earth-sheltered",
)

_HISTORIC_MARKERS = (
    "sea captain",
    "captain's house",
    "national register",
    "historic district",
    "federal style",
    "greek revival",
    "italianate",
    "victorian",
    "cape",
    "colonial",
    "original moldings",
    "original woodwork",
    "wide pine",
    "beehive oven",
    "post and beam",
)


def _blob(record: PropertyRecord) -> str:
    canonical = record.canonical
    parts = [
        canonical.description_text or "",
        canonical.raw_address or "",
        record.normalized_address,
        str(canonical.facts.get("card_text", "")),
        canonical.waterfront.water_body or "",
    ]
    return " ".join(parts).lower()


def _town(record: PropertyRecord) -> str:
    return (record.city or "").strip().lower()


def evaluate_signals(record: PropertyRecord) -> set[str]:
    """Derive the theme signals a template can require."""

    signals: set[str] = set()
    blob = _blob(record)
    town = _town(record)
    water = record.canonical.waterfront

    if water.frontage_type == "ocean" or "oceanfront" in blob:
        signals.update({"owned_ocean_frontage", "ocean_view", "waterfront"})
    if water.frontage_type in {"lake", "pond"}:
        signals.update({"lake_frontage", "waterfront"})
    if water.frontage_type == "river":
        signals.update({"river_frontage", "waterfront"})
    if water.frontage_type == "bay":
        signals.update({"owned_ocean_frontage", "waterfront"})
    if water.owned_frontage_feet:
        signals.add("owned_frontage_measured")
    if water.water_view or "ocean view" in blob or "water views" in blob:
        signals.add("ocean_view")
    if water.deeded_access or "deeded beach" in blob or "deeded access" in blob:
        signals.add("deeded_beach_access")
    if town in COASTAL_TOWNS:
        signals.add("verified_coastal_village")
    if town in ISLAND_TOWNS or "island" in town or " island" in blob[:400]:
        signals.add("island_location")
    if town in SKI_TOWNS:
        signals.add("ski_area_proximity")
    if any(
        word in blob
        for word in ("sugarloaf", "sunday river", "saddleback", "mt. abram", "ski trail")
    ):
        signals.add("ski_area_proximity")
    if any(body in blob for body in LAKE_BODIES):
        signals.add("named_lake")
    if town in PORTLAND_WALKABLE:
        signals.add("portland_area")
    if "old port" in blob or "walk to the old port" in blob:
        signals.add("old_port_walkable")

    year = record.canonical.year_built
    if year and year < 1900:
        signals.add("pre_1900")
    if year and year < 1850:
        signals.add("pre_1850")
    if any(marker in blob for marker in _HISTORIC_MARKERS):
        signals.add("historic_character")
    if any(marker in blob for marker in _UNIQUE_MARKERS):
        signals.add("unique_property")

    acres = record.canonical.acres
    if acres and acres >= 5:
        signals.add("acreage")
    if acres and acres >= 25:
        signals.add("large_acreage")
    if any(
        word in blob for word in ("hunting", "trail access", "atv", "snowmobile", "public land")
    ):
        signals.add("sporting_access")
    if any(word in blob for word in ("off-grid", "off grid", "generator", "solar")):
        signals.add("off_grid_capable")
    if record.canonical.seasonal:
        signals.add("seasonal_camp")
    if record.canonical.property_type == "condo":
        signals.add("condo")
    previous, current = record.canonical.previous_price, record.canonical.price
    if previous and current and current < previous:
        signals.add("price_reduced")
    return signals


def theme_fit(record: PropertyRecord, rules: ThemeRules) -> tuple[bool, float, list[str]]:
    """Return ``(passes, fit_ratio, matched_signals)``.

    ``fit_ratio`` is 0..1 and feeds the weighted theme-fit score.
    """

    signals = evaluate_signals(record)
    matched = sorted(signals)

    if any(signal in signals for signal in rules.none_of):
        return False, 0.0, matched

    all_ok = all(signal in signals for signal in rules.all_of) if rules.all_of else True
    any_matches = [signal for signal in rules.any_of if signal in signals]
    any_ok = bool(any_matches) if rules.any_of else True

    if not (all_ok and any_ok):
        return False, 0.0, matched

    required = len(rules.all_of) + (1 if rules.any_of else 0)
    if required == 0:
        return True, 0.5, matched

    achieved = len(rules.all_of) + min(len(any_matches), 1)
    bonus = min(0.25, 0.08 * max(0, len(any_matches) - 1))
    return True, round(min(1.0, achieved / required * 0.85 + bonus + 0.15), 3), matched
