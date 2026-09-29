#!/usr/bin/env bash
# t05 — what does the outside world see? Run against any node.
#   usage: tests/t05_attack_surface.sh <host> [service-port] [ssh-port]
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require nc python3 || exit 1

HOST="${1:?usage: t05_attack_surface.sh <host> [service-port] [ssh-port]}"
SVC="${2:-443}"
SSHP="${3:-$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).defaults.ssh_port)" 2>/dev/null || echo 2222)}"

head1 "t05 — attack surface  ${DIM}${HOST}${RST}"

probe() { timeout 4 nc -z "$HOST" "$1" 2>/dev/null; }

probe "$SVC" && pass "service port ${SVC} open (expected)" || fail "service port ${SVC} CLOSED"

head1 "ports that should be shut"
CLOSED=0; OPEN_LIST=""
for p in 21 22 23 25 53 80 110 111 135 139 445 587 993 995 1080 3128 3306 3389 5432 5900 6379 8080 8388 8443 9000 9090 10000 27017; do
  [ "$p" = "$SVC" ] && continue
  [ "$p" = "$SSHP" ] && continue
  if probe "$p"; then OPEN_LIST="${OPEN_LIST} ${p}"; else CLOSED=$((CLOSED+1)); fi
done
if [ -z "$OPEN_LIST" ]; then
  pass "all ${CLOSED} probed ports are closed"
else
  fail "unexpectedly open:${OPEN_LIST}"
fi

if [ "$SSHP" != "22" ]; then
  probe 22 && fail "port 22 is still open — sshd did not move to ${SSHP}" \
            || pass "port 22 closed (sshd moved to ${SSHP})"
fi
probe "$SSHP" && info "ssh reachable on ${SSHP} — restrict it with FLEET_SSH_ALLOW_V4" \
              || pass "ssh on ${SSHP} not reachable from here (source-restricted)"

head1 "banner leakage"
BANNER="$(timeout 5 bash -c "exec 3<>/dev/tcp/${HOST}/${SSHP}; head -c 80 <&3" 2>/dev/null)"
[ -n "$BANNER" ] && info "ssh banner: ${BANNER}" || info "no ssh banner readable"

summary
