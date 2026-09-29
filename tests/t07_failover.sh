#!/usr/bin/env bash
# t07 — kill an exit and confirm the entry node's balancer moves traffic without
# any client-side change. This is the property that makes scheduled teardown safe.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl jq python3 || exit 1

CLIENT="${1:-$(jq -r '.clients[0].name // empty' state/fleet.json 2>/dev/null)}"
SERVING="$(jq -r '[.exits[] | select(.serving)] | length' state/fleet.json)"
[ "$SERVING" -ge 2 ] || { echo "need >= 2 serving exits (have ${SERVING}); raise exits.pool_size"; exit 1; }

XRAY_BIN="$(ensure_xray)" || exit 1
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t07 — exit failover"
./bin/fleet client config "$CLIENT" --socks-port 11087 -o "$WORK/c.json" >/dev/null 2>&1
"$XRAY_BIN" run -config "$WORK/c.json" >"$WORK/c.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11087 2>/dev/null && break; sleep 1; done
P="--socks5-hostname 127.0.0.1:11087"

BEFORE="$(curl -s --max-time 30 $P https://api.ipify.org)"
info "currently exiting via ${BEFORE}"
VICTIM="$(jq -r --arg ip "$BEFORE" '.exits[] | select(.ipv4==$ip) | .name' state/fleet.json)"
[ -n "$VICTIM" ] || { fail "egress ${BEFORE} is not a known exit"; summary; exit 1; }
pass "identified the active exit: ${VICTIM}"

SSH_PORT="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).defaults.ssh_port)")"
info "stopping xray on ${VICTIM} (it will be restarted at the end)"
ssh -p "$SSH_PORT" -o UserKnownHostsFile=state/known_hosts -o BatchMode=yes \
    "root@${BEFORE}" "systemctl stop xray" 2>/dev/null \
  && pass "stopped xray on ${VICTIM}" || { fail "could not reach ${VICTIM} over SSH"; summary; exit 1; }

restore() {
  ssh -p "$SSH_PORT" -o UserKnownHostsFile=state/known_hosts -o BatchMode=yes \
      "root@${BEFORE}" "systemctl start xray" 2>/dev/null \
    && info "restarted xray on ${VICTIM}" || fail "COULD NOT RESTART ${VICTIM} — do it by hand"
}
trap 'restore; kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

info "waiting for the observatory to notice (probeInterval is 30s)"
AFTER=""
for i in $(seq 1 15); do
  sleep 5
  AFTER="$(curl -s --max-time 20 $P https://api.ipify.org 2>/dev/null)"
  [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ] && { info "failed over after ~$((i*5))s"; break; }
done

if [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ]; then
  pass "traffic moved to ${AFTER} with no client change"
elif [ -n "$AFTER" ]; then
  fail "still exiting via ${BEFORE} — the balancer did not move"
else
  fail "no egress at all after the exit died — failover did not happen"
fi

summary
