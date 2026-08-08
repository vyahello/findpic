# Setting up the findpic bot

Three things need doing by hand. Everything else — name, descriptions, command
menu, in both languages — is set by the bot itself on every start, so it lives in
git rather than in @BotFather's memory.

**Total time: about five minutes.**

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

### Set the avatar

Still in BotFather:

```
/setuserpic
```

Pick your bot, then upload **[`docs/bot-icon.png`](bot-icon.png)** from this repo
(512×512, already sized for Telegram).

### Optional but recommended

```
/setprivacy      → select the bot → Enable
```

Privacy mode means the bot only sees messages addressed to it if it is ever
added to a group. It has no effect on direct messages, which is how this bot is
meant to be used, but it is the safer default.

---

## 2. Log the token out of the cloud API

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

## 3. Add the token to GitHub

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
| `VPS_SSH_HOST_KEY` | Pins the server's SSH host key instead of trusting it on first use. Get it with `ssh-keyscan -H <host>`. |

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

## 4. Deploy

Push anything to `main`, or trigger it by hand:

**Actions → Deploy → Run workflow**

The workflow refuses to run until CI is green, deploys the exact commit CI
tested, writes `deploy/.env` from the secrets, rebuilds the image and restarts
the stack. It then greps the log for a successful `getMe` — a container can sit
"running" while failing to authenticate, so "it started" is not proof.

### Checking on it

```bash
ssh cax@178.105.143.68
cd /home/cax/findpic/deploy

docker compose ps
docker compose logs -f bot
docker compose restart bot
```

---

## What the bot publishes about itself

Set automatically at every start, in English and Ukrainian, from
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
Step 3.
