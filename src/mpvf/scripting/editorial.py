"""The anti-slop editorial gate (§7.11, §13.2).

This is the check that decides whether the episode sounds like a person or like
a content mill. It is deliberately mechanical: every rule here corresponds to a
failure mode listed in the specification, and each produces a finding an editor
can act on.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from mpvf.config.settings import QualityGates
from mpvf.models.domain import EvidenceBundle, QAFinding, Script

SLOP_PHRASES = {
    "nestled": 1,
    "boasts": 1,
    "truly special": 0,
    "perfect blend": 0,
    "dream home": 1,
    "must see": 0,
    "one of a kind": 1,
    "hidden gem": 1,
    "stunning": 3,
    "breathtaking": 2,
    "charming": 3,
    "luxurious": 2,
    "oasis": 1,
    "paradise": 1,
    "slice of heaven": 0,
    "no expense spared": 0,
    "entertainer's dream": 0,
    "the possibilities are endless": 0,
}

ENGAGEMENT_BAIT = (
    "smash that like",
    "don't forget to subscribe before",
    "you won't believe",
    "wait until you see number",
    "stay until the end",
    "hit the bell",
)

PRAISE_ADJECTIVES = {
    "beautiful",
    "gorgeous",
    "amazing",
    "incredible",
    "spectacular",
    "lovely",
    "elegant",
    "exquisite",
    "immaculate",
    "pristine",
    "magnificent",
    "impressive",
    "wonderful",
    "fantastic",
    "perfect",
    "flawless",
    "unbelievable",
    "spacious",
    "cozy",
    "serene",
    "tranquil",
    "idyllic",
}

CONCRETE_MARKERS = re.compile(
    r"\b(\d[\d,\.]*|acre|acres|feet|foot|square|bedroom|bathroom|built|frontage|"
    r"dock|granite|woodstove|fireplace|ferry|deeded|mile|miles|minute|minutes|"
    r"year|century|barn|garage|basement|well|septic|solar)\b",
    re.IGNORECASE,
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z']+")

SUBSCORE_MAX = {
    "specificity": 20,
    "usefulness": 15,
    "natural_voice": 15,
    "factual_restraint": 15,
    "pacing": 15,
    "variety": 10,
    "entertainment": 10,
}


@dataclass
class EditorialResult:
    total: float
    subscores: dict[str, float]
    findings: list[QAFinding] = field(default_factory=list)
    passed: bool = False

    def as_dict(self) -> dict[str, float]:
        return {"total": self.total, **self.subscores}


def _opening_signature(text: str, words: int = 3) -> str:
    tokens = _WORD.findall(text.lower())
    return " ".join(tokens[:words])


def _property_texts(script: Script) -> list[str]:
    return [segment.spoken_text for segment in script.segments if segment.type == "property"]


def score_specificity(script: Script) -> tuple[float, list[QAFinding]]:
    """Concrete detail must outweigh praise in every segment."""

    findings: list[QAFinding] = []
    ratios: list[float] = []
    for segment in script.segments:
        if segment.type != "property":
            continue
        words = _WORD.findall(segment.spoken_text.lower())
        praise = sum(1 for word in words if word in PRAISE_ADJECTIVES)
        concrete = len(CONCRETE_MARKERS.findall(segment.spoken_text))
        ratios.append(min(1.0, concrete / max(praise + 1, 1) / 3))
        if praise and concrete <= praise:
            findings.append(
                QAFinding(
                    check="adjectives_outnumber_facts",
                    category="editorial",
                    severity="error",
                    message=(
                        f"segment uses {praise} praise adjectives against {concrete} concrete details"
                    ),
                    detail={"segment_id": segment.segment_id},
                    repairable=True,
                )
            )
    mean = sum(ratios) / len(ratios) if ratios else 0.0
    return round(SUBSCORE_MAX["specificity"] * mean, 2), findings


def score_usefulness(script: Script) -> tuple[float, list[QAFinding]]:
    """There must be a real comparison among the five homes."""

    findings: list[QAFinding] = []
    text = script.spoken_text().lower()
    comparison_markers = (
        " than ",
        "compared",
        "unlike",
        "whereas",
        "instead of",
        "the difference",
        "trades",
        "in exchange",
        "cheaper",
        "more expensive",
        "same money",
    )
    hits = sum(1 for marker in comparison_markers if marker in text)
    closing = [s for s in script.segments if s.type == "closing"]
    has_choice = any("which" in s.spoken_text.lower() or "?" in s.spoken_text for s in closing)

    score = min(1.0, hits / 3) * 0.7 + (0.3 if has_choice else 0.0)
    if hits == 0:
        findings.append(
            QAFinding(
                check="no_comparison",
                category="editorial",
                severity="error",
                message="the script never compares the properties to each other",
                detail={},
                repairable=True,
            )
        )
    if not has_choice:
        findings.append(
            QAFinding(
                check="closing_no_question",
                category="editorial",
                severity="warning",
                message="the closing does not ask the viewer a specific choice",
                detail={},
                repairable=True,
            )
        )
    return round(SUBSCORE_MAX["usefulness"] * score, 2), findings


def score_natural_voice(script: Script) -> tuple[float, list[QAFinding]]:
    """Slop phrases, hype and engagement bait all cost points."""

    findings: list[QAFinding] = []
    text = script.spoken_text().lower()
    penalties = 0.0

    for phrase, allowance in SLOP_PHRASES.items():
        count = text.count(phrase)
        if count > allowance:
            excess = count - allowance
            penalties += 0.12 * excess
            findings.append(
                QAFinding(
                    check="slop_phrase",
                    category="editorial",
                    severity="error" if allowance == 0 else "warning",
                    message=f"{phrase!r} used {count} times (limit {allowance})",
                    detail={"phrase": phrase, "count": count},
                    repairable=True,
                )
            )

    for phrase in ENGAGEMENT_BAIT:
        if phrase in text:
            penalties += 0.2
            findings.append(
                QAFinding(
                    check="engagement_bait",
                    category="editorial",
                    severity="error",
                    message=f"artificial engagement bait: {phrase!r}",
                    detail={"phrase": phrase},
                    repairable=True,
                )
            )

    long_sentences = [
        sentence for sentence in _SENTENCE.split(script.spoken_text()) if len(sentence.split()) > 42
    ]
    if long_sentences:
        penalties += 0.06 * len(long_sentences)
        findings.append(
            QAFinding(
                check="overlong_sentence",
                category="editorial",
                severity="warning",
                message=f"{len(long_sentences)} sentences run past 42 words",
                detail={"example": long_sentences[0][:180]},
                repairable=True,
            )
        )
    return round(SUBSCORE_MAX["natural_voice"] * max(0.0, 1.0 - penalties), 2), findings


def score_factual_restraint(
    script: Script, bundle: EvidenceBundle
) -> tuple[float, list[QAFinding]]:
    """Every property flawless is itself a failure mode."""

    findings: list[QAFinding] = []
    segments = [s for s in script.segments if s.type == "property"]
    if not segments:
        return 0.0, findings

    honest = 0
    tradeoff_markers = (
        "but ",
        "though",
        "however",
        "tradeoff",
        "trade-off",
        "you would need",
        "needs work",
        "seasonal",
        "no ",
        "not ",
        "fee",
        "ferry",
        "renovat",
        "dated",
        "small",
        "steep",
        "shared",
        "leased",
    )
    for segment in segments:
        lowered = segment.spoken_text.lower()
        if any(marker in lowered for marker in tradeoff_markers):
            honest += 1
    ratio = honest / len(segments)
    if ratio < 0.4:
        findings.append(
            QAFinding(
                check="everything_is_perfect",
                category="editorial",
                severity="error",
                message=f"only {honest} of {len(segments)} segments acknowledge any limitation",
                detail={},
                repairable=True,
            )
        )

    unsupported_refs = sum(1 for segment in segments if not segment.evidence_refs)
    if unsupported_refs:
        findings.append(
            QAFinding(
                check="segment_without_citations",
                category="editorial",
                severity="warning",
                message=f"{unsupported_refs} property segments carry no evidence references",
                detail={},
            )
        )
    penalty = 0.1 * unsupported_refs
    return round(SUBSCORE_MAX["factual_restraint"] * max(0.0, ratio * 1.2 - penalty), 2), findings


def score_pacing(
    script: Script, target_words: int, word_range: tuple[int, int]
) -> tuple[float, list[QAFinding]]:
    findings: list[QAFinding] = []
    words = script.word_count()
    low, high = word_range
    if words < low or words > high:
        findings.append(
            QAFinding(
                check="word_count_out_of_range",
                category="editorial",
                severity="error",
                message=f"script is {words} words, template range is {low}-{high}",
                detail={"words": words, "range": [low, high]},
                repairable=True,
            )
        )
        drift = min(1.0, abs(words - target_words) / max(target_words, 1))
        return round(SUBSCORE_MAX["pacing"] * max(0.0, 1.0 - drift * 1.5), 2), findings

    lengths = [len(text.split()) for text in _property_texts(script)]
    if lengths:
        spread = (max(lengths) - min(lengths)) / max(max(lengths), 1)
        if spread > 0.6:
            findings.append(
                QAFinding(
                    check="uneven_segments",
                    category="editorial",
                    severity="warning",
                    message=f"property segments range from {min(lengths)} to {max(lengths)} words",
                    detail={},
                    repairable=True,
                )
            )
        balance = max(0.0, 1.0 - spread)
    else:
        balance = 0.0
    return round(SUBSCORE_MAX["pacing"] * (0.55 + 0.45 * balance), 2), findings


def score_variety(script: Script) -> tuple[float, list[QAFinding]]:
    """Repeated openings and identical transitions are the countdown-slop tell."""

    findings: list[QAFinding] = []
    texts = _property_texts(script)
    openings = [_opening_signature(text) for text in texts]
    counts = Counter(openings)
    repeated = [signature for signature, count in counts.items() if count > 2 and signature]
    penalty = 0.0
    if repeated:
        penalty += 0.4
        findings.append(
            QAFinding(
                check="repeated_openings",
                category="editorial",
                severity="error",
                message=f"more than two properties open with {repeated[0]!r}",
                detail={"signatures": repeated},
                repairable=True,
            )
        )

    transitions = [s.spoken_text.strip().lower() for s in script.segments if s.type == "transition"]
    if len(transitions) > 1 and len(set(transitions)) == 1:
        penalty += 0.3
        findings.append(
            QAFinding(
                check="identical_transitions",
                category="editorial",
                severity="error",
                message="every transition uses identical countdown filler",
                detail={},
                repairable=True,
            )
        )

    intro = next((s.spoken_text for s in script.segments if s.type == "intro"), "")
    closing = next((s.spoken_text for s in script.segments if s.type == "closing"), "")
    if intro and closing:
        intro_words = set(_WORD.findall(intro.lower()))
        closing_words = set(_WORD.findall(closing.lower()))
        if intro_words and len(intro_words & closing_words) / len(intro_words) > 0.65:
            penalty += 0.25
            findings.append(
                QAFinding(
                    check="closing_repeats_intro",
                    category="editorial",
                    severity="warning",
                    message="the closing largely restates the introduction",
                    detail={},
                    repairable=True,
                )
            )
    return round(SUBSCORE_MAX["variety"] * max(0.0, 1.0 - penalty), 2), findings


def score_entertainment(script: Script, bundle: EvidenceBundle) -> tuple[float, list[QAFinding]]:
    """Does the cold open promise what the lineup delivers?"""

    findings: list[QAFinding] = []
    cold_open = next((s.spoken_text for s in script.segments if s.type == "cold_open"), "")
    score = 0.5 if cold_open else 0.0

    if cold_open:
        towns = {p.town.lower() for p in bundle.properties}
        mentioned = [town for town in towns if town and town in cold_open.lower()]
        signal_words = set()
        for candidate in bundle.candidates:
            signal_words.update(signal.replace("_", " ") for signal in candidate.theme_signals)
        promised = [word for word in signal_words if word in cold_open.lower()]
        if mentioned or promised:
            score += 0.3
        else:
            findings.append(
                QAFinding(
                    check="cold_open_generic",
                    category="editorial",
                    severity="warning",
                    message="the cold open does not point at anything specific in the lineup",
                    detail={},
                    repairable=True,
                )
            )
    else:
        findings.append(
            QAFinding(
                check="no_cold_open",
                category="editorial",
                severity="error",
                message="the script has no cold open",
                detail={},
                repairable=True,
            )
        )

    text = script.spoken_text()
    if any(mark in text for mark in ("—", " actually", " honestly", " frankly")) or "?" in text:
        score += 0.2
    return round(SUBSCORE_MAX["entertainment"] * min(1.0, score), 2), findings


def evaluate(
    script: Script,
    bundle: EvidenceBundle,
    target_words: int,
    word_range: tuple[int, int],
    gates: QualityGates | None = None,
) -> EditorialResult:
    """Run the full editorial rubric (§13.2)."""

    gates = gates or QualityGates()
    findings: list[QAFinding] = []
    subscores: dict[str, float] = {}

    for name, scorer in (
        ("specificity", lambda: score_specificity(script)),
        ("usefulness", lambda: score_usefulness(script)),
        ("natural_voice", lambda: score_natural_voice(script)),
        ("factual_restraint", lambda: score_factual_restraint(script, bundle)),
        ("pacing", lambda: score_pacing(script, target_words, word_range)),
        ("variety", lambda: score_variety(script)),
        ("entertainment", lambda: score_entertainment(script, bundle)),
    ):
        value, scorer_findings = scorer()
        subscores[name] = value
        findings.extend(scorer_findings)

    total = round(sum(subscores.values()), 2)
    result = EditorialResult(total=total, subscores=subscores, findings=findings)

    floors_ok = True
    for name, value in subscores.items():
        floor = SUBSCORE_MAX[name] * gates.editorial_subscore_floor_pct
        if value < floor:
            floors_ok = False
            findings.append(
                QAFinding(
                    check="subscore_below_floor",
                    category="editorial",
                    severity="error",
                    message=f"{name} scored {value} of {SUBSCORE_MAX[name]}, floor is {floor:.1f}",
                    detail={"subscore": name, "value": value, "floor": floor},
                    repairable=True,
                )
            )

    result.passed = total >= gates.editorial_pass_score and floors_ok
    if not result.passed and total < gates.editorial_pass_score:
        findings.append(
            QAFinding(
                check="editorial_threshold",
                category="editorial",
                severity="error",
                message=f"editorial score {total} is below the {gates.editorial_pass_score} threshold",
                detail={"score": total},
                repairable=True,
            )
        )
    return result
