#!/usr/bin/env bash
# Install AmneziaWG on a Debian 12 node and wire it into the existing Xray cascade.
#
# The interface does NOT NAT to the internet. Instead nftables tproxy hands the
# subnet's traffic to Xray's dokodemo-door inbound, so AmneziaWG clients get exactly
# the same split rules and the same cascade to the exits as VLESS clients. One policy,
# two transports.
set -euo pipefail

AWG_PORT="${AWG_PORT:-51820}"
AWG_SUBNET="${AWG_SUBNET:-10.13.13.0/24}"
TPROXY_PORT="${TPROXY_PORT:-12345}"
FWMARK="${FWMARK:-0x1}"
ROUTE_TABLE="${ROUTE_TABLE:-100}"

log() { echo "[awg] $*" >&2; }
export DEBIAN_FRONTEND=noninteractive

log "installing build prerequisites"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git build-essential dkms linux-headers-"$(uname -r)" \
  libmnl-dev iproute2 nftables >/dev/null

# ------------------------------------------------- kernel module (preferred path)
install_kernel_module() {
  local src=/usr/src/amneziawg
  rm -rf "$src"
  git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git \
    "$src" >/dev/null 2>&1
  ( cd "$src/src" && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 )
  modprobe amneziawg
}

install_userspace() {
  log "kernel module unavailable — falling back to userspace amneziawg-go"
  local ver="go1.23.4" tmp; tmp="$(mktemp -d)"
  curl -fsSL "https://go.dev/dl/${ver}.linux-amd64.tar.gz" -o "${tmp}/go.tgz"
  rm -rf /usr/local/go && tar -C /usr/local -xzf "${tmp}/go.tgz"
  export PATH=$PATH:/usr/local/go/bin
  git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go.git "${tmp}/awg-go" >/dev/null 2>&1
  ( cd "${tmp}/awg-go" && make >/dev/null 2>&1 && install -m0755 amneziawg-go /usr/local/bin/ )
  rm -rf "$tmp"
}

log "installing the AmneziaWG kernel module"
if ! install_kernel_module; then
  install_userspace
fi

log "installing amneziawg-tools"
tmp="$(mktemp -d)"
git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-tools.git "${tmp}/tools" >/dev/null 2>&1
( cd "${tmp}/tools/src" && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 )
rm -rf "$tmp"
awg --version 2>/dev/null || { log "FATAL: awg not installed"; exit 1; }

install -d -m 0700 /etc/amnezia/amneziawg
# awg0.conf is written separately by `fleet awg deploy`.

# ------------------------------------------------------- tproxy into Xray
log "installing tproxy policy routing"
cat >/etc/systemd/system/awg-tproxy.service <<EOF
[Unit]
Description=Policy routing so AmneziaWG traffic reaches Xray
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip rule add fwmark ${FWMARK} lookup ${ROUTE_TABLE}
ExecStart=/sbin/ip route add local 0.0.0.0/0 dev lo table ${ROUTE_TABLE}
ExecStop=-/sbin/ip rule del fwmark ${FWMARK} lookup ${ROUTE_TABLE}
ExecStop=-/sbin/ip route del local 0.0.0.0/0 dev lo table ${ROUTE_TABLE}

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/nftables.d-awg.conf <<EOF
table inet awg {
  chain prerouting {
    type filter hook prerouting priority mangle; policy accept;

    # Only traffic that arrived on the AmneziaWG interface.
    iifname != "awg0" return

    # Never tproxy the node's own upstream connections or RFC1918 destinations;
    # that would loop traffic back into Xray.
    ip daddr { 127.0.0.0/8, 224.0.0.0/4, 255.255.255.255 } return

    meta l4proto tcp tproxy ip to 127.0.0.1:${TPROXY_PORT} meta mark set ${FWMARK} accept
    meta l4proto udp tproxy ip to 127.0.0.1:${TPROXY_PORT} meta mark set ${FWMARK} accept
  }
}
EOF
grep -q 'nftables.d-awg.conf' /etc/nftables.conf || \
  echo 'include "/etc/nftables.d-awg.conf"' >>/etc/nftables.conf

# Open the AmneziaWG port.
if ! grep -q "udp dport ${AWG_PORT} accept" /etc/nftables.conf; then
  sed -i "s|    tcp dport \([0-9]*\) accept|    tcp dport \1 accept\n    udp dport ${AWG_PORT} accept|" \
    /etc/nftables.conf
fi

cat >/etc/systemd/system/awg-quick@.service <<'EOF'
[Unit]
Description=AmneziaWG via awg-quick for %I
After=network-online.target nss-lookup.target awg-tproxy.service
Wants=network-online.target awg-tproxy.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/awg-quick up %i
ExecStop=/usr/bin/awg-quick down %i
Environment=WG_ENDPOINT_RESOLUTION_RETRIES=infinity

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now awg-tproxy.service
nft -f /etc/nftables.conf

log "AmneziaWG installed. Now run: fleet awg deploy"
