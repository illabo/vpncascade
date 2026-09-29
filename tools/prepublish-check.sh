#!/usr/bin/env bash
# Refuse to publish if anything private reached the index.
#
# Checks CONTENT GIT WOULD COMMIT, not the working tree — a file that is
# correctly .gitignored is allowed to contain anything.
#
#   ./tools/prepublish-check.sh          # check staged + tracked content
#
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
say() { printf '  %-22s %s\n' "$1" "$2"; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repo"; exit 1; }
git add -A >/dev/null 2>&1

echo "== files that must never be tracked =="
for f in .env inventory.toml HANDOFF.md; do
  if git ls-files --cached --error-unmatch "$f" >/dev/null 2>&1; then
    say "$f" "✗ TRACKED — remove with: git rm --cached $f"; fail=1
  else
    say "$f" "✓ not tracked"
  fi
done
if git ls-files --cached | grep -q '^state/'; then
  say "state/" "✗ TRACKED — holds every private key"; fail=1
else
  say "state/" "✓ not tracked"
fi

echo
echo "== private patterns in tracked content =="
# name|regex  — extend this list as the deployment grows
while IFS='|' read -r label rx; do
  [ -z "$label" ] && continue
  # ":!" excludes this script, whose pattern list matches itself
  hits=$(git grep -I --cached -lE "$rx" -- . ":!tools/prepublish-check.sh" ":!vendor/" 2>/dev/null || true)
  if [ -n "$hits" ]; then
    say "$label" "✗ found in:"; echo "$hits" | sed 's/^/                           /'; fail=1
  else
    say "$label" "✓ clean"
  fi
done <<'PATTERNS'
private key|-----BEGIN [A-Z ]*PRIVATE KEY-----
sporestack token|ss_t_[a-z0-9]{20,}
upcloud token|ucat_[A-Za-z0-9]{10,}
digitalocean token|dop_v1_[a-f0-9]{16,}
fornex api key|Api-Key [A-Za-z0-9]{8,}\.[A-Za-z0-9]{16,}
hetzner/vultr key|\b[A-Z0-9]{32,}\b.*(TOKEN|KEY|SECRET)
ssh public key|ssh-(rsa|ed25519|dss) AAAA[0-9A-Za-z+/]{20,}
MAC address|([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}
city|Vladivostok|Владивосток
deployment scale|[Oo]wn (two|three|four|[0-9]+) [A-Za-z]|[Tt]hree (Beryls|households|homes|sites|inventories|credentials)|[Tt]wo (Beryls|households|homes)|[Ff]our (Beryls|households|homes)|all (two|three|four) (sites|homes|households|inventories)
family details|[^a-z]([Mm]um|[Dd]ad|[Pp]arents|[Mm]other|[Ff]ather)[^a-zA-Z]|relative's house
wifi ssid|GL-MT3000-[0-9a-f]{3,}
uuid|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}
PATTERNS

echo
echo "== email addresses in tracked content =="
# A plain regex cannot say "not a placeholder", and POSIX ERE has no lookahead,
# so filter by domain here. A check that cries wolf on you@example.com is a check
# people learn to ignore.
{ git grep -I --cached -hoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
    -- . ":!tools/prepublish-check.sh" ":!vendor/" 2>/dev/null || true; } | sort -u |
python3 -c '
import sys
PLACEHOLDER_DOMAINS = ("example.com","example.org","example.net","orchestrator.local",
                       "fornex.com","test","invalid","localhost")
PLACEHOLDER_LOCAL = ("you","user","someone","me","root","admin","test")
bad = []
for line in sys.stdin:
    a = line.strip()
    if "@" not in a: continue
    local, _, dom = a.rpartition("@")
    if dom.lower().endswith(PLACEHOLDER_DOMAINS) or dom.lower() in PLACEHOLDER_DOMAINS: continue
    if local.lower() in PLACEHOLDER_LOCAL: continue
    bad.append(a)
if bad:
    print("  email                ✗ real-looking addresses:")
    for x in bad: print("                           " + x)
    sys.exit(1)
print("  email                ✓ only placeholders")
' || fail=1

echo
echo "== public IP addresses in tracked content =="
# Patterns cannot tell "our exit" from "Cloudflare's resolver", so classify by
# range instead: anything routable that is NOT documentation space and NOT a
# well-known public resolver we cite on purpose gets flagged for a human to look
# at. Catching an exit IP or a home address matters more than a little noise.
{ git grep -I --cached -hoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' \
    -- . ":!tools/prepublish-check.sh" ":!vendor/" 2>/dev/null || true; } | sort -u |
python3 -c '
import sys, ipaddress
# Public infrastructure this project deliberately documents.
ALLOW = {"1.1.1.1","1.0.0.1","8.8.8.8","8.8.4.4","9.9.9.9","149.112.112.112",
         "185.222.222.222","45.11.45.11","162.159.200.1","162.159.200.123",
         "216.239.35.0","216.239.35.4","0.0.0.0","255.255.255.255"}
DOCS = [ipaddress.ip_network(n) for n in
        ("192.0.2.0/24","198.51.100.0/24","203.0.113.0/24","233.252.0.0/24")]
bad = []
for line in sys.stdin:
    t = line.strip()
    try: ip = ipaddress.ip_address(t)
    except ValueError: continue
    if t in ALLOW: continue
    if ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_reserved \
       or ip.is_unspecified or ip.is_link_local: continue
    if any(ip in n for n in DOCS): continue
    bad.append(t)
if bad:
    print("  public IP            ✗ review these:")
    for x in bad: print("                           " + x)
    sys.exit(1)
print("  public IP            ✓ none outside documentation ranges")
' || fail=1

echo
if [ "$fail" -eq 0 ]; then
  echo "  ✓ safe to publish"
else
  echo "  ✗ DO NOT PUBLISH — fix the above first"
fi
exit "$fail"
