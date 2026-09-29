#!/usr/bin/env bash
# Score a machine as the always-on orchestrator / tunnel box.
#
# Run it on each candidate (Linux, a Linux live USB, or macOS) and compare the
# numbers. The three things that decide it are not the ones spec sheets lead with:
#
#   1. Hardware AES  — predicts TLS throughput more than clock speed does. Absent, a
#                      1.2 GHz core does software AES and you are capped around a few
#                      tens of Mbit/s no matter what else is true.
#   2. A real wired NIC — a USB 2.0 ethernet dongle caps you near 200-300 Mbit/s and
#                      adds a flaky component to an always-on box.
#   3. Idle power    — this thing runs 8760 hours a year. 20 W vs 3 W is ~150 kWh/yr,
#                      plus fan noise and dust in a machine you want to forget about.
#
# Nothing is installed and nothing is changed; it only reads and benchmarks.
set -u
# printf %f is locale-sensitive; force a dot decimal separator.
export LC_ALL=C

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '  %s\n' "$*"; }
good() { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$*"; }
hdr()  { printf '\n%s%s%s\n' "$BLD" "$*" "$RST"; }

OS="$(uname -s)"; ARCH="$(uname -m)"
printf '\n%s== candidate audit ==%s  %s %s\n' "$BLD" "$RST" "$OS" "$ARCH"

SCORE_CRYPTO=0; SCORE_NIC=0; SCORE_POWER=0

# ───────────────────────────────────────────────────────────────────── CPU
hdr "CPU"
if [ "$OS" = Darwin ]; then
  CPU="$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
  CORES="$(sysctl -n hw.physicalcpu 2>/dev/null)/$(sysctl -n hw.logicalcpu 2>/dev/null)"
  # machdep.cpu.features is x86-only; Apple Silicon reports capabilities under
  # hw.optional.arm.*, so checking only the former reports "no AES" on a chip that
  # does 10 GB/s of it.
  FEAT="$(sysctl -n machdep.cpu.features machdep.cpu.leaf7_features 2>/dev/null | tr 'A-Z' 'a-z')"
  if [ "$(sysctl -n hw.optional.arm.FEAT_AES 2>/dev/null)" = 1 ] \
     || [ "$(sysctl -n hw.optional.armv8_crypto 2>/dev/null)" = 1 ]; then
    FEAT="$FEAT aes"
  fi
  if [ "$(sysctl -n hw.optional.arm.FEAT_SHA256 2>/dev/null)" = 1 ]; then
    FEAT="$FEAT sha2"
  fi
else
  CPU="$(sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo | head -1)"
  [ -z "$CPU" ] && CPU="$(sed -n 's/^Model[[:space:]]*: //p' /proc/cpuinfo | head -1)"
  [ -z "$CPU" ] && CPU="$(sed -n 's/^Hardware[[:space:]]*: //p' /proc/cpuinfo | head -1)"
  CORES="$(grep -c '^processor' /proc/cpuinfo)"
  FEAT="$(sed -n 's/^\(flags\|Features\)[[:space:]]*: //p' /proc/cpuinfo | head -1)"
fi
say "model  : ${CPU:-unknown}"
say "cores  : ${CORES:-?}    arch: ${ARCH}"

hdr "hardware crypto  ${DIM}(the one that matters)${RST}"
case " $FEAT " in
  *" aes "*|*" aes,"*)
     good "AES acceleration present (AES-NI / ARMv8 crypto)"; SCORE_CRYPTO=2 ;;
  *)
     case "$ARCH" in
       aarch64|arm64|armv7l|armv6l)
         bad "no ARMv8 crypto extensions"
         say "   ${DIM}Every Broadcom Pi SoC except the Pi 5's BCM2712 omits these —"
         say "   Broadcom did not licence the optional extension. AES is software-only.${RST}" ;;
       *)
         bad "no AES-NI"
         say "   ${DIM}Typical of Core 2 Duo, and of i3 / Pentium / Celeron parts from"
         say "   2010-2013. Intel reserved AES-NI for i5/i7 in that era.${RST}" ;;
     esac ;;
esac
case " $FEAT " in *" sha2 "*|*" sha_ni "*|*" sha2,"*) good "SHA acceleration present" ;; esac

# ───────────────────────────────────────────────────────────────────── memory
hdr "memory"
if [ "$OS" = Darwin ]; then
  MEM=$(( $(sysctl -n hw.memsize 2>/dev/null) / 1024 / 1024 ))
else
  MEM=$(( $(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo) / 1024 ))
fi
say "${MEM} MB"
if   [ "${MEM:-0}" -ge 2000 ]; then good "ample for fleet + DNS + a tunnel"
elif [ "${MEM:-0}" -ge 900 ];  then good "enough for fleet + DNS (tight if it also proxies)"
else warn "under 1 GB — fine for DNS+cron only"; fi

# ───────────────────────────────────────────────────────────────────── network
hdr "wired networking"
FOUND_ETH=0
if [ "$OS" = Darwin ]; then
  ETHLIST="$(networksetup -listallhardwareports 2>/dev/null \
    | awk '/Hardware Port: .*(Ethernet|LAN)/{p=$3" "$4} /Device:/{if(p){print p" -> "$2; p=""}}')"
  if [ -n "$ETHLIST" ]; then
    printf '%s\n' "$ETHLIST" | sed 's/^/  /'
    FOUND_ETH=1; SCORE_NIC=1
    say "${DIM}macOS does not report link speed here; check the cable and the adapter.${RST}"
  fi
else
  for n in /sys/class/net/*; do
    d="$(basename "$n")"
    case "$d" in lo|wl*|docker*|veth*|br-*) continue ;; esac
    [ -e "$n/device" ] || continue
    SPEED="$(cat "$n/speed" 2>/dev/null)"
    CARRIER="$(cat "$n/carrier" 2>/dev/null)"
    BUS="$(readlink -f "$n/device" 2>/dev/null)"
    case "$BUS" in *usb*) KIND="USB" ;; *) KIND="onboard" ;; esac

    # For a USB NIC the host bus speed matters far more than the adapter's badge:
    # the same gigabit dongle does ~940 Mbit/s on USB 3.x and ~300 on USB 2.0.
    USBSPEED=""
    if [ "$KIND" = USB ]; then
      up="$BUS"
      while [ "$up" != "/" ] && [ -n "$up" ]; do
        if [ -f "$up/speed" ] && [ -f "$up/bDeviceClass" ]; then
          USBSPEED="$(cat "$up/speed" 2>/dev/null)"; break
        fi
        up="$(dirname "$up")"
      done
    fi

    say "${d}: ${KIND}${USBSPEED:+ @ ${USBSPEED} Mbit/s bus}, link ${SPEED:-?} Mbit/s, carrier=${CARRIER:-?}"
    FOUND_ETH=1
    if [ "$KIND" = USB ]; then
      if [ -n "$USBSPEED" ] && [ "$USBSPEED" -ge 5000 ] 2>/dev/null; then
        good "  USB 3.x bus — a gigabit dongle runs at line rate here, not a bottleneck"
        SCORE_NIC=2
      elif [ -n "$USBSPEED" ] && [ "$USBSPEED" -le 480 ] 2>/dev/null; then
        warn "  USB 2.0 bus — caps near 200-300 Mbit/s whatever the adapter claims"
        SCORE_NIC=1
      else
        warn "  USB NIC, bus speed unknown"
        SCORE_NIC=1
      fi
      say "  ${DIM}Either way a dongle (or a hub) is one more thing to fail in a box"
      say "  meant to run unattended for years. Buy a decent one.${RST}"
    else
      case "${SPEED:-0}" in
        1000|2500|10000) good "  gigabit or better, onboard"; SCORE_NIC=2 ;;
        100) warn "  100 Mbit — fine for DNS + orchestration, a ceiling if it proxies"
             SCORE_NIC=1 ;;
      esac
    fi
  done
fi
[ "$FOUND_ETH" = 0 ] && bad "no wired ethernet found — you will need a dongle"

# ───────────────────────────────────────────────────────────────────── storage
hdr "storage"
if [ "$OS" = Darwin ]; then
  diskutil info / 2>/dev/null | grep -E 'Solid State|Device / Media Name|Disk Size' | sed 's/^ */  /'
else
  for d in /sys/block/*; do
    b="$(basename "$d")"
    # Skip virtual/absent devices: they are noise, and a VM host shows dozens.
    case "$b" in loop*|ram*|dm-*|nbd*|zram*|sr*|md*) continue ;; esac
    ROT="$(cat "$d/queue/rotational" 2>/dev/null)"
    SZ=$(( $(cat "$d/size" 2>/dev/null || echo 0) / 2 / 1024 / 1024 ))
    [ "${SZ:-0}" -lt 1 ] && continue
    case "$b:$ROT" in
      mmcblk*:*) warn "${b}: ${SZ} GB SD/eMMC — SD cards wear out; put logs in RAM" ;;
      *:1) warn "${b}: ${SZ} GB spinning disk — a 2010-era HDD is the likeliest thing to fail" ;;
      *:0) good "${b}: ${SZ} GB SSD/flash" ;;
    esac
  done
fi

# ───────────────────────────────────────────────────────────────────── benchmark
hdr "crypto throughput  ${DIM}(predicts what this box can tunnel)${RST}"
if command -v openssl >/dev/null 2>&1; then
  say "$(openssl version 2>/dev/null)"
  # The human-readable line goes to stderr so that command substitution captures
  # only the number. Sending both to stdout puts the whole formatted line into the
  # variable, which then breaks every arithmetic test downstream.
  bench() { # bench <cipher> <label> -> stdout: integer MB/s, stderr: a readable line
    local out mbs
    out="$(openssl speed -elapsed -evp "$1" 2>/dev/null | tail -1)"
    mbs="$(printf '%s' "$out" | awk '{v=$NF; gsub(/k$/,"",v); printf "%d", v/1024}')"
    if [ -z "$mbs" ] || ! [ "$mbs" -gt 0 ] 2>/dev/null; then
      mbs=0
      printf '  %s: could not measure\n' "$2" >&2
    else
      awk -v l="$2" -v m="$mbs" \
        'BEGIN{printf "  %-22s %6d MB/s  ~ %6d Mbit/s\n", l, m, m*8}' >&2
    fi
    printf '%s' "$mbs"
  }
  AESMBS="$(bench aes-128-gcm "AES-128-GCM")"
  CHAMBS="$(bench chacha20-poly1305 "ChaCha20-Poly1305")"
  : "${AESMBS:=0}" "${CHAMBS:=0}"
  say ""
  BEST="$AESMBS"; BESTNAME="AES-GCM"
  [ "${CHAMBS:-0}" -gt "${AESMBS:-0}" ] 2>/dev/null && { BEST="$CHAMBS"; BESTNAME="ChaCha20"; }
  say "${DIM}Xray is written in Go, and Go picks the cipher for you: when the CPU has"
  say "no AES hardware it prefers ChaCha20-Poly1305 over AES-GCM automatically. So the"
  say "figure that predicts tunnel throughput is the FASTER of the two above"
  say "(${BESTNAME}, ~$((BEST * 8)) Mbit/s single-core), not the AES one."
  say "REALITY+Vision roughly halves it (encrypt in, splice out); XHTTP costs more.${RST}"
else
  warn "no openssl — install it to get the number that actually matters"
  AESMBS=0; CHAMBS=0
fi

# ───────────────────────────────────────────────────────────────────── verdict
hdr "verdict"
ROLE_A="orchestrator + split DNS"
AESMBS="${AESMBS:-0}"; CHAMBS="${CHAMBS:-0}"

# Xray will use whichever cipher is faster here (Go reorders its preference when AES
# hardware is missing), so the ceiling is the better of the two — using the AES number
# alone under-rates every CPU without AES-NI.
BESTMBS="$AESMBS"
[ "${CHAMBS:-0}" -gt "${AESMBS:-0}" ] 2>/dev/null && BESTMBS="$CHAMBS"

# The benchmark is ground truth; the CPU flag is only an explanation for it. Trusting
# the flag alone reports "no hardware AES" on machines that plainly have it.
if [ "$SCORE_CRYPTO" -lt 2 ] && [ "$AESMBS" -gt 500 ] 2>/dev/null; then
  warn "no AES flag detected, but the benchmark says ${AESMBS} MB/s — believe the"
  warn "benchmark; this CPU has acceleration the flag check missed"
  SCORE_CRYPTO=2
fi

# ~500 Mbit/s of single-core AES-GCM is about the floor for a box that should also
# carry a household's tunnelled traffic without becoming the bottleneck.
if [ "$BESTMBS" -ge 62 ] 2>/dev/null && [ "$SCORE_NIC" -ge 2 ]; then
  good "good for: ${ROLE_A}, AND terminating the tunnel for the LAN"
elif [ "$BESTMBS" -ge 62 ] 2>/dev/null; then
  good "good for: ${ROLE_A}; could tunnel, but the NIC is the limit"
else
  good "good for: ${ROLE_A}  (cron, DNS, subscription — all trivial workloads)"
  warn "not for terminating the tunnel: ~$((BESTMBS * 8)) Mbit/s is the crypto ceiling"
fi
if [ "${CHAMBS:-0}" -gt "${AESMBS:-0}" ] 2>/dev/null; then
  say "${DIM}ChaCha20 beats AES here, i.e. no usable AES hardware — which is fine:"
  say "Xray will select ChaCha20 on its own. Judge this box on that number.${RST}"
fi
say ""
say "${DIM}Whatever you pick, measure idle draw with a plug meter before committing."
say "8760 hours a year makes 20 W vs 3 W a real difference in heat, noise and dust —"
say "and the quiet box is the one you actually leave running.${RST}"
echo
