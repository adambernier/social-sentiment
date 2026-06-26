#!/usr/bin/env bash
#
# spcx_price_alert.sh — host-side price alert for the social-sentiment stack.
#
# Reads the latest stored quote for a symbol from the stock_quotes table (via the
# running `postgres` container) and POSTs to a Slack/Discord incoming webhook when
# the price is below a threshold. Fires ONCE per below-threshold episode and
# re-arms after the price recovers above the threshold, so it won't nag every run.
#
# Designed to run from cron ON THE DROPLET, from the compose project directory
# (the dir containing docker-compose.yml). This script lives in <project>/scripts/,
# so it locates the project dir as its own parent. It does NOT need a local DB,
# psql client, or .env parsing — it execs psql inside the postgres container using
# that container's own POSTGRES_USER / POSTGRES_DB.
#
# Config via env (all optional except the webhook URL):
#   SPCX_ALERT_WEBHOOK_URL   Slack/Discord incoming webhook URL (required;
#                            falls back to the file $HOME/.spcx_alert_webhook)
#   SPCX_ALERT_WEBHOOK_KIND  "discord" (default) or "slack"
#   SPCX_ALERT_SYMBOL        ticker to watch                  (default: SPCX)
#   SPCX_ALERT_THRESHOLD     alert when price < this          (default: 100)
#   SPCX_ALERT_STALE_HOURS   ignore quotes older than this    (default: 24)
#   DOCKER_COMPOSE           compose command                  (default: "docker compose")
#
set -euo pipefail

# cron has a minimal PATH; make sure docker/curl are findable.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

SYMBOL="${SPCX_ALERT_SYMBOL:-SPCX}"
THRESHOLD="${SPCX_ALERT_THRESHOLD:-100}"
WEBHOOK_KIND="${SPCX_ALERT_WEBHOOK_KIND:-discord}"
STALE_HOURS="${SPCX_ALERT_STALE_HOURS:-24}"
DC="${DOCKER_COMPOSE:-docker compose}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${HOME}/.spcx_alert_${SYMBOL}.state"

# --- resolve webhook URL (keep the secret out of the repo) -------------------
WEBHOOK_URL="${SPCX_ALERT_WEBHOOK_URL:-}"
if [[ -z "$WEBHOOK_URL" && -f "${HOME}/.spcx_alert_webhook" ]]; then
  WEBHOOK_URL="$(< "${HOME}/.spcx_alert_webhook")"
fi
if [[ -z "$WEBHOOK_URL" ]]; then
  echo "ERROR: no webhook URL (set SPCX_ALERT_WEBHOOK_URL or create ~/.spcx_alert_webhook)" >&2
  exit 1
fi

cd "$PROJECT_DIR"

# --- fetch latest quote: "price|age_seconds" (empty if none) -----------------
# Creds come from the postgres container's own env, so nothing is read from .env.
SQL="SELECT price, EXTRACT(EPOCH FROM (NOW() - timestamp))::int
       FROM stock_quotes
      WHERE symbol = '${SYMBOL}'
      ORDER BY timestamp DESC
      LIMIT 1;"
ROW="$($DC exec -T postgres sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tA -F'|' -c \"$SQL\"")" \
  || { echo "ERROR: DB query failed (is the postgres container up?)" >&2; exit 1; }

ROW="$(printf '%s' "$ROW" | tr -d '[:space:]')"
if [[ -z "$ROW" ]]; then
  echo "No quotes for ${SYMBOL} yet; nothing to check." >&2
  exit 0
fi

PRICE="${ROW%%|*}"
AGE="${ROW##*|}"

if (( AGE > STALE_HOURS * 3600 )); then
  echo "Latest ${SYMBOL} quote is stale (${AGE}s old > ${STALE_HOURS}h); skipping." >&2
  exit 0
fi

below="$(awk -v p="$PRICE" -v t="$THRESHOLD" 'BEGIN{print (p+0 < t+0) ? 1 : 0}')"
already="0"; [[ -f "$STATE_FILE" ]] && already="$(cat "$STATE_FILE")"

if [[ "$below" == "1" && "$already" != "1" ]]; then
  MSG="🔻 ${SYMBOL} dropped below \$${THRESHOLD} — now \$${PRICE} (quote ${AGE}s old)."
  if [[ "$WEBHOOK_KIND" == "slack" ]]; then
    payload="{\"text\": \"${MSG}\"}"
  else
    payload="{\"content\": \"${MSG}\"}"
  fi
  curl -fsS -X POST -H "Content-Type: application/json" -d "$payload" "$WEBHOOK_URL" >/dev/null
  printf '1' > "$STATE_FILE"
  echo "Alert sent: $MSG" >&2
elif [[ "$below" == "0" ]]; then
  printf '0' > "$STATE_FILE"   # re-arm once price recovers above threshold
  echo "${SYMBOL} at \$${PRICE} (>= \$${THRESHOLD}); armed." >&2
fi
