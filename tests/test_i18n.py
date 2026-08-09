"""Tests for the translation layer.

The important ones here are the parity tests: they fail when a key is added to
English and forgotten in Ukrainian, which is the way a translated tool normally
rots. An untranslated string should break the build, not quietly ship.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from findpic.analysis import AnalysisOptions, analyze
from findpic.exif import ExifTool
from findpic.i18n import (
    FALLBACK_LANGUAGE,
    LOCALE_DIR,
    Translator,
    available_languages,
    detect_language,
    load_catalog,
)

LANGUAGES = available_languages()


def test_expected_languages_are_present() -> None:
    assert set(LANGUAGES) >= {"en", "uk"}


@pytest.mark.parametrize("language", LANGUAGES)
def test_catalogue_is_valid_json(language: str) -> None:
    with open(LOCALE_DIR / f"{language}.json", encoding="utf-8") as handle:
        assert isinstance(json.load(handle), dict)


@pytest.mark.parametrize("language", [l for l in LANGUAGES if l != FALLBACK_LANGUAGE])
def test_no_missing_translations(language: str) -> None:
    """Every English key must exist in every other catalogue."""
    english = set(load_catalog(FALLBACK_LANGUAGE))
    other = set(load_catalog(language))
    missing = sorted(english - other)
    assert not missing, f"{language}.json is missing {len(missing)} keys: {missing[:10]}"


@pytest.mark.parametrize("language", [l for l in LANGUAGES if l != FALLBACK_LANGUAGE])
def test_no_orphan_translations(language: str) -> None:
    """A key with no English original is a typo or a leftover."""
    english = set(load_catalog(FALLBACK_LANGUAGE))
    orphans = sorted(set(load_catalog(language)) - english)
    assert not orphans, f"{language}.json has keys English does not: {orphans[:10]}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_placeholders_match_english(language: str) -> None:
    """A translation that drops or invents a ``{placeholder}`` renders wrongly."""
    import re

    def slots(entry: object) -> set[str]:
        if isinstance(entry, dict):
            found: set[str] = set()
            for value in entry.values():
                found |= set(re.findall(r"\{(\w+)\}", str(value)))
            return found
        return set(re.findall(r"\{(\w+)\}", str(entry)))

    english = load_catalog(FALLBACK_LANGUAGE)
    other = load_catalog(language)
    problems: list[str] = []
    for key, entry in english.items():
        if key not in other:
            continue
        expected, actual = slots(entry), slots(other[key])
        # A plural form may legitimately omit {count} in one language.
        if actual - expected:
            problems.append(f"{key}: unexpected {sorted(actual - expected)}")
    assert not problems, problems


def test_ukrainian_plural_forms_are_complete() -> None:
    """Ukrainian needs one/few/many; two forms would misdecline most counts."""
    catalog = load_catalog("uk")
    for key, entry in catalog.items():
        if isinstance(entry, dict):
            assert {"one", "few", "many"} <= set(entry), f"{key} lacks a plural form"


@pytest.mark.parametrize(
    ("count", "form"),
    [
        (1, "one"),
        (21, "one"),
        (101, "one"),
        (2, "few"),
        (3, "few"),
        (4, "few"),
        (22, "few"),
        (0, "many"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (14, "many"),
        (25, "many"),
        (100, "many"),
    ],
)
def test_ukrainian_plural_rule(count: int, form: str) -> None:
    from findpic.i18n import _plural_uk

    assert _plural_uk(count) == form


def test_ukrainian_face_counts_decline_correctly() -> None:
    t = Translator("uk")
    assert "1 обличчя" in t.get("finding.privacy.face_regions.title", 1)
    assert "2 обличчя" in t.get("finding.privacy.face_regions.title", 2)
    assert "5 облич" in t.get("finding.privacy.face_regions.title", 5)


def test_english_plural_rule() -> None:
    t = Translator("en")
    assert "1 face" in t.get("finding.privacy.face_regions.title", 1)
    assert "3 faces" in t.get("finding.privacy.face_regions.title", 3)


def test_unknown_language_falls_back_to_english() -> None:
    t = Translator("klingon")
    assert t.language == FALLBACK_LANGUAGE


def test_missing_key_returns_the_key_not_a_crash() -> None:
    t = Translator("en")
    assert t.get("no.such.key.exists") == "no.such.key.exists"
    assert "no.such.key.exists" in t.missing


def test_malformed_params_do_not_crash() -> None:
    """A template asking for a parameter the caller did not pass must not raise."""
    t = Translator("en")
    assert t.get("finding.privacy.timezone.title")  # {offset} not supplied


def test_detect_language_honours_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINDPIC_LANG", "uk")
    assert detect_language() == "uk"
    monkeypatch.delenv("FINDPIC_LANG")
    monkeypatch.setenv("LANG", "uk_UA.UTF-8")
    assert detect_language() == "uk"
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert detect_language() == "en"


def test_translated_units_and_durations() -> None:
    en, uk = Translator("en"), Translator("uk")
    assert en.bytes(2368429) == "2.3 MB"
    assert uk.bytes(2368429) == "2.3 МБ"
    assert en.duration(220478) == "2 days 13:14:38"
    assert uk.duration(220478) == "2 дні 13:14:38"
    assert uk.duration(86400 * 5 + 60) == "5 днів 0:01:00"


def test_weekday_and_daypart_are_not_locale_dependent() -> None:
    """strftime follows the C locale; the catalogue must not."""
    import datetime as dt

    saturday_night = dt.datetime(2021, 2, 27, 22, 23)
    assert Translator("en").describe_when(saturday_night) == "Saturday night"
    assert Translator("uk").describe_when(saturday_night) == "субота, ніч"


# --------------------------------------------------- end-to-end key coverage


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
@pytest.mark.parametrize("language", LANGUAGES)
def test_rendering_every_fixture_needs_no_missing_keys(
    language: str,
    blank_jpeg: Path,
    camera_jpeg: Path,
    gps_jpeg: Path,
    edited_jpeg: Path,
    polyglot_jpeg: Path,
    html_jpeg: Path,
    scripted_jpeg: Path,
    truncated_jpeg: Path,
) -> None:
    """Render every finding in every language and assert nothing falls back.

    This is the test that catches a rule shipping a key nobody translated.
    """
    translator = Translator(language)
    options = AnalysisOptions(geocode=False, language=language)
    fixtures = (
        blank_jpeg,
        camera_jpeg,
        gps_jpeg,
        edited_jpeg,
        polyglot_jpeg,
        html_jpeg,
        scripted_jpeg,
        truncated_jpeg,
    )
    for path in fixtures:
        report = analyze(path, options=options, translator=translator)
        for finding in report.findings:
            finding.title(translator)
            finding.detail(translator)
        for verdict in report.verdicts.values():
            verdict.label(translator)
            verdict.summary(translator)
            verdict.reason_lines(translator)

    assert not translator.missing, (
        f"{language}: {len(translator.missing)} untranslated keys: "
        f"{sorted(translator.missing)[:10]}"
    )


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_json_output_carries_stable_ids_and_localised_text(gps_jpeg: Path) -> None:
    """Consumers key on `id`; only the prose follows the language."""
    english = analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="en"))
    ukrainian = analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="uk"))

    en_payload, uk_payload = english.to_dict(), ukrainian.to_dict()
    assert en_payload["language"] == "en"
    assert uk_payload["language"] == "uk"
    assert [f["id"] for f in en_payload["findings"]] == [f["id"] for f in uk_payload["findings"]]
    titles_en = {f["title"] for f in en_payload["findings"]}
    titles_uk = {f["title"] for f in uk_payload["findings"]}
    assert titles_en != titles_uk, "Ukrainian output is identical to English"


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_remediation_commands_are_never_translated(gps_jpeg: Path) -> None:
    """A translated shell command would not run."""
    english = analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="en"))
    ukrainian = analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="uk"))
    en_fixes = [f.remediation for f in english.sorted_findings if f.remediation]
    uk_fixes = [f.remediation for f in ukrainian.sorted_findings if f.remediation]
    assert en_fixes == uk_fixes
    assert all(fix.startswith("exiftool ") for fix in uk_fixes)


def test_no_catalogue_key_is_missing_when_analysing_real_files(
    fixture_dir: Path, camera_jpeg: Path, gps_jpeg: Path, edited_jpeg: Path, blank_jpeg: Path
) -> None:
    """Every key a real report asks for must exist, in every language.

    Translator.get() falls back to returning the key itself, which renders as
    "source.apple" in the middle of an English sentence and looks like a typo
    rather than a bug. It hid an entire rule for months: filename_origin asked
    for 36 keys and not one of them was ever written, in either catalogue, so
    the two stayed in perfect parity while both were wrong.

    Translator records what it could not find. This asserts it found everything.
    """
    from rich.console import Console

    from findpic.analysis import analyze
    from findpic.analysis.context import AnalysisOptions
    from findpic.exif import ExifTool
    from findpic.geocode import Geocoder
    from findpic.i18n import Translator, available_languages
    from findpic.render.terminal import render_report

    # Filenames that trip the provenance patterns, so those keys get exercised.
    named = []
    for source, name in (
        (camera_jpeg, "IMG_1234.jpg"),
        (gps_jpeg, "IMG-20230813-WA0002.jpg"),
        (edited_jpeg, "Screenshot_20230813-145435.jpg"),
        (blank_jpeg, "signal-2023-08-13-145435.jpg"),
    ):
        target = fixture_dir / name
        target.write_bytes(source.read_bytes())
        named.append(target)

    for language in available_languages():
        translator = Translator(language)
        console = Console(file=io.StringIO(), width=100, no_color=True)
        for path in named:
            report = analyze(
                path,
                exiftool=ExifTool(),
                geocoder=Geocoder(enabled=False, language=language),
                options=AnalysisOptions(geocode=False, language=language),
                translator=translator,
            )
            render_report(console, report, show_info=True, show_notes=True)
        assert not translator.missing, (
            f"{language} catalogue is missing {sorted(translator.missing)}"
        )
