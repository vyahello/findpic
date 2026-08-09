"""Bot handlers.

The central UX problem this bot has to solve: **Telegram destroys the thing the
bot exists to read.** Sending a picture the ordinary way ("as photo") re-encodes
it and strips every tag, so the analysis comes back empty and the user concludes
the bot is broken. Sending the same picture "as file" delivers the original
bytes untouched.

So the bot detects which route a picture arrived by, analyses it either way, and
when it arrives as a compressed photo it explains — before the empty result, not
after — why there is nothing to see and how to get a real answer.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..exif import ExifTool, ExifToolError
from ..i18n import LANGUAGE_NAMES, Translator
from ..models import Category, Report, Severity
from .config import Config
from .format import esc, render_report, render_tag_dump
from .keyboards import (
    AnalysisCallback,
    LanguageCallback,
    help_keyboard,
    language_keyboard,
    main_keyboard,
    menu_labels,
    report_keyboard,
)
from .service import AnalysisService
from .storage import Storage

logger = logging.getLogger(__name__)

router = Router(name="findpic")
#: Analysing a file costs CPU and disk; tapping a button costs nothing. Keeping
#: them on separate routers is what lets the throttle apply to one and not the
#: other — otherwise every menu tap would burn a user's rate limit.
media_router = Router(name="findpic-media")

#: MIME types we will attempt. exiftool reads far more than this, but a bot that
#: accepts .exe "for analysis" is a service nobody should run.
IMAGE_MIME_PREFIXES = ("image/", "video/")
IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".raf",
    ".pef",
    ".srw",
    ".mp4",
    ".mov",
}


# ------------------------------------------------------------------- commands


@router.message(CommandStart())
async def handle_start(message: Message, t: Translator, config: Config) -> None:
    await message.answer(
        t.get("bot.start", name=esc(message.from_user.first_name if message.from_user else "")),
        # The persistent keyboard arrives with the welcome, so the very first
        # thing a new user sees is buttons rather than a list of slash commands.
        reply_markup=main_keyboard(t),
        disable_web_page_preview=True,
    )


async def show_help(message: Message, t: Translator) -> None:
    await message.answer(
        t.get("bot.help"), reply_markup=help_keyboard(t), disable_web_page_preview=True
    )


async def show_language(message: Message, t: Translator, language: str) -> None:
    await message.answer(t.get("bot.language.prompt"), reply_markup=language_keyboard(language))


async def show_privacy(message: Message, t: Translator, config: Config) -> None:
    await message.answer(
        t.get("bot.privacy", ttl_hours=config.analysis_ttl_seconds // 3600),
        disable_web_page_preview=True,
    )


async def show_about(message: Message, t: Translator) -> None:
    from .. import __version__

    exiftool_version = "?"
    with contextlib.suppress(ExifToolError):
        exiftool_version = ExifTool().version()
    await message.answer(
        t.get("bot.about", version=__version__, exiftool=exiftool_version),
        reply_markup=help_keyboard(t),
        disable_web_page_preview=True,
    )


@router.message(Command("help"))
async def handle_help(message: Message, t: Translator) -> None:
    await show_help(message, t)


@router.message(Command("lang", "language", "mova"))
async def handle_language_command(message: Message, t: Translator, language: str) -> None:
    await show_language(message, t, language)


@router.message(Command("privacy"))
async def handle_privacy(message: Message, t: Translator, config: Config) -> None:
    await show_privacy(message, t, config)


@router.message(Command("about"))
async def handle_about(message: Message, t: Translator) -> None:
    await show_about(message, t)


class MenuButton(BaseFilter):
    """Matches a tap on the persistent keyboard.

    Reply-keyboard buttons send their caption as plain text, so the caption has
    to be mapped back to an action. Every language's captions are accepted, not
    just the reader's: switching language leaves the old keyboard on screen until
    Telegram replaces it, and those stale buttons should still work.
    """

    def __init__(self) -> None:
        self.labels = menu_labels()

    async def __call__(self, message: Message) -> dict[str, str] | bool:
        action = self.labels.get((message.text or "").strip())
        return {"menu_action": action} if action else False


@router.message(MenuButton())
async def handle_menu_button(
    message: Message, menu_action: str, t: Translator, language: str, config: Config
) -> None:
    if menu_action == "help":
        await show_help(message, t)
    elif menu_action == "language":
        await show_language(message, t, language)
    elif menu_action == "privacy":
        await show_privacy(message, t, config)
    elif menu_action == "about":
        await show_about(message, t)


# ------------------------------------------------------------------ language


@router.callback_query(LanguageCallback.filter())
async def handle_language_choice(
    query: CallbackQuery,
    callback_data: LanguageCallback,
    storage: Storage,
    language: str,
    t: Translator,
) -> None:
    if callback_data.code == "menu":
        await query.message.answer(
            t.get("bot.language.prompt"), reply_markup=language_keyboard(language)
        )
        await query.answer()
        return

    code = callback_data.code
    if code not in LANGUAGE_NAMES:
        await query.answer()
        return

    await storage.set_language(query.from_user.id, code)
    chosen = Translator(code)
    await query.answer(chosen.get("bot.language.changed", language=chosen.language_name()))
    # The persistent keyboard is captioned in the old language until it is
    # replaced, so send a fresh one immediately.
    await query.message.answer(chosen.get("bot.menu.switched"), reply_markup=main_keyboard(chosen))
    try:
        await query.message.edit_text(
            chosen.get("bot.language.prompt"), reply_markup=language_keyboard(code)
        )
    except Exception:  # noqa: BLE001 - message may be too old to edit
        await query.message.answer(chosen.get("bot.language.prompt"))


# --------------------------------------------------------------------- media


def _looks_like_image(file_name: str | None, mime: str | None) -> bool:
    if mime and mime.startswith(IMAGE_MIME_PREFIXES):
        return True
    return bool(file_name and Path(file_name).suffix.lower() in IMAGE_SUFFIXES)


@media_router.message(F.photo)
async def handle_compressed_photo(
    message: Message,
    bot: Bot,
    t: Translator,
    config: Config,
    storage: Storage,
    service: AnalysisService,
    **_: object,
) -> None:
    """A picture sent the ordinary way — Telegram already stripped it."""
    photo = message.photo[-1]
    await _analyse_and_reply(
        message=message,
        bot=bot,
        t=t,
        config=config,
        storage=storage,
        service=service,
        file_id=photo.file_id,
        file_name=f"telegram_photo_{photo.file_unique_id}.jpg",
        file_size=photo.file_size or 0,
        compressed=True,
    )


@media_router.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
    t: Translator,
    config: Config,
    storage: Storage,
    service: AnalysisService,
    **_: object,
) -> None:
    """A picture sent as a file — the original bytes, which is what we want."""
    document = message.document
    if not _looks_like_image(document.file_name, document.mime_type):
        await message.answer(t.get("bot.error.not_an_image"))
        return
    await _analyse_and_reply(
        message=message,
        bot=bot,
        t=t,
        config=config,
        storage=storage,
        service=service,
        file_id=document.file_id,
        file_name=document.file_name or f"file_{document.file_unique_id}",
        file_size=document.file_size or 0,
        compressed=False,
    )


@media_router.message(F.video | F.animation | F.video_note | F.audio | F.voice)
async def handle_other_media(message: Message, t: Translator) -> None:
    await message.answer(t.get("bot.error.send_as_file"))


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, t: Translator) -> None:
    # Someone typing at the bot has not found the buttons, so send them.
    await message.answer(t.get("bot.error.send_a_photo"), reply_markup=main_keyboard(t))


async def _analyse_and_reply(
    *,
    message: Message,
    bot: Bot,
    t: Translator,
    config: Config,
    storage: Storage,
    service: AnalysisService,
    file_id: str,
    file_name: str,
    file_size: int,
    compressed: bool,
) -> None:
    if file_size and file_size > config.max_file_bytes:
        await message.answer(t.get("bot.error.too_big", limit=t.bytes(config.max_file_bytes)))
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        report = await service.analyse(
            bot=bot, file_id=file_id, file_name=file_name, language=t.language
        )
    except ExifToolError as error:
        logger.warning("analysis failed for %s: %s", file_name, error)
        await message.answer(t.get("bot.error.unreadable"))
        return
    except Exception:  # noqa: BLE001 - one bad file must not kill the bot
        logger.exception("unexpected failure analysing %s", file_name)
        await message.answer(t.get("bot.error.internal"))
        return

    token = await storage.remember_analysis(
        user_id=message.from_user.id,
        file_id=file_id,
        file_name=file_name,
        now=time.time(),
    )

    note = ""
    if compressed:
        # Said up front, because otherwise an empty report reads as a broken bot.
        note = f"⚠️ <b>{esc(t.get('bot.note.compressed.title'))}</b>\n{esc(t.get('bot.note.compressed.body'))}"

    body = render_report(report, source_note=note)
    await message.answer(
        body,
        reply_markup=report_keyboard(
            t,
            token,
            offer_clean=_worth_cleaning(report) and not compressed,
            # Never offer to strip a file without offering to keep a copy of
            # what is about to be removed.
            offer_backup=_worth_cleaning(report) and not compressed,
        ),
        disable_web_page_preview=True,
    )


def _worth_cleaning(report: Report) -> bool:
    """Only offer the clean-copy button when there is something to remove."""
    return any(
        finding.category is Category.PRIVACY and finding.severity.rank >= Severity.NOTICE.rank
        for finding in report.findings
    )


# ------------------------------------------------------------ report actions


@router.callback_query(AnalysisCallback.filter(F.action == "tags"))
async def handle_show_tags(
    query: CallbackQuery,
    callback_data: AnalysisCallback,
    bot: Bot,
    t: Translator,
    storage: Storage,
    service: AnalysisService,
) -> None:
    handle = await storage.recall_analysis(callback_data.token, query.from_user.id)
    if handle is None:
        await query.answer(t.get("bot.error.expired"), show_alert=True)
        return

    await query.answer(t.get("bot.status.working"))
    try:
        report = await service.analyse(
            bot=bot,
            file_id=handle.file_id,
            file_name=handle.file_name or "photo",
            language=t.language,
        )
    except Exception:  # noqa: BLE001
        logger.exception("tag dump failed")
        await query.message.answer(t.get("bot.error.internal"))
        return

    dump = render_tag_dump(report).encode("utf-8")
    await query.message.answer_document(
        BufferedInputFile(dump, filename=f"{Path(report.file.name).stem}.metadata.txt"),
        caption=t.get("bot.tags.caption", count=report.tag_count),
    )


@router.callback_query(AnalysisCallback.filter(F.action == "backup"))
async def handle_backup(
    query: CallbackQuery,
    callback_data: AnalysisCallback,
    bot: Bot,
    t: Translator,
    storage: Storage,
    service: AnalysisService,
) -> None:
    handle = await storage.recall_analysis(callback_data.token, query.from_user.id)
    if handle is None:
        await query.answer(t.get("bot.error.expired"), show_alert=True)
        return

    await query.answer(t.get("bot.status.working"))
    await bot.send_chat_action(query.message.chat.id, ChatAction.UPLOAD_DOCUMENT)
    try:
        data, name = await service.backup(
            bot=bot, file_id=handle.file_id, file_name=handle.file_name or "photo.jpg"
        )
    except Exception:  # noqa: BLE001
        logger.exception("backup failed")
        await query.message.answer(t.get("bot.backup.failed"))
        return

    await query.message.answer_document(
        BufferedInputFile(data, filename=name),
        caption=t.get("bot.backup.caption", size=t.bytes(len(data)), name=name),
    )


@router.callback_query(AnalysisCallback.filter(F.action == "clean"))
async def handle_clean_copy(
    query: CallbackQuery,
    callback_data: AnalysisCallback,
    bot: Bot,
    t: Translator,
    storage: Storage,
    service: AnalysisService,
) -> None:
    handle = await storage.recall_analysis(callback_data.token, query.from_user.id)
    if handle is None:
        await query.answer(t.get("bot.error.expired"), show_alert=True)
        return

    await query.answer(t.get("bot.status.cleaning"))
    await bot.send_chat_action(query.message.chat.id, ChatAction.UPLOAD_DOCUMENT)

    try:
        cleaned, name, removed = await service.clean(
            bot=bot, file_id=handle.file_id, file_name=handle.file_name or "photo.jpg"
        )
    except Exception:  # noqa: BLE001
        logger.exception("cleaning failed")
        await query.message.answer(t.get("bot.error.internal"))
        return

    await query.message.answer_document(
        BufferedInputFile(cleaned, filename=name),
        caption=t.get("bot.clean.caption", removed=removed),
    )
