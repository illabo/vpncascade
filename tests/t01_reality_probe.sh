#!/usr/bin/env bash
# t01 — active-probe resistance.
#
# REALITY's whole claim is that an unauthenticated prober cannot tell your node from
# the site it impersonates. This test *is* that prober. It compares what your node
# serves against what the genuine masked site serves; any difference is a fingerprint
# a censor can scan for across the whole IPv4 space.
#
#   usage: tests/t01_reality_probe.sh <host> [port] [sni]
#          tests/t01_reality_probe.sh --entry     (read host/sni from inventory)
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh
require openssl curl python3 || exit 1

if [ "${1:-}" = "--entry" ]; then
  HOST="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;i=config.load(None);print(i.entry.host)")"
  PORT="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;i=config.load(None);print(i.entry.port)")"
  SNI="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;i=config.load(None);print(i.entry.reality_sni[0])")"
else
  HOST="${1:?usage: t01_reality_probe.sh <host> [port] [sni]}"; PORT="${2:-443}"; SNI="${3:-}"
  if [ -z "$SNI" ]; then
    SNI="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;i=config.load(None);print(i.entry.reality_sni[0])")"
  fi
fi

head1 "t01 — active-probe resistance  ${DIM}${HOST}:${PORT} masking as ${SNI}${RST}"

tls() {  # tls <connect-host:port> <servername> [extra openssl args]
  local target="$1" sni="$2"; shift 2
  timeout 15 openssl s_client -connect "$target" -servername "$sni" \
    -tls1_3 -brief </dev/null 2>&1
}
cert_fp() {
  timeout 15 openssl s_client -connect "$1" -servername "$2" </dev/null 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}
cert_subject() {
  timeout 15 openssl s_client -connect "$1" -servername "$2" </dev/null 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null
}

head1 "certificate: does the node present the real site's chain?"
REAL_FP="$(cert_fp "${SNI}:443" "$SNI")"
NODE_FP="$(cert_fp "${HOST}:${PORT}" "$SNI")"
info "genuine ${SNI}: ${REAL_FP:-<none>}"
info "your node      : ${NODE_FP:-<none>}"
if [ -z "$NODE_FP" ]; then
  fail "node did not complete a TLS handshake at all — REALITY is broken or the port is wrong"
elif [ "$REAL_FP" = "$NODE_FP" ]; then
  pass "node serves the genuine certificate for ${SNI}"
else
  fail "certificate MISMATCH — a prober can distinguish your node from ${SNI}"
  info "$(cert_subject "${HOST}:${PORT}" "$SNI")"
fi

head1 "protocol shape"
BRIEF="$(tls "${HOST}:${PORT}" "$SNI")"
assert_contains "$BRIEF" "TLSv1.3" "negotiates TLS 1.3"
assert_not_contains "$BRIEF" "alert" "no TLS alert on a plain handshake"

REAL_BRIEF="$(tls "${SNI}:443" "$SNI")"
REAL_CIPHER="$(echo "$REAL_BRIEF" | sed -n 's/.*Ciphersuite: *//p' | head -1)"
NODE_CIPHER="$(echo "$BRIEF"      | sed -n 's/.*Ciphersuite: *//p' | head -1)"
assert_eq "$REAL_CIPHER" "$NODE_CIPHER" "negotiates the same ciphersuite as the real site"

head1 "unauthenticated HTTP: does it look like the real site?"
NODE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  --resolve "${SNI}:${PORT}:${HOST}" "https://${SNI}:${PORT}/" 2>/dev/null)"
REAL_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${SNI}/" 2>/dev/null)"
info "genuine site returns ${REAL_CODE}, your node returns ${NODE_CODE}"
assert_eq "$REAL_CODE" "$NODE_CODE" "root path responds like the real site"

# The XHTTP path is the one URL that behaves differently for an authenticated client.
# To a prober without credentials it must be indistinguishable from any other 404.
XPATH="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;i=config.load(None);print(i.entry.xhttp_path)" 2>/dev/null || echo /api/v1/update)"
NODE_PATH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  --resolve "${SNI}:${PORT}:${HOST}" "https://${SNI}:${PORT}${XPATH}" 2>/dev/null)"
REAL_PATH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  "https://${SNI}${XPATH}" 2>/dev/null)"
info "genuine ${XPATH} → ${REAL_PATH_CODE}, your node → ${NODE_PATH_CODE}"
assert_eq "$REAL_PATH_CODE" "$NODE_PATH_CODE" "the XHTTP path is not distinguishable"

head1 "timing"
t_real=$( { TIMEFORMAT=%R; time (timeout 15 openssl s_client -connect "${SNI}:443" \
           -servername "$SNI" </dev/null >/dev/null 2>&1); } 2>&1 )
t_node=$( { TIMEFORMAT=%R; time (timeout 15 openssl s_client -connect "${HOST}:${PORT}" \
           -servername "$SNI" </dev/null >/dev/null 2>&1); } 2>&1 )
info "handshake: genuine ${t_real}s, node ${t_node}s"
info "a node much SLOWER than the real site is proxying the handshake visibly — consider"
info "a masking dest that is network-close to the node."

head1 "garbage tolerance"
GARBAGE="$(head -c 64 /dev/urandom | timeout 10 openssl s_client -connect "${HOST}:${PORT}" \
  -servername "$SNI" 2>&1 | head -5)"
assert_not_contains "$GARBAGE" "unknown protocol" "random bytes do not produce a distinctive error"

summary
