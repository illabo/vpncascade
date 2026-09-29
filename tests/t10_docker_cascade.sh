#!/usr/bin/env bash
# t10 — the whole architecture on real, separate hosts.
#
# t00 proves the configs work; it does it on loopback, where every hop shares one
# address, so it cannot prove *which* box a request actually came out of. This one
# runs each node in its own container with its own IP and stands up two fake services
# — one standing in for a Russian site, one for the rest of the internet — that report
# the source address they see. That turns the central architectural claim into a
# measurement:
#
#   request to the "RU" service   must arrive from the ENTRY node's address
#   request to the "world" service must arrive from an EXIT node's address
#
# Configs come from `fleet render` and `fleet client config`, so this exercises the
# real rendering path, not a hand-written approximation.
#
# Needs Docker. Everything is torn down on exit.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require docker curl python3 || exit 1

NET=fleet-t10
# Must be ordinary PUBLIC unicast space, and that constraint is sharper than it looks.
# Both the client config and the exit config treat geoip:private as never-tunnel /
# never-forward — correct in production, but it means a test on a 172.16/12 Docker
# bridge sends everything direct and "passes" nothing. TEST-NET-3 (203.0.113.0/24) is
# no good either: Xray's geoip:private is a bogon list, not just RFC1918, and the
# documentation ranges are in it. So we borrow real allocated space. Nothing leaves
# the bridge, and the REALITY masking host is resolved normally.
PREFIX=185.199.108
WORK="$(mktemp -d)"
IMG=fleet-t10-xray

# Split deliberately: the pre-run sweep must clear containers left by an aborted run
# WITHOUT deleting the work directory we just created.
sweep_docker() {
  docker rm -f t10-client t10-entry t10-exit1 t10-exit2 t10-ru t10-world >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
cleanup() { sweep_docker; rm -rf "$WORK"; }
trap cleanup EXIT
sweep_docker

head1 "t10 — full cascade on separate hosts (Docker)"

# ---------------------------------------------------------------- xray binary
case "$(docker info --format '{{.Architecture}}' 2>/dev/null)" in
  aarch64|arm64) ASSET=Xray-linux-arm64-v8a.zip ;;
  *)             ASSET=Xray-linux-64.zip ;;
esac
CACHE="${TMPDIR:-/tmp}/fleet-xray-linux"
mkdir -p "$CACHE"
if [ ! -x "$CACHE/xray" ]; then
  VER="$(curl -fsSL https://api.github.com/repos/XTLS/Xray-core/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  info "fetching Xray ${VER} (${ASSET})"
  curl -fsSL -o "$CACHE/x.zip" \
    "https://github.com/XTLS/Xray-core/releases/download/${VER}/${ASSET}" || {
      fail "could not download xray"; exit 1; }
  ( cd "$CACHE" && unzip -oq x.zip )
fi
pass "xray binary for containers: $("$CACHE/xray" version 2>/dev/null | head -1 || echo present)"

# Base image: prefer something already present, so this test never depends on a
# registry pull. Any Debian/Ubuntu-ish image with python3 + curl works.
BASE="${T10_BASE:-}"
if [ -z "$BASE" ]; then
  for cand in debian:12-slim debian:13-slim ubuntu:24.04 rust:latest python:3-slim; do
    docker image inspect "$cand" >/dev/null 2>&1 && { BASE="$cand"; break; }
  done
fi
if [ -z "$BASE" ]; then
  info "no suitable local base image; pulling debian:12-slim (needs registry access)"
  docker pull -q debian:12-slim >/dev/null 2>&1 && BASE=debian:12-slim
fi
[ -n "$BASE" ] || { fail "no base image available and the pull failed"; exit 1; }
info "base image: ${BASE}"

docker build -q -t "$IMG" -f - "$CACHE" >"$WORK/build.log" 2>&1 <<DOCKER
FROM ${BASE}
COPY xray /usr/local/bin/xray
COPY geoip.dat geosite.dat /usr/local/share/xray/
ENV XRAY_LOCATION_ASSET=/usr/local/share/xray
DOCKER
if [ $? -eq 0 ]; then pass "built test image on ${BASE}"; else
  fail "docker build failed"; tail -5 "$WORK/build.log"; exit 1; fi

for c in python3 curl; do
  docker run --rm "$IMG" sh -c "command -v $c >/dev/null" \
    || { fail "base image ${BASE} lacks ${c}; set T10_BASE to one that has it"; exit 1; }
done

docker network create --subnet "${PREFIX}.0/24" "$NET" >/dev/null 2>&1 \
  && pass "created network ${PREFIX}.0/24" || { fail "network create failed"; exit 1; }

# ------------------------------------------------------- the two fake services
cat > "$WORK/echo.py" <<'PY'
import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
NAME = os.environ.get("SVC", "svc")
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
        self.wfile.write(f"{NAME} {self.client_address[0]}\n".encode())
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 80), H).serve_forever()
PY
for pair in "ru:10" "world:11"; do
  svc="${pair%%:*}"; oct="${pair##*:}"
  docker run -d --name "t10-${svc}" --network "$NET" --ip "${PREFIX}.${oct}" \
    -e "SVC=${svc}" -v "$WORK/echo.py:/echo.py:ro" "$IMG" \
    python3 /echo.py >/dev/null 2>&1
done
sleep 2
docker run --rm --network "$NET" "$IMG" curl -s --max-time 5 "http://${PREFIX}.11/" >/dev/null 2>&1 \
  && pass "fake services answering" || { fail "services did not start"; exit 1; }

# ------------------------------------------------------ render the real configs
python3 - "$WORK" "$PREFIX" <<'PY'
import json, os, sys, time
sys.path.insert(0, '.')
from fleet import config as cfgmod, keys, render
from fleet.state import State

work, prefix = sys.argv[1], sys.argv[2]
SNI, DEST = "www.wikipedia.org", "www.wikipedia.org:443"

inv_path = os.path.join(work, "inventory.toml")
open(inv_path, "w").write(f'''
mode = "cascade"
[defaults]
ssh_key = "~/.ssh/id_ed25519"
[entry]
name = "entry-ru"
provider = "manual"
host = "{prefix}.30"
port = 443
reality_dest = "{DEST}"
reality_sni = ["{SNI}"]
transport = "tcp"
[entry.split]
enabled = true
direct_domains = []
direct_ips = ["{prefix}.10/32"]
[exits]
pool_size = 2
reality_dest = "{DEST}"
reality_sni = ["{SNI}"]
transport = "tcp"
[[exits.providers]]
name = "hetzner"
regions = ["hel1"]
''')
os.makedirs(os.path.join(work, "state"), exist_ok=True)
inv = cfgmod.load(inv_path)
st = State(inv.state_path)

epriv, epub = keys.reality_keypair()
st.entry = {"name": "entry-ru", "host": inv.entry.host, "port": 443,
            "reality_private": epriv, "reality_public": epub,
            "created_at": int(time.time()), "xray_version": "test"}
st.add_client({"name": "beryl", "uuid": keys.new_uuid(),
               "short_id": keys.short_id(4), "created_at": int(time.time())})

for i, oct_ in enumerate((21, 22), start=1):
    pk, pub = keys.reality_keypair()
    st.add_exit({
        "id": f"exit{i}", "name": f"exit{i}", "role": "exit", "provider": "hetzner",
        "provider_id": str(i), "region": "hel1", "ipv4": f"{prefix}.{oct_}", "ipv6": "",
        "port": 443, "created_at": int(time.time()), "expires_at": None,
        "status": "live", "serving": True, "reality_private": pk, "reality_public": pub,
        "reality_dest": DEST, "reality_sni": SNI, "xhttp_host": SNI,
        "xhttp_path": "/api/v1/update", "xhttp_mode": "stream-one", "transport": "tcp",
        "uplink_uuid": keys.new_uuid(), "uplink_short_id": keys.short_id(),
        "host_key_public": "x", "xray_version": "test", "fingerprint": "firefox",
        "asn": 24940 + i, "asn_holder": f"TestNet{i}"})
st.save()

open(f"{work}/entry.json", "w").write(render.dumps(render.entry_config(inv, st)))
for n in st.exits:
    open(f"{work}/{n['name']}.json", "w").write(render.dumps(render.exit_config(inv, n)))
cc = render.client_config(inv, st, st.clients[0], socks_port=1080, local_split=False)
cc["inbounds"][0]["listen"] = "0.0.0.0"
cc["log"]["loglevel"] = "info"   # so the test can show the routing decision
open(f"{work}/client.json", "w").write(render.dumps(cc))
print("rendered")
PY
[ -f "$WORK/entry.json" ] && pass "rendered entry, 2 exits and client from fleet code" \
  || { fail "render failed"; exit 1; }

# ------------------------------------------------------------------ run nodes
for pair in "exit1:21" "exit2:22" "entry:30"; do
  n="${pair%%:*}"; oct="${pair##*:}"
  docker run -d --name "t10-${n}" --network "$NET" --ip "${PREFIX}.${oct}" \
    -v "$WORK/${n}.json:/c.json:ro" "$IMG" xray run -config /c.json >/dev/null 2>&1
done
docker run -d --name t10-client --network "$NET" --ip "${PREFIX}.40" \
  -v "$WORK/client.json:/c.json:ro" "$IMG" xray run -config /c.json >/dev/null 2>&1
sleep 5

for n in exit1 exit2 entry client; do
  docker ps --format '{{.Names}}' | grep -q "t10-${n}" \
    && pass "${n} running" || { fail "${n} died:"; docker logs "t10-${n}" 2>&1 | tail -5; }
done

probe() { docker run --rm --network "$NET" "$IMG" \
  curl -s --max-time 25 --socks5-hostname "${PREFIX}.40:1080" "$1" 2>/dev/null; }

routing_decisions() {
  docker logs t10-client 2>&1 | grep -Eo '\[socks (->|>>) [a-z0-9_.-]+\]' | sort -u
  docker logs t10-entry  2>&1 | grep -Eo '\[client-in (->|>>) [a-z0-9_.-]+\]' | sort -u
}

head1 "the architectural claim, measured"
WORLD="$(probe "http://${PREFIX}.11/")"
RU="$(probe "http://${PREFIX}.10/")"
info "world service reports: ${WORLD:-<no answer>}"
info "RU service reports:    ${RU:-<no answer>}"

case "$WORLD" in
  *"${PREFIX}.21"*|*"${PREFIX}.22"*) pass "foreign traffic egressed from an EXIT node" ;;
  *"${PREFIX}.30"*) fail "foreign traffic egressed from the ENTRY node — cascade not working" ;;
  *"${PREFIX}.40"*) fail "traffic never entered the tunnel — it went direct from the client" ;;
  *) fail "no usable answer from the world service" ;;
esac
if [ "$FAIL" -gt 0 ]; then
  info "routing decisions actually taken:"
  routing_decisions | sed "s/^/    /"
fi
case "$RU" in
  *"${PREFIX}.30"*) pass "RU traffic egressed from the ENTRY node (split works)" ;;
  *"${PREFIX}.21"*|*"${PREFIX}.22"*) fail "RU traffic went out an EXIT — split rule not matching" ;;
  *"${PREFIX}.40"*) fail "RU traffic never entered the tunnel — went direct from the client" ;;
  *) fail "no usable answer from the RU service" ;;
esac

head1 "exit failure"
USED="${WORLD##* }"; USED="${USED%$'\n'}"
case "$USED" in
  "${PREFIX}.21") VICTIM=t10-exit1; SURV="${PREFIX}.22" ;;
  *)              VICTIM=t10-exit2; SURV="${PREFIX}.21" ;;
esac
docker stop "$VICTIM" >/dev/null 2>&1 && pass "stopped ${VICTIM} (was serving)"
OK=""
for i in $(seq 1 12); do
  sleep 5
  A="$(probe "http://${PREFIX}.11/")"
  case "$A" in *"$SURV"*) OK="$A"; info "failed over after ~$((i*5))s"; break ;; esac
done
[ -n "$OK" ] && pass "entry balancer moved traffic to ${SURV}, client untouched" \
             || fail "no failover — entry did not route around the dead exit"

head1 "and the split still holds with an exit down"
RU2="$(probe "http://${PREFIX}.10/")"
case "$RU2" in
  *"${PREFIX}.30"*) pass "RU traffic still direct from the entry node" ;;
  *) fail "RU path broke when an exit died (${RU2:-no answer})" ;;
esac

summary
