#!/usr/bin/env bash
# t08 — "how findable am I?"  Scans a node the way RKN's automation plausibly does.
#
# This exists because of what actually happened in the December 2025 Aeza sweep: the
# users who got 24-hour takedown notices were, by their own accounts, running an x-ui
# panel on a high port with SSH on 22 alongside REALITY on 443. REALITY did not fail
# them — their management surface did. Nothing here tests whether REALITY is good
# (t01 does that); this tests whether anything *else* on the box gives you away.
#
#   usage: tests/t08_exposure_audit.sh <host> [service-port]
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl python3 || exit 1

HOST="${1:?usage: t08_exposure_audit.sh <host> [service-port]}"
SVC="${2:-443}"
SSHP="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).defaults.ssh_port)" 2>/dev/null || echo 2222)"

head1 "t08 — exposure audit  ${DIM}${HOST}${RST}"
probe() { timeout 4 bash -c "exec 3<>/dev/tcp/${HOST}/$1" 2>/dev/null; }

head1 "1. management panels  ${DIM}(the vector that caught people at Aeza)${RST}"
# Default and near-default ports for the panels people actually run.
PANEL_PORTS="54321 2053 2087 2096 8443 8880 10085 10086 50000 50001 62789 2017 8081 8888 9090 3000 7681"
FOUND=""
for p in $PANEL_PORTS; do probe "$p" && FOUND="${FOUND} ${p}"; done
if [ -z "$FOUND" ]; then
  pass "no known panel port answers"
else
  fail "open panel-ish ports:${FOUND} — this is the single biggest giveaway"
  for p in $FOUND; do
    T="$(curl -sk -m 6 -o /dev/null -w '%{http_code}' "https://${HOST}:${p}/" 2>/dev/null)"
    B="$(curl -sk -m 6 "https://${HOST}:${p}/" 2>/dev/null | head -c 400)"
    info "  :${p} → HTTP ${T}"
    case "$B" in
      *[Xx]-[Uu][Ii]*|*3x-ui*|*[Mm]arzban*|*[Hh]iddify*|*[Rr]emnawave*)
        fail "  :${p} serves a recognisable proxy panel — take it off the public interface" ;;
    esac
  done
  info "  fix: bind panels to 127.0.0.1 and reach them over an SSH tunnel."
  info "  This design has no panel at all — configs are files pushed over SSH."
fi

head1 "2. other VPN protocols on the same IP"
# A node that also answers plain WireGuard/OpenVPN/SOCKS is trivially classified,
# and one weak protocol taints the whole address.
for p in 1080 1194 8388 8080 3128 500 4500 1701; do
  probe "$p" && fail "TCP/${p} open (socks/openvpn/shadowsocks/l2tp family)" || :
done
UDPHINT=""
command -v nc >/dev/null 2>&1 && for p in 51820 1194 500 4500; do
  timeout 3 nc -u -z "$HOST" "$p" 2>/dev/null && UDPHINT="${UDPHINT} ${p}"
done
[ -z "$UDPHINT" ] && pass "no obvious extra VPN listeners" \
  || info "UDP possibly open:${UDPHINT} (UDP probing is unreliable; verify on the box)"

head1 "3. TLS is only where it should be"
probe "$SVC" && pass "service port ${SVC} answers" || fail "service port ${SVC} closed"
[ "$SVC" = "443" ] && pass "REALITY is on 443 (TLS on an odd port is its own signal)" \
  || fail "REALITY is on ${SVC}, not 443 — move it"
OTHER_TLS=""
for p in 8443 9443 4433 2083 2087; do
  probe "$p" && timeout 6 openssl s_client -connect "${HOST}:${p}" </dev/null 2>/dev/null \
    | grep -q CONNECTED && OTHER_TLS="${OTHER_TLS} ${p}"
done
[ -z "$OTHER_TLS" ] && pass "no second TLS endpoint" || fail "extra TLS on:${OTHER_TLS}"

head1 "4. reverse DNS"
PTR="$(python3 -c "
import socket,sys
try: print(socket.gethostbyaddr('${HOST}')[0])
except Exception: print('')" 2>/dev/null)"
if [ -z "$PTR" ]; then
  pass "no PTR record (fine — generic is good)"
else
  info "PTR: ${PTR}"
  case "$(printf '%s' "$PTR" | tr 'A-Z' 'a-z')" in
    *vpn*|*proxy*|*tunnel*|*xray*|*v2ray*|*shadow*|*wg*|*wireguard*)
      fail "PTR contains a giveaway word — rename it in the provider panel" ;;
    *) pass "PTR looks generic" ;;
  esac
fi

head1 "5. is the address publicly known?"
info "Automated lists are one way a node gets onto a takedown list without ever being"
info "probed. Check by hand, they need API keys:"
info "  https://www.shodan.io/host/${HOST}"
info "  https://search.censys.io/hosts/${HOST}"
info "  https://ipinfo.io/${HOST}"
info "And the one that matters most: is this IP in any public subscription link,"
info "Telegram channel, or shared config? If yes, it is already burned."

head1 "6. client count  ${DIM}(does it look personal or commercial?)${RST}"
N="$(python3 -c "
import json
try: print(len(json.load(open('state/fleet.json')).get('clients',[])))
except Exception: print(0)" 2>/dev/null)"
info "${N} client credential(s) configured"
if [ "$N" -le 8 ] 2>/dev/null; then
  pass "reads as a personal node"
else
  fail "${N} clients — that traffic profile reads as a service, which is what gets swept"
fi

head1 "7. relay signature  ${DIM}(specific to the cascade)${RST}"
info "Your entry node holds a long-lived, high-volume TLS session to a single foreign"
info "IP. Ordinary web servers do not do that, and it is the one pattern this topology"
info "creates that a plain exit node would not. Check from the entry node:"
info "  ss -tnp state established '( dport = :443 )' | grep -v <your-lan>"
info "Keeping >=2 exits in the balancer splits the flow and blunts the pattern."

summary
