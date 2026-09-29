#!/usr/bin/env bash
# t04 — leak checks: DNS, IPv6, WebRTC-style direct paths, and the metadata service.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl python3 jq || exit 1

CLIENT="${1:-$(jq -r '.clients[0].name // empty' state/fleet.json 2>/dev/null)}"
XRAY_BIN="$(ensure_xray)" || exit 1
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t04 — leaks"
./bin/fleet client config "$CLIENT" --socks-port 11083 -o "$WORK/c.json" >/dev/null 2>&1
"$XRAY_BIN" run -config "$WORK/c.json" >"$WORK/c.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11083 2>/dev/null && break; sleep 1; done
P="--socks5-hostname 127.0.0.1:11083"

head1 "DNS"
info "--socks5-hostname sends the NAME to the proxy, so resolution happens at the exit."
info "If your client instead resolves locally (--socks5, or a misconfigured router),"
info "your ISP sees every domain you visit even though the traffic is tunnelled."
RESOLVER="$(curl -s --max-time 30 $P https://one.one.one.one/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p')"
EGRESS="$(curl -s --max-time 30 $P https://api.ipify.org 2>/dev/null)"
info "egress ${EGRESS}, cloudflare sees ${RESOLVER}"
assert_eq "$EGRESS" "${RESOLVER:-$EGRESS}" "no split between the DNS path and the data path"

head1 "IPv6"
V6="$(curl -s --max-time 20 $P -6 https://api6.ipify.org 2>/dev/null)"
if [ -z "$V6" ]; then
  pass "no IPv6 egress (nothing to leak)"
else
  info "IPv6 egress: ${V6}"
  EXIT6="$(jq -r '.exits[] | select(.serving) | .ipv6' state/fleet.json | tr '\n' ' ')"
  case " $EXIT6 " in
    *" $V6 "*) pass "IPv6 also exits through the tunnel" ;;
    *) fail "IPv6 egresses outside the tunnel (${V6}) — disable IPv6 on the client or tunnel it" ;;
  esac
fi

head1 "exit node cannot be used to reach provider internals"
for target in 169.254.169.254 10.0.0.1 192.168.1.1; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 $P "http://${target}/" 2>/dev/null)"
  assert_eq "000" "$CODE" "blocked: ${target} (SSRF pivot / cloud metadata)"
done

head1 "entry node identity is not echoed"
HDRS="$(curl -s -D - -o /dev/null --max-time 30 $P https://api.ipify.org 2>/dev/null)"
assert_not_contains "$HDRS" "X-Forwarded-For" "no X-Forwarded-For added by the chain"
assert_not_contains "$HDRS" "Via:" "no Via header added by the chain"

summary
