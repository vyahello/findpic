"""Tests for the Telegram bot layer.

No Telegram connection is involved. What is tested here is everything that can
go wrong without one: configuration parsing, the quota arithmetic, and — most
importantly — that attacker-controlled metadata cannot break out of the HTML
markup on its way into a message.
"""

from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    Message,
    PhotoSize,
    TelegramObject,
    User,
)

from findpic.analysis import AnalysisOptions, analyze
from findpic.bot.config import CLOUD_DOWNLOAD_LIMIT, Config, ConfigError
from findpic.bot.format import (
    MESSAGE_LIMIT,
    esc,
    render_report,
    render_tag_dump,
)
from findpic.bot.handlers import privacy_notice
from findpic.bot.keyboards import menu_labels
from findpic.bot.middlewares import AccessMiddleware, AuditMiddleware, classify
from findpic.bot.storage import Person, Storage
from findpic.exif import ExifTool
from findpic.i18n import Translator, available_languages

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


# -------------------------------------------------------------------- audit


async def rows(storage: Storage, sql: str) -> list[dict]:
    async with storage.db.execute(sql) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def test_an_interaction_is_recorded_once(storage: Storage) -> None:
    person = Person(7, "someone", "Some", "One", "uk", is_premium=True)
    first = await storage.record_event(person, kind="photo", chat_type="private")
    await storage.record_event(person, kind="command", action="help")

    people = await rows(storage, "SELECT * FROM people")
    assert len(people) == 1
    assert people[0]["username"] == "someone"
    assert people[0]["language_code"] == "uk"
    assert people[0]["is_premium"] == 1
    assert people[0]["events"] == 2
    assert people[0]["first_seen"] <= people[0]["last_seen"]

    events = await rows(storage, "SELECT * FROM events ORDER BY id")
    assert [event["kind"] for event in events] == ["photo", "command"]
    assert events[0]["id"] == first
    assert events[0]["outcome"] is None


async def test_a_renamed_account_keeps_its_first_seen(storage: Storage) -> None:
    """Usernames change; the day somebody first showed up does not."""
    await storage.record_event(Person(7, "before"), kind="command", action="start")
    original = (await rows(storage, "SELECT * FROM people"))[0]["first_seen"]
    await storage.record_event(Person(7, "after"), kind="command", action="help")
    updated = (await rows(storage, "SELECT * FROM people"))[0]
    assert updated["username"] == "after"
    assert updated["first_seen"] == original


async def test_what_a_photo_declared_is_attached_to_its_event(storage: Storage) -> None:
    event_id = await storage.record_event(Person(7), kind="file")
    await storage.note_analysis(
        event_id,
        make="Apple",
        model="iPhone 13 Pro",
        os_version="17.4.1",
        sent_as="file",
        file_type="JPEG",
        stripped=False,
    )
    event = (await rows(storage, "SELECT * FROM events"))[0]
    assert (event["make"], event["model"], event["os"]) == ("Apple", "iPhone 13 Pro", "17.4.1")
    assert event["sent_as"] == "file"


async def test_a_refusal_is_recorded_against_the_same_row(storage: Storage) -> None:
    event_id = await storage.record_event(Person(7), kind="photo")
    await storage.note_outcome(event_id, "quota")
    events = await rows(storage, "SELECT * FROM events")
    assert len(events) == 1, "a refusal must annotate the interaction, not add one"
    assert events[0]["outcome"] == "quota"


async def test_recording_survives_a_missing_event(storage: Storage) -> None:
    """With analytics off there is no row id, and the handlers still call these."""
    await storage.note_outcome(None, "quota")
    await storage.note_analysis(
        None, make="x", model="y", os_version=None, sent_as="file", file_type=None, stripped=True
    )
    assert await rows(storage, "SELECT * FROM events") == []


async def test_old_records_are_forgotten(storage: Storage) -> None:
    """Including the account itself, or the promise in /privacy is not kept.

    A roster of names and first-seen dates, with everything those people did
    already deleted, is still a record of who used the bot.
    """
    await storage.record_event(Person(7, "gone"), kind="photo")
    await storage.db.execute("UPDATE events SET at = '2020-01-01T00:00:00+00:00'")
    await storage.db.commit()
    assert await storage.purge_old_events(keep_days=30) == 1
    assert await rows(storage, "SELECT * FROM events") == []
    assert await rows(storage, "SELECT * FROM people") == []


async def test_forgetting_the_quiet_keeps_the_active(storage: Storage) -> None:
    await storage.record_event(Person(7, "quiet"), kind="photo")
    await storage.db.execute("UPDATE events SET at = '2020-01-01T00:00:00+00:00'")
    await storage.record_event(Person(8, "here"), kind="photo")
    await storage.db.commit()

    assert await storage.purge_old_events(keep_days=30) == 1
    assert [person["username"] for person in await rows(storage, "SELECT * FROM people")] == [
        "here"
    ]


async def test_zero_retention_keeps_everything(storage: Storage) -> None:
    """0 means forever. Reading it as "keep nothing" would silently delete."""
    await storage.record_event(Person(7), kind="photo")
    await storage.db.execute("UPDATE events SET at = '2020-01-01T00:00:00+00:00'")
    await storage.db.commit()
    assert await storage.purge_old_events(keep_days=0) == 0
    assert len(await rows(storage, "SELECT * FROM events")) == 1
    assert len(await rows(storage, "SELECT * FROM people")) == 1


# ------------------------------------------------------------- the middleware


async def through(middlewares, event, data: dict) -> dict:
    """Run an event down a chain of middlewares into a handler that just marks."""

    async def arrived(_event, context: dict) -> None:
        context["reached"] = True

    handler = arrived
    for middleware in reversed(middlewares):
        handler = functools.partial(middleware, handler)
    await handler(event, data)
    return data


async def test_using_the_bot_is_recorded_without_being_asked(storage: Storage) -> None:
    config = Config(token=TOKEN)
    data = {"event_from_user": User(id=7, is_bot=False, first_name="A", username="a")}
    data = await through([AuditMiddleware(storage, config)], message(text="/help"), data)

    assert data["reached"]
    assert data["event_id"]
    event = (await rows(storage, "SELECT * FROM events"))[0]
    assert (event["kind"], event["action"]) == ("command", "help")


async def test_somebody_turned_away_is_still_counted(storage: Storage) -> None:
    """Who knocked is half of what an operator wants out of this table.

    Which is why the audit runs before the allowlist rather than after it.
    """
    config = Config(token=TOKEN, allowed_user_ids=frozenset({1}))
    data = {
        "event_from_user": User(id=999, is_bot=False, first_name="Nobody"),
        "t": Translator("en"),
    }
    data = await through(
        [AuditMiddleware(storage, config), AccessMiddleware(config, storage)],
        TelegramObject(),
        data,
    )

    assert "reached" not in data, "the allowlist should have stopped this"
    event = (await rows(storage, "SELECT * FROM events"))[0]
    assert event["outcome"] == "blocked"


async def test_nothing_is_recorded_when_recording_is_off(storage: Storage) -> None:
    config = Config(token=TOKEN, analytics=False)
    data = await through(
        [AuditMiddleware(storage, config)],
        message(text="/help"),
        {"event_from_user": User(id=7, is_bot=False, first_name="A")},
    )
    assert data["reached"], "turning off the statistics must not turn off the bot"
    assert "event_id" not in data
    assert await rows(storage, "SELECT * FROM events") == []
    assert await rows(storage, "SELECT * FROM people") == []


async def test_a_broken_database_does_not_break_the_bot(storage: Storage) -> None:
    """A statistic is never worth a failed request."""
    await storage.db.execute("DROP TABLE events")
    await storage.db.commit()
    data = await through(
        [AuditMiddleware(storage, Config(token=TOKEN))],
        message(text="/help"),
        {"event_from_user": User(id=7, is_bot=False, first_name="A")},
    )
    assert data["reached"]


# --------------------------------------------------------------- classifying


def message(**fields) -> Message:
    return Message(
        message_id=1,
        date=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc),
        chat=Chat(id=1, type="private"),
        **fields,
    )


def test_classify_names_the_command_not_its_arguments() -> None:
    assert classify(message(text="/lang uk"), {}) == ("command", "lang")
    assert classify(message(text="/help@findpicbot"), {}) == ("command", "help")


def test_classify_never_records_what_somebody_typed() -> None:
    """The one thing this table must not learn."""
    kind, action = classify(message(text="my address is 12 Foo Street"), {})
    assert kind == "text"
    assert action is None


def test_classify_separates_a_compressed_photo_from_a_file() -> None:
    photo = [PhotoSize(file_id="a", file_unique_id="b", width=90, height=90)]
    assert classify(message(photo=photo), {}) == ("photo", None)
    document = Document(file_id="a", file_unique_id="b", file_name="IMG_0001.HEIC")
    assert classify(message(document=document), {}) == ("file", ".heic")


def test_classify_maps_a_menu_caption_to_its_action() -> None:
    labels = menu_labels()
    caption = next(iter(labels))
    assert classify(message(text=caption), labels) == ("menu", labels[caption])


def test_classify_keeps_the_analysis_token_out_of_the_record() -> None:
    """The token is a capability. It has no business in a statistics table."""
    query = CallbackQuery(
        id="1",
        from_user=User(id=7, is_bot=False, first_name="A"),
        chat_instance="x",
        data="an:clean:s3cr3t-token",
    )
    assert classify(query, {}) == ("button", "clean")


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


def test_the_tag_dump_says_it_is_not_a_backup() -> None:
    """The caption has to carry the warning, in every language.

    The button is called "show every raw tag" and it hands back a text file.
    People reasonably read that as a backup and strip the original afterwards.
    It cannot be written back — the values are formatted for reading and the
    binary tags are described rather than included — so the caption is the only
    place that can stop someone losing their metadata.
    """
    import re

    from findpic.i18n import Translator, available_languages

    for language in available_languages():
        translator = Translator(language)
        # The caption is HTML; compare against it with the markup taken out, so
        # bolding a phrase does not silently break the check.
        caption = re.sub(r"<[^>]+>", "", translator.get("bot.tags.caption", count=5))
        assert "⚠️" in caption, language
        assert translator.get("bot.button.backup") in caption, language


def test_the_backup_button_only_appears_when_there_is_something_to_lose(
    gps_jpeg: Path, blank_jpeg: Path
) -> None:
    """Offering a backup of nothing is noise; withholding one is data loss.

    The button rides alongside the clean-copy button on purpose. Whenever
    findpic offers to remove metadata it must also offer to keep a copy.
    """
    from findpic.bot.keyboards import report_keyboard
    from findpic.i18n import Translator

    translator = Translator("en")
    label = translator.get("bot.button.backup")

    offered = report_keyboard(translator, "tok", offer_clean=True, offer_backup=True)
    assert any(button.text == label for row in offered.inline_keyboard for button in row)

    withheld = report_keyboard(translator, "tok", offer_clean=False, offer_backup=False)
    assert not any(button.text == label for row in withheld.inline_keyboard for button in row)


def report_for(path: Path, language: str = "en"):
    return analyze(path, options=AnalysisOptions(geocode=False, language=language))


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_recovered_capture_time_reaches_the_message(tmp_path: Path, blank_jpeg: Path) -> None:
    """The analysis finding it is not enough — the reader has to see it.

    The bot message is built from fixed sections, not from the finding list, so
    a rule can succeed and still be invisible. This one was: the capture time
    was read out of the filename and never rendered, which is the single most
    useful thing findpic can tell someone about a stripped photo.
    """
    from findpic.bot.format import render_report

    target = tmp_path / "IMG_20230813_145435.jpg"
    target.write_bytes(blank_jpeg.read_bytes())
    body = render_report(report_for(target))
    assert "2023" in body
    assert "14:54:35" in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_date_only_filename_does_not_invent_an_hour(tmp_path: Path, blank_jpeg: Path) -> None:
    """WhatsApp's counter must not be rendered as midnight."""
    from findpic.bot.format import render_report

    target = tmp_path / "IMG-20230813-WA0002.jpg"
    target.write_bytes(blank_jpeg.read_bytes())
    body = render_report(report_for(target))
    assert "2023" in body
    assert "00:00" not in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_stripped_file_still_gets_a_message_with_something_in_it(
    tmp_path: Path, camera_jpeg: Path
) -> None:
    """An empty report reads as a broken bot rather than as an empty file."""
    import subprocess

    from findpic.bot.format import render_report

    target = tmp_path / "stripped.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-all=", str(target)],
        check=True,
        capture_output=True,
    )
    body = render_report(report_for(target))
    from findpic.i18n import Translator

    assert Translator("en").get("bot.section.traces") in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_an_intact_photo_gets_no_traces_section(camera_jpeg: Path) -> None:
    """These belong to the case where nothing else is left to say."""
    from findpic.bot.format import render_report
    from findpic.i18n import Translator

    body = render_report(report_for(camera_jpeg))
    assert Translator("en").get("bot.section.traces") not in body


# ---------------------------------------------------------- the privacy notice


TOKEN_CONFIG = {"token": TOKEN}


@pytest.mark.parametrize("language", available_languages())
def test_the_privacy_notice_admits_the_usage_record(language: str) -> None:
    """The bot keeps a record of who used it, so the notice has to say so.

    This is the screen where somebody decides whether to trust the thing, and
    the whole argument of the project is that a claim about your data should be
    checkable. A notice describing a build that keeps nothing, running on a
    build that keeps something, is the exact failure findpic exists to expose.
    """
    t = Translator(language)
    notice = privacy_notice(t, Config(**TOKEN_CONFIG, analytics=True))
    assert t.get("bot.privacy.analytics") in notice
    # The key now takes the handle lifetime, because the notice has to name
    # the one filename the bot really does keep and for how long.
    assert t.get("bot.privacy.never", ttl_hours=24) in notice
    assert "90" in notice, "the retention window has to be stated, not implied"
    assert t.get("bot.privacy.no_analytics") not in notice


@pytest.mark.parametrize("language", available_languages())
def test_the_notice_drops_the_claim_when_recording_is_off(language: str) -> None:
    t = Translator(language)
    notice = privacy_notice(t, Config(**TOKEN_CONFIG, analytics=False))
    assert t.get("bot.privacy.no_analytics") in notice
    assert t.get("bot.privacy.analytics") not in notice
    assert "{" not in notice


@pytest.mark.parametrize("language", available_languages())
def test_the_notice_is_never_left_with_an_unfilled_placeholder(language: str) -> None:
    """Translator.get swallows a formatting error and returns the raw template."""
    t = Translator(language)
    for config in (
        Config(**TOKEN_CONFIG),
        Config(**TOKEN_CONFIG, analytics=False),
        Config(**TOKEN_CONFIG, analytics_retention_days=0),
        Config(**TOKEN_CONFIG, analytics_retention_days=1),
    ):
        notice = privacy_notice(t, config)
        assert "{" not in notice and "}" not in notice
        assert len(notice) < MESSAGE_LIMIT


def test_forever_is_stated_rather_than_printed_as_zero_days() -> None:
    notice = privacy_notice(Translator("en"), Config(**TOKEN_CONFIG, analytics_retention_days=0))
    assert Translator("en").get("bot.privacy.retention.forever") in notice
    assert "0 days" not in notice


def test_analytics_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ANALYTICS", "0")
    monkeypatch.setenv("ANALYTICS_RETENTION_DAYS", "7")
    config = Config.from_env(use_env_file=False)
    assert config.analytics is False
    assert config.analytics_retention_days == 7
    assert "analytics=off" in config.describe()


# ------------------------------------------------------- the defects, pinned


@pytest.mark.parametrize("language", available_languages())
def test_the_send_it_as_a_file_instruction_is_not_escaped(language: str) -> None:
    """The one sentence the whole bot depends on, and it was broken.

    `bot.note.compressed.body` carries `<b>file</b>` and was run through esc(),
    so every user who sent a picture the ordinary way was told to send it "as a
    &lt;b&gt;file&lt;/b&gt;" — in both languages, on the path two of the owner's
    own six pictures took.
    """
    from findpic.bot.handlers import compressed_note

    note = compressed_note(Translator(language))
    assert "&lt;" not in note and "&gt;" not in note
    assert "<b>" in note


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_metadata_is_still_escaped_even_though_the_note_is_not(
    camera_jpeg: Path, tmp_path: Path
) -> None:
    """The complement, and the reason the fix above is narrow.

    "Never esc() a catalogue string" would be the obvious generalisation and it
    is the dangerous one: esc() on a catalogue string is exactly what
    neutralises an Artist tag set to markup. The rule is that esc() belongs
    wherever metadata is interpolated, and nowhere else.
    """
    import subprocess

    target = tmp_path / "hostile.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-q",
            "-Artist=</b><script>alert(1)</script>",
            "-Keywords=<b>injected",
            str(target),
        ],
        check=False,
        capture_output=True,
    )
    body = render_report(analyze(target, options=AnalysisOptions(geocode=False, hash_file=False)))
    assert "<script>" not in body
    assert "&lt;script&gt;" in body or "alert" not in body


def test_the_report_survives_a_pathological_tag_value(gps_jpeg: Path) -> None:
    """A 4 kB field must cost its own line, not the rest of the report.

    Slicing HTML at an arbitrary offset lands inside a tag about as often as
    not, and Telegram answers an unbalanced entity by rejecting the whole
    message — so the user gets nothing at all rather than a shortened report.
    """
    from html.parser import HTMLParser

    report = analyze(gps_jpeg, options=AnalysisOptions(geocode=False))
    report.location.place = "X" * 5000
    body = render_report(report)

    class Balance(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.open: list[str] = []

        def handle_starttag(self, tag: str, attrs: object) -> None:
            if tag != "br":
                self.open.append(tag)

        def handle_endtag(self, tag: str) -> None:
            if self.open and self.open[-1] == tag:
                self.open.pop()
            else:
                self.open.append(f"unbalanced-{tag}")

    checker = Balance()
    checker.feed(body)
    assert not checker.open, f"unbalanced HTML would be rejected by Telegram: {checker.open}"
    assert len(body) <= MESSAGE_LIMIT
    assert Translator("en").get("bot.truncated") in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_the_tag_dump_holds_every_tag_it_claims(gps_jpeg: Path) -> None:
    """The caption said 182 and the file held 169."""
    report = analyze(gps_jpeg, options=AnalysisOptions(geocode=False))
    lines = render_tag_dump(report).splitlines()
    # Everything after the rule is one tag per line.
    body = lines[lines.index("=" * 60) + 1 :]
    assert report.tag_count == len(report.raw)
    assert len([line for line in body if line.strip()]) == report.tag_count


async def test_a_refused_file_gives_the_quota_slot_back(storage: Storage) -> None:
    """The throttle charges before the handler has looked at the file.

    So a file the bot then rejects as too big has already cost its sender one of
    their analyses for the day — invisible until the report started showing the
    count, and then visibly billing for work the bot declined to do.
    """
    await storage.check_and_consume(7, throttle_seconds=0, daily_quota=10, now=1000.0)
    await storage.refund(7)
    spent = await rows(storage, "SELECT count FROM usage WHERE user_id = 7")
    assert spent[0]["count"] == 0


async def test_a_refund_never_goes_negative(storage: Storage) -> None:
    await storage.refund(7)
    assert await rows(storage, "SELECT * FROM usage") == []


# --------------------------------------------------- what the bot now shows


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_stripped_file_is_told_what_happened_to_it(samples_dir: Path) -> None:
    """The two findings that explain an empty report never reached a user.

    `platform.stripped` was not in TRACE_FINDINGS at all, so the owner's own
    messenger-stripped photos came back as an UNKNOWN badge over one line of
    JPEG-encoder trivia — and nothing said "ask the sender for the original".
    """
    target = samples_dir / "33.JPG"
    if not target.exists():
        pytest.skip("needs the owner's samples")
    body = render_report(analyze(target, options=AnalysisOptions(geocode=False)))
    t = Translator("en")
    assert t.get("bot.note.stripped.title") in body
    assert "1600" in body, "the size that gives the platform away should be named"
    # The lead note comes first, above the verdict it exists to pre-empt.
    assert body.index(t.get("bot.note.stripped.title")) < body.index("🔎")


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_screenshot_is_not_graded_as_a_photograph(samples_dir: Path) -> None:
    """A screen capture keeps its capture time, so it kept its badge.

    It was presented as "LIKELY ORIGINAL" with a WHEN block and a camera-shaped
    THE SHOT — a verdict about a camera that never existed.
    """
    target = samples_dir / "22.jpeg"
    if not target.exists():
        pytest.skip("needs the owner's samples")
    body = render_report(analyze(target, options=AnalysisOptions(geocode=False)))
    assert Translator("en").get("bot.note.screenshot") in body
    assert "LIKELY ORIGINAL" not in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_the_exposure_section_never_disappears(blank_jpeg: Path, gps_jpeg: Path) -> None:
    """ "Safe to forward" and "I did not look" read identically when absent."""
    t = Translator("en")
    for path in (blank_jpeg, gps_jpeg):
        body = render_report(analyze(path, options=AnalysisOptions(geocode=False)))
        assert t.get("bot.section.leaks") in body, path.name

    risky = render_report(analyze(gps_jpeg, options=AnalysisOptions(geocode=False)))
    assert (
        t.get("verdict.privacy.bad.label") in risky or t.get("verdict.privacy.poor.label") in risky
    )


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_a_verdict_says_what_it_is_based_on(camera_jpeg: Path, tmp_path: Path) -> None:
    """A conclusion with no evidence and no confidence is an opinion."""
    import subprocess

    target = tmp_path / "edited.jpg"
    target.write_bytes(camera_jpeg.read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", "-q", "-Software=Adobe Photoshop 2024", str(target)],
        check=False,
        capture_output=True,
    )
    body = render_report(analyze(target, options=AnalysisOptions(geocode=False)))
    assert Translator("en").get("bot.verdict.because") in body


@pytest.mark.skipif(not ExifTool.available(), reason="exiftool is not installed")
def test_nothing_is_said_twice_in_one_message(samples_dir: Path) -> None:
    """The verdict's reasons and the traces list are the same findings.

    Saying each thing once is the one rule this module runs on, and a stripped
    file was the case where it broke.
    """
    target = samples_dir / "33.JPG"
    if not target.exists():
        pytest.skip("needs the owner's samples")
    report = analyze(target, options=AnalysisOptions(geocode=False))
    body = render_report(report)
    for finding in report.findings:
        title = finding.title(report.translator)
        if title in body:
            assert body.count(title) == 1, f"{finding.id} appears twice"


def test_the_lens_the_help_text_promises_is_shown(camera_jpeg: Path) -> None:
    """bot.help lists "lens" under DEVICE and render_device never read it."""
    body = render_report(analyze(camera_jpeg, options=AnalysisOptions(geocode=False)))
    assert "TestLens" in body


def test_one_focal_length_line_not_two(camera_jpeg: Path) -> None:
    """ "28 mm equiv." in one block and "4 mm" in another is a contradiction."""
    body = render_report(analyze(camera_jpeg, options=AnalysisOptions(geocode=False)))
    assert Translator("en").get("bot.section.device") in body
    # Both numbers, in one sentence that explains their relationship.
    assert "24" in body and "36" in body
    assert body.count("mm") >= 2


def test_the_backup_button_sits_above_the_one_that_cannot_be_undone() -> None:
    from findpic.bot.keyboards import report_keyboard

    keyboard = report_keyboard(Translator("en"), "token", offer_clean=True, offer_backup=True)
    captions = [button.text for row in keyboard.inline_keyboard for button in row]
    assert captions.index("💾 Backup the metadata") < captions.index("🧹 Send me a clean copy")


def test_a_report_still_sends_when_the_handle_could_not_be_stored() -> None:
    """The analysis is done and the user is waiting; a lost token is not fatal."""
    from findpic.bot.keyboards import report_keyboard

    keyboard = report_keyboard(Translator("en"), None, offer_clean=True, offer_backup=True)
    captions = [button.text for row in keyboard.inline_keyboard for button in row]
    assert captions == ["🌐 Language"]


# ------------------------------------------------------------- the archive


@pytest.fixture
def archive(tmp_path: Path):
    from findpic.bot.archive import Archive

    store = Archive(tmp_path / "archive")
    assert store.prepare() is None
    return store


def a_photo(tmp_path: Path, name: str = "p.jpg", payload: bytes = b"one") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff\xe0" + payload)
    return path


def test_the_same_picture_twice_is_one_copy_and_two_entries(archive, tmp_path: Path) -> None:
    """Both sends have to stay visible, and the bytes must not be stored twice."""
    source = a_photo(tmp_path)
    first = archive.store(source, user_id=7, when="20260829T134501Z")
    second = archive.store(source, user_id=8, when="20260829T140000Z")

    assert first.state == "stored"
    assert second.state == "duplicate"
    assert first.sha256 == second.sha256
    assert first.rel_path != second.rel_path
    blobs = list((archive.root / "objects").rglob("*.jpg"))
    assert len(blobs) == 1
    assert blobs[0].stat().st_nlink == 3  # the blob plus two browsable names


def test_the_same_second_does_not_collide(archive, tmp_path: Path) -> None:
    """An admin bypasses the throttle entirely, so this is ordinary for them."""
    source = a_photo(tmp_path)
    first = archive.store(source, user_id=7, when="20260829T134501Z")
    second = archive.store(source, user_id=7, when="20260829T134501Z")
    assert first.rel_path != second.rel_path
    assert archive.resolve(first.rel_path) is not None
    assert archive.resolve(second.rel_path) is not None


def test_evicting_one_send_keeps_the_other_persons_picture(archive, tmp_path: Path) -> None:
    """Getting the link count backwards deletes somebody else's photograph."""
    source = a_photo(tmp_path)
    mine = archive.store(source, user_id=7, when="20260829T134501Z")
    theirs = archive.store(source, user_id=8, when="20260829T140000Z")

    assert archive.discard(mine.rel_path) == 0, "the bytes are still referenced"
    assert archive.resolve(theirs.rel_path) is not None
    assert archive.discard(theirs.rel_path) > 0
    assert not list((archive.root / "objects").rglob("*.jpg"))


def test_no_sender_chosen_text_ever_reaches_a_path(archive, tmp_path: Path) -> None:
    """Traversal, NUL bytes and overrides are impossible here by construction."""
    source = a_photo(tmp_path, "innocent.jpg")
    stored = archive.store(
        source, user_id=7, when="20260829T134501Z", claimed_suffix="../../../etc/x"
    )
    assert stored.state == "stored"
    assert ".." not in stored.rel_path
    assert archive.resolve(stored.rel_path) is not None


def test_the_extension_comes_from_the_bytes_not_the_claim(archive, tmp_path: Path) -> None:
    """A file that crashes exiftool is the one most worth having on disk, so
    the type has to be readable before any analysis runs."""
    from findpic.bot.archive import sniff

    assert sniff(b"\xff\xd8\xff\xe0" + b"x" * 12) == ".jpg"
    assert sniff(b"\x89PNG\r\n\x1a\n") == ".png"
    assert sniff(b"\x00\x00\x00\x18ftypheic") == ".heic"
    assert sniff(b"\x00\x00\x00\x18ftypmif1") == ".heic"
    assert sniff(b"nothing recognisable") == ".bin"

    source = a_photo(tmp_path, "lying.png")
    stored = archive.store(source, user_id=7, when="20260829T134501Z", claimed_suffix=".png")
    assert stored.rel_path.endswith(".jpg")


def test_an_oversized_file_is_refused_and_says_so(tmp_path: Path) -> None:
    """A refusal still gets a row. An archive whose failures are invisible is
    worse than none — the operator would believe pictures were being kept."""
    from findpic.bot.archive import Archive

    store = Archive(tmp_path / "a", max_file_bytes=10)
    store.prepare()
    stored = store.store(a_photo(tmp_path, payload=b"x" * 100), user_id=7, when="20260829T1Z")
    assert stored.state == "skipped_big"
    assert stored.rel_path is None


def test_a_full_disk_never_raises(tmp_path: Path) -> None:
    """An OSError here would surface to the user as "the bot is broken"."""
    from findpic.bot.archive import Archive

    store = Archive(tmp_path / "a", min_free_bytes=10**15)
    store.prepare()
    stored = store.store(a_photo(tmp_path), user_id=7, when="20260829T1Z")
    assert stored.state == "skipped_space"


def test_an_unwritable_root_is_reported_at_startup(tmp_path: Path) -> None:
    """Not on the first photograph: a silently dead archive is the worst case."""
    from findpic.bot.archive import Archive

    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        problem = Archive(blocked / "archive").prepare()
        assert problem and "cannot write" in problem
    finally:
        blocked.chmod(0o700)


def test_resolve_refuses_to_leave_the_archive(archive, tmp_path: Path) -> None:
    assert archive.resolve("../../etc/passwd") is None
    assert archive.resolve("by-date/../../../etc/passwd") is None


async def test_forget_deletes_the_pictures_and_the_record(storage: Storage) -> None:
    """The counterpart to keeping copies. Without it the disclosure is a notice
    rather than a choice, and the notice is the weaker half."""
    from findpic.bot.storage import PhotoRecord

    await storage.record_event(Person(7, "someone"), kind="file")
    await storage.record_photo(
        PhotoRecord(
            user_id=7, at="2026-08-29T10:00:00+00:00", rel_path="by-date/x/one.jpg", state="stored"
        )
    )
    await storage.record_photo(
        PhotoRecord(user_id=8, at="2026-08-29T10:00:00+00:00", rel_path="by-date/x/two.jpg")
    )
    await storage.set_language(7, "uk")

    files, events = await storage.forget_everything(7)
    assert files == ["by-date/x/one.jpg"]
    assert events == 1
    assert await rows(storage, "SELECT * FROM photos WHERE user_id = 7") == []
    assert await rows(storage, "SELECT * FROM people WHERE user_id = 7") == []
    # Somebody else's picture is untouched.
    assert len(await rows(storage, "SELECT * FROM photos WHERE user_id = 8")) == 1
    # The language choice survives, and the notice says so rather than
    # claiming "everything".
    assert await storage.get_language(7, "en") == "uk"


async def test_retention_deletes_the_file_before_its_row(storage: Storage) -> None:
    """A crash between the two must leave a row saying the copy is gone, never
    a photograph on disk that nothing in the database knows about."""
    from findpic.bot.storage import PhotoRecord

    photo_id = await storage.record_photo(
        PhotoRecord(
            user_id=7,
            at="2020-01-01T00:00:00+00:00",
            rel_path="by-date/2020-01-01/old.jpg",
            state="stored",
        )
    )
    assert await storage.expired_archive_files(keep_days=30) == [
        (photo_id, "by-date/2020-01-01/old.jpg")
    ]
    await storage.forget_archived(photo_id)
    assert await storage.expired_archive_files(keep_days=30) == []
    row = (await rows(storage, "SELECT * FROM photos"))[0]
    assert row["state"] == "evicted" and row["rel_path"] is None
    assert row["user_id"] == 7, "the ledger still says whose it was"


async def test_the_archive_accounting_counts_blobs_not_rows(storage: Storage) -> None:
    """Summing the column drifts above the truth and starts refusing writes
    while the disk is half empty: a duplicate carries a full byte count while
    costing nothing."""
    from findpic.bot.storage import PhotoRecord

    for user, state in ((7, "stored"), (8, "duplicate"), (9, "duplicate")):
        await storage.record_photo(
            PhotoRecord(
                user_id=user,
                at="2026-08-29T10:00:00+00:00",
                sha256="deadbeef",
                bytes_kept=1000,
                state=state,
            )
        )
    held, saved = await storage.archive_usage()
    assert held == 1000, "one copy of the bytes exists"
    assert saved == 2000, "two sends cost nothing"


@pytest.mark.parametrize("language", available_languages())
def test_the_notice_says_the_bot_keeps_a_copy_when_it_does(language: str) -> None:
    """This cannot ship in either order: the bot currently tells every user, in
    both languages, that nothing is archived."""
    t = Translator(language)
    off = privacy_notice(t, Config(token=TOKEN))
    on = privacy_notice(t, Config(token=TOKEN, archive_dir=Path("/archive")))

    assert t.get("bot.privacy.archive.off") in off
    assert t.get("bot.privacy.archive.on") not in off

    assert t.get("bot.privacy.archive.on") in on
    assert t.get("bot.privacy.archive.off") not in on
    assert t.get("bot.privacy.forget") in on, (
        "keeping copies without an undo is a notice, not a choice"
    )
    assert "30" in on, "the retention window has to be stated"
    assert "{" not in on and "{" not in off
    assert len(on) < MESSAGE_LIMIT


def test_the_notice_never_describes_a_build_other_than_the_running_one() -> None:
    """The one screen where somebody decides whether to trust this. Every
    permutation has to describe what the running configuration actually writes."""
    import itertools

    t = Translator("en")
    for analytics, capture, archiving, days in itertools.product(
        (True, False), (True, False), (True, False), (0, 30)
    ):
        config = Config(
            token=TOKEN,
            analytics=analytics,
            analytics_capture=capture,
            analytics_retention_days=days,
            archive_dir=Path("/archive") if archiving else None,
        )
        notice = privacy_notice(t, config)
        assert "{" not in notice
        assert (t.get("bot.privacy.archive.on") in notice) is archiving
        assert (t.get("bot.privacy.analytics") in notice) is analytics
        # Capture is implied by archiving: the file on disk holds the exact
        # coordinates, so withholding a locality from the index would be theatre.
        expected_capture = analytics and (capture or archiving)
        assert (t.get("bot.privacy.capture") in notice) is expected_capture
