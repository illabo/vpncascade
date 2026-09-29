#!/usr/bin/env bash
# t09 — direct mode on loopback: single hop, two exits, client-side balancer.
#
# In cascade mode the entry node hides exit churn from clients. Direct mode has no
# entry node, so the client's own balancer is the only thing standing between a
# rotation and every device breaking at once. This test kills an exit mid-flight and
# checks that traffic keeps flowing. If this fails, `fleet rotate` in direct mode is
# an outage, not a maintenance task.
#
# Needs nothing but an xray binary.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh

require curl python3 unzip || exit 1
XRAY_BIN="$(ensure_xray)" || exit 1
WORK="$(mktemp -d)"
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t09 — direct mode, client-side failover"

python3 - "$WORK" <<'PY'
import json, sys
sys.path.insert(0, '.')
from fleet import keys
work = sys.argv[1]

DEST, SNI = "www.wikipedia.org:443", "www.wikipedia.org"
cli_uuid, cli_sid = keys.new_uuid(), keys.short_id(4)
xh = {"host": SNI, "path": "/api/v1/update", "mode": "stream-one"}
sniff = {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": True}

outbounds, ports = [], [18451, 18452]
for i, port in enumerate(ports):
    priv, pub = keys.reality_keypair()
    json.dump({"log": {"loglevel": "info"},
      "inbounds": [{"tag": "client-in", "listen": "127.0.0.1", "port": port,
        "protocol": "vless",
        "settings": {"clients": [{"id": cli_uuid, "email": "beryl"}], "decryption": "none"},
        "streamSettings": {"network": "xhttp", "xhttpSettings": xh, "security": "reality",
          "realitySettings": {"show": False, "dest": DEST, "xver": 0,
            "serverNames": [SNI], "privateKey": priv, "shortIds": [cli_sid]}},
        "sniffing": sniff}],
      "outbounds": [{"tag": "out", "protocol": "freedom",
                     "settings": {"domainStrategy": "UseIP"}}]},
      open(f"{work}/exit{i}.json", "w"), indent=1)
    outbounds.append({"tag": f"exit-n{i}", "protocol": "vless",
      "settings": {"vnext": [{"address": "127.0.0.1", "port": port,
        "users": [{"id": cli_uuid, "encryption": "none", "flow": ""}]}]},
      "streamSettings": {"network": "xhttp", "xhttpSettings": xh, "security": "reality",
        "realitySettings": {"show": False, "serverName": SNI, "fingerprint": "firefox",
          "publicKey": pub, "shortId": cli_sid, "spiderX": "/"}}})

outbounds += [{"tag": "direct", "protocol": "freedom",
               "settings": {"domainStrategy": "UseIP"}},
              {"tag": "block", "protocol": "blackhole"}]
json.dump({"log": {"loglevel": "info"},
  "inbounds": [{"tag": "socks", "listen": "127.0.0.1", "port": 11090, "protocol": "socks",
    "settings": {"udp": True, "auth": "noauth"},
    "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}}],
  "outbounds": outbounds,
  "routing": {"domainStrategy": "AsIs", "rules": [
    {"type": "field", "domain": ["domain:ifconfig.me"], "outboundTag": "direct"},
    {"type": "field", "network": "tcp,udp", "balancerTag": "exits"}],
    "balancers": [{"tag": "exits", "selector": ["exit-"],
                   "strategy": {"type": "leastPing"}}]},
  "observatory": {"subjectSelector": ["exit-"],
    "probeURL": "https://www.gstatic.com/generate_204", "probeInterval": "10s",
    "enableConcurrency": True}},
  open(f"{work}/client.json", "w"), indent=1)
PY

for n in exit0 exit1 client; do
  "$XRAY_BIN" run -test -config "$WORK/$n.json" >/dev/null 2>&1 \
    && pass "$n config accepted" || fail "$n config rejected"
done

"$XRAY_BIN" run -config "$WORK/exit0.json"  >"$WORK/exit0.log" 2>&1 &
E0=$!
"$XRAY_BIN" run -config "$WORK/exit1.json"  >"$WORK/exit1.log" 2>&1 &
E1=$!
"$XRAY_BIN" run -config "$WORK/client.json" >"$WORK/client.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11090 2>/dev/null && break; sleep 1; done
sleep 3

head1 "both exits up"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 40 \
        --socks5-hostname 127.0.0.1:11090 https://api.ipify.org/ 2>/dev/null)"
assert_eq "200" "$CODE" "single-hop direct path carries traffic"

USED="$(grep -o 'api.ipify.org:443 \[socks >> exit-n[01]\]' "$WORK/client.log" | tail -1)"
info "routed via: ${USED:-unknown}"

head1 "kill the exit currently in use"
case "$USED" in
  *exit-n0*) kill "$E0" 2>/dev/null; KILLED=exit-n0; SURV=exit-n1 ;;
  *)         kill "$E1" 2>/dev/null; KILLED=exit-n1; SURV=exit-n0 ;;
esac
pass "killed ${KILLED}"

info "waiting for the observatory (probeInterval 10s)"
OK=""
for i in $(seq 1 12); do
  sleep 5
  C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 \
       --socks5-hostname 127.0.0.1:11090 https://api.ipify.org/ 2>/dev/null)"
  if [ "$C" = "200" ]; then OK="yes"; info "recovered after ~$((i*5))s"; break; fi
done
[ -n "$OK" ] && pass "traffic continued on ${SURV} with no client change" \
             || fail "client did not fail over — a rotation would be an outage"

grep -q "socks >> ${SURV}" "$WORK/client.log" \
  && pass "client log confirms the surviving exit took over" \
  || info "could not confirm from the log (balancer may have reused a live connection)"

summary
