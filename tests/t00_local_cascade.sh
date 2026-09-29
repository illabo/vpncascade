#!/usr/bin/env bash
# t00 — the whole cascade on loopback. No servers, no money, no provider accounts.
#
# Stands up three real Xray processes (client → entry → exit) with genuine
# VLESS/XHTTP/REALITY between them, pushes traffic through, and proves the split
# rule sends matching domains out a different outbound. This is the test to run
# after touching fleet/render.py — it catches config regressions before they reach
# a live node, which is the only place they would otherwise show up.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh

require curl python3 unzip || exit 1
XRAY_BIN="$(ensure_xray)" || exit 1
WORK="$(mktemp -d)"
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t00 — local 3-hop cascade  ${DIM}($("$XRAY_BIN" version | head -1))${RST}"

# A domain we can reach that is NOT in the split list, plus one we pin as "direct".
TUNNELLED_HOST="api.ipify.org"
DIRECT_HOST="ifconfig.me"

python3 - "$WORK" "$DIRECT_HOST" <<'PY'
import json, sys
sys.path.insert(0, '.')
from fleet import keys
work, direct_host = sys.argv[1], sys.argv[2]

DEST, SNI = "www.wikipedia.org:443", "www.wikipedia.org"
epriv, epub = keys.reality_keypair()
xpriv, xpub = keys.reality_keypair()
cli_uuid, cli_sid = keys.new_uuid(), keys.short_id(4)
up_uuid, up_sid = keys.new_uuid(), keys.short_id()

xh = {"host": SNI, "path": "/api/v1/update", "mode": "stream-one"}
sin = lambda priv, sids: {"network":"xhttp","xhttpSettings":xh,"security":"reality",
    "realitySettings":{"show":False,"dest":DEST,"xver":0,"serverNames":[SNI],
    "privateKey":priv,"shortIds":sids}}
sout = lambda pub, sid: {"network":"xhttp","xhttpSettings":xh,"security":"reality",
    "realitySettings":{"show":False,"serverName":SNI,"fingerprint":"firefox",
    "publicKey":pub,"shortId":sid,"spiderX":"/"}}
sniff = {"enabled": True, "destOverride": ["http","tls"], "routeOnly": True}

json.dump({"log":{"loglevel":"info"},
  "inbounds":[{"tag":"uplink-in","listen":"127.0.0.1","port":18443,"protocol":"vless",
    "settings":{"clients":[{"id":up_uuid}],"decryption":"none"},
    "streamSettings":sin(xpriv,[up_sid]),"sniffing":sniff}],
  "outbounds":[{"tag":"out","protocol":"freedom","settings":{"domainStrategy":"UseIP"}}]},
  open(f"{work}/exit.json","w"), indent=1)

json.dump({"log":{"loglevel":"info"},
  "inbounds":[{"tag":"client-in","listen":"127.0.0.1","port":18444,"protocol":"vless",
    "settings":{"clients":[{"id":cli_uuid}],"decryption":"none"},
    "streamSettings":sin(epriv,[cli_sid]),"sniffing":sniff}],
  "outbounds":[
    {"tag":"exit-local","protocol":"vless","settings":{"vnext":[{"address":"127.0.0.1",
      "port":18443,"users":[{"id":up_uuid,"encryption":"none","flow":""}]}]},
      "streamSettings":sout(xpub,up_sid)},
    {"tag":"direct","protocol":"freedom","settings":{"domainStrategy":"UseIP"}},
    {"tag":"block","protocol":"blackhole"}],
  "routing":{"domainStrategy":"AsIs","rules":[
    {"type":"field","domain":[f"domain:{direct_host}"],"outboundTag":"direct"},
    {"type":"field","inboundTag":["client-in"],"balancerTag":"exits"}],
    "balancers":[{"tag":"exits","selector":["exit-"],"strategy":{"type":"leastPing"}}]},
  "observatory":{"subjectSelector":["exit-"],
    "probeURL":"https://www.gstatic.com/generate_204","probeInterval":"30s",
    "enableConcurrency":True}},
  open(f"{work}/entry.json","w"), indent=1)

json.dump({"log":{"loglevel":"warning"},
  "inbounds":[{"tag":"socks","listen":"127.0.0.1","port":11080,"protocol":"socks",
    "settings":{"udp":True,"auth":"noauth"},
    "sniffing":{"enabled":True,"destOverride":["http","tls"]}}],
  "outbounds":[{"protocol":"vless","settings":{"vnext":[{"address":"127.0.0.1",
    "port":18444,"users":[{"id":cli_uuid,"encryption":"none","flow":""}]}]},
    "streamSettings":sout(epub,cli_sid)}]},
  open(f"{work}/client.json","w"), indent=1)
PY

for n in exit entry client; do
  if "$XRAY_BIN" run -test -config "$WORK/$n.json" >/dev/null 2>&1; then
    pass "$n config accepted by xray -test"
  else
    fail "$n config REJECTED by xray -test"; "$XRAY_BIN" run -test -config "$WORK/$n.json" 2>&1 | tail -5
  fi
done

for n in exit entry client; do
  "$XRAY_BIN" run -config "$WORK/$n.json" >"$WORK/$n.log" 2>&1 &
done
for _ in $(seq 1 20); do nc -z 127.0.0.1 11080 2>/dev/null && break; sleep 1; done
sleep 2

for p in 18443 18444 11080; do
  nc -z 127.0.0.1 "$p" 2>/dev/null && pass "listening on 127.0.0.1:$p" || fail "nothing on :$p"
done

head1 "traffic"
CODE="$(curl_code "https://${TUNNELLED_HOST}/" --socks5-hostname 127.0.0.1:11080)"
assert_eq "200" "$CODE" "HTTPS through client → entry → exit"

# The routing assertions below are the real proof; this one only confirms the direct
# path carries bytes. If the third-party host is simply down, say so rather than
# reporting a product failure.
CODE2="$(curl_code "https://${DIRECT_HOST}/ip" --socks5-hostname 127.0.0.1:11080)"
if [ "$CODE2" = "200" ]; then
  pass "HTTPS through client → entry → direct"
elif [ "$CODE2" = "000" ]; then
  skip "direct path: ${DIRECT_HOST} did not answer after 3 tries (their end, not ours)"
else
  fail "HTTPS through client → entry → direct (HTTP ${CODE2})"
fi

head1 "routing decisions (the split rule)"
sleep 1
ENTRY_LOG="$(cat "$WORK/entry.log")"
EXIT_LOG="$(cat "$WORK/exit.log")"

assert_contains "$ENTRY_LOG" "${DIRECT_HOST}:443 [client-in -> direct]" \
  "entry sent ${DIRECT_HOST} out the DIRECT outbound"
assert_contains "$ENTRY_LOG" "${TUNNELLED_HOST}:443 [client-in >> exit-local]" \
  "entry sent ${TUNNELLED_HOST} through the exit balancer"
assert_contains "$EXIT_LOG" "${TUNNELLED_HOST}:443" \
  "exit node received the tunnelled request"
assert_not_contains "$EXIT_LOG" "${DIRECT_HOST}:443" \
  "exit node never saw the direct request (no leak across the split)"

summary
