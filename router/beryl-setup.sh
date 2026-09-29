#!/usr/bin/env bash
# Provision a Beryl AX (or any OpenWrt 24.10+ router) as the tunnel box.
# Run it FROM your workstation; it drives the router over SSH.
#
#   ./router/beryl-setup.sh root@192.168.8.1 beryl
#
# arg1: user@host of the router      arg2: fleet client name for this site
#
# Options (environment):
#   DISABLE_WIFI=1   turn the router's radios off (recommended when MikroTik APs
#                    carry the WiFi — it frees both cores for routing and crypto)
#   SKIP_DNS=1       do not touch DNS (leave whatever the router already uses)
#   DRY_RUN=1        show what would happen, change nothing
set -euo pipefail

TARGET="${1:?usage: beryl-setup.sh <user@host> <client-name>}"
CLIENT="${2:?usage: beryl-setup.sh <user@host> <client-name>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DISABLE_WIFI="${DISABLE_WIFI:-0}"
DRY_RUN="${DRY_RUN:-0}"

c_ok=$'\033[32m✓\033[0m'; c_no=$'\033[31m✗\033[0m'; c_w=$'\033[33m!\033[0m'
c_hi=$'\033[1m'; c_d=$'\033[2m'; c_r=$'\033[0m'
step() { printf '\n%s== %s ==%s\n' "$c_hi" "$*" "$c_r"; }
ok()   { printf '  %s %s\n' "$c_ok" "$*"; }
warn() { printf '  %s %s\n' "$c_w" "$*"; }
die()  { printf '  %s %s\n' "$c_no" "$*" >&2; exit 1; }
note() { printf '  %s%s%s\n' "$c_d" "$*" "$c_r"; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  %s[dry-run]%s %s\n' "$c_d" "$c_r" "$*"; else sh_ "$@"; fi; }
sh_()  { ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$@"; }
# (scp is deliberately unused: OpenWrt has no sftp-server.)

step "checking the router"
sh_ true 2>/dev/null || die "cannot SSH to ${TARGET}. On stock GL.iNet, enable SSH in the UI first."
sh_ 'test -f /etc/openwrt_release' || die "this does not look like OpenWrt"
eval "$(sh_ 'cat /etc/openwrt_release' | tr -d '\r')"
ok "${DISTRIB_ID:-OpenWrt} ${DISTRIB_RELEASE:-?}  (${DISTRIB_TARGET:-?})"
case "${DISTRIB_RELEASE:-0}" in
  24.*|25.*|26.*|SNAPSHOT) ok "userland is new enough for podkop" ;;
  *) die "podkop needs OpenWrt >= 24.10. Flash 4.9.0-op24 or vanilla 25.12.x first —
     see docs/00-setup-plan.md step 3." ;;
esac
PKGMGR="$(sh_ 'command -v apk >/dev/null && echo apk || (command -v opkg >/dev/null && echo opkg || echo none)')"
[ "$PKGMGR" = none ] && die "neither apk nor opkg on the router"
ok "package manager: ${PKGMGR}"
FREE_KB="$(sh_ "df -k /overlay 2>/dev/null | awk 'NR==2{print \$4}'" || echo 0)"
[ "${FREE_KB:-0}" -ge 25000 ] && ok "$((FREE_KB/1024)) MB free on /overlay" \
  || die "only $((FREE_KB/1024)) MB free on /overlay; podkop needs ~25 MB"

step "generating this site's client URIs"
# `fleet client uri` indents its URIs under a heading, so an anchored ^vless://
# match finds nothing and the script exits claiming the client has no exits.
mapfile -t URIS < <("${ROOT}/bin/fleet" client uri "$CLIENT" 2>/dev/null \
                    | grep -oE 'vless://[^[:space:]]+' || true)
[ "${#URIS[@]}" -gt 0 ] || die "no vless:// URIs for client '${CLIENT}'.
     Run:  ./bin/fleet client add ${CLIENT} && ./bin/fleet up"
ok "${#URIS[@]} exit(s) for client '${CLIENT}'"
for u in "${URIS[@]}"; do note "${u:0:78}..."; done
case "${URIS[0]}" in
  *type=xhttp*) die "these URIs use XHTTP, which podkop's sing-box cannot speak.
     Set transport = \"tcp\" in inventory.toml, run ./bin/fleet sync, and retry." ;;
esac
ok "transport is TCP + XTLS-Vision (sing-box compatible)"

if [ "$DISABLE_WIFI" = 1 ]; then
  step "disabling the router's radios"
  note "WiFi shares the two CPU cores with routing and crypto. If MikroTik APs carry"
  note "the WiFi, turning the radios off here is free throughput for the tunnel."
  run "for r in \$(uci show wireless | sed -n 's/^wireless\.\([^.]*\)=wifi-device$/\1/p'); do
         uci set wireless.\$r.disabled='1'; done; uci commit wireless; wifi reload"
  ok "radios disabled"
else
  note "radios left alone (set DISABLE_WIFI=1 to turn them off)"
fi

step "installing podkop and the local encrypted resolver"
# OpenWrt ships no sftp-server, so plain scp fails with "Connection closed".
# Pipe through ssh instead — same trick fleet/deploy.py uses.
< "${ROOT}/router/podkop-setup.sh" sh_ "cat > /tmp/podkop-setup.sh"
if [ "$DRY_RUN" = 1 ]; then
  note "[dry-run] would run: sh /tmp/podkop-setup.sh"
else
  sh_ "SKIP_DNS=${SKIP_DNS:-0} sh /tmp/podkop-setup.sh"
fi

step "pushing the exits"
# Deliberately delegated to fleet/router.py rather than duplicated here. Having the
# podkop config written in two places is precisely how this project ended up
# emitting a pre-0.7 schema that could never work (HANDOFF #32) — one implementation,
# one place to fix.
if [ "$DRY_RUN" = 1 ]; then
  note "[dry-run] would run: ./bin/fleet sync"
else
  "${ROOT}/bin/fleet" sync || die "fleet sync failed — the router has no exits yet"
fi

step "verifying"
if [ "$DRY_RUN" = 1 ]; then note "[dry-run] skipping checks"; exit 0; fi
sleep 5
# A running sing-box is NOT proof of a working tunnel: podkop will happily start,
# install nft rules and route nothing. Check the things that fail silently.
PROCS="$(sh_ "ps w | grep -c '[s]ing-box'" 2>/dev/null | tail -1 | tr -dc '0-9')"; PROCS="${PROCS:-0}"
RULES="$(sh_ "nft list ruleset 2>/dev/null | grep -c podkop" 2>/dev/null | tail -1 | tr -dc '0-9')"; RULES="${RULES:-0}"
FATAL="$(sh_ "logread -e podkop | tail -30 | grep -ciE 'Aborted|not found|fatal'" 2>/dev/null | tail -1 | tr -dc '0-9')"; FATAL="${FATAL:-0}"
[ "${PROCS:-0}" -ge 1 ] && ok "sing-box running" || warn "sing-box NOT running"
[ "${RULES:-0}" -ge 1 ] && ok "${RULES} podkop nftables rules installed" \
  || warn "no podkop nftables rules — traffic is NOT being intercepted"
[ "${FATAL:-0}" -eq 0 ] && ok "no fatal lines in podkop's log" \
  || warn "${FATAL} fatal line(s) in podkop's log — run: ssh ${TARGET} 'logread -e podkop | tail -20'"

step "DNS"
DNSSRV="$(sh_ "uci -q get podkop.settings.dns_server" 2>/dev/null || true)"
DNSTYPE="$(sh_ "uci -q get podkop.settings.dns_type" 2>/dev/null || true)"
note "podkop resolves via: ${DNSTYPE:-?} ${DNSSRV:-?}"
case "$DNSSRV" in
  127.*) ok "local resolver (dnsproxy) — several encrypted upstreams, load-balanced" ;;
  *)     warn "single upstream resolver: if it goes away, all DNS goes with it" ;;
esac

EXIT_IPS="$("${ROOT}/bin/fleet" status 2>/dev/null | awk '/^  exit-/{print $4}' | tr '\n' ' ')"
SEEN="$(sh_ "curl -s --max-time 25 https://api.ipify.org" 2>/dev/null || true)"
note "router's own egress: ${SEEN:-<no answer>}   known exits: ${EXIT_IPS:-none}"
note "the ROUTER's own traffic is not proxied by podkop — only forwarded LAN traffic is."
note "Test from a LAN client instead:"
note "  curl -s https://api.ipify.org                    # expect an exit address"
note "  curl -s https://yandex.ru/internet/api/v0/ip     # expect your home ISP address"

cat <<DONE

  ${c_hi}Fail-open check — do this before you walk away${c_r}
    ssh ${TARGET} '/etc/init.d/podkop stop'
    # from a LAN client: Russian sites must still work
    ssh ${TARGET} '/etc/init.d/podkop start'

  That one property is what makes an unattended site survivable.
DONE
