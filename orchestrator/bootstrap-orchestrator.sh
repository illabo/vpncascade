#!/usr/bin/env bash
# Set up the always-on box (Raspberry Pi, or any Debian-ish machine) as the
# orchestrator: fleet on a timer, plus split DNS. Run it FROM your workstation.
#
#   ./orchestrator/bootstrap-orchestrator.sh pi@192.168.1.50
#
# It is idempotent — re-run it after changing the split list or upgrading fleet.
# Nothing here touches the data path: this box never carries your traffic.
set -euo pipefail

TARGET="${1:?usage: bootstrap-orchestrator.sh <user@host> [inventory.toml]}"
INVENTORY="${2:-inventory.toml}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR="${FLEET_REMOTE_DIR:-/opt/vpncascade}"

c_ok=$'\033[32m✓\033[0m'; c_no=$'\033[31m✗\033[0m'; c_hi=$'\033[1m'; c_d=$'\033[2m'; c_r=$'\033[0m'
step() { printf '\n%s== %s ==%s\n' "$c_hi" "$*" "$c_r"; }
ok()   { printf '  %s %s\n' "$c_ok" "$*"; }
die()  { printf '  %s %s\n' "$c_no" "$*" >&2; exit 1; }
note() { printf '  %s%s%s\n' "$c_d" "$*" "$c_r"; }

sh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$@"; }

step "checking prerequisites here"
if [ ! -f "${ROOT}/${INVENTORY}" ]; then
  die "no ${INVENTORY} in $(basename "$ROOT") — create one first:
    cp inventory.example.toml inventory.toml && \$EDITOR inventory.toml
  Without it the box has nothing to configure DNS or providers from."
fi
ok "${INVENTORY} present"
[ -f "${ROOT}/.env" ] && ok ".env present" \
  || note "no .env yet — fleet can be installed, but cannot rent anything until it has one"

step "checking the target"
sh_ true 2>/dev/null || die "cannot SSH to ${TARGET} with a key. Set that up first."
ARCH="$(sh_ 'uname -m')"; OSREL="$(sh_ '. /etc/os-release 2>/dev/null && echo $ID-$VERSION_ID')"
PYV="$(sh_ 'python3 -c "import sys;print(\"%d.%d\"%sys.version_info[:2])" 2>/dev/null' || echo none)"
ok "${TARGET} — ${OSREL:-unknown}, ${ARCH}, python ${PYV}"
case "$PYV" in
  none) die "python3 missing on the target" ;;
  3.1[1-9]|3.[2-9]*) ok "python is new enough (fleet needs 3.11+ for tomllib)" ;;
  *) die "fleet needs python 3.11+ (tomllib); target has ${PYV}" ;;
esac

step "packages"
sh_ "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq" >/dev/null
sh_ "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
     openssh-client curl ca-certificates rsync dnsmasq stubby bind9-dnsutils qrencode" >/dev/null
ok "installed: openssh-client curl rsync dnsmasq stubby bind9-dnsutils qrencode"
note "bind9-dnsutils provides dig, which tests/t11_dns_integrity.sh needs"
note "qrencode is what \`fleet client uri --qr\` shells out to; without it that flag silently degrades to printing the URI"

step "protecting the SD card"
# An SD card dies from writes, and the two biggest writers on a default install are
# the journal and swap. Neither earns its keep on a box whose job is a cron tick.
sh_ "sudo install -d -m0755 /etc/systemd/journald.conf.d && \
     printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' \
       | sudo tee /etc/systemd/journald.conf.d/99-fleet.conf >/dev/null && \
     sudo systemctl restart systemd-journald" 
ok "journal moved to RAM, capped at 32 MB"
if sh_ "test -f /etc/dphys-swapfile" 2>/dev/null; then
  sh_ "sudo dphys-swapfile swapoff 2>/dev/null; sudo systemctl disable --now dphys-swapfile" >/dev/null 2>&1 || true
  ok "swap disabled (dphys-swapfile)"
else
  note "no dphys-swapfile; check 'swapon --show' yourself if this is not a Pi"
fi

step "installing fleet to ${REMOTE_DIR}"
sh_ "sudo install -d -o \$(id -un) -g \$(id -gn) -m0755 ${REMOTE_DIR}"
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'state/' --exclude '.env' \
  `# backups/ holds plaintext device configs and .env.* holds credential` \
  `# copies; neither belongs on a second box. HANDOFF.md deliberately DOES` \
  `# sync — it is the portable state doc and a stale copy is worse than none.` \
  --exclude 'backups/' --exclude '.env.*' \
  -e "ssh -o BatchMode=yes" \
  "${ROOT}/" "${TARGET}:${REMOTE_DIR}/"
ok "code synced"

if [ -f "${ROOT}/${INVENTORY}" ]; then
  scp -q -o BatchMode=yes "${ROOT}/${INVENTORY}" "${TARGET}:${REMOTE_DIR}/inventory.toml"
  ok "inventory copied"
else
  note "no ${INVENTORY} locally — copy one to ${REMOTE_DIR}/inventory.toml yourself"
fi
if [ -f "${ROOT}/.env" ]; then
  scp -q -o BatchMode=yes "${ROOT}/.env" "${TARGET}:${REMOTE_DIR}/.env"
  sh_ "chmod 600 ${REMOTE_DIR}/.env"
  ok "provider credentials copied (chmod 600)"
else
  note "no .env locally — the box cannot provision until it has provider tokens"
fi
sh_ "test -f ~/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519"
FLEET_KEY="$(sh_ 'cat ~/.ssh/id_ed25519.pub')"
ok "fleet's SSH key on the box:"
printf '      %s\n' "$FLEET_KEY"
note "this key is what fleet uses to reach the exits it creates — nothing to do,"
note "it is injected into new servers automatically."

step "split DNS"
if ! sh_ "cd ${REMOTE_DIR} && ./bin/fleet dns -o ${REMOTE_DIR}/orchestrator/dns" >/dev/null; then
  warn "fleet dns failed — skipping split DNS for now."
  warn "Fix ${REMOTE_DIR}/inventory.toml on the box, then re-run this script."
  SKIP_DNS=1
fi
if [ "${SKIP_DNS:-0}" != 1 ]; then
sh_ "sudo install -m0644 ${REMOTE_DIR}/orchestrator/dns/dnsmasq-split.conf /etc/dnsmasq.d/99-fleet.conf && \
     sudo install -m0644 ${REMOTE_DIR}/orchestrator/dns/stubby.yml /etc/stubby/stubby.yml"
sh_ "sudo systemctl enable --now stubby >/dev/null 2>&1; sudo systemctl restart stubby"
sh_ "sudo systemctl restart dnsmasq"
sleep 2
if sh_ "systemctl is-active --quiet stubby && systemctl is-active --quiet dnsmasq"; then
  ok "stubby + dnsmasq running"
else
  die "stubby or dnsmasq failed to start — 'journalctl -u stubby -u dnsmasq -n30' on the box"
fi
RESOLVED="$(sh_ "dig +short +time=3 +tries=1 @127.0.0.1 sber.ru A 2>/dev/null | head -1" || true)"
[ -n "$RESOLVED" ] && ok "domestic name resolves via DoT: sber.ru -> ${RESOLVED}" \
                   || note "sber.ru did not resolve yet; check 'journalctl -u stubby'"
fi

step "scheduling fleet"
sh_ "sudo tee /etc/systemd/system/fleet.service >/dev/null" <<UNIT
[Unit]
Description=fleet maintenance (reap, top up, rotate, refill, health)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$(echo "$TARGET" | cut -d@ -f1)
WorkingDirectory=${REMOTE_DIR}
ExecStart=${REMOTE_DIR}/cron/fleet-cron.sh cron
UNIT
sh_ "sudo tee /etc/systemd/system/fleet.timer >/dev/null" <<'UNIT'
[Unit]
Description=fleet maintenance every 15 minutes

[Timer]
OnCalendar=*:0/15
# Jitter on purpose: a fleet that churns on the exact same wall-clock minute is
# itself a pattern worth not having.
RandomizedDelaySec=180
Persistent=true

[Install]
WantedBy=timers.target
UNIT
sh_ "sudo systemctl daemon-reload && sudo systemctl enable --now fleet.timer" >/dev/null
ok "fleet.timer enabled (every 15 min, up to 3 min jitter)"
sh_ "systemctl list-timers fleet.timer --no-pager | sed -n '2p'" | sed 's/^/      /'

step "LAN control panel"
sh_ "sudo tee /etc/systemd/system/fleet-web.service >/dev/null" <<UNIT
[Unit]
Description=fleet LAN control panel
After=network-online.target
Wants=network-online.target

[Service]
User=$(echo "$TARGET" | cut -d@ -f1)
WorkingDirectory=${REMOTE_DIR}
ExecStart=${REMOTE_DIR}/orchestrator/fleet-web.py -i ${REMOTE_DIR}/inventory.toml --port 8088
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sh_ "sudo systemctl daemon-reload && sudo systemctl enable --now fleet-web.service" >/dev/null
sleep 2
if sh_ "systemctl is-active --quiet fleet-web"; then
  ok "panel running on http://$(echo "$TARGET" | cut -d@ -f2):8088"
  PANEL_PW="$(sh_ "journalctl -u fleet-web -n 40 --no-pager 2>/dev/null | grep -o 'generated panel password: .*' | tail -1 | sed 's/.*: //'" || true)"
  if [ -n "$PANEL_PW" ]; then
    printf '      password: %s\n' "$PANEL_PW"
    note "text this to whoever needs it; change it with:"
    note "  ${REMOTE_DIR}/orchestrator/fleet-web.py --set-password"
  else
    note "password already set previously; reset with --set-password if needed"
  fi
  note "LAN only — do not port-forward 8088."
else
  warn "panel failed to start — 'journalctl -u fleet-web -n30' on the box"
fi

step "done"
cat <<DONE

  Next, on this box:
    ssh ${TARGET}
    cd ${REMOTE_DIR}
    ./bin/fleet status          # should list your clients and (soon) exits
    ./bin/fleet up              # create the exit pool
    ./bin/fleet client uri beryl

  Point the router's DHCP at $(echo "$TARGET" | cut -d@ -f2) for DNS, then verify:
    ./tests/t11_dns_integrity.sh

  Control panel:  http://$(echo "$TARGET" | cut -d@ -f2):8088
                  status, credentials, and the actions above — for when you cannot
                  SSH in, e.g. a site you do not live at, behind CGNAT.

  Logs:  journalctl -u fleet.service -u fleet-web.service -f
         ${REMOTE_DIR}/state/cron.log
DONE
