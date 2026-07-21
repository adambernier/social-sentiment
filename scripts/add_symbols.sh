#!/usr/bin/env bash
#
# Add tracked symbols to social-sentiment via the admin API.
#
# Usage:
#   export ADMIN_API_KEY=...           # required
#   export API_BASE=https://feelinggoodtoday.xyz   # optional, this is the default
#   ./scripts/add_symbols.sh
#
set -euo pipefail

API_BASE="${API_BASE:-https://feelinggoodtoday.xyz}"

if [[ -z "${ADMIN_API_KEY:-}" ]]; then
  echo "error: ADMIN_API_KEY is not set. Run: export ADMIN_API_KEY=..." >&2
  exit 1
fi

# One JSON object per symbol. Edit freely before running.
read -r -d '' SYMBOLS <<'JSON' || true
{"symbol":"AAPL","keywords":["Apple","iPhone","Tim Cook","Vision Pro"],"future":"NQ=F","sector":"Technology","require_uppercase":false,"block_phrases":["apple pie","big apple","apple juice","apple cider","adam's apple"],"is_active":true}
{"symbol":"INTC","keywords":["Intel","Lip-Bu Tan","Intel Foundry"],"future":"NQ=F","sector":"Semiconductors","require_uppercase":false,"block_phrases":["intel on","got intel","the intel","intelligence"],"is_active":true}
{"symbol":"IREN","keywords":["Iris Energy","IREN Limited"],"future":"RTY=F","sector":"Crypto Mining","require_uppercase":false,"block_phrases":[],"is_active":true}
{"symbol":"AVGO","keywords":["Broadcom","Hock Tan","VMware"],"future":"NQ=F","sector":"Semiconductors","require_uppercase":false,"block_phrases":[],"is_active":true}
{"symbol":"GOOGL","keywords":["Google","Alphabet","Sundar Pichai","Waymo","YouTube"],"future":"NQ=F","sector":"Communication Services","require_uppercase":false,"block_phrases":["google it","googled","just google","google search"],"is_active":true}
{"symbol":"SWYGX","keywords":[],"future":null,"sector":"AOA","require_uppercase":false,"require_cashtag":true,"block_phrases":[],"is_active":true}
JSON

fail=0
while IFS= read -r payload; do
  [[ -z "$payload" ]] && continue
  symbol=$(printf '%s' "$payload" | sed -n 's/.*"symbol":"\([^"]*\)".*/\1/p')

  http_code=$(curl -sS -o /tmp/add_symbol_resp.json -w '%{http_code}' \
    -X POST "${API_BASE}/api/admin/symbols" \
    -H "X-API-Key: ${ADMIN_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$payload" || echo "000")

  if [[ "$http_code" =~ ^2 ]]; then
    echo "ok    ${symbol} (HTTP ${http_code})"
  else
    echo "FAIL  ${symbol} (HTTP ${http_code}): $(cat /tmp/add_symbol_resp.json)" >&2
    fail=1
  fi
done <<< "$SYMBOLS"

rm -f /tmp/add_symbol_resp.json
exit "$fail"
