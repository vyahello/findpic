# `bot-stats.py` — who used the bot, and what they sent

One script, three ways to reach the bot's database. Which one you want depends
only on **where the database is relative to where you are typing**.

```
on the server        scripts/bot-stats.py --docker
from your laptop     scripts/bot-stats.py --ssh you@server
on a copy            scripts/bot-stats.py --db findpic-bot.sqlite3
```

Everything after that — the filters, the CSV, the export — is identical in all
three.

---

## Why `--docker` on the server

Because on the server the database is **not a file you can point at**.

The bot runs in a container, and its `/data` is a Docker *named volume*
(`findpic_findpic-data`). Docker keeps that volume's contents under
`/var/lib/docker/volumes/…`, owned by `root`. So there is no
`findpic-bot.sqlite3` anywhere in your home directory, in `~/findpic`, or
anywhere else a script would think to look.

Run it with no flags on the server and it tells you exactly that:

```console
$ scripts/bot-stats.py
no database found. Pass --db PATH, or --ssh HOST to pull it off the server.
  looked in: /data/findpic-bot.sqlite3, findpic-bot.sqlite3,
             deploy/findpic-bot.sqlite3, ~/.local/share/findpic/findpic-bot.sqlite3
```

`/data/findpic-bot.sqlite3` is the *container's* path. It does not exist on the
host.

`--docker` gets at it the way the volume is meant to be read: a throwaway
container that mounts the volume **read-only** and writes the database to
stdout as a tar.

```
docker run --rm -v findpic_findpic-data:/data:ro alpine:3.20 \
    sh -c 'cd /data && tar -cf - findpic-bot.sqlite3*'
```

Two things that follow from doing it that way:

- **No `sudo`.** Membership of the `docker` group is enough. Reading
  `/var/lib/docker/…` directly would need root.
- **The running bot is not disturbed.** The mount is `:ro`, and the script
  works on the unpacked copy, never the live file. The worst case is catching
  the database mid-write, which SQLite's write-ahead log exists to survive —
  and the `-wal` file is pulled along with it, so recent activity is not lost.

### Why the *photos* do not need this

The archive is a **bind mount** — an ordinary directory on the host, mounted
into the container at `/archive`. So `--export` reads those files straight off
the disk. Only the database is inside a volume.

That is the whole asymmetry: database → volume → needs `--docker`; photos →
bind mount → does not.

---

## From your laptop: `--ssh`

`--ssh` runs **the same `docker run` command** at the far end, over ssh, and
unpacks the result locally. You do not add `--docker` as well — `--ssh`
already implies it.

```bash
scripts/bot-stats.py --ssh you@server
```

If the server's ssh is not on port 22:

```bash
scripts/bot-stats.py --ssh you@server --ssh-port PORT
```

Set it once instead of typing it every time:

```bash
export FINDPIC_BOT_HOST=you@server
export FINDPIC_BOT_PORT=PORT
scripts/bot-stats.py --all          # picks both up from the environment
```

Requirements at the far end: an ssh account that is **in the `docker` group**,
and `docker` on its `PATH`. Nothing is installed and nothing is written — the
script only reads.

---

## On a copy: `--db`

Once you have a `findpic-bot.sqlite3` on your own disk — from a backup, or
copied off the server — point at it directly. Nothing is needed here but the
Python standard library.

```bash
scripts/bot-stats.py --db findpic-bot.sqlite3
```

With no `--db` and no other flag, these are searched in order:

```
/data/findpic-bot.sqlite3                     (inside the container only)
./findpic-bot.sqlite3
deploy/findpic-bot.sqlite3
~/.local/share/findpic/findpic-bot.sqlite3
```

---

## Python

Standard library only, no dependencies, no virtualenv. On the server the
system `python3` runs it as-is:

```bash
cd ~/findpic
python3 scripts/bot-stats.py --docker
```

On this laptop `scripts/bot-stats.py` works directly (it is executable and has
a `#!/usr/bin/env python3` line). It does **not** need the `findpic` venv —
that is only for the `findpic` command itself.

---

## What you get

The report opens with the **ledger**: one line per photograph, in arrival
order, refusals included. Everything below it summarises those same lines —
so the totals and the ledger can never disagree.

```
scripts/bot-stats.py --docker --photos --limit 0     # the ledger, all of it
scripts/bot-stats.py --docker                        # ledger + summaries
```

### Two columns that are not what they look like

Telegram tells a bot **nothing** about a device, an operating system, an app
version, an IP address or a location. A bot sees an account — id, name,
username, the language the client asks for, the premium flag — and when each
message arrived. That is the complete list.

So:

- **Device and OS** are read out of the photographs by findpic. That is a claim
  about the *camera*, usually the phone in the sender's hand but not the same
  statement — and blank for anyone whose photos arrived already stripped.
- **Where** has three proxies of different quality, printed in three different
  places on purpose: the language the client asks for; the hours somebody is
  active, which is a guess and is printed with the width of the guess; and the
  country in a photograph's GPS tags, which is a fact about the picture and
  says nothing about where the sender was sitting.

Both are labelled in the report. Neither is a location.

---

## Narrowing it

The window is the last 30 days unless you say otherwise.

```bash
--all                     every record kept, ignoring the window
--days 7                  the last week
--since 2026-08-01        from a date
--until 2026-08-31        to a date, inclusive
--user @someone           one account, by @username or numeric id (repeatable)
--device iphone           cameras matching this text
--os "iOS 17"             OS versions matching this
--with-gps / --no-gps     only photos that carry coordinates, or only those that do not
--failed                  only refusals and failures
--sent-as file            how the picture arrived (file keeps metadata, photo does not)
--min-events 5            skip accounts quieter than this
```

Prefer the numeric id over `@username` for anything you keep: a username can be
given up and taken over, the id cannot.

---

## Getting it out

```bash
--json                    the whole report as JSON
--csv roster.csv          one row per account
--csv-photos photos.csv   one row per picture
--export DIR              copy the kept pictures into DIR, one folder per sender
--limit 0                 no row cap on any table
--no-color                plain text, for a pipe or a log
```

`--export` needs to reach the archive as well as the database, and does so the
same way you reached the database — directly on the server, over ssh from your
laptop.

```bash
# on the server
scripts/bot-stats.py --docker --all --export ~/exported

# from your laptop
scripts/bot-stats.py --ssh you@server --all --export ~/photos
```

It lays them out to be read rather than to be deduplicated:

```
~/photos/
  someone-5829771410/
    2026-08-29/
      2026-08-29 15-39 Apple iPhone X 01dde9b2.jpg
  id7332288724/
    2026-08-22/
      2026-08-22 21-15 no camera fc68d2f9.jpg
```

Folder is the sender — username first, numeric id after it, `id<number>` when
there is no username. Filename is date, time, camera, then eight hex of the
SHA-256, which is the key that joins it to `--csv-photos`. `no camera` means
the picture arrived already stripped.

If the bot recorded an archive path that is wrong for where you are running,
override it with `--archive DIR` (or `ARCHIVE_DIR` in the environment).

---

## When it does not work

| What you see | What it means |
|---|---|
| `no database found` | No `--docker`, `--ssh` or `--db`, and nothing at the four searched paths. On the server you want `--docker`. |
| `could not read … permission denied` | The account is not in the `docker` group. `sudo usermod -aG docker "$USER"`, then log out and back in. |
| `check the volume name (docker volume ls) — assumed 'findpic_findpic-data'` | The stack was brought up under a different Compose project name. Find the real one with `docker volume ls` and pass `--volume`. |
| `this database has no usage tables` | The database predates analytics. Redeploy the bot and it creates them on start. |
| `0 accounts` with a database that clearly has data | The default window is 30 days. Add `--all`. |
| ssh asks for a password | Fine, it just needs a working login — but an agent or a key is less tedious. |

---

## What it never does

- Never writes to the bot's database. Both remote paths unpack a copy into a
  temporary directory; the local path copies the file first. The live file is
  never opened.
- Never needs root. The `docker` group is the whole requirement.
- Never installs anything, locally or on the server.
- Never sends anything anywhere. Every byte stays on the machine you ran it on.
