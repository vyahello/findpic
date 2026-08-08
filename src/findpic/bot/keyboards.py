"""Inline keyboards.

Callback payloads go through aiogram's ``CallbackData`` factory rather than
hand-rolled strings, so the 64-byte limit is enforced at construction time
instead of failing silently at runtime. Analysis handles are short tokens for
exactly that reason — a Telegram ``file_id`` alone would overflow the budget.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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
    translator: Translator, token: str, *, offer_clean: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if offer_clean:
        builder.button(
            text=translator.get("bot.button.clean"),
            callback_data=AnalysisCallback(action="clean", token=token),
        )
    builder.button(
        text=translator.get("bot.button.tags"),
        callback_data=AnalysisCallback(action="tags", token=token),
    )
    builder.button(
        text=translator.get("bot.button.language"),
        callback_data=LanguageCallback(code="menu"),
    )
    builder.adjust(1)
    return builder.as_markup()


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
