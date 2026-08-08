#!/usr/bin/env bash
#
# Rotate the bot token after @BotFather /revoke.
#
# Rotation has four steps that must happen in order, and getting them wrong
# fails quietly: a token that was never logged out leaves the bot running,
# logging "connected as", and never receiving a single message. This does all
# four against the same token, so they cannot drift apart.
#
#   1. @BotFather -> /revoke -> your bot        (only you can do this)
#   2. deploy/.env                              ) all three
#   3. the BOT_TOKEN GitHub secret              ) done here
#   4. logOut, so the local API server can serve it
#
# Usage:
#   scripts/rotate-token.sh              prompts for the token, never echoes it
#   scripts/rotate-token.sh --deploy     …and triggers a deploy afterwards

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="deploy/.env"
REPO="vyahello/findpic"
DEPLOY=false
[[ "${1:-}" == "--deploy" ]] && DEPLOY=true

die() { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

command -v gh >/dev/null || die "the GitHub CLI (gh) is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"

previous=""
if [[ -f "$ENV_FILE" ]]; then
  previous="$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
fi

step "Paste the NEW token from @BotFather (input is hidden)"
read -rs -p "  BOT_TOKEN: " token
echo
token="$(printf '%s' "$token" | tr -d '[:space:]')"

[[ -n "$token" ]] || die "no token entered"
[[ "$token" == *:* ]] || die "that does not look like a bot token (expected 123456:ABC...)"

# The mistake this script exists to prevent: running the rotation without having
# actually revoked, so every later step silently operates on the old token.
if [[ -n "$previous" && "$token" == "$previous" ]]; then
  die "that is the token already in $ENV_FILE.
  Revoke it first: @BotFather -> /revoke -> select your bot -> copy the NEW token."
fi

step "1/4  Writing $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ -f "$ENV_FILE" ]]; then
  # Keep every other setting exactly as it was; replace only the token line.
  umask 077
  awk -v tok="$token" '
    /^BOT_TOKEN=/ { print "BOT_TOKEN=" tok; found = 1; next }
    { print }
    END { if (!found) print "BOT_TOKEN=" tok }
  ' "$ENV_FILE" > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
else
  umask 077
  cp deploy/.env.example "$ENV_FILE"
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$token|" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
echo "     ok — $ENV_FILE updated (mode 600, gitignored)"

step "2/4  Setting the BOT_TOKEN secret on $REPO"
printf '%s' "$token" | gh secret set BOT_TOKEN --repo "$REPO"
echo "     ok"

step "3/4  Logging the NEW token out of the cloud Bot API"
# Required because this deployment uses a self-hosted Bot API server. Telegram
# will not deliver updates to a local server until the token is logged out of
# the cloud one, and skipping it produces no error anywhere.
response="$(curl -sS -X POST "https://api.telegram.org/bot${token}/logOut" || true)"
case "$response" in
  *'"ok":true'*)      echo "     ok — cloud API released the token" ;;
  *'Logged out'*)     echo "     already logged out — fine" ;;
  *Unauthorized*)     die "Telegram says Unauthorized. Is the token correct?
  Response: $response" ;;
  *)                  echo "     unexpected response: $response"
                      echo "     continuing, but check this before relying on the bot" ;;
esac

step "4/4  Deploy"
if $DEPLOY; then
  gh workflow run Deploy --repo "$REPO"
  echo "     triggered — watch it with: gh run watch --repo $REPO"
else
  echo "     skipped. Run it when ready:"
  echo "       gh workflow run Deploy --repo $REPO"
fi

printf '\n\033[32mRotation complete.\033[0m The old token is dead; nothing that leaked still works.\n'
