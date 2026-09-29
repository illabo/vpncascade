#!/usr/bin/env bash
# t12 — does fleet actually reconfigure the router after a rotation?
#
# This is the loop that direct mode otherwise leaves open: fleet replaces an exit, and
# something has to tell the router. The answer is that the orchestrator and the router
# share a LAN, so fleet renders the new client URIs and SSHes across to write them into
# podkop. No hosted subscription, no DNS, no inbound path from the internet.
#
# Here that router is a container running sshd plus stand-ins for uci, podkop and
# sing-box, so the whole push path runs for real against something that answers like
# OpenWrt does.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require docker python3 ssh || exit 1

NAME=fleet-t12-router
IMG=fleet-t12
WORK="$(mktemp -d)"
PORT=22212
# Split, like t10: the pre-run sweep must clear a container left by an aborted run
# WITHOUT deleting the work directory we just created.
sweep() { docker rm -f "$NAME" >/dev/null 2>&1; }
cleanup() { sweep; rm -rf "$WORK"; }
trap cleanup EXIT
sweep

head1 "t12 — fleet → router push"

BASE=""
for cand in debian:12-slim debian:13-slim ubuntu:24.04 rust:latest; do
  docker image inspect "$cand" >/dev/null 2>&1 && { BASE="$cand"; break; }
done
[ -n "$BASE" ] || { fail "no local base image"; exit 1; }

ssh-keygen -q -t ed25519 -N '' -f "$WORK/key" -C fleet-t12
PUB="$(cat "$WORK/key.pub")"

# A stand-in OpenWrt: sshd, a uci that persists to a file, an init script, and a
# process named sing-box so the liveness check has something real to find.
docker build -q -t "$IMG" -f - "$WORK" >/dev/null 2>&1 <<DOCKER
FROM ${BASE}
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    openssh-server procps && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd
RUN printf '%s\n' '#!/bin/bash' \
 'DB=/etc/uci.db; touch \$DB' \
 'case "\$1" in' \
 '  set) echo "\${2}" >> \$DB ;;' \
 '  add_list) echo "LIST \${2}" >> \$DB ;;' \
 '  -q) case "\$2" in delete) sed -i "/^LIST \${3}=/d" \$DB ;; get) grep -m1 "^LIST \${3}=" \$DB | cut -d= -f2- ;; esac ;;' \
 '  commit) : ;;' \
 '  show) cat \$DB ;;' \
 'esac; exit 0' > /usr/bin/uci && chmod +x /usr/bin/uci
RUN mkdir -p /etc/init.d && printf '%s\n' '#!/bin/bash' \
 'case "\$1" in' \
 '  restart|start) pkill -f "sing-box" 2>/dev/null; (setsid /usr/bin/sing-box </dev/null >/dev/null 2>&1 &) ; sleep 1 ;;' \
 '  stop) pkill -f "sing-box" ;;' \
 'esac; exit 0' > /etc/init.d/podkop && chmod +x /etc/init.d/podkop
RUN printf '%s\n' '#!/bin/bash' 'exec sleep 86400' > /usr/bin/sing-box && chmod +x /usr/bin/sing-box
RUN printf '%s\n' '#!/bin/bash' 'echo "podkop stub: \$*"' > /usr/bin/logread && chmod +x /usr/bin/logread
RUN mkdir -p /root/.ssh && echo '${PUB}' > /root/.ssh/authorized_keys && chmod 700 /root/.ssh
CMD ["/usr/sbin/sshd","-D","-e"]
DOCKER
[ $? -eq 0 ] && pass "built a stand-in OpenWrt router" || { fail "build failed"; exit 1; }

docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:22" "$IMG" >/dev/null
for _ in $(seq 1 30); do
  ssh -q -p "$PORT" -i "$WORK/key" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o BatchMode=yes root@127.0.0.1 true 2>/dev/null && break
  sleep 1
done
ssh -q -p "$PORT" -i "$WORK/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes root@127.0.0.1 true 2>/dev/null \
  && pass "router reachable over SSH" || { fail "router never came up"; exit 1; }

# An inventory whose [router] points at the container, plus two fake exits.
cat > "$WORK/inventory.toml" <<TOML
mode = "direct"
[defaults]
ssh_key = "$WORK/key"
[entry]
reality_dest = "corp.ozon.ru:443"
[router]
enabled = true
host = "root@127.0.0.1"
ssh_port = ${PORT}
ssh_key = "$WORK/key"
client = "beryl"
[exits]
pool_size = 2
transport = "tcp"
reality_dest = "www.wikipedia.org:443"
[[exits.providers]]
name = "hetzner"
regions = ["hel1"]
TOML
python3 - "$WORK" <<'PY'
import sys, time; sys.path.insert(0, '.')
from fleet import config as cfgmod, keys
from fleet.state import State
w = sys.argv[1]
inv = cfgmod.load(f"{w}/inventory.toml")
st = State(inv.state_path)
st.add_client({"name": "beryl", "uuid": keys.new_uuid(),
               "short_id": keys.short_id(4), "created_at": int(time.time())})
for i, ip in enumerate(("198.51.100.21", "198.51.100.22")):
    pk, pub = keys.reality_keypair()
    st.add_exit({"id": f"e{i}", "name": f"exit-a{i}", "role": "exit", "provider": "hetzner",
        "provider_id": str(i), "region": "hel1", "ipv4": ip, "ipv6": "", "port": 443,
        "created_at": int(time.time()), "expires_at": None, "status": "live",
        "serving": True, "reality_private": pk, "reality_public": pub,
        "reality_dest": "www.wikipedia.org:443", "reality_sni": "www.wikipedia.org",
        "xhttp_host": "www.wikipedia.org", "xhttp_path": "/api/v1/update",
        "xhttp_mode": "stream-one", "transport": "tcp",
        "uplink_uuid": keys.new_uuid(), "uplink_short_id": keys.short_id(),
        "host_key_public": "x", "xray_version": "test", "fingerprint": "firefox",
        "asn": 24940 + i})
st.save()
PY
pass "inventory + 2 exits staged"

head1 "push"
OUT="$(./bin/fleet -i "$WORK/inventory.toml" router push 2>&1)"
echo "$OUT" | grep -q "router updated" && pass "fleet reported a successful push" \
  || { fail "push failed"; echo "$OUT" | sed 's/^/    /'; }

RCONF="$(ssh -q -p "$PORT" -i "$WORK/key" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null root@127.0.0.1 'cat /etc/uci.db')"
COUNT="$(printf '%s\n' "$RCONF" | grep -c 'proxy_string=vless://' || true)"
assert_eq "2" "$COUNT" "both exit URIs landed in the router's config"
assert_contains "$RCONF" "198.51.100.21" "first exit address present"
assert_contains "$RCONF" "198.51.100.22" "second exit address present"
assert_contains "$RCONF" "proxy_type=urltest" "urltest failover enabled for >1 exit"
ssh -q -p "$PORT" -i "$WORK/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@127.0.0.1 'pgrep -f sing-box >/dev/null' \
  && pass "podkop was restarted and sing-box is running" || fail "sing-box not running"

head1 "idempotence"
OUT2="$(./bin/fleet -i "$WORK/inventory.toml" router push 2>&1)"
echo "$OUT2" | grep -q "already has these" && pass "second push is a no-op (fingerprint match)" \
  || fail "pushed again unnecessarily"

head1 "after a rotation"
python3 - "$WORK" <<'PY'
import sys, time; sys.path.insert(0, '.')
from fleet import config as cfgmod, keys
from fleet.state import State
w = sys.argv[1]
inv = cfgmod.load(f"{w}/inventory.toml"); st = State(inv.state_path)
st.drop_exit("e0")                                  # the old exit is destroyed
pk, pub = keys.reality_keypair()
st.add_exit({**st.exits[0], "id": "e2", "name": "exit-a2", "ipv4": "198.51.100.99",
             "provider_id": "2", "reality_private": pk, "reality_public": pub,
             "asn": 201814})
st.save()
PY
OUT3="$(./bin/fleet -i "$WORK/inventory.toml" router push 2>&1)"
echo "$OUT3" | grep -q "router updated" && pass "rotation triggered a fresh push" || fail "no push after rotation"
RCONF2="$(ssh -q -p "$PORT" -i "$WORK/key" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null root@127.0.0.1 'cat /etc/uci.db')"
assert_contains "$RCONF2" "198.51.100.99" "the NEW exit reached the router"
assert_not_contains "$(printf '%s' "$RCONF2" | tail -5)" "198.51.100.21" \
  "the destroyed exit is gone from the latest write"

summary
