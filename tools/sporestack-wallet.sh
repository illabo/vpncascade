#!/usr/bin/env bash
# SporeStack wallet helper. The token in .env IS the wallet — this reads it from
# there so it never lands in shell history or scrollback.
#
#   ./tools/sporestack-wallet.sh balance
#   ./tools/sporestack-wallet.sh topup 25 xmr     # prints an invoice to pay
#   ./tools/sporestack-wallet.sh invoices
#   ./tools/sporestack-wallet.sh reveal           # print the token (for backup)
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env"; exit 1; }
TOK=$(grep -E '^SPORESTACK_TOKEN=' .env | cut -d= -f2- | tr -d '"'"'"' \r')
[ -n "$TOK" ] || { echo "SPORESTACK_TOKEN is empty in .env"; exit 1; }
API=https://api.sporestack.com
# Never pipe straight into json.tool: if it fails it has already eaten stdin and
# the `|| cat` fallback prints nothing, which silently hides API errors.
req() {  # req <method> <url> [body]
  local m="$1" u="$2" b="${3:-}" code out
  out=$(mktemp); trap 'rm -f "$out"' RETURN
  if [ -n "$b" ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' --max-time 30 -X "$m" "$u" \
             -H 'content-type: application/json' -d "$b")
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' --max-time 30 -X "$m" "$u")
  fi
  if [ "$code" -ge 400 ]; then
    echo "  HTTP $code from the API:" >&2
    sed 's/^/  /' "$out" >&2; echo >&2
    return 1
  fi
  python3 -m json.tool < "$out" 2>/dev/null || cat "$out"
}

case "${1:-balance}" in
  balance)  req GET "$API/token/$TOK/balance" ;;
  invoices) req GET "$API/token/$TOK/invoices" ;;
  reveal)   echo "$TOK" ;;
  topup)
    d="${2:?usage: topup <whole-dollars> <xmr|btc|bch|usdt>}"
    c="${3:?usage: topup <whole-dollars> <xmr|btc|bch|usdt>}"
    echo "Requesting an invoice for \$$d in $c — this does NOT pay it."
    echo "(SporeStack requires a \$100 minimum on a token that has never been funded.)"
    req POST "$API/token/$TOK/add" "{\"dollars\":$d,\"currency\":\"$c\"}" || exit 1
    echo
    echo "Pay the address/URI above from your own wallet. Then:"
    echo "  ./tools/sporestack-wallet.sh balance"
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
