#!/usr/bin/env bash
# Run the suite. t00 needs nothing; the rest need a deployed fleet.
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh

ONLY="${1:-}"
ENTRY_HOST="$(python3 -c "import sys;sys.path.insert(0,'.');from fleet import config;print(config.load(None).entry.host)" 2>/dev/null || true)"

run() {
  local script="$1"; shift
  [ -n "$ONLY" ] && [ "${script#tests/}" != "$ONLY" ] && return 0
  echo; echo "${BLD}════ ${script} ════${RST}"
  bash "$script" "$@"
}

run tests/t00_local_cascade.sh
run tests/t13_panel_import.sh      # offline: throwaway inventory, no hardware
run tests/t09_direct_failover.sh
run tests/t11_dns_integrity.sh

if [ -z "$ENTRY_HOST" ] || [ ! -f state/fleet.json ]; then
  echo; echo "${YLW}No deployed fleet — skipping live tests.${RST}"
  echo "Set entry.host in inventory.toml and run: fleet entry deploy && fleet up"
  exit 0
fi

run tests/t01_reality_probe.sh --entry
run tests/t02_chain_egress.sh
run tests/t03_split_routing.sh
run tests/t04_dns_and_leaks.sh
run tests/t05_attack_surface.sh "$ENTRY_HOST"
run tests/t06_throughput.sh
run tests/t07_failover.sh
run tests/t08_exposure_audit.sh "$ENTRY_HOST"
