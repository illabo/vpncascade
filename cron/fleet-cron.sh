#!/usr/bin/env bash
# Scheduled maintenance wrapper. This is what cron actually calls.
#
# Does the boring-but-essential things a bare `fleet cron` does not:
#   * refuses to overlap with itself (a rotation takes minutes; a 15-minute tick
#     must not start a second one on top)
#   * writes a bounded log
#   * shouts when something fails, via whatever $FLEET_NOTIFY_CMD you set
#
# Install:  crontab -e   and see cron/crontab.example
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${FLEET_LOCK:-${ROOT}/state/.cron.lock}"
LOG="${FLEET_LOG:-${ROOT}/state/cron.log}"
MAX_LOG_BYTES="${FLEET_MAX_LOG_BYTES:-2000000}"
ACTION="${1:-cron}"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"

# Rotate before writing, so the log can never grow without bound on an unattended box.
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt "$MAX_LOG_BYTES" ]; then
  mv -f "$LOG" "${LOG}.1"
fi

notify() {
  local msg="$1"
  if [ -n "${FLEET_NOTIFY_CMD:-}" ]; then
    printf '%s\n' "$msg" | sh -c "$FLEET_NOTIFY_CMD" >/dev/null 2>&1 || true
  fi
}

# flock on Linux; a mkdir-based lock everywhere else (macOS has no flock by default).
acquire() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK"
export FLEET_LOCK_HELD=1   # tell bin/fleet the lock is already ours
    flock -n 9 || { echo "$(date -Is) another fleet run is in progress; skipping" >>"$LOG"; exit 0; }
  else
    if ! mkdir "${LOCK}.d" 2>/dev/null; then
      # Break a lock left behind by a crash (older than 2 hours).
      if [ -d "${LOCK}.d" ] && [ -z "$(find "${LOCK}.d" -maxdepth 0 -mmin -120 2>/dev/null)" ]; then
        echo "$(date -Is) breaking a stale lock" >>"$LOG"
        rmdir "${LOCK}.d" 2>/dev/null && mkdir "${LOCK}.d" 2>/dev/null || exit 0
      else
        echo "$(date -Is) another fleet run is in progress; skipping" >>"$LOG"
        exit 0
      fi
    fi
    trap 'rmdir "${LOCK}.d" 2>/dev/null' EXIT
  fi
}
acquire

cd "$ROOT" || exit 1
START="$(date -Is)"
{
  echo "════════════════════════════════════════════════════════════"
  echo "${START}  fleet ${ACTION}"
} >>"$LOG"

OUTPUT="$("${ROOT}/bin/fleet" "$ACTION" 2>&1)"
RC=$?
printf '%s\n' "$OUTPUT" >>"$LOG"
echo "$(date -Is)  exit ${RC}" >>"$LOG"

if [ "$RC" -ne 0 ]; then
  notify "fleet ${ACTION} FAILED (exit ${RC}) at ${START}

$(printf '%s\n' "$OUTPUT" | tail -25)"
fi
exit "$RC"
