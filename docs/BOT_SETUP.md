# Setting up the findpic bot

@BotFather is needed for exactly one thing: creating the bot. The name, both
descriptions, the command menu and the avatar are published by a single command
from the message catalogue, so the bot's public identity lives in git — in two
languages, reviewable in a diff — rather than in a chat window nobody can audit.

**Total time: about five minutes.**

| Step | Where |
|---|---|
| 1. Create the bot | @BotFather, once |
| 2. Publish its identity | `python -m findpic.bot --setup` |
| 3. Log the token out of the cloud API | one `curl`, mandatory here |
| 4. Add `BOT_TOKEN` to GitHub | repository secrets |
| 5. Deploy | Actions → Deploy |

---

## 1. Create the bot

In Telegram, open [@BotFather](https://t.me/BotFather):

```
/newbot
```

It asks for two things:

| Prompt | Suggestion |
|---|---|
| Name | `findpic` |
| Username | must end in `bot` — e.g. `findpic_bot`, `findpicbot`, `metadata_findpic_bot` |

BotFather replies with a token that looks like `8123456789:AAF…`.

> **The token is the entire credential.** Anyone holding it controls the bot.
> Do not paste it into a chat, a commit, or an issue. If it leaks, `/revoke` in
> BotFather and the old one dies immediately.

That is the only thing BotFather is needed for. The name, both descriptions, the
command menu **and the avatar** are all published by the setup command in step 2.

### Optional but recommended

```
/setprivacy      → select the bot → Enable
```

Privacy mode means the bot only sees messages addressed to it if it is ever
added to a group. It has no effect on direct messages, which is how this bot is
meant to be used, but it is the safer default.

---

## 2. Publish the bot's identity

One command sets the name, the short description, the description, the command
menu and the avatar — in English and Ukrainian — from the message catalogue.

Preview it first; this needs no token and contacts nobody:

```bash
python -m findpic.bot --setup --dry-run
```

Happy with it? Put the token somewhere the command can read it:

```bash
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env          # set BOT_TOKEN=…
```

`deploy/.env` is gitignored and is read automatically. **Do not put the token in
`deploy/.env.example`** — that one is tracked in git and would publish it.

Then:

```bash
python -m findpic.bot --setup
```

Or skip the file entirely for a one-off — a real environment variable always
wins over `.env`:

```bash
BOT_TOKEN='<your token>' python -m findpic.bot --setup
```

```
connected as @your_bot (id=8123456789)

  [ok]   English      name
  [ok]   English      short description
  [ok]   English      description
  [ok]   English      command menu
  [ok]   Українська   name
  [ok]   Українська   short description
  [ok]   Українська   description
  [ok]   Українська   command menu

  [ok]   avatar        docs/bot-icon.png
```

Run it whenever you change the texts — it is idempotent. Telegram rate-limits
**name** changes hard, so a `[fail] name` shortly after a previous change means
"wait a while", not that something is broken.

To edit the wording: `src/findpic/locales/en.json` and `uk.json`, keys
`bot.profile.*` and `bot.command.*`. The test suite fails if a text exceeds
Telegram's limits, so an over-long description cannot reach the API.

---

## 3. Log the token out of the cloud API

**This step is mandatory and easy to miss.** This deployment uses your own local
Bot API server (`remy-bot-api`), and Telegram will not deliver updates to a
locally-hosted server until the token has been logged out of the cloud one.

Run this once, on any machine:

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/logOut"
```

Expected reply:

```json
{"ok":true,"result":true}
```

Two things to know:

- After this you **cannot** go back to the cloud API for **10 minutes**.
- If you skip it, the bot appears to start fine and simply never receives a
  message. That failure mode is silent, which is why it is called out here.

To move the bot back to the cloud API later: stop the container, call
`http://127.0.0.1:8081/bot<TOKEN>/close`, and unset `BOT_API_BASE`.

---

## 4. Add the token to GitHub

Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `BOT_TOKEN` | the token from BotFather |

These four already exist and are used by the deploy workflow:
`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_APP_DIR`.

### Optional secrets

| Secret | Effect |
|---|---|
| `ADMIN_USER_IDS` | Comma-separated Telegram IDs exempt from throttling and the daily quota. Put your own ID here — [@userinfobot](https://t.me/userinfobot) will tell you what it is. |
| `ALLOWED_USER_IDS` | Set this to make the bot **private**. Only these IDs may use it; everyone else is told so and shown their own ID. Leave unset to keep it public. |
| `VPS_SSH_HOST_KEY` | Pins the server's SSH host key instead of trusting it on first use. Get it with `ssh-keyscan -p <port> -H <host>`. |

### Optional repository variables

Variables rather than secrets — none of these is sensitive, and seeing their
values in a workflow log is what you want when a deploy goes wrong.

| Variable | Effect |
|---|---|
| `VPS_SSH_PORT` | The port sshd listens on. Defaults to 22; set it if yours is elsewhere, or every deploy fails with `connection refused` and nothing else to go on. A variable rather than a secret because GitHub will not mask it in a log either way — if you would rather it were not readable at all, make it a secret and reference it as `secrets.VPS_SSH_PORT`. |
| `ARCHIVE_DIR` | Set to `/archive` to keep a copy of every picture the bot receives, under `~/findpic-archive` on the server. Empty (the default) keeps none. **Turning this on rewrites what `/privacy` tells your users, in both languages** — read it once afterwards. |
| `ANALYTICS` | `0` stops the bot recording who used it. Default `1`. |
| `ANALYTICS_RETENTION_DAYS` | How long that record is kept. `0` keeps it forever, which is a decision rather than a default. |

### Optional variables

Under the **Variables** tab (not secrets — these are not sensitive):

| Variable | Default | Meaning |
|---|---|---|
| `DAILY_QUOTA` | `50` | Analyses per user per day. `0` disables. |
| `THROTTLE_SECONDS` | `3` | Minimum gap between one user's analyses. |
| `MAX_FILE_MB` | `64` | Largest accepted file. Up to 2000 with the local API server. |
| `BOT_DEFAULT_LANGUAGE` | `en` | Fallback when the user's client language is neither `en` nor `uk`. |
| `LOG_LEVEL` | `INFO` | Leave at INFO — `DEBUG` can put file identifiers in the logs. |

---

## 5. Deploy

Push anything to `main`, or trigger it by hand:

**Actions → Deploy → Run workflow**

The workflow refuses to run until CI is green, deploys the exact commit CI
tested, writes `deploy/.env` from the secrets, rebuilds the image and restarts
the stack. It then greps the log for a successful `getMe` — a container can sit
"running" while failing to authenticate, so "it started" is not proof.

### Checking on it

The host, the account and the port are in your repository secrets and
variables, not here — this file is public.

```bash
ssh you@your-server            # add -p <port> if sshd is not on 22
cd ~/findpic/deploy

docker compose ps
docker compose logs -f bot
docker compose restart bot
```

---

## What the bot publishes about itself

Set by `--setup`, and re-applied on every start, in English and Ukrainian, from
`src/findpic/locales/*.json`:

- **Name** — `bot.profile.name`
- **Short description** (profile page) — `bot.profile.short`
- **Description** (empty-chat screen) — `bot.profile.description`
- **Command menu** — `bot.command.*`

To change any of it: edit the catalogue, push, and the next deploy applies it in
both languages. Telegram rate-limits name changes fairly aggressively; a
`could not set name` warning in the log usually means "you changed it recently",
not a real failure.

---

## Rotating the token

If the token ever leaks — pasted into a chat, committed, screenshotted — it must
be replaced. Anyone holding it controls the bot and can read every photo sent to
it.

Rotation is four ordered steps, and three of them fail *silently* if done
against the wrong token. `scripts/rotate-token.sh` does them together so they
cannot drift apart:

```bash
# 1. @BotFather -> /revoke -> select your bot -> copy the NEW token
# 2. then:
./scripts/rotate-token.sh --deploy
```

It prompts for the token without echoing it, refuses one that matches what is
already in `deploy/.env` (which means you skipped the revoke), updates the file,
sets the GitHub secret, logs the **new** token out of the cloud API, and triggers
a deploy.

> The `logOut` is per-token, not per-bot. A new token starts out logged in to the
> cloud API, so it needs its own — otherwise the bot starts, reports
> `connected as`, and never receives a message.

Expect the running bot to stop working between the revoke and the redeploy: the
old token is dead the moment BotFather issues a new one.

## Troubleshooting

**The bot never answers.**
Almost always the `logOut` step. Check with:
```bash
docker compose logs bot | grep "connected as"
```
No line means it never authenticated.

**`connected as` appears but messages do nothing.**
The local API server has the token but Telegram is still routing to the cloud.
Run the `logOut` call and restart the container.

**Every report comes back empty.**
The picture was sent as a *photo*. Telegram strips metadata from compressed
photos before the bot sees them. Send it as a **file** — the bot says this too,
above every such report.

**`/var/lib/telegram-bot-api` is filling up.**
The janitor container deletes this bot's uploads after five minutes. Check it is
alive: `docker compose ps janitor`. It only ever touches this bot's own token
directory, never another bot's.

**Deploy fails with `BOT_TOKEN secret is not set`.**
Step 4.
