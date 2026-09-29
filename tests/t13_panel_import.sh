#!/usr/bin/env bash
# Config import in the LAN panel: upload -> confirm -> push.
#
# SAFETY, and the reason this file reads the way it does: an earlier version of
# this test pointed the panel at the real `inventory.toml` and let the confirm
# step run for real. It executed `fleet router push --force` against the live
# router, replaced three working exits with two fabricated URIs that had no
# REALITY public key, and took the household off the internet.
#
# So this test builds a THROWAWAY inventory with `[router] enabled = false` and a
# throwaway state directory, and never touches anything real. `fleet router push`
# refuses early on a disabled router, which exercises the whole HTTP path —
# routing, auth, multipart, staging, replay protection — while being physically
# incapable of reconfiguring hardware.
#
# If you change this file, keep that property. A test that can break the network
# it is testing is not a test.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

head1 "t13 — panel config import (offline, no hardware touched)"

command -v python3 >/dev/null 2>&1 || { skip "python3 not available"; summary; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A minimal inventory that is valid but inert: the router is disabled, so the
# push path returns before opening any connection.
cat > "$TMP/inventory.toml" <<'TOML'
mode = "direct"

[defaults]
ssh_key  = "~/.ssh/id_ed25519"
ssh_user = "root"
ssh_port = 2222

[exits]
pool_size = 2
port      = 8443
reality_dest = "www.wikipedia.org:443"
reality_sni  = ["www.wikipedia.org"]
transport    = "tcp"

[[exits.providers]]
name        = "manual"
weight      = 1
regions     = ["x"]
server_type = "x"
image       = "debian-13"

[router]
enabled = false
host    = ""
client  = "beryl"
TOML

info "throwaway inventory at $TMP (router disabled — cannot reach hardware)"

OUT="$(FLEET_TEST_STATE_DIR="$TMP/state" python3 - "$TMP" <<'PY' 2>&1
import base64, http.client, importlib.util, os, re, sys, threading, time

tmp = sys.argv[1]
sys.path.insert(0, ".")
os.makedirs(os.path.join(tmp, "state"), exist_ok=True)

spec = importlib.util.spec_from_file_location("fw", "orchestrator/fleet-web.py")
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)

sd = os.path.join(tmp, "state")
fw.set_password(sd, "testpw")
fw.Handler.sdir = sd
fw.Handler.secret = fw.load_or_make_secret(sd)
fw.Handler.inventory = os.path.join(tmp, "inventory.toml")   # NOT the real one

srv = fw.ThreadingHTTPServer(("127.0.0.1", 0), fw.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

def req(method, path, body=b"", ctype=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    h = {}
    if ctype:
        h["Content-Type"] = ctype
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()

def multipart(field, filename, content: bytes):
    b = "----t" + base64.b16encode(os.urandom(6)).decode()
    out = (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; '
           f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n').encode()
    out += content + f"\r\n--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"

R = []
def check(label, ok):
    R.append(f"{'OK' if ok else 'NO'}|{label}")

st, hd, _ = req("GET", "/import")
check("unauthenticated GET /import redirects to login",
      st == 303 and hd.get("Location") == "/login")

st, hd, _ = req("POST", "/login", b"password=testpw",
                "application/x-www-form-urlencoded")
cookie = hd.get("Set-Cookie", "").split(";")[0]
check("login issues a session", st == 303 and bool(cookie))

st, _, body = req("GET", "/import", cookie=cookie)
check("upload form renders", st == 200 and b"type=file" in body)

# Deliberately NOT a real UUID and not even v4-shaped: the real client UUID is a
# credential, and `tools/prepublish-check.sh` rightly refuses to ship one.
UUID = "deadbeef-0000-0000-0000-000000000000"
uris = [f"vless://{UUID}@203.0.113.10:8443?security=reality&sni=www.example.org"
        "&pbk=ABC&sid=deadbeef&type=tcp&flow=xtls-rprx-vision#one",
        f"vless://{UUID}@203.0.113.11:8443?security=reality&sni=www.example.net"
        "&pbk=DEF&sid=cafebabe&type=tcp#two"]
sub = base64.b64encode("\n".join(uris).encode())

body_, ct = multipart("config", "sub.txt", sub)
st, _, prev = req("POST", "/import", body_, ct, cookie)
check("base64 subscription is parsed", st == 200 and prev.count(b"8443") >= 2)
check("full client UUID is NOT rendered", UUID.encode() not in prev)
check("truncated id is shown", b"deadbeef" in prev)
m = re.search(rb'name=token value="([^"]+)"', prev)
check("confirm token issued", bool(m))

body_, ct = multipart("config", "sub.txt", "\n".join(uris).encode())
st, _, prev2 = req("POST", "/import", body_, ct, cookie)
check("plain (non-base64) list is parsed", st == 200 and prev2.count(b"8443") >= 2)

body_, ct = multipart("config", "notes.txt", b"just a shopping list")
st, _, bad = req("POST", "/import", body_, ct, cookie)
check("a file with no URIs is rejected", st == 200 and b"no vless://" in bad)

st, _, none = req("POST", "/import", *multipart("wrongfield", "x.txt", sub)[::1], cookie)
check("missing file field is rejected", b"no file received" in none)

tok = m.group(1).decode() if m else "x"
st1, _, _ = req("POST", "/import/apply", f"token={tok}".encode(),
                "application/x-www-form-urlencoded", cookie)
check("apply accepts a valid token", st1 == 303)
st2, _, again = req("POST", "/import/apply", f"token={tok}".encode(),
                    "application/x-www-form-urlencoded", cookie)
check("a confirmation cannot be replayed",
      st2 == 400 and b"expired or was already used" in again)

st, hd, _ = req("POST", "/import/apply", b"token=whatever",
                "application/x-www-form-urlencoded")
check("unauthenticated apply redirects to login",
      st == 303 and hd.get("Location") == "/login")

srv.shutdown()
print("\n".join(R))
PY
)"

while IFS='|' read -r verdict label; do
  [ -z "${label:-}" ] && continue
  case "$verdict" in
    OK) pass "$label" ;;
    NO) fail "$label" ;;
    *)  : ;;
  esac
done <<< "$(printf '%s\n' "$OUT" | grep -E '^(OK|NO)\|')"

# If the python block died, the loop above finds nothing — say so loudly rather
# than reporting a silent pass.
if ! printf '%s' "$OUT" | grep -qE '^(OK|NO)\|'; then
  fail "the panel harness produced no results"
  info "$(printf '%s' "$OUT" | tail -15)"
fi

summary
