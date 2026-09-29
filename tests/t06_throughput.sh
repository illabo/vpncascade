#!/usr/bin/env bash
# t06 — how fast is the cascade, and what does each hop cost?
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require curl jq python3 || exit 1

CLIENT="${1:-$(jq -r '.clients[0].name // empty' state/fleet.json 2>/dev/null)}"
BYTES="${BYTES:-25000000}"
XRAY_BIN="$(ensure_xray)" || exit 1
export XRAY_LOCATION_ASSET="$(dirname "$XRAY_BIN")"
WORK="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

head1 "t06 — throughput  ${DIM}$((BYTES/1000000)) MB per sample${RST}"
./bin/fleet client config "$CLIENT" --socks-port 11086 -o "$WORK/c.json" >/dev/null 2>&1
"$XRAY_BIN" run -config "$WORK/c.json" >"$WORK/c.log" 2>&1 &
for _ in $(seq 1 20); do nc -z 127.0.0.1 11086 2>/dev/null && break; sleep 1; done

URL="https://speed.cloudflare.com/__down?bytes=${BYTES}"
measure() {  # measure <label> [curl args...]
  local label="$1"; shift
  local out
  out="$(curl -s -o /dev/null --max-time 180 -w '%{speed_download} %{time_total}' "$@" "$URL" 2>/dev/null)"
  local bps="${out%% *}" secs="${out##* }"
  if [ -z "$bps" ] || [ "${bps%%.*}" -eq 0 ] 2>/dev/null; then
    fail "${label}: transfer failed"
    echo 0
  else
    printf "  ${GRN}✓${RST} %-28s %8.2f Mbit/s  ${DIM}(%ss)${RST}\n" \
      "$label" "$(python3 -c "print(${bps}*8/1000000)")" "$secs"
    PASS=$((PASS+1))
    echo "$bps"
  fi
}

BASE="$(measure 'direct (no tunnel)' | tail -1)"
TUN="$(measure 'through the cascade' --socks5-hostname 127.0.0.1:11086 | tail -1)"

if [ "${BASE%%.*}" -gt 0 ] 2>/dev/null && [ "${TUN%%.*}" -gt 0 ] 2>/dev/null; then
  PCT="$(python3 -c "print(f'{${TUN}/${BASE}*100:.0f}')")"
  info "the cascade retains ${PCT}% of your line rate"
  [ "$PCT" -ge 25 ] 2>/dev/null && pass "throughput is usable (≥25% of line rate)" \
    || fail "heavy loss — check exit CPU, MTU, and whether an exit is being throttled"
fi

head1 "reference points"
info "two REALITY+XHTTP hops cost real CPU. Expect roughly:"
info "  GL-MT3000 terminating VLESS itself   ~80-150 Mbit/s (userspace TLS on MT7981B)"
info "  GL-MT3000 on AmneziaWG               ~250-350 Mbit/s"
info "  a small x86 box behind the router    line rate"
summary
