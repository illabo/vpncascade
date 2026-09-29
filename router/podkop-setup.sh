#!/bin/sh
# shellcheck shell=dash
# Provision podkop + encrypted DNS on a GL.iNet / OpenWrt router.
#
# Runs ON the router:
#   ssh root@192.168.8.1 'sh /tmp/podkop-setup.sh'
#
# What it does NOT do: configure the exits. That belongs to `fleet sync`
# (fleet/router.py), which is the single implementation of the podkop 0.7 config.
# Two copies of that logic is exactly how this project shipped four silently-fatal
# schema bugs at once — see HANDOFF #32. Install here, configure there.
#
# Environment:
#   DNS_UPSTREAMS  space-separated dnsproxy upstreams (default: IP-pinned DoT below)
#   DNS_BIND       loopback address for the local resolver (default 127.0.0.43)
#   SKIP_DNS=1     leave DNS alone
set -eu

say()  { echo "[podkop-setup] $*"; }
warn() { echo "[podkop-setup] WARNING: $*"; }
die()  { echo "[podkop-setup] FATAL: $*" >&2; exit 1; }

DNS_BIND="${DNS_BIND:-127.0.0.43}"
SKIP_DNS="${SKIP_DNS:-0}"

# Every upstream is an IP LITERAL whose certificate carries a matching IP SAN, so
# dnsproxy needs no bootstrap at all — nothing is resolved in plaintext, and there
# is no hostname for a hijacked resolver to poison. Verified 2026-09-28 from a
# Rostelecom line: all four validate (`Verify return code: 0`) and egress outside
# Russia. Yandex is deliberately absent: it egresses 37.140.169.x (RU).
#   185.222.222.222 / 45.11.45.11  DNS.SB  (xTom, Osaka JP)
#   9.9.9.9                        Quad9   (egressed JP)
#   1.1.1.1                        Cloudflare
DNS_UPSTREAMS="${DNS_UPSTREAMS:-tls://185.222.222.222 tls://45.11.45.11 tls://9.9.9.9 tls://1.1.1.1}"

# ------------------------------------------------------------------ sanity
[ -f /etc/openwrt_release ] || die "this does not look like OpenWrt"
. /etc/openwrt_release 2>/dev/null || true
case "${DISTRIB_RELEASE:-0}" in
  24.*|25.*|26.*|SNAPSHOT) : ;;
  *) die "podkop needs OpenWrt >= 24.10 (found ${DISTRIB_RELEASE:-unknown})" ;;
esac

PKG=opkg; command -v apk >/dev/null 2>&1 && PKG=apk
say "OpenWrt ${DISTRIB_RELEASE:-?}, package manager: $PKG"

FREE_KB="$(df -k /overlay 2>/dev/null | awk 'NR==2{print $4}')"
[ "${FREE_KB:-0}" -ge 25000 ] || die "only $((${FREE_KB:-0}/1024)) MB free on /overlay; need ~25 MB"

# ------------------------------------------------------------------ podkop
if ! command -v podkop >/dev/null 2>&1 && [ ! -f /etc/config/podkop ]; then
  say "installing podkop"
  # podkop's README gives `sh <(wget -O - ...)`, a bashism that fails on ash.
  wget -O /tmp/podkop-install.sh \
    https://raw.githubusercontent.com/itdoginfo/podkop/refs/heads/main/install.sh \
    || die "could not fetch the podkop installer"
  # The installer is interactive (Russian-UI prompt, and an https-dns-proxy conflict
  # prompt if that package is present). Feed it 'n' or it blocks forever over SSH.
  printf 'n\nn\nn\n' | sh /tmp/podkop-install.sh || die "podkop installer failed"
else
  say "podkop already installed ($(podkop show_version 2>/dev/null || echo '?'))"
fi
command -v podkop >/dev/null 2>&1 || die "podkop still not on PATH after install"

# ------------------------------------------------------- local encrypted DNS
if [ "$SKIP_DNS" = 1 ]; then
  say "SKIP_DNS=1 — leaving DNS configuration alone"
else
  if ! command -v dnsproxy >/dev/null 2>&1; then
    say "installing dnsproxy"
    if [ "$PKG" = apk ]; then apk add dnsproxy >/dev/null 2>&1 || true
    else opkg update >/dev/null 2>&1; opkg install dnsproxy >/dev/null 2>&1 || true; fi
  fi

  if command -v dnsproxy >/dev/null 2>&1; then
    say "configuring dnsproxy on ${DNS_BIND}:53"
    # Why a local resolver at all: podkop takes exactly ONE `dns_server`, so pointing
    # it straight at a resolver makes that resolver a single point of failure for the
    # whole house — DNS dies, everything dies, even though the tunnel is fine.
    # dnsproxy fans out across several, so one going away is a non-event.
    #
    # load_balance (NOT parallel): parallel queries every upstream on every lookup,
    # so all of them see your full history. load_balance picks one per query, which
    # fragments history across operators instead of handing it to each of them.
    mkdir -p /etc/dnsproxy
    [ -f /etc/dnsproxy/dnsproxy.yaml ] && \
      cp /etc/dnsproxy/dnsproxy.yaml /etc/dnsproxy/dnsproxy.yaml.bak-fleet 2>/dev/null || true
    {
      echo "# Written by router/podkop-setup.sh. GL firmware updates may reset this;"
      echo "# re-running the script restores it (it is idempotent)."
      echo "upstream:"
      for u in $DNS_UPSTREAMS; do echo "  - ${u}"; done
      echo "upstream-mode: load_balance"
      echo "listen-addrs:"
      echo "  - \"${DNS_BIND}\""
      echo "listen-ports:"
      echo "  - 53"
      echo "cache: true"
      echo "cache-size: 4194304"
      echo "timeout: '5s'"
      # No `bootstrap:` on purpose — every upstream is an IP literal, so there is
      # nothing to resolve and nothing to poison.
    } > /etc/dnsproxy/dnsproxy.yaml

    # Do NOT use GL.iNet's /etc/init.d/dnsproxy. Two reasons, both silent:
    #   * its start_service() returns 0 without doing anything unless
    #     `gl-dns-v2.@dns[0].mode` is "secure", so the resolver never comes up and
    #     nothing logs why — this is what made dnsproxy look like it "won't stay up
    #     under procd" for weeks;
    #   * it hardcodes `--upstream-mode=parallel`, which overrides the config file
    #     and queries EVERY upstream on EVERY lookup, handing each operator the
    #     complete query history — the opposite of what load_balance is for.
    # Ship our own instance instead, independent of the vendor's DNS layer.
    say "installing the fleet-dnsproxy service (GL's launcher is gated and hardcodes parallel)"
    cat > /etc/init.d/fleet-dnsproxy <<'INIT'
#!/bin/sh /etc/rc.common
# dnsproxy for fleet, independent of GL.iNet's gl-dns-v2 gating.
# Binds 127.0.0.43:53 — dnsmasq holds 127.0.0.1 and the LAN address, sing-box
# holds 127.0.0.42, so this address is free.
START=89
STOP=11
USE_PROCD=1
PROG=/usr/sbin/dnsproxy
CONFIG_FILE=/etc/dnsproxy/dnsproxy.yaml

start_service() {
    [ -f "$CONFIG_FILE" ] || return 1
    procd_open_instance
    procd_set_param command "$PROG" --config-path="$CONFIG_FILE"
    procd_set_param file "$CONFIG_FILE"
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_set_param respawn
    procd_close_instance
}
INIT
    chmod +x /etc/init.d/fleet-dnsproxy
    /etc/init.d/fleet-dnsproxy enable  >/dev/null 2>&1 || true
    /etc/init.d/fleet-dnsproxy restart >/dev/null 2>&1 || true
    sleep 4

    if nslookup openwrt.org "$DNS_BIND" >/dev/null 2>&1; then
      say "local resolver answering on ${DNS_BIND}:53"
      uci set podkop.settings.dns_type='udp'
      uci set podkop.settings.dns_server="$DNS_BIND"
      uci set podkop.settings.bootstrap_dns_server="$DNS_BIND"
      uci commit podkop
      say "podkop now uses ${DNS_BIND} (plaintext over loopback; encrypted upstream)"
    else
      warn "local resolver did not answer — falling back to direct IP-pinned DoT"
      warn "so DNS keeps working; re-run this script to retry the local resolver"
      uci set podkop.settings.dns_type='dot'
      uci set podkop.settings.dns_server='185.222.222.222'
      uci set podkop.settings.bootstrap_dns_server='45.11.45.11'
      uci commit podkop
    fi
  else
    warn "dnsproxy unavailable; using direct IP-pinned DoT to DNS.SB"
    uci set podkop.settings.dns_type='dot'
    uci set podkop.settings.dns_server='185.222.222.222'
    uci set podkop.settings.bootstrap_dns_server='45.11.45.11'
    uci commit podkop
  fi
fi

say "done — now run 'fleet sync' from the orchestrator to push the exits"
say "podkop will not route anything until it has them"
