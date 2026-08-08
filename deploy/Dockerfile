# findpic Telegram bot.
#
# exiftool ships inside the image on purpose: the target host has no exiftool and
# no passwordless sudo to install one, and pinning it here means the bot analyses
# with exactly the version the test suite was validated against.
#
# It is installed from the upstream CPAN release rather than from Debian, whose
# bookworm package is 12.57 — old enough to predate decoders findpic relies on.

FROM python:3.12-slim-bookworm AS exiftool

# The latest *released* version. exiftool.org's ver.txt advertises higher numbers
# (13.59 at time of writing); those are development snapshots that are neither
# published to CPAN nor checksummed, so they are not what a bot should run.
# To bump: change both lines — the build fails loudly if they disagree.
ARG EXIFTOOL_VERSION=13.55
ARG EXIFTOOL_SHA256=5f4c81d34ad406538c2871ad72dbfceb5d9b412b2f16cbbeb4d712d270846667

ADD https://cpan.metacpan.org/authors/id/E/EX/EXIFTOOL/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz /tmp/exiftool.tar.gz

RUN set -eux; \
    echo "${EXIFTOOL_SHA256}  /tmp/exiftool.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/exiftool; \
    tar xzf /tmp/exiftool.tar.gz -C /opt/exiftool --strip-components=1; \
    rm /tmp/exiftool.tar.gz; \
    # Pure Perl: no compilation, and the script finds its own lib/ alongside it.
    # Dropping the test suite and docs keeps the copied layer small.
    rm -rf /opt/exiftool/t /opt/exiftool/html /opt/exiftool/README /opt/exiftool/Changes; \
    ln -s /opt/exiftool/exiftool /usr/local/bin/exiftool


FROM python:3.12-slim-bookworm

# The slim image ships only `perl-base`, which is missing most of the standard
# library. exiftool degrades *silently* without it: no Digest::MD5 means no
# JPEGDigest (the encoder fingerprint findpic uses to catch re-saves), and no
# Compress::Zlib means compressed PNG text chunks — where diffusion models write
# their prompt and seed — cannot be read at all. Both would just never fire.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini perl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=exiftool /opt/exiftool /opt/exiftool
RUN set -eux; \
    ln -s /opt/exiftool/exiftool /usr/local/bin/exiftool; \
    exiftool -ver; \
    # Fail the build rather than ship an exiftool that quietly cannot do its job.
    for module in Digest::MD5 Compress::Zlib Encode Time::Local; do \
      perl -M"$module" -e 1 || { echo "missing Perl module: $module"; exit 1; }; \
    done

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer survives source edits.
COPY pyproject.toml README.md ./
COPY src/findpic/__init__.py ./src/findpic/__init__.py
RUN pip install --no-cache-dir ".[bot]" && pip uninstall -y findpic

COPY src ./src
# The avatar, so `--setup` works from inside the container as well.
COPY docs/bot-icon.png ./docs/bot-icon.png
RUN pip install --no-cache-dir --no-deps -e .

# Runs as the same UID the host grants read access to the shared Bot API volume,
# so the bot never needs root to read an upload. Deleting them is the janitor's
# job precisely so this process can stay unprivileged while it parses files that
# arrived from strangers.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin findpic \
 && mkdir -p /data /tmp/findpic \
 && chown -R findpic:findpic /data /tmp/findpic /app

USER findpic

# tini reaps the exiftool subprocesses so a crashed analysis cannot leave zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "findpic.bot"]
