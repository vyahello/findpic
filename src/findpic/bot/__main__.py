"""``python -m findpic.bot`` — run the bot, or ``--setup`` its profile."""

from __future__ import annotations

import sys

from .runner import main

if __name__ == "__main__":
    sys.exit(main())
