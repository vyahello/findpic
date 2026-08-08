"""One-shot publisher for the bot's public identity.

Everything a user sees before they press Start — the name, the two descriptions,
the command menu and the avatar — is applied by this command from the message
catalogue and `docs/bot-icon.png`.

Why a command rather than @BotFather: profile text typed into a chat window is
invisible to review, drifts between languages, and nobody remembers what the
Ukrainian description says six months later. Here it is a diff.

    python -m findpic.bot --setup            apply it
    python -m findpic.bot --setup --dry-run  print it without touching Telegram
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    FSInputFile,
    InputProfilePhotoStatic,
)

from ..i18n import FALLBACK_LANGUAGE, LANGUAGE_NAMES, Translator, available_languages
from .config import Config
from .runner import MENU_COMMANDS, build_session

logger = logging.getLogger("findpic.bot.setup")

#: Telegram's own limits. Exceeding one is rejected at the API, so they are
#: checked here first to give a useful message instead of a 400.
LIMITS = {"name": 64, "short": 120, "description": 512}

#: Where to find the avatar, in order. The repository layout is the normal case;
#: /app/docs is where the container has it. BOT_ICON_PATH overrides both, so a
#: different image can be used without editing anything.
ICON_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "docs" / "bot-icon.png",
    Path("/app/docs/bot-icon.png"),
    Path("docs/bot-icon.png"),
)


def find_icon() -> Path | None:
    """The avatar to upload, or None when it cannot be located."""
    override = os.environ.get("BOT_ICON_PATH")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    return next((path for path in ICON_CANDIDATES if path.is_file()), None)


def profile_for(language: str) -> dict[str, str]:
    translator = Translator(language)
    return {
        "name": translator.get("bot.profile.name"),
        "short": translator.get("bot.profile.short"),
        "description": translator.get("bot.profile.description"),
    }


def check_limits() -> list[str]:
    """Return a list of problems, empty when every text fits."""
    problems: list[str] = []
    for language in available_languages():
        for field, text in profile_for(language).items():
            if len(text) > LIMITS[field]:
                problems.append(
                    f"{language}/{field}: {len(text)} characters, limit is {LIMITS[field]}"
                )
    return problems


def render_preview() -> str:
    """Exactly what each language's profile will look like."""
    lines: list[str] = []
    for language in available_languages():
        profile = profile_for(language)
        translator = Translator(language)
        lines.append("=" * 72)
        lines.append(f"  {LANGUAGE_NAMES.get(language, language)}  ({language})")
        lines.append("=" * 72)
        lines.append("")
        lines.append(f"NAME                     [{len(profile['name'])}/{LIMITS['name']}]")
        lines.append(f"  {profile['name']}")
        lines.append("")
        lines.append(f"SHORT DESCRIPTION        [{len(profile['short'])}/{LIMITS['short']}]")
        lines.append("  (shown on the bot's profile page)")
        for line in profile["short"].splitlines():
            lines.append(f"  {line}")
        lines.append("")
        lines.append(
            f"DESCRIPTION              [{len(profile['description'])}/{LIMITS['description']}]"
        )
        lines.append("  (shown on the empty chat, before the user presses Start)")
        for line in profile["description"].splitlines():
            lines.append(f"  {line}")
        lines.append("")
        lines.append("COMMAND MENU")
        for name in MENU_COMMANDS:
            lines.append(f"  /{name:<10} {translator.get(f'bot.command.{name}')}")
        lines.append("")
    return "\n".join(lines)


async def apply(config: Config, *, set_photo: bool = True) -> int:
    """Publish the profile. Returns the number of failed calls."""
    bot = Bot(token=config.token, session=build_session(config))
    failures = 0
    try:
        me = await bot.get_me()
        print(f"connected as @{me.username} (id={me.id})\n")

        for language in available_languages():
            translator = Translator(language)
            # English is the fallback profile, so it carries no language_code.
            code = None if language == FALLBACK_LANGUAGE else language
            profile = profile_for(language)
            label = LANGUAGE_NAMES.get(language, language)

            commands = [
                BotCommand(command=name, description=translator.get(f"bot.command.{name}"))
                for name in MENU_COMMANDS
            ]
            steps = (
                ("name", bot.set_my_name(name=profile["name"], language_code=code)),
                (
                    "short description",
                    bot.set_my_short_description(
                        short_description=profile["short"], language_code=code
                    ),
                ),
                (
                    "description",
                    bot.set_my_description(description=profile["description"], language_code=code),
                ),
                (
                    "command menu",
                    bot.set_my_commands(
                        commands,
                        scope=BotCommandScopeAllPrivateChats(),
                        language_code=code,
                    ),
                ),
            )
            for what, call in steps:
                try:
                    await call
                    print(f"  [ok]   {label:<12} {what}")
                except Exception as error:  # noqa: BLE001 - report, do not abort
                    failures += 1
                    print(f"  [fail] {label:<12} {what}: {error}")

        if set_photo:
            print()
            icon = find_icon()
            if icon is None:
                print("  [skip] avatar: bot-icon.png not found (set BOT_ICON_PATH)")
            else:
                try:
                    await bot.set_my_profile_photo(
                        photo=InputProfilePhotoStatic(
                            photo=FSInputFile(icon, filename="findpic.png")
                        )
                    )
                    print(f"  [ok]   avatar        {icon}")
                except Exception as error:  # noqa: BLE001
                    failures += 1
                    print(f"  [fail] avatar: {error}")
                    print("         Set it by hand instead: @BotFather → /setuserpic")
    finally:
        await bot.session.close()
    return failures


def run_setup(config: Config | None, *, dry_run: bool) -> int:
    problems = check_limits()
    if problems:
        print("These texts exceed Telegram's limits:\n")
        for problem in problems:
            print(f"  {problem}")
        print("\nShorten them in src/findpic/locales/*.json and try again.")
        return 1

    print(render_preview())

    if dry_run:
        print("Dry run: nothing was sent to Telegram.")
        return 0
    if config is None:
        print("BOT_TOKEN is not set, so there is nothing to publish to.")
        return 2

    print("Publishing…\n")
    failures = asyncio.run(apply(config))
    print()
    if failures:
        print(
            f"{failures} call(s) failed. Telegram rate-limits name changes hard — "
            "if that is what failed, wait a while and run it again."
        )
        return 1
    print("Done. Open your bot in Telegram; the profile is live.")
    print("Note: Telegram caches profiles, so your own client may take a minute.")
    return 0
