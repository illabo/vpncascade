#!/usr/bin/env sh
# Minimal alternative to podkop: xray-core on OpenWrt with nftables TPROXY.
# Run ON THE ROUTER. Needs OpenWrt 23.05+ with fw4/nftables and ~20 MB free.
#
#   fleet client config beryl -o beryl.json
#   scp beryl.json router/xray-openwrt.sh root@192.168.8.1:/tmp/
#   ssh root@192.168.8.1 'sh /tmp/xray-openwrt.sh /tmp/beryl.json'
set -eu

CONFIG_SRC="${1:?usage: xray-openwrt.sh <client-config.json>}"
TPROXY_PORT="${TPROXY_PORT:-12345}"
FWMARK="${FWMARK:-1}"
TABLE="${TABLE:-100}"

say() { echo "[xray-openwrt] $*"; }

# OpenWrt 25 replaced opkg with apk, and GL.iNet's op25 builds ship no opkg shim —
# so detect rather than assume. podkop's own installer does the same thing.
if command -v apk >/dev/null 2>&1; then
  PKG_UPDATE="apk update"; PKG_ADD="apk add"
elif command -v opkg >/dev/null 2>&1; then
  PKG_UPDATE="opkg update"; PKG_ADD="opkg install"
else
  say "neither apk nor opkg found — is this OpenWrt?"; exit 1
fi
say "package manager: ${PKG_ADD%% *}"
$PKG_UPDATE >/dev/null 2>&1 || true
$PKG_ADD xray-core kmod-nft-tproxy ip-full >/dev/null 2>&1 || {
  say "package install failed — check free space with: df -h /overlay"; exit 1; }

mkdir -p /etc/xray
# The config from `fleet client config` has socks/http inbounds; swap them for a
# dokodemo-door tproxy inbound so the router can transparently proxy the LAN.
jq --argjson port "$TPROXY_PORT" '
  .inbounds = [{
    "tag":"tproxy-in","listen":"0.0.0.0","port":$port,"protocol":"dokodemo-door",
    "settings":{"network":"tcp,udp","followRedirect":true},
    "streamSettings":{"sockopt":{"tproxy":"tproxy"}},
    "sniffing":{"enabled":true,"destOverride":["http","tls","quic"]}
  }]' "$CONFIG_SRC" > /etc/xray/config.json 2>/dev/null || {
    say "jq missing (install it with ${PKG_ADD} jq) — copying the config unchanged; add a"
    say "dokodemo-door tproxy inbound by hand"; cp "$CONFIG_SRC" /etc/xray/config.json; }
chmod 600 /etc/xray/config.json

say "installing policy routing"
cat >/etc/hotplug.d/iface/99-xray-tproxy <<EOF
#!/bin/sh
[ "\$ACTION" = ifup ] || exit 0
ip rule add fwmark ${FWMARK} lookup ${TABLE} 2>/dev/null
ip route add local 0.0.0.0/0 dev lo table ${TABLE} 2>/dev/null
EOF
chmod +x /etc/hotplug.d/iface/99-xray-tproxy
ip rule add fwmark ${FWMARK} lookup ${TABLE} 2>/dev/null || true
ip route add local 0.0.0.0/0 dev lo table ${TABLE} 2>/dev/null || true

say "installing nftables rules"
cat >/etc/nftables.d/20-xray-tproxy.nft <<EOF
chain xray_prerouting {
  type filter hook prerouting priority mangle; policy accept;

  iifname != "br-lan" return
  # Never tproxy traffic to the local network or to the tunnel endpoint itself.
  ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, \
             169.254.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } return

  meta l4proto tcp tproxy ip to 127.0.0.1:${TPROXY_PORT} meta mark set ${FWMARK} accept
  meta l4proto udp tproxy ip to 127.0.0.1:${TPROXY_PORT} meta mark set ${FWMARK} accept
}
EOF

say "installing the init script"
cat >/etc/init.d/xray <<'EOF'
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1
start_service() {
  procd_open_instance
  procd_set_param command /usr/bin/xray run -config /etc/xray/config.json
  procd_set_param respawn
  procd_set_param stderr 1
  procd_close_instance
}
EOF
chmod +x /etc/init.d/xray
/etc/init.d/xray enable
/etc/init.d/xray restart
/etc/init.d/firewall restart

sleep 2
say "status:"
pgrep -f 'xray run' >/dev/null && say "xray is running" || { say "xray FAILED"; logread -e xray | tail -20; }
nft list chain inet fw4 xray_prerouting 2>/dev/null | head -12 || true

cat <<'EOF'

Test from a LAN client:
  curl -s https://api.ipify.org     # should be an EXIT node address

Roll back:
  /etc/init.d/xray stop && /etc/init.d/xray disable
  rm /etc/nftables.d/20-xray-tproxy.nft && /etc/init.d/firewall restart
EOF
