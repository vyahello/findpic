"""Middlewares: language resolution, audit, access control, and rate limiting.

Every handler needs the user's language and every media handler needs the quota
check, so both live here rather than being repeated — and, more importantly, so
neither can be forgotten when a handler is added. The audit is here for the same
reason: a usage record that a new handler can forget to write is a usage record
that lies.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from ..i18n import Translator, available_languages
from .config import Config
from .keyboards import menu_labels
from .storage import Person, Storage

logger = logging.getLogger(__name__)


class LanguageMiddleware(BaseMiddleware):
    """Put a :class:`Translator` for this user into every handler's context.

    Falls back to the user's Telegram client language before the bot default, so
    a Ukrainian speaker gets Ukrainian on their very first message rather than
    having to find the menu first.
    """

    def __init__(self, storage: Storage, config: Config) -> None:
        self.storage = storage
        self.config = config
        self._supported = frozenset(available_languages())

    def _from_client(self, user: User | None) -> str | None:
        code = (getattr(user, "language_code", None) or "").split("-")[0].lower()
        return code if code in self._supported else None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        default = self._from_client(user) or self.config.language
        language = default
        if user is not None:
            language = await self.storage.get_language(user.id, default)
        data["language"] = language
        data["t"] = Translator(language)
        return await handler(event, data)


def classify(event: TelegramObject, labels: dict[str, str]) -> tuple[str, str | None]:
    """What just happened, in two words the report can group by.

    Returns ``(kind, action)``. The action is a command name, a menu action, a
    button name or a file extension — never anything the user wrote. Somebody
    typing at the bot is recorded as having typed; what they typed is theirs.
    """
    if isinstance(event, CallbackQuery):
        parts = (event.data or "").split(":")
        if parts[0] == "an" and len(parts) > 1:
            return "button", parts[1]  # the analysis handle stays out of it
        if parts[0] == "lang" and len(parts) > 1:
            return "button", f"lang:{parts[1]}"
        return "button", parts[0] or None

    if isinstance(event, Message):
        if event.photo:
            return "photo", None
        if event.document:
            return "file", (Path(event.document.file_name or "").suffix or "").lower() or None
        if event.video or event.animation or event.video_note or event.audio or event.voice:
            return "media", None
        text = (event.text or "").strip()
        if text.startswith("/"):
            return "command", text[1:].split()[0].split("@")[0].lower()[:32]
        if text in labels:
            return "menu", labels[text]
        if text:
            return "text", None
    return "other", None


class AuditMiddleware(BaseMiddleware):
    """Record who used the bot and what they asked it for.

    Runs before the allowlist so that an attempt from somebody who is not
    allowed in is still counted — "who is knocking" is half of what an operator
    wants to know from this table.

    The row id goes into the handler's context so the media handler can attach
    what the analysis found. A failure here is swallowed: a bot that stops
    answering because it could not write a statistic is worse than a gap in the
    statistics.
    """

    def __init__(self, storage: Storage, config: Config) -> None:
        self.storage = storage
        self.config = config
        self.labels = menu_labels()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if self.config.analytics and user is not None:
            kind, action = classify(event, self.labels)
            chat = data.get("event_chat")
            try:
                data["event_id"] = await self.storage.record_event(
                    Person(
                        user_id=user.id,
                        username=user.username,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        language_code=user.language_code,
                        is_premium=bool(user.is_premium),
                        is_bot=bool(user.is_bot),
                    ),
                    kind=kind,
                    action=action,
                    chat_type=getattr(chat, "type", None),
                )
            except Exception:  # noqa: BLE001 - never fail a request over a statistic
                logger.exception("could not record usage")
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    """Enforce the allowlist when one is configured."""

    def __init__(self, config: Config, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self.config.is_public:
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is not None and user.id in self.config.allowed_user_ids:
            return await handler(event, data)

        if self.storage is not None:
            await self.storage.note_outcome(data.get("event_id"), "blocked")
        translator: Translator = data["t"]
        text = translator.get("bot.error.private", user_id=user.id if user else "unknown")
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        return None


class ThrottleMiddleware(BaseMiddleware):
    """Rate limit and daily quota, applied only to work that costs something.

    Commands and button taps are cheap and stay unmetered; analysing a file is
    what consumes CPU and disk, so only that path is counted. The check and the
    increment happen in one transaction so two photos sent together cannot both
    slip past.
    """

    def __init__(self, storage: Storage, config: Config) -> None:
        self.storage = storage
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id in self.config.admin_user_ids:
            return await handler(event, data)

        verdict = await self.storage.check_and_consume(
            user.id,
            throttle_seconds=self.config.throttle_seconds,
            daily_quota=self.config.daily_quota,
            now=time.time(),
        )
        if verdict.allowed:
            data["quota"] = verdict
            return await handler(event, data)

        await self.storage.note_outcome(data.get("event_id"), verdict.reason)
        translator: Translator = data["t"]
        if verdict.reason == "throttled":
            text = translator.get("bot.error.throttled", seconds=verdict.retry_after)
        else:
            text = translator.get("bot.error.quota", limit=verdict.limit)
        if isinstance(event, Message):
            await event.answer(text)
        logger.info("rate-limited user=%s reason=%s", user.id, verdict.reason)
        return None
