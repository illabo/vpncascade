# Shared test helpers. Source this, don't run it.
set -uo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
PASS=0; FAIL=0; SKIP=0

pass() { PASS=$((PASS+1)); echo "  ${GRN}✓${RST} $*"; }
fail() { FAIL=$((FAIL+1)); echo "  ${RED}✗${RST} $*"; }
skip() { SKIP=$((SKIP+1)); echo "  ${YLW}−${RST} $* ${DIM}(skipped)${RST}"; }
info() { echo "  ${DIM}$*${RST}"; }
head1() { echo; echo "${BLD}$*${RST}"; }

# assert_eq <expected> <actual> <label>
assert_eq() {
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 ${DIM}(want '$1', got '$2')${RST}"; fi
}
# assert_contains <haystack> <needle> <label>
assert_contains() {
  case "$1" in *"$2"*) pass "$3" ;; *) fail "$3 ${DIM}(missing '$2')${RST}" ;; esac
}
# assert_not_contains <haystack> <needle> <label>
assert_not_contains() {
  case "$1" in *"$2"*) fail "$3 ${DIM}(unexpectedly found '$2')${RST}" ;; *) pass "$3" ;; esac
}
# assert_ne <a> <b> <label>
assert_ne() {
  if [ "$1" != "$2" ]; then pass "$3"; else fail "$3 ${DIM}(both '$1')${RST}"; fi
}

summary() {
  echo
  local total=$((PASS+FAIL+SKIP))
  if [ "$FAIL" -eq 0 ]; then
    echo "${GRN}${BLD}${PASS}/${total} passed${RST}${SKIP:+, ${SKIP} skipped}"
  else
    echo "${RED}${BLD}${FAIL} of ${total} FAILED${RST} (${PASS} passed, ${SKIP} skipped)"
  fi
  [ "$FAIL" -eq 0 ]
}

# curl_code <url> [curl args...] -> HTTP status, retried.
# Third-party echo services rate-limit and time out. A gate test that goes red
# because ifconfig.me had a bad afternoon trains you to ignore it, so retry first
# and let the caller decide what a persistent failure means.
curl_code() {
  local url="$1"; shift
  local code=""
  for _ in 1 2 3; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$@" "$url" 2>/dev/null)"
    [ "$code" != "000" ] && [ -n "$code" ] && { printf '%s' "$code"; return 0; }
    sleep 3
  done
  printf '%s' "${code:-000}"
  return 1
}

# Find or fetch an xray binary for local testing.
ensure_xray() {
  if [ -n "${XRAY:-}" ] && [ -x "${XRAY}" ]; then echo "$XRAY"; return; fi
  if command -v xray >/dev/null 2>&1; then command -v xray; return; fi
  local cache="${TMPDIR:-/tmp}/fleet-xray"
  if [ -x "${cache}/xray" ]; then echo "${cache}/xray"; return; fi
  mkdir -p "$cache"
  local ver arch asset
  ver="$(curl -fsSL https://api.github.com/repos/XTLS/Xray-core/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  arch="$(uname -m)"
  case "$(uname -s)-${arch}" in
    Darwin-arm64) asset="Xray-macos-arm64-v8a.zip" ;;
    Darwin-*)     asset="Xray-macos-64.zip" ;;
    Linux-aarch64) asset="Xray-linux-arm64-v8a.zip" ;;
    Linux-*)      asset="Xray-linux-64.zip" ;;
    *) echo "unsupported platform" >&2; return 1 ;;
  esac
  curl -fsSL -o "${cache}/x.zip" \
    "https://github.com/XTLS/Xray-core/releases/download/${ver}/${asset}" >&2
  ( cd "$cache" && unzip -oq x.zip ) >&2
  echo "${cache}/xray"
}

# Read a value out of the fleet state file.
state_get() { python3 -c "
import json,sys
d=json.load(open('${FLEET_STATE:-state/fleet.json}'))
print(eval(sys.argv[1], {'d': d, 'json': json}))" "$1"; }

require() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { echo "${RED}missing required tool: $c${RST}" >&2; return 1; }
  done
}
