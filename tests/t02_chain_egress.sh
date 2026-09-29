#!/usr/bin/env bash
# t02 — end-to-end: does traffic actually come out of an exit node?
#
# Builds a real client config with `fleet client config`, runs xray locally, and
# checks the public egress IP is one of the exits in state — not your ISP, and not
# the entry node.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl python3 jq || exit 1

CLIENT="${1:-}"
if [ -z "$CLIENT" ]; then
  CLIENT="$(jq -r '.clients[0].name // empty' state/fleet.json 2>/dev/null)"
fi
[ -n "$CLIENT" ] || { echo "no client — run: fleet client add <name>"; exit 1; }

XRAY_BIN="$(ensure_xray)" || exit 1
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t02 — chain egress  ${DIM}client=${CLIENT}${RST}"

./bin/fleet client config "$CLIENT" --socks-port 11081 -o "$WORK/client.json" >/dev/null 2>&1 \
  || { fail "could not generate a client config"; exit 1; }
pass "generated client config"

"$XRAY_BIN" run -test -config "$WORK/client.json" >/dev/null 2>&1 \
  && pass "client config accepted by xray -test" || fail "client config rejected"

"$XRAY_BIN" run -config "$WORK/client.json" >"$WORK/client.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11081 2>/dev/null && break; sleep 1; done

BASELINE="$(curl -s --max-time 20 https://api.ipify.org)"
TUNNELLED="$(curl -s --max-time 40 --socks5-hostname 127.0.0.1:11081 https://api.ipify.org)"
ENTRY_IP="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).entry.host)")"
EXIT_IPS="$(jq -r '.exits[] | select(.serving) | .ipv4' state/fleet.json 2>/dev/null | tr '\n' ' ')"

info "your ISP address : ${BASELINE}"
info "through tunnel   : ${TUNNELLED}"
info "serving exits    : ${EXIT_IPS:-<none>}"

[ -n "$TUNNELLED" ] && pass "tunnel carries traffic" || fail "no response through the tunnel"
assert_ne "$BASELINE" "$TUNNELLED" "egress differs from your ISP address"
assert_ne "$ENTRY_IP" "$TUNNELLED" "egress is NOT the entry node (cascade is working, not just one hop)"

case " $EXIT_IPS " in
  *" $TUNNELLED "*) pass "egress matches a known serving exit" ;;
  *) fail "egress ${TUNNELLED} is not any exit in state — stale state, or an unexpected hop" ;;
esac

head1 "latency and MTU"
curl -s -o /dev/null -w "  ${DIM}connect=%{time_connect}s ttfb=%{time_starttransfer}s total=%{time_total}s${RST}\n" \
  --max-time 40 --socks5-hostname 127.0.0.1:11081 https://api.ipify.org
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 --socks5-hostname 127.0.0.1:11081 \
        https://speed.cloudflare.com/__down?bytes=1000000 2>/dev/null)"
assert_eq "200" "$CODE" "1 MB transfer completes (no MTU blackhole)"

summary
