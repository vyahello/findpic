"""Inline keyboards.

Callback payloads go through aiogram's ``CallbackData`` factory rather than
hand-rolled strings, so the 64-byte limit is enforced at construction time
instead of failing silently at runtime. Analysis handles are short tokens for
exactly that reason — a Telegram ``file_id`` alone would overflow the budget.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..i18n import LANGUAGE_NAMES, Translator, available_languages

#: Flags are recognisable at a glance in a language menu; the name follows so the
#: choice never depends on reading a flag correctly.
LANGUAGE_FLAGS = {"en": "🇬🇧", "uk": "🇺🇦"}


class LanguageCallback(CallbackData, prefix="lang"):
    code: str


class AnalysisCallback(CallbackData, prefix="an"):
    action: str  # "clean" | "tags"
    token: str


def language_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in available_languages():
        name = LANGUAGE_NAMES.get(code, code)
        flag = LANGUAGE_FLAGS.get(code, "🌐")
        mark = " ✓" if code == current else ""
        builder.button(text=f"{flag} {name}{mark}", callback_data=LanguageCallback(code=code))
    builder.adjust(2)
    return builder.as_markup()


def report_keyboard(
    translator: Translator,
    token: str | None,
    *,
    offer_clean: bool,
    offer_backup: bool = False,
) -> InlineKeyboardMarkup:
    """The actions under a report.

    ``token`` is None when the handle could not be stored. The report is
    still worth sending — the analysis is done and the user is waiting — so
    the buttons that need a handle are simply left off rather than offered
    and then failing.
    """
    builder = InlineKeyboardBuilder()
    # Order is a safety property, not a layout preference. The irreversible
    # button used to be the top one and the backup that undoes it sat below,
    # which is the wrong way round for a tool whose whole subject is people
    # losing metadata they cannot get back.
    if token is not None:
        if offer_backup:
            builder.button(
                text=translator.get("bot.button.backup"),
                callback_data=AnalysisCallback(action="backup", token=token),
            )
        builder.button(
            text=translator.get("bot.button.tags"),
            callback_data=AnalysisCallback(action="tags", token=token),
        )
        if offer_clean:
            builder.button(
                text=translator.get("bot.button.clean"),
                callback_data=AnalysisCallback(action="clean", token=token),
            )
    builder.button(
        text=translator.get("bot.button.language"),
        callback_data=LanguageCallback(code="menu"),
    )
    builder.adjust(1)
    return builder.as_markup()


def main_keyboard(translator: Translator) -> ReplyKeyboardMarkup:
    """The persistent keyboard under the message box.

    Slash commands are discoverable only if you already know they exist, and
    typing them on a phone is tedious. These buttons send ordinary text, which a
    filter maps back to the same handlers — so both routes stay supported and
    anyone who prefers /help keeps it.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=translator.get("bot.menu.help")),
                KeyboardButton(text=translator.get("bot.menu.language")),
            ],
            [
                KeyboardButton(text=translator.get("bot.menu.privacy")),
                KeyboardButton(text=translator.get("bot.menu.about")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=translator.get("bot.menu.placeholder"),
    )


def menu_labels() -> dict[str, str]:
    """Every button caption in every language, mapped to what it does.

    Built across all languages, not just the reader's: someone can switch
    language while an old keyboard is still on screen, and the stale buttons
    should keep working rather than being echoed back as unrecognised text.
    """
    actions = ("help", "language", "privacy", "about")
    labels: dict[str, str] = {}
    for code in available_languages():
        translator = Translator(code)
        for action in actions:
            labels[translator.get(f"bot.menu.{action}")] = action
    return labels


def help_keyboard(translator: Translator) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=translator.get("bot.button.language"),
                    callback_data=LanguageCallback(code="menu").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=translator.get("bot.button.source"),
                    url="https://github.com/vyahello/findpic",
                )
            ],
        ]
    )
