"""Bootstrap: build the bot, wire the middlewares, and run it.

Dependencies are injected through the dispatcher's workflow data rather than
module-level globals, so handlers stay testable and nothing is constructed at
import time.
"""

from __future__ import annotations

import argparse
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
from ..i18n import FALLBACK_LANGUAGE, Translator, available_languages
from .archive import Archive
from .config import Config, ConfigError
from .handlers import media_router, router
from .middlewares import AccessMiddleware, AuditMiddleware, LanguageMiddleware, ThrottleMiddleware
from .service import AnalysisService
from .storage import Storage

logger = logging.getLogger("findpic.bot")

#: Commands offered in Telegram's menu, per language.
#: Commands offered in Telegram's menu, per language. `forget` appears only
#: when there is something to forget — offering to delete photographs a bot
#: does not keep is a confusing promise.
MENU_COMMANDS = ("help", "lang", "privacy", "about")
ARCHIVE_COMMANDS = ("forget",)

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


async def configure_profile(bot: Bot, config: Config | None = None) -> None:
    """Publish the bot's public profile, in every language it speaks.

    Name, descriptions and command menu are set through the API on every start
    rather than typed into @BotFather once. That keeps them in version control,
    reviewable in a diff, and identical across languages — and it means a
    redeploy is all it takes to correct a typo a user reported.

    None of it is load-bearing: a failure here is logged and the bot carries on.
    """
    for code in available_languages():
        translator = Translator(code)
        # English is the fallback profile, so it goes in with no language_code.
        language = None if code == FALLBACK_LANGUAGE else code

        offered = MENU_COMMANDS + (ARCHIVE_COMMANDS if config and config.archiving else ())
        commands = [
            BotCommand(command=name, description=translator.get(f"bot.command.{name}"))
            for name in offered
        ]
        calls = (
            (
                "commands",
                bot.set_my_commands(
                    commands,
                    scope=BotCommandScopeAllPrivateChats(),
                    language_code=language,
                ),
            ),
            (
                "name",
                bot.set_my_name(name=translator.get("bot.profile.name"), language_code=language),
            ),
            (
                "short description",
                bot.set_my_short_description(
                    short_description=translator.get("bot.profile.short"),
                    language_code=language,
                ),
            ),
            (
                "description",
                bot.set_my_description(
                    description=translator.get("bot.profile.description"),
                    language_code=language,
                ),
            ),
        )
        for what, call in calls:
            try:
                await call
            except Exception as error:  # noqa: BLE001 - never block startup
                # Telegram rate-limits name changes hard; a rejection here is
                # usually "you already set this recently", not a real problem.
                logger.warning("could not set %s for %s: %s", what, code, error)


async def purge_archive(storage: Storage, config: Config) -> int:
    """Delete kept pictures past their window — the file before its row.

    That order is deliberate. A crash between the two leaves a row pointing at a
    file that is gone: visible, harmless, and reportable as "copy already
    deleted". The other order leaves a photograph on disk that nothing in the
    database knows about — a promise broken silently, and forever.
    """
    if not config.archiving or config.archive_retention_days <= 0:
        return 0
    archive = Archive(config.archive_dir)
    dropped = 0
    for photo_id, rel_path in await storage.expired_archive_files(config.archive_retention_days):
        await asyncio.to_thread(archive.discard, rel_path)
        await storage.forget_archived(photo_id)
        dropped += 1
    return dropped


async def cleanup_loop(storage: Storage, config: Config) -> None:
    """Drop stale analysis handles so the database stays small."""
    import time

    # Sweep first, then wait. The other order means a bot that restarts more
    # often than hourly never purges anything at all, while /privacy promises a
    # retention window — and a redeploying bot restarts far more often than that.
    while True:
        try:
            removed = await storage.purge_expired(time.time() - config.analysis_ttl_seconds)
            await storage.purge_old_usage()
            forgotten, stranded = await storage.purge_old_events(config.analytics_retention_days)
            # Their rows are gone, so nothing else will ever find these files.
            if stranded and config.archiving:
                store = Archive(config.archive_dir)
                for rel_path in stranded:
                    await asyncio.to_thread(store.discard, rel_path)
            # Deliberately outside any `keep_days <= 0` guard on the analytics
            # side: "keep the usage log forever" is a documented option, and it
            # must not silently disable the archive's own, separate clock.
            dropped = await purge_archive(storage, config)
            if removed:
                logger.info("purged %s expired analysis handles", removed)
            if forgotten:
                logger.info("forgot %s usage records past the retention window", forgotten)
            if dropped:
                logger.info("deleted %s archived pictures past their retention window", dropped)
            await asyncio.sleep(CLEANUP_INTERVAL)
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

    archive = None
    if config.archiving:
        archive = Archive(
            config.archive_dir,
            max_file_bytes=config.archive_max_file_bytes,
            max_total_bytes=config.archive_max_total_bytes,
            max_user_bytes=config.archive_max_user_bytes,
            min_free_bytes=config.archive_min_free_bytes,
        )
        # Proved at startup rather than on the first photograph. A silently
        # non-functioning archive is the worst outcome available: the operator
        # would believe pictures were being kept for weeks.
        problem = await asyncio.to_thread(archive.prepare)
        if problem:
            logger.error("archiving is configured but not working — %s", problem)
        else:
            logger.info("archiving to %s", config.archive_dir)
        # So the report can resolve rel_path even when it is reading this
        # database on somebody's laptop, a long way from the disk it describes.
        await storage.remember_setting("archive_dir", str(config.archive_dir))

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
        service=AnalysisService(config, exiftool, archive),
    )

    # Order matters. Language first, so every later middleware can phrase its
    # refusal in the user's own language. Audit second, before the allowlist, so
    # that a request from somebody who is turned away is still counted.
    language = LanguageMiddleware(storage, config)
    audit = AuditMiddleware(storage, config)
    access = AccessMiddleware(config, storage)
    dispatcher.message.middleware(language)
    dispatcher.callback_query.middleware(language)
    dispatcher.message.middleware(audit)
    dispatcher.callback_query.middleware(audit)
    dispatcher.message.middleware(access)
    dispatcher.callback_query.middleware(access)
    # Only the media router is metered, so commands and button taps stay free.
    media_router.message.middleware.register(ThrottleMiddleware(storage, config))

    dispatcher.include_router(media_router)
    dispatcher.include_router(router)

    await configure_profile(bot, config)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="findpic-bot",
        description="Run the findpic Telegram bot, or publish its public profile.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="publish the name, descriptions, command menu and avatar, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --setup, print the profile without contacting Telegram",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as error:
        if args.setup and args.dry_run:
            # Reviewing the texts should not require a token.
            from .setup import run_setup

            return run_setup(None, dry_run=True)
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if args.setup:
        from .setup import run_setup

        return run_setup(config, dry_run=args.dry_run)

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
