#!/usr/bin/env python3
"""A small LAN control panel for fleet.

Why this exists: at a site you do not live at, the orchestrator sits behind CGNAT
and SORM, so you cannot SSH in to change two lines. Someone in the room can open a
page on the local network instead and paste in a credential you sent them.

Deliberately narrow, because a web UI that holds provider tokens and can spend money
is worth keeping boring:

  * stdlib only — no framework, nothing to keep patched
  * one password, hashed on disk, constant-time compared, rate-limited
  * sessions are HMAC-signed cookies with an expiry, not server state
  * actions are a fixed allowlist invoked with argument lists — never a shell
  * secrets are written, never rendered back

It is not exposed to the internet and must not be. Bind it to the LAN.

    ./orchestrator/fleet-web.py --port 8088
"""
from __future__ import annotations

import argparse
import email.parser
import email.policy
import hashlib
import hmac
import html
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SESSION_HOURS = 12
LOGIN_WINDOW = 300          # seconds
LOGIN_MAX_TRIES = 8

#: The only things the UI may run. Anything not here cannot be invoked, whatever the
#: request says.
ACTIONS: dict[str, list[str]] = {
    "status":      ["status"],
    "health":      ["health"],
    "up":          ["up"],
    "rotate":      ["rotate"],
    "reap":        ["reap"],
    "sync":        ["sync"],
    "router-push": ["router", "push"],
    "dns":         ["dns"],
}

#: Credentials the settings page may set, and the env var each maps to.
TOKENS = {
    "SPORESTACK_TOKEN":   "SporeStack (this is also the wallet)",
    "HCLOUD_TOKEN":       "Hetzner Cloud",
    "VULTR_API_KEY":      "Vultr",
    "DIGITALOCEAN_TOKEN": "DigitalOcean",
}

_lock = threading.Lock()
_last_run: dict[str, str] = {"action": "", "when": "", "output": ""}
_login_attempts: list[float] = []


# ------------------------------------------------------------------ small utils

try:
    from . import router_stats as rstats          # noqa: F401  (package import)
except ImportError:                                # run as a plain script
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "router_stats", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "router_stats.py"))
    rstats = _ilu.module_from_spec(_spec); _spec.loader.exec_module(rstats)


#: How often to sample the router. The Clash API call is cheap; the SSH probe is
#: the expensive half, which is why this is minutes rather than seconds.
#: An upload is parsed, shown for confirmation, and only then pushed. The parsed
#: URIs wait here between those two steps rather than being round-tripped through a
#: hidden form field, because they contain the client UUID and this page is plain
#: HTTP on the LAN. Keyed by an unguessable token, and short-lived.
IMPORT_TTL = 600
MAX_UPLOAD = 256 * 1024
_pending_imports: dict[str, dict] = {}

#: Last known outbound->exit-name map, learned by the 3-minute SSH probe. The live
#: endpoint reuses it rather than opening an SSH connection of its own: the mapping
#: only changes when exits do, and an SSH handshake every few seconds to a small
#: router is exactly the kind of self-inflicted load this project has measured before.
_name_map: dict[str, str] = {}
#: Previous (t, up_total, down_total) from the live endpoint, so it can report a
#: real rate. Clash only exposes cumulative counters; a rate needs two samples.
_live_prev: dict[str, float] = {}

#: Cloudflare rather than the gstatic default podkop bakes in: measured from this
#: router at 0.91s against gstatic's 1.79s, and it is not a Google endpoint, which
#: matters on a line where Google is partially degraded.
PROBE_URL = "http://cp.cloudflare.com/generate_204"

#: Live-refresh script for the stats page. Kept OUT of the page f-string on
#: purpose: JavaScript is full of `{` and `}`, which an f-string reads as
#: placeholders, and escaping every one of them is a mistake waiting to happen.
LIVE_JS = r"""<script>
// Live refresh for the numbers that come from sing-box's local HTTP API. The
// slow, SSH-backed parts of this page (podkop, DNS, the sparklines) stay as
// rendered.
//
// POLLS ONLY WHEN SOMEONE IS LOOKING. Every tick is a request to the router, so
// a tab left open overnight would be thousands of pointless queries against a
// small box. Guarded by document.visibilityState and a 10-minute idle timer.
//
// NOTE FOR EDITORS: single-quoted JS strings throughout, and no backslash
// escapes. This block is embedded in a Python string, and an earlier version
// used \" for the HTML attribute quotes -- Python collapsed them to bare quotes
// and shipped a page whose script died on parse, silently. Keep it escape-free.
(function () {
  var EVERY = 5000, IDLE_MS = 10 * 60 * 1000;
  var last = Date.now();
  var el = function (i) { return document.getElementById(i); };

  function bps(v) {
    if (v === null || v === undefined) return "—";
    var u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"], i = 0;
    while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i ? v.toFixed(1) : Math.round(v)) + " " + u[i];
  }
  function watching() {
    return document.visibilityState === "visible" && (Date.now() - last) < IDLE_MS;
  }
  function draw(d) {
    var t = el("extable");
    if (t && d.exits && d.exits.length) {
      var h = "<tr><th>exit</th><th>latency</th><th></th></tr>";
      d.exits.forEach(function (e) {
        var lat, cls = "";
        if (e.delay_ms === null || e.delay_ms === undefined) {
          lat = e.serving ? "carrying traffic" : "no probe yet";
        } else if (e.delay_ms === 0) { lat = "unreachable"; cls = " class=bad"; }
        else { lat = e.delay_ms + " ms"; }
        h += '<tr><td title="' + e.tag + '">' + e.label + '</td><td' + cls
           + '>' + lat + '</td><td class=sub>' + (e.serving ? '← serving' : '')
           + '</td></tr>';
      });
      t.innerHTML = h;
    }
    if (el("lv-conns")) el("lv-conns").textContent = (d.direct_conns + d.tunnel_conns);
    if (el("lv-down")) el("lv-down").textContent = bps(d.down_bps);
    if (el("lv-up")) el("lv-up").textContent = bps(d.up_bps);
    if (el("lv-age")) el("lv-age").textContent = " · live";
    if (d.sample_t) { ageBase = Math.max(0, d.t - d.sample_t); ageAt = Date.now(); }
    if (d.poll_seconds) pollSecs = d.poll_seconds;
    ageTick();
  }
  // Age of the last stored sample. Re-baselined from the server on every poll,
  // then counted up locally each second so the figure is never frozen at whatever
  // it happened to be when the page rendered.
  var ageBase = null, ageAt = 0;
  function ageTick() {
    var n = el("lv-sampled");
    if (!n || ageBase === null) return;
    var a = Math.max(0, Math.round(ageBase + (Date.now() - ageAt) / 1000));
    var nxt = pollSecs ? Math.max(0, pollSecs - a) : null;
    n.textContent = "sampled " + a + "s ago"
                  + (nxt === null ? "" : " · next in " + nxt + "s");
  }
  var pollSecs = 0;

  function tick() {
    if (!watching()) { if (el("lv-age")) el("lv-age").textContent = " · paused"; return; }
    fetch("/stats.json", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) draw(d); })
      .catch(function () { /* keep the last good values on screen */ });
  }
  ["mousemove", "keydown", "touchstart", "scroll", "click"].forEach(function (ev) {
    window.addEventListener(ev, function () { last = Date.now(); }, { passive: true });
  });
  document.addEventListener("visibilitychange", function () {
    last = Date.now();
    if (document.visibilityState === "visible") tick();
  });
  setInterval(tick, EVERY);
  setInterval(function () { if (watching()) ageTick(); }, 1000);
  tick();
})();
</script>"""


POLL_SECONDS = int(os.environ.get("FLEET_STATS_INTERVAL", "180"))
CLASH_BASE = os.environ.get("FLEET_CLASH_URL", "")
ROUTER_SSH = os.environ.get("FLEET_ROUTER_SSH", "")


def _remember_names(sample: dict) -> None:
    got = ((sample or {}).get("router") or {}).get("outbound_names") or {}
    if got:
        with _lock:
            _name_map.clear()
            _name_map.update(got)


def fill_probe_gaps(exits: list) -> None:
    """Ask sing-box to measure any outbound it has never measured.

    urltest stops probing once it finds a member that is good enough, so a healthy
    exit can sit permanently unmeasured — and urltest keeps selecting a slower one
    because it never looked at the alternative. Observed here: 445 ms unprobed
    against 614 ms selected, a 27% penalty paid purely because nothing had asked.

    **Called from the scheduled poller only, never from a page view.** Two reasons.
    A panel is a viewer: opening it must not generate traffic at the router, or
    leaving a tab open turns into an unbounded probe source. And every probe is a
    small, fixed-size, regularly-timed flow to an exit — exactly the beacon shape
    traffic analysis looks for — so the rate must be bounded by a schedule we set,
    not by how often somebody refreshes a browser.

    Only fills genuine gaps. Outbounds that already have a measurement are left
    alone, so this can never oscillate a choice between two exits that urltest has
    already compared.
    """
    import threading

    todo = [e.get("name") for e in exits
            if e.get("delay_ms") is None and e.get("name")]
    if not todo or not CLASH_BASE:
        return

    def _probe(tag: str) -> None:
        try:
            rstats._get_json(
                f"{CLASH_BASE}/proxies/{quote(tag)}/delay"
                f"?timeout=8000&url={quote(PROBE_URL, safe='')}"
            )
        except Exception:                            # noqa: BLE001 - best effort
            pass

    for tag in todo:
        threading.Thread(target=_probe, args=(tag,), daemon=True).start()


def _poller_loop():
    """Sample the router forever. Never let a failure kill the thread — a panel
    that stops updating silently is worse than one showing stale numbers."""
    while True:
        try:
            if CLASH_BASE:
                sample = rstats.collect(CLASH_BASE, ROUTER_SSH)
                _remember_names(sample)
                rstats.append(sample)
                # Once per cycle, and only for outbounds urltest never measured.
                fill_probe_gaps(sample.get("exits", []))
        except Exception:                          # noqa: BLE001 - poller must not die
            pass
        time.sleep(max(60, POLL_SECONDS))


def state_dir(inventory: str) -> str:
    from fleet import config as cfgmod
    return cfgmod.load(inventory).state_dir


def secret_path(sd: str) -> str:
    return os.path.join(sd, "web-secret")


def load_or_make_secret(sd: str) -> bytes:
    p = secret_path(sd)
    if os.path.exists(p):
        return open(p, "rb").read()
    os.makedirs(sd, exist_ok=True)
    s = secrets.token_bytes(32)
    with open(p, "wb") as fh:
        fh.write(s)
    os.chmod(p, 0o600)
    return s


def pw_path(sd: str) -> str:
    return os.path.join(sd, "web-password")


#: `hashlib.scrypt` needs a Python linked against OpenSSL. macOS system Python is
#: built against LibreSSL 2.8.3 and simply does not have it, so calling it raised
#: AttributeError and took the whole panel down — on that interpreter the password
#: could be neither set nor checked. PBKDF2 is always present, so it is the
#: fallback. 600k iterations of SHA-256 is the current OWASP guidance.
HAVE_SCRYPT = hasattr(hashlib, "scrypt")
PBKDF2_ROUNDS = 600_000


def _derive(password: str, salt: bytes, algo: str) -> str:
    if algo == "scrypt":
        if not HAVE_SCRYPT:
            raise RuntimeError(
                "this password was hashed with scrypt, but this Python has no "
                "hashlib.scrypt (it is not linked against OpenSSL). Run the panel "
                "on the same interpreter that set the password, or reset it with "
                "--set-password."
            )
        return hashlib.scrypt(password.encode(), salt=salt,
                              n=2**14, r=8, p=1, dklen=32).hex()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                               PBKDF2_ROUNDS, dklen=32).hex()


def set_password(sd: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    algo = "scrypt" if HAVE_SCRYPT else "pbkdf2"
    dk = _derive(password, salt, algo)
    os.makedirs(sd, exist_ok=True)
    with open(pw_path(sd), "w") as fh:
        # Tagged, so a file written by one build is still readable by another.
        fh.write(f"{algo}${salt.hex()}${dk}")
    os.chmod(pw_path(sd), 0o600)


def check_password(sd: str, password: str) -> bool:
    try:
        raw = open(pw_path(sd)).read().strip()
    except Exception:                                # noqa: BLE001
        return False
    try:
        if "$" in raw:
            algo, salt_hex, dk_hex = raw.split("$", 2)
        else:
            # Pre-tag format was always scrypt. Keep reading it, so upgrading the
            # panel never locks anybody out of a password they already set.
            algo, (salt_hex, dk_hex) = "scrypt", raw.split(":")
        return hmac.compare_digest(_derive(password, bytes.fromhex(salt_hex), algo),
                                   dk_hex)
    except Exception:                                # noqa: BLE001
        return False


def sign(secret: bytes, value: str) -> str:
    mac = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def verify(secret: bytes, token: str) -> bool:
    try:
        value, mac = token.rsplit(".", 1)
        expires = int(value)
    except Exception:
        return False
    if not hmac.compare_digest(sign(secret, value), token):
        return False
    return expires > int(time.time())


def run_fleet(inventory: str, args: list[str], timeout: int = 900) -> str:
    cmd = [os.path.join(ROOT, "bin", "fleet"), "-i", inventory, *args]
    env = dict(os.environ, FLEET_NO_COLOR="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=ROOT, env=env)
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"


def strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    """Pull named fields out of a multipart/form-data body.

    `cgi.FieldStorage` was removed in Python 3.13 and this panel is stdlib-only, so
    this uses `email`, the documented replacement, rather than hand-rolling boundary
    splitting — which is exactly the sort of parser that goes wrong quietly.
    """
    if not content_type.lower().startswith("multipart/"):
        return {}
    raw = (b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n"
           + body)
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    out: dict[str, bytes] = {}
    if not msg.is_multipart():
        return out
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[str(name)] = part.get_payload(decode=True) or b""
    return out


def stage_import(uris: list[str], filename: str) -> str:
    now = time.time()
    with _lock:
        for k, v in list(_pending_imports.items()):
            if v["exp"] < now:
                del _pending_imports[k]
        tok = secrets.token_urlsafe(24)
        _pending_imports[tok] = {"uris": uris, "name": filename,
                                 "exp": now + IMPORT_TTL}
    return tok


def take_import(token: str) -> dict | None:
    """One-shot: a confirmation cannot be replayed."""
    with _lock:
        item = _pending_imports.pop(token, None)
    if not item or item["exp"] < time.time():
        return None
    return item


# ------------------------------------------------------------------------ views

CSS = """
:root{--fg:#1a1a1a;--dim:#666;--line:#ddd;--ok:#0a7d32;--bad:#b3261e;--bg:#fff}
@media(prefers-color-scheme:dark){:root{--fg:#e8e8e8;--dim:#999;--line:#333;
--ok:#4ade80;--bad:#f87171;--bg:#141414}}
*{box-sizing:border-box}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
color:var(--fg);background:var(--bg);margin:0;padding:16px;max-width:900px}
h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
pre{background:rgba(127,127,127,.09);padding:12px;border-radius:6px;overflow:auto;
font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
word-break:break-word}
form{display:inline}
button{font:inherit;padding:7px 13px;margin:0 6px 6px 0;border:1px solid var(--line);
background:transparent;color:var(--fg);border-radius:6px;cursor:pointer}
button:hover{background:rgba(127,127,127,.12)}
input[type=password],input[type=text]{font:inherit;padding:8px;width:100%;
max-width:460px;border:1px solid var(--line);border-radius:6px;background:transparent;
color:var(--fg)}
label{display:block;margin:12px 0 4px;font-size:13px;color:var(--dim)}
a{color:inherit}.set{color:var(--ok)}.unset{color:var(--dim)}
nav{margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--line)}
nav a{margin-right:14px;text-decoration:none;font-size:14px}\ntable{border-collapse:collapse;margin:4px 0}\ntd,th{padding:3px 14px 3px 0;text-align:left;font-weight:400;vertical-align:middle}\nth{color:var(--dim);font-size:12px}\n.bad{color:var(--bad)}
"""


def page(title: str, body: str, nav: bool = True) -> bytes:
    n = ('<nav><a href="/">status</a><a href="/stats">stats</a>'
         '<a href="/import">import</a><a href="/settings">credentials</a>'
         '<a href="/logout">log out</a></nav>') if nav else ""
    return (f"<!doctype html><meta charset=utf-8>"
            f'<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style>"
            f"{n}{body}").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-web"
    inventory = "inventory.toml"
    sdir = ""
    secret = b""

    def log_message(self, fmt, *a):   # quieter than the default
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % a))

    # ------------------------------------------------------------- helpers
    def _send(self, body: bytes, code: int = 200, cookie: str | None = None,
              ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str, cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["fleet_session"].value if "fleet_session" in c else ""
        return bool(tok) and verify(self.secret, tok)

    def _raw_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            # Read and discard so the connection stays in a sane state.
            self.rfile.read(min(n, MAX_UPLOAD))
            return b""
        return self.rfile.read(n) if n else b""

    def _body(self) -> dict:
        return parse_qs(self._raw_body().decode("utf-8", "replace"))

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self._send(self._login_page())
        if path == "/logout":
            return self._redirect("/login", "fleet_session=; Max-Age=0; Path=/")
        if not self._authed():
            return self._redirect("/login")
        if path == "/":
            return self._send(self._status_page())
        if path == "/stats":
            return self._send(self._stats_page())
        if path == "/settings":
            return self._send(self._settings_page())
        if path == "/stats.json":
            return self._send(self._stats_json(), ctype="application/json")
        if path == "/import":
            return self._send(self._import_page())
        self._send(page("not found", "<h1>not found</h1>"), 404)

    # --------------------------------------------------------------- POST
    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._raw_body()
        ctype = self.headers.get("Content-Type", "")
        form = ({} if ctype.lower().startswith("multipart/")
                else parse_qs(raw.decode("utf-8", "replace")))

        if path == "/login":
            now = time.time()
            with _lock:
                _login_attempts[:] = [t for t in _login_attempts if now - t < LOGIN_WINDOW]
                if len(_login_attempts) >= LOGIN_MAX_TRIES:
                    return self._send(self._login_page(
                        "too many attempts — wait a few minutes"), 429)
                _login_attempts.append(now)
            if check_password(self.sdir, (form.get("password") or [""])[0]):
                with _lock:
                    _login_attempts.clear()
                exp = int(time.time()) + SESSION_HOURS * 3600
                tok = sign(self.secret, str(exp))
                return self._redirect("/", f"fleet_session={tok}; Path=/; HttpOnly; "
                                           f"SameSite=Strict; Max-Age={SESSION_HOURS*3600}")
            return self._send(self._login_page("wrong password"), 401)

        if not self._authed():
            return self._redirect("/login")

        if path == "/action":
            name = (form.get("name") or [""])[0]
            if name not in ACTIONS:
                return self._send(page("no", "<h1>unknown action</h1>"), 400)
            out = strip_ansi(run_fleet(self.inventory, ACTIONS[name]))
            with _lock:
                _last_run.update(action=name,
                                 when=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 output=out[-20000:])
            return self._redirect("/")

        if path == "/settings":
            written = []
            env_path = os.path.join(ROOT, ".env")
            lines: dict[str, str] = {}
            if os.path.exists(env_path):
                for ln in open(env_path):
                    if "=" in ln and not ln.strip().startswith("#"):
                        k, _, v = ln.partition("=")
                        lines[k.strip()] = v.rstrip("\n")
            for key in TOKENS:
                val = (form.get(key) or [""])[0].strip()
                if val:                      # blank means "leave as is"
                    lines[key] = val
                    written.append(key)
            with open(env_path, "w") as fh:
                fh.write("# managed by fleet-web; blank fields above were left alone\n")
                for k, v in sorted(lines.items()):
                    fh.write(f"{k}={v}\n")
            os.chmod(env_path, 0o600)
            with _lock:
                _last_run.update(action="save credentials",
                                 when=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 output="updated: " + (", ".join(written) or "nothing"))
            return self._redirect("/settings")

        if path == "/import":
            return self._send(self._import_preview(raw))

        if path == "/import/apply":
            item = take_import((form.get("token") or [""])[0])
            if not item:
                return self._send(self._import_page(
                    "that confirmation expired or was already used — upload again"), 400)
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
                fh.write("\n".join(item["uris"]) + "\n")
                tmp = fh.name
            try:
                os.chmod(tmp, 0o600)
                out = strip_ansi(run_fleet(self.inventory,
                                           ["router", "push", "--from-file", tmp,
                                            "--force"], timeout=300))
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            with _lock:
                _last_run.update(action=f"import {item['name']}",
                                 when=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 output=out[-20000:])
            return self._redirect("/")

        self._send(page("not found", "<h1>not found</h1>"), 404)

    # --------------------------------------------------------------- pages
    def _login_page(self, err: str = "") -> bytes:
        e = f'<p style="color:var(--bad)">{html.escape(err)}</p>' if err else ""
        return page("fleet — sign in", f"""
<h1>fleet</h1><div class=sub>local control panel</div>{e}
<form method=post action="/login">
<label for=p>password</label>
<input id=p type=password name=password autofocus autocomplete=current-password>
<p><button type=submit>sign in</button></p></form>""", nav=False)


    def _spark(self, vals, w=260, h=34, label=""):
        """Tiny inline SVG sparkline. No JS, no CDN — the panel must work on a LAN
        with the tunnel down, which rules out anything fetched from the internet."""
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            return '<div class=sub>not enough samples yet</div>'
        top = max(vals) or 1
        n = len(vals)
        step = w / (n - 1)
        pts = " ".join(f"{i*step:.1f},{h - (v/top)*(h-3):.1f}" for i, v in enumerate(vals))
        return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
                f'preserveAspectRatio="none" role="img" aria-label="{html.escape(label)}">'
                f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
                f'stroke-linejoin="round" points="{pts}"/></svg>')


    def _splitbar(self, direct, tunnel, w=380, h=22):
        """Stacked bar: how much is leaving direct vs through the tunnel."""
        tot = (direct or 0) + (tunnel or 0)
        if tot <= 0:
            return '<div class=sub>nothing measured in this window</div>'
        dw = (direct / tot) * w
        return (
            f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="direct {direct}, tunnelled {tunnel}">'
            f'<rect x="0" y="0" width="{w}" height="{h}" rx="3" fill="currentColor" '
            f'opacity="0.18"/>'
            f'<rect x="0" y="0" width="{dw:.1f}" height="{h}" rx="3" fill="currentColor" '
            f'opacity="0.55"/></svg>')

    def _hostlist(self, rows, empty):
        if not rows:
            return f'<div class=sub>{empty}</div>'
        return "<table>" + "".join(
            f"<tr><td>{html.escape(r['host'])}</td>"
            f"<td class=sub>{rstats.human_bytes(r['bytes'])}</td></tr>"
            for r in rows) + "</table>"

    def _stats_json(self) -> bytes:
        """Live numbers for the page to poll. Deliberately CHEAP.

        Only the Clash API is touched — plain HTTP to sing-box on the LAN, a few
        milliseconds. The SSH-backed probe (DNS settings, nft rules, podkop
        version, the outbound name map) is left to the slow poller, because an
        SSH handshake every few seconds against a small router is real load for
        data that changes hourly at most.

        Throughput is computed here rather than read: Clash exposes cumulative
        byte counters, so a rate needs two samples. A counter that went backwards
        means sing-box restarted, and that interval is dropped rather than
        reported as a negative or an enormous spike.
        """
        now = time.time()
        try:
            cur = rstats.clash_snapshot(CLASH_BASE) if CLASH_BASE else {}
            hosts = rstats.live_hosts(CLASH_BASE) if CLASH_BASE else {"direct": [], "tunnel": []}
        except Exception:                            # noqa: BLE001 - never 500 the page
            cur, hosts = {}, {"direct": [], "tunnel": []}

        with _lock:
            names = dict(_name_map)
            prev = dict(_live_prev)
            _live_prev.update(t=now, up=cur.get("up_total", 0), dn=cur.get("down_total", 0))

        up_bps = dn_bps = None
        dt = now - prev.get("t", 0) if prev else 0
        if prev and 0 < dt < 600:
            du = cur.get("up_total", 0) - prev.get("up", 0)
            dd = cur.get("down_total", 0) - prev.get("dn", 0)
            if du >= 0 and dd >= 0:                  # negative => sing-box restarted
                up_bps, dn_bps = du * 8 / dt, dd * 8 / dt

        sel = cur.get("selected", "")
        # The age of the last STORED sample (the 3-minute SSH-backed one), plus the
        # server's own clock. The page ticks the counter locally between polls but
        # re-baselines from these, so it can never drift or depend on the browser's
        # clock being right.
        try:
            rows = rstats.load()
            sample_t = rows[-1]["t"] if rows else None
        except Exception:                            # noqa: BLE001
            sample_t = None

        out = {
            "t": int(now),
            "sample_t": sample_t,
            "poll_seconds": POLL_SECONDS,
            "reachable": cur.get("reachable", False),
            "selected": sel,
            "selected_label": names.get(sel, sel),
            "exits": [{"tag": e["name"], "label": names.get(e["name"], e["name"]),
                       "delay_ms": e.get("delay_ms"),
                       "serving": e["name"] == sel} for e in cur.get("exits", [])],
            "direct_conns": cur.get("direct_conns", 0),
            "tunnel_conns": cur.get("tunnel_conns", 0),
            "direct_bytes": cur.get("direct_bytes", 0),
            "tunnel_bytes": cur.get("tunnel_bytes", 0),
            "up_bps": up_bps, "down_bps": dn_bps,
            # Hostnames are rendered and discarded, never written to disk — a
            # rolling log of everything the household visits is the artefact this
            # project exists to avoid.
            "hosts": hosts,
        }
        return json.dumps(out).encode()

    def _stats_page(self) -> bytes:
        rows = rstats.load()
        cur = rows[-1] if rows else {}
        d = rstats.deltas(rows)
        router = (cur.get("router") or {})

        if not rows:
            body = ("<h1>router stats</h1><div class=sub>no samples yet — the poller "
                    f"runs every {POLL_SECONDS//60} min. Check back shortly.</div>")
            return page("stats", body)

        age = int(time.time()) - cur.get("t", 0)
        reach = cur.get("reachable")
        dot = '<span class=set>●</span>' if reach else '<span class=bad>●</span>'

        # --- exits -------------------------------------------------------------
        sel = cur.get("selected", "")
        ex = []
        for e in cur.get("exits", []):
            ms = e.get("delay_ms")
            serving = e["name"] == sel
            # THREE states, not two. `None` means sing-box has recorded no probe
            # for this outbound; `0` means a probe ran and failed. Collapsing them
            # with `if not ms` reported a healthy, traffic-carrying exit as
            # "unreachable" — a false alarm that cost real debugging time, because
            # the panel was contradicting the fact that the internet plainly worked.
            if ms is None:
                lat = "carrying traffic" if serving else "no probe yet"
                cls = ""
            elif ms == 0:
                lat, cls = "unreachable", " class=bad"
            else:
                lat, cls = f"{ms} ms", ""
            mark = " &larr; serving" if serving else ""
            # The friendly name if router_stats could resolve one, else the tag.
            shown = e.get("label") or e["name"]
            title = f' title="{html.escape(e["name"])}"' if shown != e["name"] else ""
            ex.append(f"<tr><td{title}>{html.escape(shown)}</td>"
                      f"<td{cls}>{lat}</td><td class=sub>{mark}</td></tr>")
        extable = ("<table id=extable><tr><th>exit</th><th>latency</th><th></th></tr>"
                   + "".join(ex) + "</table>") if ex else "<div class=sub>no exits</div>"

        # --- throughput --------------------------------------------------------
        recent = d[-48:]
        down_sp = self._spark([x["down_bps"] for x in recent], label="download")
        up_sp = self._spark([x["up_bps"] for x in recent], label="upload")
        last = recent[-1] if recent else {}
        win_up = sum(x["up"] for x in d)
        win_dn = sum(x["down"] for x in d)
        span_h = ((rows[-1]["t"] - rows[0]["t"]) / 3600) if len(rows) > 1 else 0

        # --- the split: direct (RU) vs tunnelled --------------------------------
        # Live hostnames are fetched here and thrown away with the response. Only
        # the aggregate counts in `rows` are ever written to disk.
        try:
            hosts = rstats.live_hosts(CLASH_BASE) if CLASH_BASE else {"direct": [], "tunnel": []}
        except Exception:                          # noqa: BLE001
            hosts = {"direct": [], "tunnel": []}
        dc, tc = cur.get("direct_conns", 0), cur.get("tunnel_conns", 0)
        db, tb = cur.get("direct_bytes", 0), cur.get("tunnel_bytes", 0)
        win_dc = sum(r.get("direct_conns", 0) for r in rows)
        win_tc = sum(r.get("tunnel_conns", 0) for r in rows)
        pct = (100 * dc / (dc + tc)) if (dc + tc) else 0
        split_block = f"""
<table><tr>
<td style="padding-right:18px">connections<br><span class=sub>now</span></td>
<td>{self._splitbar(dc, tc)}</td>
<td><b>{dc}</b> direct <span class=sub>/ {tc} tunnelled &middot; {pct:.0f}% direct</span></td>
</tr><tr>
<td style="padding-right:18px">bytes<br><span class=sub>now</span></td>
<td>{self._splitbar(db, tb)}</td>
<td><b>{rstats.human_bytes(db)}</b> direct
<span class=sub>/ {rstats.human_bytes(tb)} tunnelled</span></td>
</tr><tr>
<td style="padding-right:18px">connections<br><span class=sub>window</span></td>
<td>{self._splitbar(win_dc, win_tc)}</td>
<td><b>{win_dc}</b> direct <span class=sub>/ {win_tc} tunnelled (summed over samples)</span></td>
</tr></table>
<div class=sub style="margin-top:10px">Left/darker = straight out your own ISP
(Russian services). Right = through the exit.
<br><b>Read the direct figure as a floor, not a total.</b> These are sampled from
sing-box&rsquo;s <i>live</i> connection list every {POLL_SECONDS//60} min, and a
Russian page load lasts under a second &mdash; so most of them open and close
between samples and are never counted. Long-lived flows (streaming, apps, downloads)
are represented fairly; short ones are not. A low direct count is NOT evidence the
split is broken &mdash; check an actual egress address for that.</div>

<table style="margin-top:12px"><tr>
<td style="vertical-align:top;padding-right:34px">
  <div class=sub>direct right now &mdash; your ISP sees these</div>
  {self._hostlist(hosts['direct'], 'nothing direct at this moment')}
</td>
<td style="vertical-align:top">
  <div class=sub>tunnelled right now &mdash; the exit sees these</div>
  {self._hostlist(hosts['tunnel'], 'nothing tunnelled at this moment')}
</td>
</tr></table>
<div class=sub style="margin-top:6px">Hostnames are read live and never written to
disk &mdash; a stored history of visited sites is the artefact this project exists to
avoid.</div>
"""

        # --- render ------------------------------------------------------------
        body = f"""
<h1>router stats</h1>
<div class=sub>{dot} <span id=lv-sampled>sampled {age}s ago</span> &middot; {len(rows)} samples over
{span_h:.1f} h &middot; every {POLL_SECONDS//60} min</div>

<h2>tunnel</h2>
{extable}
<div class=sub style="margin-top:6px">active connections:
<b id=lv-conns>{cur.get('conns', 0)}</b>
<span class=sub id=lv-age></span></div>

<h2>throughput</h2>
<table>
<tr><td>down</td><td>{down_sp}</td>
    <td id=lv-down>{rstats.human_bps(last.get('down_bps', 0))}</td></tr>
<tr><td>up</td><td>{up_sp}</td>
    <td id=lv-up>{rstats.human_bps(last.get('up_bps', 0))}</td></tr>
</table>
<div class=sub>transferred in this window:
{rstats.human_bytes(win_dn)} down / {rstats.human_bytes(win_up)} up
&middot; since sing-box started: {rstats.human_bytes(cur.get('down_total', 0))} down /
{rstats.human_bytes(cur.get('up_total', 0))} up</div>

<h2>split — what goes where</h2>
{split_block}

{LIVE_JS}

<h2>podkop &amp; DNS</h2>
<table>
<tr><td>sing-box</td><td>{'running' if router.get('singbox') else
  '<span class=bad>not running</span>'}</td></tr>
<tr><td>nftables rules</td><td>{router.get('nft_rules', 0)}
  {'' if router.get('nft_rules') else '<span class=bad>&mdash; traffic is NOT intercepted</span>'}</td></tr>
<tr><td>podkop</td><td>{html.escape(router.get('podkop') or '?')}</td></tr>
<tr><td>resolver</td><td>{html.escape(router.get('dns_type') or '?')}://{html.escape(router.get('dns_server') or '?')}</td></tr>
<tr><td>local dnsproxy</td><td>{'running' if router.get('dnsproxy') else
  '<span class=sub>not running</span>'}</td></tr>
</table>
<div class=sub style="margin-top:8px">A running sing-box does not prove traffic is
being tunnelled — the nftables rule count is the honest signal.</div>
"""
        return page("stats", body)

    def _status_page(self) -> bytes:
        st = strip_ansi(run_fleet(self.inventory, ["status"], timeout=60))
        with _lock:
            last = dict(_last_run)
        buttons = "".join(
            f'<form method=post action="/action">'
            f'<input type=hidden name=name value="{a}">'
            f'<button type=submit>{a}</button></form>' for a in ACTIONS)
        lastblock = ""
        if last["action"]:
            lastblock = (f"<h2>last action — {html.escape(last['action'])} "
                         f"<span class=sub>{html.escape(last['when'])}</span></h2>"
                         f"<pre>{html.escape(last['output'])}</pre>")
        return page("fleet", f"""
<h1>fleet</h1><div class=sub>{html.escape(self.inventory)}</div>
<pre>{html.escape(st)}</pre>
<h2>actions</h2><div>{buttons}</div>
<div class=sub style="margin-top:10px">Actions can take minutes — the page returns
when the command finishes.</div>
{lastblock}""")

    def _import_page(self, err: str = "") -> bytes:
        e = f'<p style="color:var(--bad)">{html.escape(err)}</p>' if err else ""
        return page("fleet — import config", f"""
<h1>import a config</h1>
<div class=sub>Upload a subscription and push it to the router on this LAN. Accepts
what <code>fleet subscription</code> writes (base64) or a plain list of
<code>vless://</code> lines. Nothing is applied until you confirm on the next
screen.</div>{e}
<form method=post action="/import" enctype="multipart/form-data">
  <input type=file name=config accept=".txt,.json,text/plain" required>
  <p><button type=submit>read the file</button></p>
</form>
<div class=sub>This pushes to the router only. It does not add anything to
<code>fleet</code>'s own state, so a later rotation on this orchestrator will
replace it — fine for a remote site or a quick recovery, not a permanent
arrangement.</div>""")

    def _import_preview(self, raw: bytes) -> bytes:
        fields = parse_multipart(self.headers.get("Content-Type", ""), raw)
        blob = fields.get("config", b"")
        if not blob:
            return self._import_page(
                "no file received — or it was larger than "
                f"{MAX_UPLOAD // 1024} KB")
        try:
            from fleet import render as _render
            uris = _render.parse_subscription(blob.decode("utf-8", "replace"))
            rows_src = [_render.describe_uri(u) for u in uris]
        except Exception as exc:                      # noqa: BLE001 - show, never 500
            return self._import_page(f"could not read that file: {exc}")
        if not uris:
            return self._import_page(
                "no vless:// URIs in that file. Expected a `fleet subscription` "
                "blob or a plain list.")

        rows = "".join(
            f"<tr><td><code>{html.escape(d['host'])}:{html.escape(d['port'])}</code></td>"
            f"<td>{html.escape(d['sni'])}</td><td>{html.escape(d['transport'])}</td>"
            f"<td>{html.escape(d['flow'])}</td><td><code>{html.escape(d['id'])}</code></td>"
            f"<td>{html.escape(d['label'])}</td></tr>"
            for d in rows_src)

        # Two things worth knowing BEFORE clicking, not after.
        notes = ""
        serving = strip_ansi(run_fleet(self.inventory, ["status"], timeout=60))
        import re as _re
        m = _re.search(r"EXITS\s+\((\d+) serving", serving)
        if m and int(m.group(1)) > 0:
            notes += (f'<p style="color:var(--warn,#b80)">This orchestrator has '
                      f'<b>{m.group(1)} serving exit(s)</b> of its own. A rotation or '
                      f'the 15-minute <code>fleet cron</code> will overwrite this '
                      f'import.</p>')
        if any(d["security"] != "reality" for d in rows_src):
            notes += ('<p style="color:var(--warn,#b80)">At least one entry is not '
                      'REALITY. Check that is intended.</p>')

        tok = stage_import(uris, "uploaded config")
        return page("fleet — confirm import", f"""
<h1>confirm import</h1>
<div class=sub>{len(uris)} server(s) found. Client UUIDs are truncated here on
purpose. Nothing has been sent to the router yet.</div>{notes}
<table><tr><th>address</th><th>sni</th><th>transport</th><th>flow</th>
<th>id</th><th>label</th></tr>{rows}</table>
<form method=post action="/import/apply">
  <input type=hidden name=token value="{html.escape(tok)}">
  <p><button type=submit>push these to the router</button>
  &nbsp;<a href="/import">cancel</a></p>
</form>
<div class=sub>The confirmation is single-use and expires in
{IMPORT_TTL // 60} minutes.</div>""")

    def _settings_page(self) -> bytes:
        env_path = os.path.join(ROOT, ".env")
        present = set()
        if os.path.exists(env_path):
            for ln in open(env_path):
                k, _, v = ln.partition("=")
                if v.strip():
                    present.add(k.strip())
        rows = ""
        for key, desc in TOKENS.items():
            mark = ('<span class=set>set</span>' if key in present
                    else '<span class=unset>not set</span>')
            rows += (f"<label for={key}>{html.escape(desc)} "
                     f"<code>{key}</code> — {mark}</label>"
                     f'<input id={key} type=password name={key} autocomplete=off '
                     f'placeholder="leave blank to keep the current value">')
        return page("fleet — credentials", f"""
<h1>credentials</h1>
<div class=sub>Stored in <code>.env</code> on this machine, mode 600. Never shown
again once saved — a blank field leaves the existing value alone.</div>
<form method=post action="/settings">{rows}
<p><button type=submit>save</button></p></form>
<div class=sub>After adding a provider you still need an <code>[[exits.providers]]</code>
entry in <code>inventory.toml</code> for fleet to use it.</div>""")


def main() -> int:
    # Line-buffer stdout. Under systemd (or any redirect) Python block-buffers, and
    # serve_forever() never returns — so the generated password would sit in a buffer
    # forever and nobody could log in.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(description="fleet LAN control panel")
    ap.add_argument("-i", "--inventory", default=os.path.join(ROOT, "inventory.toml"))
    ap.add_argument("--host", default="0.0.0.0", help="bind address (LAN only!)")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--clash-url", default="",
                    help="sing-box Clash API (default: derived from [router].host)")
    ap.add_argument("--router-ssh", default="",
                    help="user@host of the router (default: [router].host)")
    ap.add_argument("--no-stats", action="store_true",
                    help="do not poll the router for stats")
    ap.add_argument("--set-password", action="store_true",
                    help="set the panel password and exit")
    a = ap.parse_args()

    sd = state_dir(a.inventory)
    os.makedirs(sd, exist_ok=True)

    if a.set_password or not os.path.exists(pw_path(sd)):
        import getpass
        if sys.stdin.isatty():
            p1 = getpass.getpass("panel password: ")
            if len(p1) < 8:
                print("at least 8 characters, please", file=sys.stderr)
                return 1
            if p1 != getpass.getpass("again: "):
                print("they did not match", file=sys.stderr)
                return 1
        else:
            p1 = secrets.token_urlsafe(12)
            print(f"\n  generated panel password: {p1}\n  (text this to whoever "
                  f"needs it; change it with --set-password)\n")
        set_password(sd, p1)
        if a.set_password:
            print("password set")
            return 0

    Handler.inventory = a.inventory
    Handler.sdir = sd
    Handler.secret = load_or_make_secret(sd)

    # Point the poller at the router. Defaults come from the inventory so the
    # service unit needs no extra flags.
    global CLASH_BASE, ROUTER_SSH
    ROUTER_SSH = a.router_ssh
    CLASH_BASE = a.clash_url
    if not (ROUTER_SSH and CLASH_BASE):
        try:
            import sys as _s
            _s.path.insert(0, ROOT)
            from fleet import config as _cfg
            _inv = _cfg.load(a.inventory)
            ROUTER_SSH = ROUTER_SSH or (_inv.router.host if _inv.router.enabled else "")
            if not CLASH_BASE and ROUTER_SSH:
                CLASH_BASE = f"http://{ROUTER_SSH.split('@')[-1]}:9090"
        except Exception:                          # noqa: BLE001
            pass
    if a.no_stats or not CLASH_BASE:
        print("  stats polling disabled" if a.no_stats else
              "  stats polling off (no [router] in inventory)")
    else:
        print(f"  stats: polling {CLASH_BASE} every {POLL_SECONDS//60} min")
        threading.Thread(target=_poller_loop, daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"  fleet panel on http://{a.host}:{a.port}  (inventory: {a.inventory})")
    print("  LAN only — do not port-forward this.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
