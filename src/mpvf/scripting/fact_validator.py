"""Script fact validation (§7.10).

The script is parsed back into atomic claims and each one is matched against
the evidence bundle. An unsupported numeric, status, attribution, proximity or
historical claim fails validation — there is no "probably fine" tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from mpvf.models.domain import Claim, EvidenceBundle, QAFinding, Script, ScriptSegment
from mpvf.normalization.address import fold_text

ClaimKind = Literal["numeric", "status", "attribution", "proximity", "historical", "other"]

_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|k|m)\b)?", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")
_YEAR = re.compile(r"\b(1[6-9]\d{2}|20[0-4]\d)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

_STATUS_WORDS = (
    "for sale",
    "active",
    "pending",
    "under contract",
    "contingent",
    "sold",
    "off market",
)
_ATTRIBUTION_WORDS = (
    "listed by",
    "listing agent",
    "brokerage",
    "courtesy of",
    "represented by",
    "credit to",
)
_PROXIMITY_WORDS = (
    "minutes from",
    "miles from",
    "walk to",
    "walking distance",
    "next to",
    "across from",
    "steps from",
    "close to",
    "near ",
)
_HISTORY_WORDS = (
    "built in",
    "century",
    "historic",
    "originally",
    "captain",
    "18",
    "19",
    "national register",
    "founded",
)

# Words that make a numeric statement editorial rather than a hard claim.
_APPROX_WORDS = (
    "about",
    "approximately",
    "roughly",
    "around",
    "just over",
    "just under",
    "±",
    "some",
)


@dataclass
class ParsedClaim:
    text: str
    kind: ClaimKind
    segment_id: str
    segment_order: int
    property_key: str | None = None
    numbers: list[str] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    passed: bool
    findings: list[QAFinding] = field(default_factory=list)
    matched: dict[str, list[str]] = field(default_factory=dict)
    unsupported: list[ParsedClaim] = field(default_factory=list)

    @property
    def citation_map(self) -> dict[str, list[str]]:
        return self.matched


def classify_claim(sentence: str) -> ClaimKind | None:
    """Return the claim kind, or ``None`` when the sentence asserts nothing checkable."""

    lowered = sentence.lower()
    if any(word in lowered for word in _ATTRIBUTION_WORDS):
        return "attribution"
    if any(word in lowered for word in _STATUS_WORDS):
        return "status"
    if any(word in lowered for word in _PROXIMITY_WORDS):
        return "proximity"
    if _MONEY.search(sentence) or _NUMBER.search(sentence):
        if _YEAR.search(sentence) and any(word in lowered for word in _HISTORY_WORDS):
            return "historical"
        return "numeric"
    if any(word in lowered for word in _HISTORY_WORDS):
        return "historical"
    return None


def parse_claims(script: Script) -> list[ParsedClaim]:
    """Split the script into atomic, checkable claims (FR-100)."""

    parsed: list[ParsedClaim] = []
    for segment in script.segments:
        if segment.type == "disclaimer":
            continue
        for sentence in _SENTENCE.split(segment.spoken_text.strip()):
            sentence = sentence.strip()
            if len(sentence) < 8:
                continue
            kind = classify_claim(sentence)
            if kind is None:
                continue
            parsed.append(
                ParsedClaim(
                    text=sentence,
                    kind=kind,
                    segment_id=segment.segment_id,
                    segment_order=segment.order,
                    property_key=segment.property_key,
                    numbers=_normalize_numbers(sentence),
                )
            )
    return parsed


def _normalize_numbers(text: str) -> list[str]:
    """Comparable numeric tokens, with money and shorthand expanded."""

    values: list[str] = []
    for match in _MONEY.finditer(text):
        raw = match.group().lower().replace("$", "").replace(",", "").strip()
        multiplier = 1
        if raw.endswith("million"):
            multiplier, raw = 1_000_000, raw[: -len("million")].strip()
        elif raw.endswith("m"):
            multiplier, raw = 1_000_000, raw[:-1].strip()
        elif raw.endswith("k"):
            multiplier, raw = 1_000, raw[:-1].strip()
        try:
            values.append(str(int(float(raw) * multiplier)))
        except ValueError:
            continue
    for match in _NUMBER.finditer(text):
        token = match.group().replace(",", "")
        try:
            number = float(token)
        except ValueError:
            continue
        values.append(str(int(number)) if number.is_integer() else str(number))
    return sorted(set(values))


def _evidence_numbers(claim: Claim) -> set[str]:
    numbers = set(_normalize_numbers(claim.text))
    if isinstance(claim.value, (int, float)):
        value = float(claim.value)
        numbers.add(str(int(value)) if value.is_integer() else str(value))
    return numbers


def _supports(parsed: ParsedClaim, claim: Claim) -> bool:
    """Does ``claim`` support ``parsed``?"""

    if parsed.property_key and claim.property_key and parsed.property_key != claim.property_key:
        return False

    if parsed.numbers:
        evidence_numbers = _evidence_numbers(claim)
        if not any(number in evidence_numbers for number in parsed.numbers):
            return False
        if parsed.kind in {"numeric", "historical"}:
            return True

    left = set(fold_text(parsed.text).split())
    right = set(fold_text(claim.text).split())
    if not right:
        return False
    overlap = len(left & right) / len(right)
    return overlap >= 0.45


def validate_script(
    script: Script,
    bundle: EvidenceBundle,
    require_approximation: bool = True,
) -> ValidationOutcome:
    """Match every atomic claim to evidence (FR-101 .. FR-103)."""

    outcome = ValidationOutcome(passed=True)
    eligible = [claim for claim in bundle.claims if claim.eligible_for_script]
    parsed_claims = parse_claims(script)

    for parsed in parsed_claims:
        pool = [
            claim
            for claim in eligible
            if claim.property_key == parsed.property_key or claim.property_key is None
        ] or eligible
        supporting = [claim for claim in pool if _supports(parsed, claim)]

        if not supporting:
            outcome.unsupported.append(parsed)
            outcome.findings.append(
                QAFinding(
                    check="claim_unsupported",
                    category="factual",
                    severity="error",
                    message=f"unsupported {parsed.kind} claim: {parsed.text[:160]}",
                    detail={
                        "segment_id": parsed.segment_id,
                        "property_key": parsed.property_key,
                        "kind": parsed.kind,
                    },
                )
            )
            continue

        outcome.matched.setdefault(parsed.segment_id, []).extend(
            claim.claim_id for claim in supporting
        )

        if require_approximation:
            finding = _check_approximation(parsed, supporting)
            if finding:
                outcome.findings.append(finding)

    outcome.findings.extend(_check_scope_violations(script, bundle))
    outcome.findings.extend(_check_prohibited(script, bundle))
    outcome.findings.extend(_check_segment_coverage(script, bundle))
    outcome.passed = not any(finding.severity == "error" for finding in outcome.findings)

    for segment in script.segments:
        refs = outcome.matched.get(segment.segment_id)
        if refs:
            segment.evidence_refs = sorted(set(refs))
        segment.validation_state = (
            "failed"
            if any(
                finding.detail.get("segment_id") == segment.segment_id
                and finding.severity == "error"
                for finding in outcome.findings
            )
            else "passed"
        )
    script.citation_map = outcome.matched
    script.status = "validated" if outcome.passed else "rejected"
    return outcome


def _check_approximation(parsed: ParsedClaim, supporting: list[Claim]) -> QAFinding | None:
    """FR-103: approximate sources must be spoken approximately."""

    approximate_source = any(
        word in claim.text.lower()
        for claim in supporting
        for word in ("approximately", "about", "±")
    )
    if not approximate_source:
        return None
    if any(word in parsed.text.lower() for word in _APPROX_WORDS):
        return None
    if parsed.kind != "numeric":
        return None
    return QAFinding(
        check="approximation_dropped",
        category="factual",
        severity="warning",
        message=f"source is approximate but narration states it exactly: {parsed.text[:140]}",
        detail={"segment_id": parsed.segment_id},
        repairable=True,
    )


def _check_scope_violations(script: Script, bundle: EvidenceBundle) -> list[QAFinding]:
    """FR-064: town history may not be told as the history of the house."""

    findings: list[QAFinding] = []
    town_claims = {
        claim.claim_id: claim
        for claim in bundle.claims
        if claim.scope in {"town", "region"} and claim.type in {"history", "context"}
    }
    house_words = (
        "this house",
        "the house was",
        "the property was",
        "this home",
        "the home was",
        "its first owner",
    )

    for segment in script.segments:
        if segment.type != "property":
            continue
        lowered = segment.spoken_text.lower()
        used_town_claim = any(ref in town_claims for ref in segment.evidence_refs)
        if used_town_claim and any(phrase in lowered for phrase in house_words):
            findings.append(
                QAFinding(
                    check="history_scope",
                    category="factual",
                    severity="error",
                    message="town/regional context is being narrated as this property's own history",
                    detail={"segment_id": segment.segment_id, "property_key": segment.property_key},
                )
            )
    return findings


def _check_prohibited(script: Script, bundle: EvidenceBundle) -> list[QAFinding]:
    """FR-090: unsupported promotional claims are not allowed."""

    findings: list[QAFinding] = []
    for segment in script.segments:
        lowered = segment.spoken_text.lower()
        for phrase in bundle.prohibited_claims:
            if phrase in lowered:
                findings.append(
                    QAFinding(
                        check="prohibited_claim",
                        category="factual",
                        severity="error",
                        message=f"prohibited phrase in narration: {phrase!r}",
                        detail={"segment_id": segment.segment_id, "phrase": phrase},
                        repairable=True,
                    )
                )
    return findings


def _check_segment_coverage(script: Script, bundle: EvidenceBundle) -> list[QAFinding]:
    """FR-085/FR-086: each property segment must carry its required beats."""

    findings: list[QAFinding] = []
    selected = {c.property_key for c in bundle.candidates if c.selected}
    covered: set[str] = set()

    for segment in script.segments:
        if segment.type != "property" or not segment.property_key:
            continue
        covered.add(segment.property_key)
        lowered = segment.spoken_text.lower()
        record = next(
            (p for p in bundle.properties if p.property_key == segment.property_key), None
        )

        if not _MONEY.search(segment.spoken_text):
            findings.append(_missing(segment, "price", "no price stated in the property segment"))
        if record and record.town and record.town.lower() not in lowered:
            findings.append(
                _missing(segment, "location", f"segment never says the town ({record.town})")
            )
        credit = bundle.credit_for(segment.property_key)
        if credit and (credit.brokerage_name or credit.agent_name):
            broker = (credit.brokerage_name or credit.agent_name or "").lower()
            head = broker.split()[0] if broker else ""
            if head and head not in lowered:
                findings.append(
                    _missing(segment, "broker_credit", "no broker acknowledgment in this segment")
                )

        if record:
            limitation = _material_limitation(record)
            if limitation and limitation["keyword"] not in lowered:
                findings.append(
                    QAFinding(
                        check="material_limitation_omitted",
                        category="factual",
                        severity="error",
                        message=f"material limitation not stated: {limitation['label']}",
                        detail={
                            "segment_id": segment.segment_id,
                            "property_key": segment.property_key,
                        },
                        repairable=True,
                    )
                )

    for missing_key in selected - covered:
        findings.append(
            QAFinding(
                check="property_missing",
                category="factual",
                severity="error",
                message=f"selected property has no segment: {missing_key}",
                detail={"property_key": missing_key},
            )
        )
    return findings


def _missing(segment: ScriptSegment, check: str, message: str) -> QAFinding:
    return QAFinding(
        check=f"segment_missing_{check}",
        category="factual",
        severity="error",
        message=message,
        detail={"segment_id": segment.segment_id, "property_key": segment.property_key},
        repairable=True,
    )


def _material_limitation(record) -> dict[str, str] | None:
    """FR-086: limitations that must be spoken when central to the listing."""

    canonical = record.canonical
    if canonical.property_type == "condo":
        return {"keyword": "condo", "label": "the property is a condominium"}
    if canonical.seasonal:
        return {"keyword": "season", "label": "the property is a seasonal residence"}
    if canonical.hoa_fee and canonical.hoa_fee >= 400:
        return {"keyword": "fee", "label": f"HOA fee of ${int(canonical.hoa_fee)}/month"}
    blob = (canonical.description_text or "").lower()
    if "ferry" in blob:
        return {"keyword": "ferry", "label": "access depends on a ferry"}
    return None


def build_citation_map(script: Script, outcome: ValidationOutcome) -> dict[str, list[str]]:
    """FR-106: machine-readable citations even though they are never spoken."""

    return {
        segment.segment_id: sorted(set(outcome.matched.get(segment.segment_id, [])))
        for segment in script.segments
    }
