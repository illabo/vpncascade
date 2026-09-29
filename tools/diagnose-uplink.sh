#!/bin/sh
# Uplink diagnosis, run ON the router. Works with no internet.
#
#   ssh root@192.168.8.1 'cat > /tmp/diag.sh' < tools/diagnose-uplink.sh
#   ssh root@192.168.8.1 'sh /tmp/diag.sh'
#
# Pipe it; do NOT scp. OpenWrt ships no sftp-server, so scp fails — silently, in
# some versions, leaving you running a stale copy or none at all.
#
# Every reachability test carries a KNOWN-GOOD CONTROL in the same run. This
# project has drawn wrong conclusions three separate times from a test that
# failed for its own reasons — `/dev/tcp` (bash-only), `curl telnet://`
# (unsupported), BusyBox `nc` (reports failure on a port that is serving). If a
# control fails, every result beside it is meaningless; the report says so.
#
# It also reads each exit's OWN SNI out of podkop's config rather than assuming
# one. REALITY answers a wrong SNI with TLS alert 40, which looks exactly like a
# blocked exit — a mistake made twice here already.
echo "==================== uplink diagnosis ===================="
date
echo "router uptime: $(cut -d' ' -f1 /proc/uptime)s"

# ---------------------------------------------------------------- 1. uplink
echo
echo "--- 1. UPLINK ---"
iw dev sta0 link 2>/dev/null | sed -n '1,4p' | sed 's/^/  /'
ip -4 addr show dev sta0 2>/dev/null | awk '/inet /{print "  addr: "$2}'
ip r | grep '^default' | sed 's/^/  route: /'
GW=$(ip r | awk '/^default/{print $3; exit}')
echo "  gateway: ${GW:-NONE}"
case "$(ip -4 addr show dev sta0 2>/dev/null | awk '/inet /{print $2}')" in
  100.*) echo "  !! lease is CGNAT (100.x) — the ISP box is answering DHCP directly." ;;
  192.168.88.*) echo "  ok: lease is from the upstream router (192.168.88.0/24)" ;;
  172.20.10.*) echo "  note: iOS hotspot" ;;
esac

echo
echo "--- 2. LAYER 3 TO THE GATEWAY ---"
if [ -n "$GW" ]; then
  ping -c3 -W2 "$GW" 2>/dev/null | tail -2 | sed 's/^/  /'
else
  echo "  no default gateway — nothing to test"
fi

# ------------------------------------------------------- 3. raw reachability
echo
echo "--- 3. TCP/TLS OFF-NET  (controls first; if these fail, ignore the rest) ---"
probe() {  # host port sni label
  printf '  %-34s ' "$4"
  if [ -n "$3" ]; then
    out=$(timeout 10 openssl s_client -connect "$1:$2" -servername "$3" </dev/null 2>&1)
  else
    out=$(timeout 10 openssl s_client -connect "$1:$2" </dev/null 2>&1)
  fi
  case "$out" in
    *"Verify return code"*) echo "TLS OK" ;;
    *"CONNECTED"*)          echo "TCP ok, TLS FAILED  ($(echo "$out" | grep -oE 'alert [a-z ]+|errno=[0-9]+' | head -1))" ;;
    *)                      echo "NO TCP ($(echo "$out" | grep -oE 'errno=[0-9]+|timed out|refused' | head -1))" ;;
  esac
}
probe 1.1.1.1        443 ""                 "CONTROL cloudflare 1.1.1.1:443"
probe 9.9.9.9        443 ""                 "CONTROL quad9      9.9.9.9:443"
probe 185.222.222.222 853 ""                "CONTROL DNS.SB DoT   :853"

# ----------------------------------------------------------------- 4. exits
echo
echo "--- 4. EXITS, each with ITS OWN SNI (read from podkop) ---"
URIS="$(uci -q get podkop.main.urltest_proxy_links) $(uci -q get podkop.main.proxy_string)"
if [ -z "$(echo "$URIS" | tr -d ' ')" ]; then
  echo "  no exits configured in podkop — that alone explains a dead tunnel"
else
  for u in $URIS; do
    hp=$(echo "$u" | sed -e 's|.*@||' -e 's|?.*||')
    h=${hp%:*}; p=${hp#*:}
    sni=$(echo "$u" | sed -n 's/.*[?&]sni=\([^&#]*\).*/\1/p')
    pbk=$(echo "$u" | grep -c 'pbk=')
    probe "$h" "$p" "$sni" "$h:$p sni=${sni:-none}"
    [ "$pbk" = "0" ] && echo "      !! this URI has NO pbk= — REALITY cannot work"
  done
fi

# ------------------------------------------------------------------- 5. DNS
echo
echo "--- 5. DNS ---"
echo "  podkop: type=$(uci -q get podkop.settings.dns_type) server=$(uci -q get podkop.settings.dns_server)"
echo "  fleet-dnsproxy procs: $(ps w | grep -c '[d]nsproxy')   listening 127.0.0.43:53: $(netstat -lnu 2>/dev/null | grep -c '127.0.0.43:53')"
for s in 127.0.0.43 "$GW" 1.1.1.1; do
  [ -z "$s" ] && continue
  printf '  resolve github.com via %-16s ' "$s"
  nslookup github.com "$s" >/dev/null 2>&1 && echo "OK" || echo "NO ANSWER"
done

# --------------------------------------------------------- 6. stale sockets
echo
echo "--- 6. SOCKETS BOUND TO A DEAD UPLINK ---"
CUR=$(ip -4 addr show dev sta0 2>/dev/null | awk '/inet /{split($2,a,"/"); print a[1]}')
echo "  current source address: ${CUR:-none}"
# Only PRIVATE addresses can be a former uplink of ours. Public ones in the
# "local" column are TPROXY sockets: sing-box binds the original destination
# (IP_TRANSPARENT), so netstat shows the remote site as local. Flagging those
# would report ~20 bogus "stale" addresses on a perfectly healthy router.
netstat -tn 2>/dev/null | awk -v cur="$CUR" '
  NR>2 && $4 ~ /:/ {
    split($4,a,":"); ip=a[1]
    if (ip == cur) next
    if (ip ~ /^127\./ || ip ~ /^192\.168\.8\./ || ip ~ /^::/) next
    if (ip ~ /^10\./ || ip ~ /^192\.168\./ || ip ~ /^172\.(1[6-9]|2[0-9]|3[01])\./ || ip ~ /^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\./) n[ip]++
  }
  END { c=0; for (i in n) { printf "  !! %-18s %d socket(s) bound to a FORMER uplink address\n", i, n[i]; c++ }
        if (c==0) print "  none — no leftovers from a previous uplink" }'

# ------------------------------------------------------------- 7. tunnel svc
echo
echo "--- 7. TUNNEL SERVICES ---"
echo "  sing-box procs: $(ps w | grep -c '[s]ing-box')   podkop nft rules: $(nft list ruleset 2>/dev/null | grep -c podkop)"
printf '  sing-box lost its default interface: '
logread 2>/dev/null | grep -ci 'missing default interface' | tr -d '\n'
echo "  occurrence(s)  <- if >0, sing-box's interface monitor is stale;"
echo "                     'no route to internet' from it while the router itself"
echo "                     reaches the world is this, not censorship."
echo "  recent sing-box errors:"
logread 2>/dev/null | grep -i 'sing-box' | grep -iE 'error|fail|unreachable|refus|no such device' \
  | tail -6 | sed -e 's/\x1b\[[0-9;]*m//g' -e 's/^/    /'
[ -z "$(logread 2>/dev/null | grep -i sing-box | grep -icE 'error|fail')" ] && echo "    (none)"

# ------------------------------------------------ 8. the upstream router
echo
echo "--- 8. UPSTREAM ROUTER (only meaningful on the fixed line) ---"
if [ -n "$GW" ] && ping -c1 -W2 "$GW" >/dev/null 2>&1; then
  echo "  gateway $GW reachable"
  printf '  gateway serves DNS: '
  nslookup github.com "$GW" >/dev/null 2>&1 && echo "yes" || echo "no"
  printf '  a SECOND dhcp server on this LAN? '
  # Two replies to one discover is the signature of the ISP box bridged onto the LAN.
  if command -v udhcpc >/dev/null 2>&1; then
    echo "(run 'udhcpc -i sta0 -n -q -f' by hand and watch for two offers)"
  else echo "(udhcpc absent)"; fi
else
  echo "  gateway not reachable — the problem is at or below layer 3"
fi
echo
echo "==================== end ===================="
