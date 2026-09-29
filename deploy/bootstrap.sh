#!/usr/bin/env bash
# Runs once, on first boot, from cloud-init. Idempotent — safe to re-run by hand.
set -euo pipefail

XRAY_VERSION="${XRAY_VERSION:?}"
XRAY_SHA256="${XRAY_SHA256:-}"
SSH_PORT="${SSH_PORT:-2222}"
NODE_ROLE="${NODE_ROLE:-exit}"
SERVICE_PORT="${SERVICE_PORT:-443}"
SSH_ALLOW_V4="${SSH_ALLOW_V4:-0.0.0.0/0}"

log() { echo "[fleet-bootstrap] $*" >&2; }

export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------- packages
log "installing packages"
for _ in 1 2 3 4 5; do
  apt-get update -qq && break || sleep 10
done
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl unzip nftables chrony jq >/dev/null

# ----------------------------------------------------------- kernel tuning
log "applying sysctl tuning"
cat >/etc/sysctl.d/99-fleet.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
net.ipv4.tcp_slow_start_after_idle = 0
net.netfilter.nf_conntrack_max = 262144
fs.file-max = 1048576
EOF
modprobe tcp_bbr 2>/dev/null || true
sysctl --system >/dev/null 2>&1 || true

cat >/etc/security/limits.d/99-fleet.conf <<'EOF'
* soft nofile 1048576
* hard nofile 1048576
EOF

# ------------------------------------------------------------------- xray
install_xray() {
  local ver="$1" url dgst tmp want got
  tmp="$(mktemp -d)"
  url="https://github.com/XTLS/Xray-core/releases/download/v${ver}/Xray-linux-64.zip"
  log "downloading Xray v${ver}"
  curl -fsSL --retry 5 --retry-delay 3 -o "${tmp}/xray.zip" "$url"

  if [ -n "$XRAY_SHA256" ]; then
    want="$XRAY_SHA256"
  else
    curl -fsSL --retry 3 -o "${tmp}/xray.dgst" "${url}.dgst" || true
    # Xray's .dgst labels the line "SHA2-256=", NOT "SHA256". Matching the latter
    # silently yielded an empty checksum, so the guard below aborted EVERY build.
    # Match the line first, then pull the digest out of it.
    want="$(grep -iE 'sha-?2?-?256' "${tmp}/xray.dgst" 2>/dev/null \
            | grep -Eo '[0-9a-f]{64}' | head -1 || true)"
  fi
  if [ -z "$want" ]; then
    log "FATAL: no SHA256 available for the Xray release; refusing to install unverified"
    exit 1
  fi
  got="$(sha256sum "${tmp}/xray.zip" | cut -d' ' -f1)"
  if [ "$want" != "$got" ]; then
    log "FATAL: Xray checksum mismatch (want ${want}, got ${got})"
    exit 1
  fi
  log "checksum verified"

  unzip -oq "${tmp}/xray.zip" -d "${tmp}/x"
  install -m 0755 "${tmp}/x/xray" /usr/local/bin/xray
  install -d -m 0755 /usr/local/share/xray
  install -m 0644 "${tmp}/x/geoip.dat" "${tmp}/x/geosite.dat" /usr/local/share/xray/
  rm -rf "$tmp"
}
install_xray "$XRAY_VERSION"

install -d -m 0700 /usr/local/etc/xray
id -u xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin xray
chown -R xray:xray /usr/local/etc/xray

cat >/etc/systemd/system/xray.service <<EOF
[Unit]
Description=Xray
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
User=xray
Group=xray
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStartPre=/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=/var/log/xray
Environment=XRAY_LOCATION_ASSET=/usr/local/share/xray

[Install]
WantedBy=multi-user.target
EOF
install -d -m 0750 -o xray -g xray /var/log/xray
systemctl daemon-reload

# --------------------------------------------------------------------- ssh
log "moving SSH to port ${SSH_PORT}"
cat >/etc/ssh/sshd_config.d/99-fleet.conf <<EOF
Port ${SSH_PORT}
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
X11Forwarding no
AllowAgentForwarding no
MaxAuthTries 3
ClientAliveInterval 30
EOF
# Ubuntu ships socket-activated sshd, which ignores the Port directive.
if systemctl is-enabled ssh.socket >/dev/null 2>&1; then
  systemctl disable --now ssh.socket >/dev/null 2>&1 || true
  systemctl enable ssh.service >/dev/null 2>&1 || true
fi
sshd -t && systemctl restart ssh 2>/dev/null || systemctl restart sshd

# --------------------------------------------------------------- firewall
log "installing nftables ruleset"
SSH_ALLOW_V4="${SSH_ALLOW_V4:-0.0.0.0/0}"

# Two shapes, because an open allowlist and a restricted one want different rules:
#  - restricted: match a named interval set, no rate limit (our own deploy opens a
#    dozen connections in a minute and must not lock itself out), and no v6 at all.
#  - open: no set (nft is unreliable about 0.0.0.0/0 inside an interval set), but
#    rate-limit new connections generously enough to survive a deploy.
if [ "$SSH_ALLOW_V4" = "0.0.0.0/0" ]; then
  log "WARNING: SSH is reachable from anywhere. Set FLEET_SSH_ALLOW_V4 if you have a static IP."
  SSH_RULES="    tcp dport ${SSH_PORT} ct state new limit rate 30/minute burst 20 packets accept
    tcp dport ${SSH_PORT} ct state established,related accept"
else
  SSH_RULES="    tcp dport ${SSH_PORT} ip saddr @ssh_allow accept"
fi

SSH_SET=""
if [ "$SSH_ALLOW_V4" != "0.0.0.0/0" ]; then
  SSH_SET="  set ssh_allow {
    type ipv4_addr
    flags interval
    elements = { ${SSH_ALLOW_V4} }
  }
"
fi

cat >/etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
${SSH_SET}
  chain input {
    type filter hook input priority filter; policy drop;

    iif lo accept
    ct state established,related accept
    ct state invalid drop

    ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } \
      limit rate 10/second accept
    ip6 nexthdr ipv6-icmp limit rate 10/second accept

    tcp dport ${SERVICE_PORT} accept
    udp dport ${SERVICE_PORT} accept
${SSH_RULES}
  }

  chain forward { type filter hook forward priority filter; policy accept; }
  chain output  { type filter hook output  priority filter; policy accept; }
}
EOF
systemctl enable nftables >/dev/null 2>&1 || true
if ! nft -f /etc/nftables.conf; then
  log "FATAL: nftables ruleset rejected — refusing to leave the node unfirewalled"
  exit 1
fi

# -------------------------------------------------------------- hardening
log "disabling unattended reboots and trimming attack surface"
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
# Journald: keep it small and in RAM. Nothing useful survives a seizure anyway, and a
# node that fills its disk with logs is a node that goes down on its own.
install -d -m 0755 /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/99-fleet.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
MaxLevelStore=warning
EOF
systemctl restart systemd-journald || true

# Scrub the cloud-init user-data: it holds this node's private keys and is readable
# by any local process (and via the metadata service).
log "scrubbing cloud-init user-data"
for f in /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \
         /var/lib/cloud/instances/*/user-data.txt /var/lib/cloud/instances/*/user-data.txt.i; do
  [ -f "$f" ] && shred -u "$f" 2>/dev/null || rm -f "$f" 2>/dev/null || true
done

touch /var/lib/fleet-bootstrapped
log "bootstrap complete (role=${NODE_ROLE})"
