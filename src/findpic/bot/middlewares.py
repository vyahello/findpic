"""Middlewares: language resolution, access control, and rate limiting.

Every handler needs the user's language and every media handler needs the quota
check, so both live here rather than being repeated — and, more importantly, so
neither can be forgotten when a handler is added.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from ..i18n import Translator, available_languages
from .config import Config
from .storage import Storage

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


class AccessMiddleware(BaseMiddleware):
    """Enforce the allowlist when one is configured."""

    def __init__(self, config: Config) -> None:
        self.config = config

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

        translator: Translator = data["t"]
        if verdict.reason == "throttled":
            text = translator.get("bot.error.throttled", seconds=verdict.retry_after)
        else:
            text = translator.get("bot.error.quota", limit=verdict.limit)
        if isinstance(event, Message):
            await event.answer(text)
        logger.info("rate-limited user=%s reason=%s", user.id, verdict.reason)
        return None
