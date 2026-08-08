"""Tests for the Telegram bot layer.

No Telegram connection is involved. What is tested here is everything that can
go wrong without one: configuration parsing, the quota arithmetic, and — most
importantly — that attacker-controlled metadata cannot break out of the HTML
markup on its way into a message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from findpic.analysis import AnalysisOptions, analyze
from findpic.bot.config import CLOUD_DOWNLOAD_LIMIT, Config, ConfigError
from findpic.bot.format import (
    MESSAGE_LIMIT,
    esc,
    render_report,
    render_tag_dump,
)
from findpic.bot.storage import Storage
from findpic.exif import ExifTool

TOKEN = "1234567890:AAHc0000000000000000000000000000000"


# ------------------------------------------------------------------- config


def test_config_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        Config.from_env(use_env_file=False)


def test_env_file_is_loaded_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`deploy/.env` is what people expect to edit, so it has to be read."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"# a comment\n\nexport BOT_TOKEN='{TOKEN}'\nBOT_DEFAULT_LANGUAGE=\"uk\"\nDAILY_QUOTA=7\n"
    )
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_DEFAULT_LANGUAGE", raising=False)
    monkeypatch.delenv("DAILY_QUOTA", raising=False)
    monkeypatch.setenv("FINDPIC_ENV_FILE", str(env_file))

    config = Config.from_env()
    assert config.token == TOKEN
    assert config.language == "uk"
    assert config.daily_quota == 7


def test_real_environment_beats_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `BOT_TOKEN=… python -m findpic.bot` must win over a stale file."""
    env_file = tmp_path / ".env"
    env_file.write_text("BOT_TOKEN=111:FROM_FILE\n")
    monkeypatch.setenv("FINDPIC_ENV_FILE", str(env_file))
    monkeypatch.setenv("BOT_TOKEN", "222:FROM_ENVIRONMENT")

    assert Config.from_env().token == "222:FROM_ENVIRONMENT"


def test_missing_env_file_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from findpic.bot.config import load_env_file

    monkeypatch.setenv("FINDPIC_ENV_FILE", "/nonexistent/.env")
    assert load_env_file() is None


def test_the_tracked_template_carries_no_token() -> None:
    """deploy/.env.example is committed; a real token there would be published."""
    template = Path(__file__).resolve().parent.parent / "deploy" / ".env.example"
    for line in template.read_text().splitlines():
        if line.startswith("BOT_TOKEN="):
            assert line.strip() == "BOT_TOKEN=", (
                "a real token has been written into the tracked .env.example"
            )


def test_config_rejects_a_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "not-a-token")
    with pytest.raises(ConfigError, match="does not look like"):
        Config.from_env(use_env_file=False)


def test_cloud_api_is_capped_at_twenty_megabytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram will not serve more than this, so a larger setting is a lie."""
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.delenv("BOT_API_BASE", raising=False)
    monkeypatch.setenv("MAX_FILE_MB", "500")
    config = Config.from_env(use_env_file=False)
    assert config.max_file_bytes == CLOUD_DOWNLOAD_LIMIT
    assert not config.api_is_local


def test_local_api_lifts_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("BOT_API_BASE", "http://remy-bot-api:8081")
    monkeypatch.setenv("MAX_FILE_MB", "500")
    config = Config.from_env(use_env_file=False)
    assert config.api_is_local
    assert config.max_file_bytes == 500 * 1024 * 1024


def test_allowlist_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222;333 , junk,")
    config = Config.from_env(use_env_file=False)
    assert config.allowed_user_ids == frozenset({111, 222, 333})
    assert not config.is_public


def test_empty_allowlist_means_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    assert Config.from_env(use_env_file=False).is_public


def test_describe_never_prints_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    description = Config.from_env(use_env_file=False).describe()
    assert TOKEN not in description
    assert "AAHc" not in description


def test_token_directory_is_scoped_to_this_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    config = Config.from_env(use_env_file=False)
    assert config.token_directory == config.api_files_root / TOKEN


# ------------------------------------------------------------------ storage


@pytest.fixture
async def storage(tmp_path: Path):
    store = Storage(tmp_path / "test.sqlite3")
    await store.connect()
    yield store
    await store.close()


async def test_language_round_trips(storage: Storage) -> None:
    assert await storage.get_language(42, "en") == "en"
    await storage.set_language(42, "uk")
    assert await storage.get_language(42, "en") == "uk"
    await storage.set_language(42, "en")
    assert await storage.get_language(42, "uk") == "en"


async def test_throttle_blocks_a_rapid_second_request(storage: Storage) -> None:
    first = await storage.check_and_consume(1, throttle_seconds=3, daily_quota=50, now=1000.0)
    assert first.allowed

    second = await storage.check_and_consume(1, throttle_seconds=3, daily_quota=50, now=1001.0)
    assert not second.allowed
    assert second.reason == "throttled"
    assert second.retry_after == pytest.approx(2.0, abs=0.1)

    later = await storage.check_and_consume(1, throttle_seconds=3, daily_quota=50, now=1005.0)
    assert later.allowed


async def test_daily_quota_is_enforced(storage: Storage) -> None:
    now = 1000.0
    for index in range(3):
        verdict = await storage.check_and_consume(
            7, throttle_seconds=0, daily_quota=3, now=now + index
        )
        assert verdict.allowed, f"request {index} should have been allowed"

    blocked = await storage.check_and_consume(7, throttle_seconds=0, daily_quota=3, now=now + 10)
    assert not blocked.allowed
    assert blocked.reason == "quota"


async def test_quota_is_per_user(storage: Storage) -> None:
    await storage.check_and_consume(1, throttle_seconds=0, daily_quota=1, now=1000.0)
    other = await storage.check_and_consume(2, throttle_seconds=0, daily_quota=1, now=1000.0)
    assert other.allowed


async def test_analysis_handles_are_scoped_to_their_owner(storage: Storage) -> None:
    """A guessed token from another chat must not resolve to someone's file."""
    token = await storage.remember_analysis(
        user_id=1, file_id="FILE-ID", file_name="a.jpg", now=1000.0
    )
    assert await storage.recall_analysis(token, user_id=1) is not None
    assert await storage.recall_analysis(token, user_id=2) is None
    assert await storage.recall_analysis("made-up", user_id=1) is None


async def test_expired_handles_are_purged(storage: Storage) -> None:
    await storage.remember_analysis(1, "old", "old.jpg", now=1000.0)
    await storage.remember_analysis(1, "new", "new.jpg", now=9000.0)
    assert await storage.purge_expired(older_than=5000.0) == 1
    assert await storage.known_users() == 0


# ---------------------------------------------------------------- rendering


def test_escaping_neutralises_markup() -> None:
    assert esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert esc("a & b") == "a &amp; b"


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_hostile_metadata_cannot_break_the_markup(camera_jpeg: Path, tmp_path: Path) -> None:
    """A caption containing HTML must not reach Telegram as markup."""
    import shutil
    import subprocess

    # No slash in the name: "</b>" would make this a nested path, not a filename.
    hostile = tmp_path / "<b>pwn<i>.jpg"
    shutil.copyfile(camera_jpeg, hostile)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-ImageDescription=<a href='https://evil.example'>click</a>",
            "-Artist=</b><script>alert(1)</script>",
            str(hostile),
        ],
        check=True,
        capture_output=True,
    )

    report = analyze(hostile, options=AnalysisOptions(geocode=False))
    message = render_report(report)

    assert "<script>" not in message
    assert "evil.example" not in message or "&lt;a href" in message
    assert "&lt;script&gt;" in message or "&lt;/b&gt;" in message
    # The only tags present must be ones we emitted ourselves.
    for fragment in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        assert message.count(fragment) >= 0


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
@pytest.mark.parametrize("language", ["en", "uk"])
def test_message_fits_telegram_limit(gps_jpeg: Path, language: str) -> None:
    report = analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language=language))
    message = render_report(report, source_note="⚠️ <b>note</b>")
    assert 0 < len(message) <= MESSAGE_LIMIT


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_report_renders_in_the_requested_language(gps_jpeg: Path) -> None:
    english = render_report(
        analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="en"))
    )
    ukrainian = render_report(
        analyze(gps_jpeg, options=AnalysisOptions(geocode=False, language="uk"))
    )
    assert "WHAT THIS GIVES AWAY" in english
    assert "ЩО ЦЕ ВИДАЄ ПРО ВАС" in ukrainian


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_stripped_file_still_produces_a_message(blank_jpeg: Path) -> None:
    """The empty case is the common one; it must not render as a blank message."""
    report = analyze(blank_jpeg, options=AnalysisOptions(geocode=False))
    message = render_report(report)
    assert report.file.name in message
    assert len(message) > 50


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_tag_dump_lists_every_tag(gps_jpeg: Path) -> None:
    report = analyze(gps_jpeg, options=AnalysisOptions(geocode=False))
    dump = render_tag_dump(report)
    assert "IFD0:Model" in dump
    assert report.file.sha256 in dump


# --------------------------------------------------------------- keyboards


def test_callback_payloads_fit_telegrams_64_byte_budget() -> None:
    """This is why analyses get a short token instead of carrying a file_id."""
    from findpic.bot.keyboards import AnalysisCallback, LanguageCallback

    packed = AnalysisCallback(action="clean", token="a" * 11).pack()
    assert len(packed.encode()) <= 64
    assert len(LanguageCallback(code="uk").pack().encode()) <= 64


def test_language_keyboard_marks_the_active_choice() -> None:
    from findpic.bot.keyboards import language_keyboard

    markup = language_keyboard("uk")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert any("✓" in label and "Укра" in label for label in labels)
    assert sum("✓" in label for label in labels) == 1


# ------------------------------------------------------------ profile setup


def test_profile_texts_fit_telegrams_limits() -> None:
    """Telegram rejects an over-long name or description with a bare 400."""
    from findpic.bot.setup import check_limits

    assert check_limits() == []


def test_profile_is_defined_in_every_language() -> None:
    from findpic.bot.setup import profile_for
    from findpic.i18n import available_languages

    for language in available_languages():
        profile = profile_for(language)
        for field, text in profile.items():
            assert text and not text.startswith("bot.profile"), (
                f"{language}/{field} is untranslated"
            )


def test_english_and_ukrainian_profiles_differ() -> None:
    """A copy-paste slip would leave one language showing the other's text."""
    from findpic.bot.setup import profile_for

    english, ukrainian = profile_for("en"), profile_for("uk")
    assert english["short"] != ukrainian["short"]
    assert english["description"] != ukrainian["description"]


def test_the_avatar_ships_with_the_repository() -> None:
    from findpic.bot.setup import find_icon

    icon = find_icon()
    assert icon is not None and icon.is_file()
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_needs_no_token_and_no_network() -> None:
    from findpic.bot.setup import render_preview

    preview = render_preview()
    assert "NAME" in preview and "COMMAND MENU" in preview
    assert "findpic" in preview
    # Both languages must be represented.
    assert "English" in preview and "Українська" in preview


# --------------------------------------------------------------- menu buttons


def test_menu_labels_cover_every_language() -> None:
    """A stale keyboard from before a language switch must still work."""
    from findpic.bot.keyboards import menu_labels
    from findpic.i18n import available_languages

    labels = menu_labels()
    actions = {"help", "language", "privacy", "about"}
    assert set(labels.values()) == actions
    # Every language contributes a caption for every action.
    assert len(labels) == len(actions) * len(available_languages())


def test_menu_captions_are_unique_across_languages() -> None:
    """Two languages sharing a caption would make the action ambiguous."""
    from findpic.bot.keyboards import menu_labels
    from findpic.i18n import Translator, available_languages

    seen: dict[str, str] = {}
    for code in available_languages():
        t = Translator(code)
        for action in ("help", "language", "privacy", "about"):
            caption = t.get(f"bot.menu.{action}")
            assert caption not in seen or seen[caption] == action, (
                f"{caption!r} means {seen.get(caption)} in one language and {action} in {code}"
            )
            seen[caption] = action
    assert len(menu_labels()) == len(seen)


def test_main_keyboard_is_persistent_and_sized() -> None:
    from findpic.bot.keyboards import main_keyboard
    from findpic.i18n import Translator

    markup = main_keyboard(Translator("uk"))
    assert markup.is_persistent and markup.resize_keyboard
    rows = markup.keyboard
    assert len(rows) == 2 and all(len(row) == 2 for row in rows)
    # Captions sit in a phone-width button; long ones wrap and look broken.
    for row in rows:
        for button in row:
            assert len(button.text) <= 22, button.text


async def test_menu_filter_maps_captions_to_actions() -> None:
    from findpic.bot.handlers import MenuButton

    filt = MenuButton()

    class FakeMessage:
        def __init__(self, text):
            self.text = text

    assert await filt(FakeMessage("📖 Як користуватися")) == {"menu_action": "help"}
    assert await filt(FakeMessage("🌐 Language")) == {"menu_action": "language"}
    # Whitespace happens when a caption is copied rather than tapped.
    assert await filt(FakeMessage("  ℹ️ About  ")) == {"menu_action": "about"}
    assert await filt(FakeMessage("just some text")) is False
    assert await filt(FakeMessage(None)) is False


def test_media_and_command_routers_are_separate() -> None:
    """Only the media router is throttled; button taps must stay free."""
    from findpic.bot.handlers import media_router, router

    assert media_router is not router
    assert media_router.name == "findpic-media"
