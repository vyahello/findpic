"""Bootstrap: build the bot, wire the middlewares, and run it.

Dependencies are injected through the dispatcher's workflow data rather than
module-level globals, so handlers stay testable and nothing is constructed at
import time.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from ..exif import ExifTool, ExifToolMissing
from ..i18n import Translator, available_languages
from .config import Config, ConfigError
from .handlers import router
from .middlewares import AccessMiddleware, LanguageMiddleware, ThrottleMiddleware
from .service import AnalysisService
from .storage import Storage

logger = logging.getLogger("findpic.bot")

#: Commands offered in Telegram's menu, per language.
MENU_COMMANDS = ("help", "lang", "privacy", "about")

#: How often expired analysis handles are swept.
CLEANUP_INTERVAL = 3600


def build_session(config: Config) -> AiohttpSession | None:
    """A session pointed at a self-hosted Bot API server, when one is configured.

    ``is_local`` matters: it tells aiogram that ``getFile`` returns a filesystem
    path rather than a URL, which is what removes the 20 MB download ceiling.
    """
    if not config.api_base:
        return None
    return AiohttpSession(
        api=TelegramAPIServer.from_base(config.api_base, is_local=config.api_is_local)
    )


async def set_bot_commands(bot: Bot) -> None:
    """Publish the command menu in every language the bot speaks."""
    for code in available_languages():
        translator = Translator(code)
        commands = [
            BotCommand(command=name, description=translator.get(f"bot.command.{name}"))
            for name in MENU_COMMANDS
        ]
        try:
            await bot.set_my_commands(
                commands,
                scope=BotCommandScopeAllPrivateChats(),
                language_code=None if code == "en" else code,
            )
        except Exception as error:  # noqa: BLE001 - never block startup on this
            logger.warning("could not set commands for %s: %s", code, error)


async def cleanup_loop(storage: Storage, config: Config) -> None:
    """Drop stale analysis handles so the database stays small."""
    import time

    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            removed = await storage.purge_expired(time.time() - config.analysis_ttl_seconds)
            await storage.purge_old_usage()
            if removed:
                logger.info("purged %s expired analysis handles", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("cleanup pass failed")


async def run(config: Config) -> None:
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Telegram tokens must never reach a log line.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    exiftool = ExifTool()
    logger.info("findpic bot starting · exiftool %s · %s", exiftool.version(), config.describe())

    storage = Storage(config.database_path)
    await storage.connect()

    session = build_session(config)
    bot = Bot(
        token=config.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )

    dispatcher = Dispatcher()
    dispatcher.workflow_data.update(
        config=config,
        storage=storage,
        service=AnalysisService(config, exiftool),
    )

    # Order matters: language first so every later middleware can phrase its
    # refusal in the user's own language.
    language = LanguageMiddleware(storage, config)
    access = AccessMiddleware(config)
    dispatcher.message.middleware(language)
    dispatcher.callback_query.middleware(language)
    dispatcher.message.middleware(access)
    dispatcher.callback_query.middleware(access)
    # Only the media handlers are metered; commands and buttons stay free.
    router.message.middleware.register(ThrottleMiddleware(storage, config))

    dispatcher.include_router(router)

    await set_bot_commands(bot)
    cleaner = asyncio.create_task(cleanup_loop(storage, config))

    try:
        me = await bot.get_me()
        logger.info("connected as @%s (id=%s)", me.username, me.id)
        await dispatcher.start_polling(
            bot,
            drop_pending_updates=config.drop_pending_updates,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        cleaner.cancel()
        await asyncio.gather(cleaner, return_exceptions=True)
        await storage.close()
        await bot.session.close()
        logger.info("findpic bot stopped")


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    try:
        asyncio.run(run(config))
    except ExifToolMissing as error:
        print(f"{error}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
