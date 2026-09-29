#!/usr/bin/env bash
# t11 — is your DNS being intercepted?
#
# Since August 2026 TSPU has been recognising DNS in transit and rewriting the
# destination to NSDI, the state resolver. It is a silent redirect, not a block: you
# query Cloudflare, the state answers, and nothing tells you. That makes this the one
# failure in the whole system you cannot notice by using the internet normally — hence
# a test rather than a paragraph.
#
# Run it ON THE BOX THAT RESOLVES — the always-on machine at home — not from a laptop
# on some other network.
#
#   usage: tests/t11_dns_integrity.sh [socks-host:port]
#          (pass a SOCKS proxy to also compare answers through the tunnel)
cd "$(dirname "$0")/.." || exit 1
. tests/lib.sh

PROXY="${1:-}"
DIG="$(command -v dig || command -v kdig || true)"
[ -n "$DIG" ] || { echo "${RED}need dig (bind-tools / dnsutils / knot-dnsutils)${RST}"; exit 1; }

head1 "t11 — DNS integrity  ${DIM}$($DIG -v 2>&1 | head -1)${RST}"

# ── 1. Is the resolver we asked the resolver that answered? ────────────────────
head1 "identity checks (does the real operator answer?)"

CF="$($DIG +short +time=4 +tries=1 @1.1.1.1 id.server CH TXT 2>/dev/null | tr -d '"')"
if [ -n "$CF" ]; then
  pass "1.1.1.1 identifies as Cloudflare (${CF})"
else
  fail "1.1.1.1 did NOT return a CHAOS id.server — blocked, or answered by something"
  info "  real Cloudflare returns a datacentre code here; NSDI does not"
fi

GO="$($DIG +short +time=4 +tries=1 @8.8.8.8 o-o.myaddr.l.google.com TXT 2>/dev/null | tr -d '"')"
if [ -n "$GO" ]; then
  pass "8.8.8.8 echoed a resolver address (${GO}) — real Google answered"
else
  fail "8.8.8.8 did NOT echo — blocked, or the query was redirected"
fi

YA="$($DIG +short +time=4 +tries=1 @77.88.8.8 yandex.ru A 2>/dev/null | head -1)"
[ -n "$YA" ] && pass "domestic resolver 77.88.8.8 answers (yandex.ru -> ${YA})" \
             || fail "domestic resolver 77.88.8.8 did not answer"

# ── 2. Transport reachability ──────────────────────────────────────────────────
head1 "encrypted transports"
tcp_open() { (exec 3<>/dev/tcp/"$1"/"$2") 2>/dev/null; }
for pair in "1.1.1.1 853 Cloudflare-DoT" "8.8.8.8 853 Google-DoT" \
            "1.1.1.1 53 Cloudflare-TCP53" "77.88.8.8 853 Yandex-DoT"; do
  set -- $pair
  if tcp_open "$1" "$2"; then pass "$3 reachable (${1}:${2})"
  else fail "$3 unreachable (${1}:${2})"; fi
done
info "Cloudflare/Google DoT failing while Yandex DoT works is the expected shape"
info "inside Russia since mid-2026 — it is the filtering, not your box."

# ── 3. Do different paths agree? ───────────────────────────────────────────────
head1 "answer divergence"
# A name that is filtered in Russia but not elsewhere shows the split most clearly.
for name in rutracker.org www.wikipedia.org; do
  SYS="$($DIG +short +time=4 +tries=1 "$name" A 2>/dev/null | grep -E '^[0-9.]+$' | sort | tr '\n' ' ')"
  DOM="$($DIG +short +time=4 +tries=1 @77.88.8.8 "$name" A 2>/dev/null | grep -E '^[0-9.]+$' | sort | tr '\n' ' ')"
  info "${name}"
  info "  system resolver : ${SYS:-<none>}"
  info "  77.88.8.8       : ${DOM:-<none>}"
  if [ -n "$PROXY" ]; then
    # Resolving THROUGH the tunnel is the reference answer: it is the only path that
    # never touches Russian DNS infrastructure.
    TUN="$(curl -s --max-time 15 --socks5-hostname "$PROXY" \
           "https://dns.google/resolve?name=${name}&type=A" 2>/dev/null \
           | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(" ".join(sorted(a["data"] for a in d.get("Answer",[]) if a.get("type")==1)))
except Exception: print("")' 2>/dev/null)"
    info "  via tunnel      : ${TUN:-<none>}"
    if [ -n "$TUN" ] && [ -n "$SYS" ]; then
      if [ "$TUN" = "$SYS" ]; then pass "  ${name}: local and tunnel agree"
      else fail "  ${name}: LOCAL ANSWER DIFFERS from the tunnel — local DNS is filtered"; fi
    fi
  fi
done
[ -z "$PROXY" ] && info "pass a SOCKS proxy (e.g. 127.0.0.1:10808) to compare against the tunnel"

# ── 4. What is this box actually configured to use? ────────────────────────────
head1 "configured resolvers on this host"
if [ -r /etc/resolv.conf ]; then
  grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | sed 's/^/  /' || info "  none listed"
else
  info "  no /etc/resolv.conf (macOS: scutil --dns)"
  command -v scutil >/dev/null && scutil --dns 2>/dev/null \
    | grep -E 'nameserver\[[0-9]+\]' | sort -u | head -6 | sed 's/^/  /'
fi
info "If any of these is 8.8.8.8 or 1.1.1.1 and the identity checks above failed,"
info "this host is talking to the state resolver and does not know it."

summary
