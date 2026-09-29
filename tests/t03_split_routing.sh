#!/usr/bin/env bash
# t03 — the split. Russian destinations must leave from the Russian entry node;
# everything else must leave from an exit abroad.
#
# This is the test that proves the design claim. If it fails, either your banking
# app sees a German IP (breaks), or your YouTube traffic is going out of a Russian
# datacenter registered to your passport (much worse).
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl python3 jq || exit 1

CLIENT="${1:-$(jq -r '.clients[0].name // empty' state/fleet.json 2>/dev/null)}"
[ -n "$CLIENT" ] || { echo "no client in state"; exit 1; }
XRAY_BIN="$(ensure_xray)" || exit 1
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t03 — split routing"

# --no-local-split so we are testing the ENTRY node's rules, not the client's.
./bin/fleet client config "$CLIENT" --socks-port 11082 --no-local-split \
  -o "$WORK/client.json" >/dev/null 2>&1
"$XRAY_BIN" run -config "$WORK/client.json" >"$WORK/c.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11082 2>/dev/null && break; sleep 1; done

ENTRY_IP="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).entry.host)")"
P="--socks5-hostname 127.0.0.1:11082"

head1 "foreign destinations should leave via an exit"
FOREIGN="$(curl -s --max-time 40 $P https://api.ipify.org)"
info "api.ipify.org sees: ${FOREIGN}"
assert_ne "$ENTRY_IP" "$FOREIGN" "foreign traffic does not egress from the RU entry node"

head1 "Russian destinations should leave from the entry node"
# Yandex echoes the client address it sees.
RU_SEEN="$(curl -s --max-time 40 $P https://yandex.ru/internet/api/v0/ip 2>/dev/null \
           | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("ipv4") or d.get("ip",""))' 2>/dev/null)"
if [ -z "$RU_SEEN" ]; then
  skip "yandex.ru echo endpoint unavailable — checking reachability instead"
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 $P https://yandex.ru/)"
  [ "$CODE" = "200" ] || [ "$CODE" = "302" ] && pass "yandex.ru reachable through the tunnel (HTTP $CODE)" \
    || fail "yandex.ru returned $CODE"
else
  info "yandex.ru sees: ${RU_SEEN}"
  assert_eq "$ENTRY_IP" "$RU_SEEN" "RU traffic egresses from the entry node's Russian IP"
fi

head1 "a sample of the split list actually resolves and connects"
for host in sber.ru gosuslugi.ru ozon.ru avito.ru; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 $P "https://${host}/" 2>/dev/null)"
  case "$CODE" in
    200|301|302|403) pass "${host} reachable (HTTP ${CODE})" ;;
    000) fail "${host} unreachable — split rule may be sending it abroad into a geo-block" ;;
    *) info "${host} → HTTP ${CODE}" ;;
  esac
done

head1 "reminder"
info "Anything Russian that is NOT in entry.split.direct_domains goes abroad and back."
info "Add misses to inventory.toml and run: fleet entry sync"

summary
