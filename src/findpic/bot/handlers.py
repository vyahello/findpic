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
import datetime as dt
import json
import logging
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..exif import ExifTool, ExifToolError
from ..i18n import LANGUAGE_NAMES, Translator
from ..models import Category, Report, Severity
from ..util import parse_exif_datetime
from .archive import Stored
from .config import Config
from .filenames import IMAGE_SUFFIXES, safe_suffix
from .format import esc, render_report, render_tag_dump, report_is_stripped
from .keyboards import (
    AnalysisCallback,
    LanguageCallback,
    help_keyboard,
    language_keyboard,
    main_keyboard,
    menu_labels,
    report_keyboard,
)
from .service import AnalysisService, CleanResult, KeepRequest
from .storage import PhotoRecord, QuotaVerdict, Storage

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def _quietly(coro: Awaitable[T], what: str, default: T | None = None) -> T | None:
    """Await something whose failure must not cost the user their answer.

    Everything the bot writes down about a request — the usage record, the
    button handle, the photo ledger — is bookkeeping. An analysis that
    succeeded and then died on an UPDATE leaves the user with "Something broke
    on my side" for work that was done and paid for. The audit middleware has
    always swallowed its own failures for this reason (middlewares.py); this
    puts the media path on the same footing, and routes every such await
    through one place so the next one added is guarded by construction.
    """
    try:
        return await coro
    except Exception:  # noqa: BLE001 - deliberate: see above
        logger.exception("could not %s", what)
        return default


def compressed_note(t: Translator) -> str:
    """Why an empty report is not a broken bot, and how to get a real one.

    NOT escaped, and that is not an oversight. ``esc()`` belongs on a catalogue
    string only when *metadata* is interpolated into it — that is what
    neutralises an Artist tag set to ``</b><script>``. These two strings take no
    parameters and carry their own markup, so escaping them printed the tags to
    the reader: for months everyone who sent a picture the ordinary way was told
    to send it "as a &lt;b&gt;file&lt;/b&gt;", which is the single instruction
    the whole bot depends on.
    """
    return "\n".join(
        (
            f"⚠️ <b>{t.get('bot.note.compressed.title')}</b>",
            t.get("bot.note.compressed.body"),
        )
    )


async def _refuse(storage: Storage, event_id: int | None, user, reason: str) -> None:
    """Record why the bot declined, and give back the quota slot it charged.

    ThrottleMiddleware spends a slot before the handler has looked at the file,
    so a file the bot then rejects as too big, as not an image, or as
    unreadable has already been billed. Nobody noticed while the count was
    invisible; the moment the report shows "9 of 10 used today" under a refusal,
    the bot is visibly charging for work it declined to do.
    """
    await _quietly(storage.note_outcome(event_id, reason), "note the refusal")
    if user is not None:
        await _quietly(storage.refund(user.id), "refund the quota")


router = Router(name="findpic")
#: Analysing a file costs CPU and disk; tapping a button costs nothing. Keeping
#: them on separate routers is what lets the throttle apply to one and not the
#: other — otherwise every menu tap would burn a user's rate limit.
media_router = Router(name="findpic-media")

#: MIME types we will attempt. exiftool reads far more than this, but a bot that
#: accepts .exe "for analysis" is a service nobody should run. The suffix list
#: lives beside the sanitiser that has to agree with it.
IMAGE_MIME_PREFIXES = ("image/", "video/")


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
    event_id: int | None = None,
    quota: QuotaVerdict | None = None,
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
        event_id=event_id,
        quota=quota,
    )


@media_router.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
    t: Translator,
    config: Config,
    storage: Storage,
    service: AnalysisService,
    event_id: int | None = None,
    quota: QuotaVerdict | None = None,
    **_: object,
) -> None:
    """A picture sent as a file — the original bytes, which is what we want."""
    document = message.document
    if not _looks_like_image(document.file_name, document.mime_type):
        await _refuse(storage, event_id, message.from_user, "not_an_image")
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
        mime_type=document.mime_type,
        event_id=event_id,
        quota=quota,
    )


# On the unmetered router deliberately: this path never analyses anything,
# so charging a daily slot for it would bill the user for a signpost.
@router.message(F.video | F.animation | F.video_note | F.audio | F.voice)
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
    mime_type: str | None = None,
    event_id: int | None = None,
    quota: QuotaVerdict | None = None,
) -> None:
    if file_size and file_size > config.max_file_bytes:
        await _refuse(storage, event_id, message.from_user, "too_big")
        await message.answer(t.get("bot.error.too_big", limit=t.bytes(config.max_file_bytes)))
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    started = time.monotonic()
    keep = None
    if config.archiving:
        held, _ = await _quietly(storage.archive_usage(), "read the archive size", (0, 0))
        keep = KeepRequest(
            user_id=message.from_user.id,
            when=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            held_bytes=held,
            user_bytes=await _quietly(
                storage.user_archive_bytes(message.from_user.id), "read a user's usage", 0
            )
            or 0,
        )
    try:
        analysis = await service.analyse(
            bot=bot,
            file_id=file_id,
            file_name=file_name,
            language=t.language,
            keep=keep,
        )
        report = analysis.report
    except ExifToolError as error:
        logger.warning("analysis failed for %s: %s", file_name, error)
        await _keep_receipt(storage, message.from_user, keep)
        await _refuse(storage, event_id, message.from_user, "unreadable")
        await message.answer(t.get("bot.error.unreadable"))
        return
    except Exception:  # noqa: BLE001 - one bad file must not kill the bot
        logger.exception("unexpected failure analysing %s", file_name)
        # Deliberately not refunded: a file that reliably crashes the analysis
        # would otherwise be an unlimited retry loop.
        await _keep_receipt(storage, message.from_user, keep)
        await _quietly(storage.note_outcome(event_id, "failed"), "note failure")
        await message.answer(t.get("bot.error.internal"))
        return

    offer_clean = _worth_cleaning(report) and not compressed
    # ANALYTICS=0 is documented as "the bot writes nothing but the language
    # preference". The photo ledger is the most detailed thing the bot records,
    # so it has to obey that switch before anything else does.
    photo_id = None
    if not config.analytics:
        # ANALYTICS=0 does not mean "keep the picture and lose the receipt".
        # Without a row nothing can ever delete the file — the retention sweep
        # works from the ledger — and it is invisible to the disk budget. Six
        # columns, none of which describes the photograph: no camera, no
        # verdict, no filename.
        await _keep_receipt(storage, message.from_user, keep)
    if config.analytics:
        photo_id = await _quietly(
            storage.record_photo(
                build_photo_record(
                    report,
                    user_id=message.from_user.id,
                    event_id=event_id,
                    chat_type=message.chat.type,
                    compressed=compressed,
                    mime_type=mime_type,
                    # The extension, never the whole name. The full name
                    # lives in `analyses` for the hours the report's buttons
                    # need it; this row lives for the whole retention window,
                    # which is a different bargain and a smaller one.
                    claimed_name=None if compressed else safe_suffix(file_name, "") or None,
                    clean_offered=offer_clean,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    capture=config.keeps_capture,
                    # Recorded even when the archive refused. An archive whose
                    # failures are invisible is worse than none: the operator
                    # would believe pictures were being kept and find out only
                    # when they went looking for one.
                    stored=analysis.stored,
                )
            ),
            "record the photo",
        )

    token = await _quietly(
        storage.remember_analysis(
            user_id=message.from_user.id,
            file_id=file_id,
            file_name=file_name,
            photo_id=photo_id,
            now=time.time(),
        ),
        "remember the analysis",
    )

    note = ""
    if compressed:
        # Said up front, because otherwise an empty report reads as a broken bot.
        #
        # NOT escaped, and that is not an oversight. esc() belongs on a
        # catalogue string only when *metadata* is interpolated into it — that
        # is what neutralises an Artist tag set to "</b><script>". These two
        # strings take no parameters and carry their own markup, so escaping
        # them printed the tags to the user: for months every reader of this
        # note was told to send the picture "as a &lt;b&gt;file&lt;/b&gt;",
        # which is the single instruction the whole bot depends on.
        note = compressed_note(t)

    body = render_report(
        report,
        source_note=note,
        # A name the bot invented for a compressed photo means nothing to the
        # person who sent it — they never saw "telegram_photo_AgADBAADq6cx.jpg".
        name=esc(t.get("bot.headline.compressed_photo")) if compressed else "",
        footer=_quota_footer(t, quota),
    )
    await message.answer(
        body,
        reply_markup=report_keyboard(
            t,
            token,
            offer_clean=offer_clean,
            # Never offer to strip a file without offering to keep a copy of
            # what is about to be removed.
            offer_backup=offer_clean,
        ),
        disable_web_page_preview=True,
    )


async def _keep_receipt(storage: Storage, user, keep: KeepRequest | None) -> None:
    """Record a copy that was kept for a file the analysis then failed on.

    The archive runs before the analysis on purpose — a file that crashes
    exiftool is the one most worth having on disk — so a failure leaves a
    picture stored with no row naming it, invisible to the retention sweep and
    to the disk budget. This is the only thing that stops it becoming
    permanent.
    """
    if keep is None or keep.stored is None or not keep.stored.kept or user is None:
        return
    await _quietly(
        storage.record_photo(
            PhotoRecord(
                user_id=user.id,
                at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                sha256=keep.stored.sha256,
                bytes_kept=keep.stored.size,
                state=keep.stored.state,
                rel_path=keep.stored.rel_path,
            )
        ),
        "record the kept copy",
    )


def _quota_footer(t: Translator, quota: QuotaVerdict | None) -> str:
    """How much of today's allowance is left, said only when it is news.

    The middleware has always put this in the handler's context and the handler
    swallowed it through **kwargs; the string it needs was translated in both
    languages and read by no Python file. Shown from two remaining, so it is a
    warning rather than a running total — and never for an admin, who bypasses
    the quota entirely and so has a `quota` of None.
    """
    if not quota or not quota.limit or quota.limit - quota.used > 2:
        return ""
    return f"<i>{esc(t.get('bot.quota.footer', used=quota.used, limit=quota.limit))}</i>"


def build_photo_record(
    report: Report,
    *,
    user_id: int,
    event_id: int | None,
    chat_type: str | None,
    compressed: bool,
    mime_type: str | None,
    claimed_name: str | None,
    clean_offered: bool,
    duration_ms: int,
    capture: bool,
    stored: Stored | None = None,
) -> PhotoRecord:
    """One picture's row, from what the analysis actually found.

    Telegram tells a bot nothing about the device on the other end — no model,
    no operating system, no app version, no location. The picture does, when it
    still has its tags, and that is the only honest source for "what do my users
    shoot with". Everything here is read out of the photograph, which is a
    different claim from "this is the sender's phone", and the report labels it
    as one.

    ``capture`` gates the date and the place. Left off, the bot can say
    truthfully that it never records when or where a picture was taken.
    """
    device, image, place = report.device, report.image, report.location

    record = PhotoRecord(
        user_id=user_id,
        at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        event_id=event_id,
        sent_as="photo" if compressed else "file",
        chat_type=chat_type,
        file_type=report.file.file_type,
        mime_type=mime_type,
        claimed_name=claimed_name,
        size_bytes=report.file.size_bytes or None,
        width=image.width,
        height=image.height,
        make=device.make,
        model=device.model,
        os=device.os,
        lens=device.lens_model,
        editor=device.editor,
        # Levels, not labels: a label resolves through the message catalogue, so
        # the same row would read differently depending on who ran the report.
        originality=_level(report, "originality"),
        privacy=_level(report, "privacy"),
        structure=_level(report, "structure"),
        tag_count=report.tag_count,
        finding_ids=json.dumps(
            sorted({finding.id for finding in report.findings}), separators=(",", ":")
        ),
        stripped=int(report_is_stripped(report)),
        had_gps=int(place.present),
        has_serial=int(bool(device.body_serial or device.lens_serial)),
        # Written when the keyboard is built, never recomputed: the button is
        # withheld from every compressed photo regardless of what was found, so
        # a conversion rate derived from the verdicts would be wrong both ways.
        clean_offered=int(clean_offered),
        duration_ms=duration_ms,
        warnings=len(report.exiftool_warnings),
        errors=len(report.errors),
        sha256=stored.sha256 if stored else None,
        bytes_kept=stored.size if stored else None,
        state=stored.state if stored else None,
        rel_path=stored.rel_path if stored else None,
    )
    if capture:
        _add_capture(record, report)
    return record


def _level(report: Report, axis: str) -> str:
    verdict = report.verdicts.get(axis)
    return verdict.level.value if verdict else "unknown"


def _add_capture(record: PhotoRecord, report: Report) -> None:
    """The day and the town, never the second and never the street.

    Two rules that do not bend. The exact capture second and a six-decimal
    coordinate describe a person's movements to the metre; a date and a locality
    answer "when and roughly where were these taken" without doing that. And the
    place is built by skipping the address tier — `location.place` routinely
    begins with a street name, which is not a locality.
    """
    taken = parse_exif_datetime(report.capture.taken) if report.capture.taken else None
    if taken is not None:
        record.taken_date = taken.date().isoformat()
        record.age_days = max(0, (dt.datetime.now(dt.timezone.utc).date() - taken.date()).days)
    record.taken_offset = report.capture.taken_offset

    location = report.location
    if location.present:
        # Two decimal places is about a kilometre. It is meaningful protection
        # for one photograph and weaker across many from one account, which is
        # the trade-off the operator is told about in .env.example.
        record.lat = round(location.latitude, 2)
        record.lon = round(location.longitude, 2)
        address = (location.place_detail or {}).get("address") or {}
        record.country = (address.get("country_code") or "").upper() or None
        record.place = _locality(address) or None


def _locality(address: dict) -> str:
    """A town and a region out of Nominatim's address parts, never a street."""
    tiers = (
        ("village", "town", "city", "municipality", "county"),
        ("state", "region", "province"),
        ("country",),
    )
    parts = [next((address[key] for key in tier if address.get(key)), None) for tier in tiers]
    return ", ".join(str(part) for part in parts if part)


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

    await _quietly(storage.note_photo_action(handle.photo_id, "tags"), "note the tap")
    await query.answer(t.get("bot.status.working"))
    try:
        # keep is not passed: pressing a button is not sending a picture, and
        # counting it as one would double every archived file in the ledger.
        analysis = await service.analyse(
            bot=bot,
            file_id=handle.file_id,
            file_name=handle.file_name or "photo",
            language=t.language,
        )
        report = analysis.report
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

    await _quietly(storage.note_photo_action(handle.photo_id, "backup"), "note the tap")
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
        caption=t.get("bot.backup.caption", size=t.bytes(len(data)), name=esc(name)),
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

    await _quietly(storage.note_photo_action(handle.photo_id, "clean"), "note the tap")
    await query.answer(t.get("bot.status.cleaning"))
    await bot.send_chat_action(query.message.chat.id, ChatAction.UPLOAD_DOCUMENT)

    try:
        result = await service.clean(
            bot=bot, file_id=handle.file_id, file_name=handle.file_name or "photo.jpg"
        )
    except Exception:  # noqa: BLE001
        logger.exception("cleaning failed")
        await query.message.answer(t.get("bot.error.internal"))
        return

    await query.message.answer_document(
        BufferedInputFile(result.data, filename=result.name),
        caption=_clean_caption(t, result),
    )


def _clean_caption(t: Translator, result: CleanResult) -> str:
    """What the strip actually cost, in the terms the reader cares about.

    A tag count is not one of those terms. "131 metadata tags removed" tells
    somebody nothing they can act on, and it was silently wrong whenever either
    exiftool read failed. What they want to know is whether the place, the
    moment and the camera are gone — so that is what this says, and when the
    count could not be taken it simply is not claimed.
    """
    if not result.lost:
        # Either nothing identifying was in the file, or the comparison could
        # not be made. Do not assert which.
        return t.get("bot.clean.caption.uncounted")
    lost = t.get("ui.list.separator").join(t.get(f"clean.lost.{name}") for name in result.lost)
    return t.get("bot.clean.caption", lost=lost)
