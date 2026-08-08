"""Translation layer.

findpic phrases its findings as full sentences rather than tag dumps, which means
those sentences have to be translatable — a Ukrainian speaker should get the same
clarity an English speaker gets, not a half-translated hybrid.

Design:

* Message keys mirror finding IDs (``privacy.gps_location`` becomes
  ``finding.privacy.gps_location.title``), so a rule never handles display text.
  Rules produce facts; the catalogue produces sentences.
* Catalogues are plain JSON, so adding a language is a file, not a code change.
* Plurals go through per-language rules. English has two forms, Ukrainian has
  three, and "1 обличчя / 2 обличчя / 5 облич" is exactly the kind of detail that
  makes a translation feel machine-made when it is wrong.
* A missing key falls back to English and then to the key itself. A translation
  gap degrades one line; it never crashes a report.

Shell commands are deliberately *not* translated. ``exiftool -gps:all=`` is the
same in every language, and translating it would produce something that does not
run.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_LANGUAGE = "en"
FALLBACK_LANGUAGE = "en"

LOCALE_DIR = Path(__file__).resolve().parent / "locales"

#: Language code -> the name of that language, written in that language.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "uk": "Українська",
}


def available_languages() -> list[str]:
    """Language codes with a catalogue on disk."""
    if not LOCALE_DIR.is_dir():
        return [FALLBACK_LANGUAGE]
    return sorted(p.stem for p in LOCALE_DIR.glob("*.json"))


# --------------------------------------------------------------- plural rules


def _plural_en(count: int) -> str:
    return "one" if count == 1 else "other"


def _plural_uk(count: int) -> str:
    """Ukrainian: one / few / many.

    1, 21, 31 …        -> one   (1 обличчя)
    2-4, 22-24 …       -> few   (2 обличчя)
    0, 5-20, 25-30 …   -> many  (5 облич)
    """
    if count % 10 == 1 and count % 100 != 11:
        return "one"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "few"
    return "many"


PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "en": _plural_en,
    "uk": _plural_uk,
}


# ------------------------------------------------------------------ catalogue


@lru_cache(maxsize=8)
def load_catalog(language: str) -> dict[str, Any]:
    """Read one language's catalogue. Cached — catalogues never change at runtime."""
    path = LOCALE_DIR / f"{language}.json"
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def detect_language(default: str = DEFAULT_LANGUAGE) -> str:
    """Guess the user's language from the environment.

    Honours ``FINDPIC_LANG`` first, then the standard locale variables, so a
    Ukrainian desktop gets Ukrainian output without anyone passing a flag.
    """
    for variable in ("FINDPIC_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(variable)
        if not raw:
            continue
        code = re.split(r"[.@_-]", raw.strip())[0].lower()
        if code in available_languages():
            return code
    return default


class Translator:
    """Resolves message keys to sentences in one language."""

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        supported = available_languages()
        self.language = language if language in supported else FALLBACK_LANGUAGE
        self._catalog = load_catalog(self.language)
        self._fallback = (
            load_catalog(FALLBACK_LANGUAGE) if self.language != FALLBACK_LANGUAGE else self._catalog
        )
        self._plural = PLURAL_RULES.get(self.language, _plural_en)
        #: Keys that were asked for and not found — surfaced by the test suite so
        #: an untranslated string cannot ship unnoticed.
        self.missing: set[str] = set()

    # ------------------------------------------------------------------ lookup

    def _raw(self, key: str) -> Any:
        if key in self._catalog:
            return self._catalog[key]
        if key in self._fallback:
            self.missing.add(key)
            return self._fallback[key]
        return None

    def has(self, key: str) -> bool:
        return key in self._catalog or key in self._fallback

    def has_placeholder(self, key: str, name: str) -> bool:
        """Whether a catalogue entry has a ``{name}`` slot in it."""
        entry = self._raw(key)
        if isinstance(entry, dict):
            entry = next(iter(entry.values()), "")
        return "{" + name + "}" in str(entry or "")

    def get(self, key: str, count: int | None = None, /, **params: Any) -> str:
        """Resolve ``key`` to a formatted sentence.

        ``count`` selects the plural form when the catalogue entry is a mapping,
        and is also made available to the template as ``{count}``.
        """
        entry = self._raw(key)
        if entry is None:
            self.missing.add(key)
            return key

        if isinstance(entry, dict):
            form = self._plural(count if count is not None else 0)
            template = entry.get(form) or entry.get("other") or entry.get("many")
            if template is None:
                template = next(iter(entry.values()), key)
        else:
            template = entry

        if count is not None:
            params.setdefault("count", count)

        try:
            return str(template).format(**params)
        except (KeyError, IndexError, ValueError):
            # A malformed template must not take down a report; show it raw.
            return str(template)

    #: Terse alias, because this gets called a lot.
    t = get

    def language_name(self) -> str:
        return LANGUAGE_NAMES.get(self.language, self.language)

    # ------------------------------------------------------- domain formatting

    def weekday(self, index: int) -> str:
        """Weekday name, Monday = 0. Not ``strftime`` — that follows the C locale."""
        return self.get(f"time.weekday.{index}")

    def daypart(self, hour: int) -> str:
        if 5 <= hour < 12:
            part = "morning"
        elif 12 <= hour < 17:
            part = "afternoon"
        elif 17 <= hour < 22:
            part = "evening"
        else:
            part = "night"
        return self.get(f"time.daypart.{part}")

    def describe_when(self, value: Any) -> str | None:
        """``Saturday night`` / ``субота, ніч``."""
        if value is None:
            return None
        return self.get(
            "time.when",
            weekday=self.weekday(value.weekday()),
            daypart=self.daypart(value.hour),
        )

    def compass(self, degrees: float) -> str:
        points = (
            "n",
            "nne",
            "ne",
            "ene",
            "e",
            "ese",
            "se",
            "sse",
            "s",
            "ssw",
            "sw",
            "wsw",
            "w",
            "wnw",
            "nw",
            "nnw",
        )
        return self.get(f"compass.{points[int((degrees % 360) / 22.5 + 0.5) % 16]}")

    def bytes(self, size: float) -> str:
        """Byte count with a translated unit."""
        if size < 1024:
            return self.get("unit.bytes", count=int(size), value=int(size))
        value = float(size)
        for unit in ("kb", "mb", "gb", "tb"):
            value /= 1024.0
            if value < 1024 or unit == "tb":
                rendered = f"{value:.1f}".removesuffix(".0")
                return self.get(f"unit.{unit}", value=rendered)
        return f"{value:.1f} TB"

    def duration(self, seconds: float) -> str:
        """``2 days 13:14:39`` with a translated, correctly-pluralised day count."""
        seconds = int(seconds)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)
        clock = f"{hours}:{minutes:02d}:{secs:02d}"
        if not days:
            return clock
        return self.get("unit.days_clock", days, clock=clock)


#: Shared default so callers that do not care about language need no setup.
_DEFAULT = Translator(DEFAULT_LANGUAGE)


def default_translator() -> Translator:
    return _DEFAULT
