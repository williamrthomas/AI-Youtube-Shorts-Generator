"""Maine pronunciation lexicon (FR-113, FR-114).

Maine place names are where a generic TTS voice embarrasses itself. The lexicon
is an operator-editable YAML file; terms marked ``watchlist`` block publication
until a human has heard them at least once during the pilot (§13.4).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from mpvf.models.domain import PronunciationEntry

# Shipped defaults. The YAML file overrides and extends these.
DEFAULT_ENTRIES: dict[str, tuple[str, str, bool]] = {
    "Bangor": ("BANG-gor", "not 'banger'", True),
    "Calais": ("CAL-us", "not the French 'ca-LAY'", True),
    "Machias": ("muh-CHY-us", "hard ch as in 'chy'", True),
    "Damariscotta": ("dam-uh-ris-COT-uh", "", True),
    "Passamaquoddy": ("pass-uh-muh-QUOD-ee", "Wabanaki nation and bay", True),
    "Piscataquis": ("pis-CAT-uh-kwis", "", True),
    "Androscoggin": ("an-druh-SCOG-in", "", True),
    "Saco": ("SAW-ko", "not 'SACK-o'", True),
    "Rangeley": ("RANE-jlee", "", True),
    "Moosabec": ("MOO-suh-bec", "the reach at Jonesport", True),
    "Narraguagus": ("nar-uh-GWAY-gus", "river in Cherryfield", True),
    "Carrabassett": ("care-uh-BASS-et", "valley below Sugarloaf", True),
    "Wiscasset": ("wis-CASS-et", "", True),
    "Ogunquit": ("oh-GUN-quit", "", True),
    "Mount Desert": ("mount des-SERT", "locals say it like the sweet course", True),
    "Schoodic": ("SKOO-dik", "", True),
    "Kennebunkport": ("KEN-uh-bunk-port", "", False),
    "Skowhegan": ("skow-HEE-gun", "", True),
    "Norridgewock": ("NOR-ij-wok", "", True),
    "Vinalhaven": ("VINE-ul-hay-ven", "", True),
    "Islesboro": ("EYELS-bur-oh", "", True),
    "Isle au Haut": ("EYE-luh-HO", "", True),
    "Matinicus": ("muh-TIN-ih-cus", "", True),
    "Chebeague": ("shuh-BIG", "", True),
    "Mooselookmeguntic": ("moose-look-muh-GUN-tik", "", True),
    "Cobbosseecontee": ("cob-uh-see-CON-tee", "", True),
    "Medomak": ("meh-DOM-ak", "", True),
    "Sebago": ("suh-BAY-go", "", True),
    "Kezar": ("KEE-zer", "", True),
    "Arundel": ("uh-RUN-del", "", False),
    "Lubec": ("loo-BECK", "", True),
    "Eastport": ("EAST-port", "", False),
    "Katahdin": ("kuh-TAH-din", "", True),
    "Allagash": ("AL-uh-gash", "", False),
    "Millinocket": ("mil-in-OCK-et", "", True),
    "Bowdoinham": ("BOW-din-ham", "", True),
    "Buoy": ("BOO-ee", "New England pronunciation", False),
    "Nor'easter": ("nor-EAST-er", "", False),
    "Gunkhole": ("GUNK-hole", "cruising term", False),
}

_WORD = re.compile(r"[A-Za-z'’]+")


class PronunciationLexicon:
    """Lookup, override application and watchlist reporting."""

    def __init__(self, entries: dict[str, PronunciationEntry] | None = None) -> None:
        self._entries: dict[str, PronunciationEntry] = entries or {}
        if not self._entries:
            for term, (phonetic, note, watch) in DEFAULT_ENTRIES.items():
                self._entries[term.lower()] = PronunciationEntry(
                    term=term, phonetic=phonetic, note=note, watchlist=watch
                )

    @classmethod
    def load(cls, path: Path | str | None) -> PronunciationLexicon:
        lexicon = cls()
        if path is None:
            return lexicon
        path = Path(path)
        if not path.exists():
            return lexicon
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for term, value in (payload.get("terms") or {}).items():
            if isinstance(value, str):
                entry = PronunciationEntry(term=term, phonetic=value, watchlist=True)
            else:
                entry = PronunciationEntry(
                    term=term,
                    phonetic=str(value.get("phonetic", "")),
                    note=str(value.get("note", "")),
                    watchlist=bool(value.get("watchlist", True)),
                )
            lexicon._entries[term.lower()] = entry
        return lexicon

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "terms": {
                entry.term: {
                    "phonetic": entry.phonetic,
                    "note": entry.note,
                    "watchlist": entry.watchlist,
                }
                for entry in sorted(self._entries.values(), key=lambda e: e.term)
            }
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def lookup(self, term: str) -> PronunciationEntry | None:
        return self._entries.get(term.strip().lower())

    def add(self, entry: PronunciationEntry) -> None:
        self._entries[entry.term.lower()] = entry

    def entries(self) -> list[PronunciationEntry]:
        return sorted(self._entries.values(), key=lambda entry: entry.term)

    def terms_in(self, text: str) -> list[PronunciationEntry]:
        """Lexicon entries appearing in ``text``, including two-word names."""

        found: dict[str, PronunciationEntry] = {}
        lowered = text.lower()
        for key, entry in self._entries.items():
            if " " in key:
                if key in lowered:
                    found[key] = entry
                continue
            for word in _WORD.findall(lowered):
                if word == key:
                    found[key] = entry
                    break
        return sorted(found.values(), key=lambda entry: entry.term)

    def apply(self, text: str) -> str:
        """Rewrite spoken text to phonetic spellings the TTS engine can say.

        Only whole words are replaced, and capitalization of the original is
        not preserved on purpose: the phonetic form is what should be spoken.
        """

        def replace(match: re.Match[str]) -> str:
            entry = self._entries.get(match.group(0).lower())
            return entry.phonetic if entry and entry.phonetic else match.group(0)

        # Multi-word terms first so "Mount Desert" is not eaten by "Mount".
        result = text
        for key, entry in sorted(self._entries.items(), key=lambda kv: -len(kv[0])):
            if " " not in key or not entry.phonetic:
                continue
            result = re.sub(re.escape(key), entry.phonetic, result, flags=re.IGNORECASE)
        return _WORD.sub(replace, result)

    def watchlist_terms(self, text: str) -> list[PronunciationEntry]:
        return [entry for entry in self.terms_in(text) if entry.watchlist]

    def missing_terms(self, candidate_terms: list[str]) -> list[str]:
        """Place names we will speak but have no lexicon entry for."""

        return sorted(
            {
                term
                for term in candidate_terms
                if term and len(term) > 4 and self.lookup(term) is None
            }
        )
