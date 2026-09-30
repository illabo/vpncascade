"""Generate Xray configs for the entry (RU anchor) and exit (ephemeral) nodes.

Two deliberate choices worth knowing about before you edit this:

1. `domainStrategy` is **AsIs**, not IPIfNonMatch. IPIfNonMatch makes the entry node
   resolve every foreign domain locally so it can test IP rules — which in Russia means
   asking a resolver subject to DNS poisoning. A poisoned answer for a blocked foreign
   domain returns a Russian sinkhole address, that matches `geoip:ru`, and the request
   gets routed *direct* instead of through the tunnel. That is the exact opposite of what
   you want, and it fails silently. With AsIs, domain requests match domain rules and
   raw-IP requests match IP rules, and nothing foreign is ever resolved on Russian soil.

2. Exits are selected by an Xray **balancer + observatory**, not by a single outbound.
   That gives automatic failover when an exit is IP-blocked from Russia at 3am, and it
   makes rotation a config regeneration rather than a cutover.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Inventory
from .util import FleetError
from .state import State

PROBE_URL = "https://www.gstatic.com/generate_204"

#: Outbound tags for exits must share this prefix — it is what the balancer's
#: `selector` matches on, so renaming it here without renaming the selector silently
#: empties the balancer.
EXIT_TAG_PREFIX = "exit-"


def exit_tag(node: dict) -> str:
    name = node["name"]
    return name if name.startswith(EXIT_TAG_PREFIX) else EXIT_TAG_PREFIX + name


def _stream(
    *,
    inbound: bool,
    host: str,
    path: str,
    mode: str,
    sni: str | list[str],
    fingerprint: str = "firefox",
    dest: str = "",
    private_key: str = "",
    short_ids: list[str] | None = None,
    public_key: str = "",
    short_id: str = "",
    transport: str = "xhttp",
) -> dict[str, Any]:
    """One place that knows the REALITY shape, so both ends can never drift.

    Two transports, and the choice is forced on you by what the *client* runs:

    * ``xhttp`` — VLESS over XHTTP. Better against traffic-shape analysis and the only
      one that can hide behind a CDN. **Xray-only**: upstream sing-box does not
      implement XHTTP and has said it will not, so anything sing-box-based — podkop,
      most OpenWrt tooling, several mobile clients — cannot speak it.
    * ``tcp`` — VLESS over TCP with XTLS-Vision. Universally supported, and materially
      faster because Vision splices instead of re-encrypting: that is the difference
      between ~80-150 Mbit/s and near line rate on the Beryl's MT7981B.

    Vision is the better default for a router. Reach for XHTTP when you specifically
    want CDN fronting or are worried about flow-shape fingerprinting.
    """
    if transport not in ("xhttp", "tcp"):
        raise FleetError(f"unknown transport {transport!r} (want \"xhttp\" or \"tcp\")")

    if inbound:
        reality = {
            "show": False,
            "dest": dest,
            "xver": 0,
            "serverNames": sni if isinstance(sni, list) else [sni],
            "privateKey": private_key,
            "shortIds": short_ids or [""],
        }
    else:
        reality = {
            "show": False,
            "serverName": sni if isinstance(sni, str) else sni[0],
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": short_id,
            "spiderX": "/",
        }

    if transport == "tcp":
        return {"network": "tcp", "security": "reality", "realitySettings": reality}

    xhttp: dict[str, Any] = {"path": path, "mode": mode}
    if host:
        xhttp["host"] = host
    return {"network": "xhttp", "xhttpSettings": xhttp, "security": "reality",
            "realitySettings": reality}


def _flow(transport: str) -> str:
    """XTLS-Vision is TCP-only; with XHTTP the flow field must be empty."""
    return "xtls-rprx-vision" if transport == "tcp" else ""


# --------------------------------------------------------------------- entry node


def entry_config(inv: Inventory, state: State) -> dict[str, Any]:
    entry = state.entry or {}
    if not entry:
        raise FleetError("entry node has no state yet — run `fleet entry init` first")

    spec = inv.entry
    clients = [
        {"id": c["uuid"], "email": c["name"], "level": 0, "flow": _flow(spec.transport)}
        for c in state.clients
    ]
    if not clients:
        raise FleetError("no clients defined — run `fleet client add <name>`")

    inbound = {
        "tag": "client-in",
        "listen": "0.0.0.0",
        "port": spec.port,
        "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": _stream(
            inbound=True,
            host=spec.xhttp_host,
            path=spec.xhttp_path,
            mode=spec.xhttp_mode,
            sni=spec.reality_sni,
            dest=spec.reality_dest,
            private_key=entry["reality_private"],
            short_ids=[c["short_id"] for c in state.clients],
            transport=spec.transport,
        ),
        "sniffing": {
            # Needed so domain rules can match traffic the client sent as an IP,
            # and so the exit gets the hostname rather than a bare address.
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly": True,
        },
    }

    inbounds: list[dict[str, Any]] = [inbound]

    # AmneziaWG clients arrive on awg0 and are tproxy'd here by nftables, so they get
    # the same split rules and the same cascade as VLESS clients — one policy, two
    # transports. See deploy/amneziawg/install-awg.sh.
    awg = state.data.get("awg")
    if awg:
        inbounds.append(
            {
                "tag": "awg-in",
                "listen": "127.0.0.1",
                "port": awg.get("tproxy_port", 12345),
                "protocol": "dokodemo-door",
                "settings": {"network": "tcp,udp", "followRedirect": True},
                "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            }
        )

    outbounds: list[dict[str, Any]] = []
    for node in state.serving_exits():
        outbounds.append(
            {
                "tag": exit_tag(node),
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": node["ipv4"],
                            "port": node["port"],
                            "users": [{
                                "id": node["uplink_uuid"], "encryption": "none",
                                "flow": _flow(node.get("transport", "xhttp")),
                            }],
                        }
                    ]
                },
                "streamSettings": _stream(
                    inbound=False,
                    host=node["xhttp_host"],
                    path=node["xhttp_path"],
                    mode=node["xhttp_mode"],
                    sni=node["reality_sni"],
                    fingerprint=node.get("fingerprint", "firefox"),
                    public_key=node["reality_public"],
                    short_id=node["uplink_short_id"],
                    transport=node.get("transport", "xhttp"),
                ),
            }
        )

    outbounds.append(
        {
            "tag": "direct",
            "protocol": "freedom",
            # Resolve RU hostnames locally so we land on the right domestic CDN edge.
            # (Note: the reference config used "targetStrategy", which Xray's freedom
            # outbound does not implement — the correct key is "domainStrategy".)
            "settings": {"domainStrategy": "UseIP"},
        }
    )
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    rules: list[dict[str, Any]] = []
    if spec.split.block_domains:
        rules.append({"type": "field", "domain": list(spec.split.block_domains),
                      "outboundTag": "block"})
    # Keep the node's own management traffic off the tunnel.
    rules.append({"type": "field", "inboundTag": ["dns-in"], "outboundTag": "direct"})
    rules.append({"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"})

    if spec.split.enabled:
        if spec.split.direct_domains:
            rules.append({"type": "field", "domain": list(spec.split.direct_domains),
                          "outboundTag": "direct"})
        if spec.split.direct_ips:
            rules.append({"type": "field", "ip": list(spec.split.direct_ips),
                          "outboundTag": "direct"})

    has_exits = bool(state.serving_exits())
    tunnelled = ["client-in"] + (["awg-in"] if state.data.get("awg") else [])
    rules.append(
        {"type": "field", "inboundTag": tunnelled, "balancerTag": "exits"}
        if has_exits
        # No exits yet: fail closed rather than silently leaking everything out of a
        # Russian datacenter under the user's real identity.
        else {"type": "field", "inboundTag": tunnelled, "outboundTag": "block"}
    )

    config: dict[str, Any] = {
        "log": {"loglevel": "warning", "dnsLog": False},
        "dns": {
            "servers": (
                [
                    {
                        "address": server,
                        "domains": list(spec.split.direct_domains),
                        "skipFallback": True,
                    }
                    for server in spec.split.ru_dns
                ]
                + ["localhost"]
            ),
            "queryStrategy": "UseIPv4",
            "disableFallbackIfMatch": True,
        },
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
        "policy": {
            "levels": {"0": {"handshake": 4, "connIdle": 300, "uplinkOnly": 0,
                             "downlinkOnly": 0}},
            "system": {"statsInboundUplink": False, "statsInboundDownlink": False},
        },
    }

    if has_exits:
        config["routing"]["balancers"] = [
            {"tag": "exits", "selector": [EXIT_TAG_PREFIX], "strategy": {"type": "leastPing"}}
        ]
        config["observatory"] = {
            "subjectSelector": [EXIT_TAG_PREFIX],
            "probeURL": PROBE_URL,
            "probeInterval": "30s",
            "enableConcurrency": True,
        }
    return config


# ---------------------------------------------------------------------- exit node


def exit_config(inv: Inventory, node: dict, clients: list[dict] | None = None) -> dict[str, Any]:
    """Config for an exit node.

    In cascade mode the exit knows exactly one credential — the entry node's — and
    nothing about clients, so losing an exit leaks no client identity.

    In direct mode there is no entry node, so the exit necessarily holds every client
    credential. That is the real cost of dropping the cascade: each ephemeral foreign
    box, in a jurisdiction you did not choose, now holds the UUIDs and shortIds of all
    your devices. They are only bearer tokens for your own proxy, but it is a genuine
    downgrade rather than a free simplification.

    **Rotation does NOT revoke them.** An earlier version of this docstring claimed
    they "die with the rotation"; that is false, and the mistake matters. Client
    credentials live in state and are re-pushed to every new exit — verified against a
    live fleet, where one client UUID outlived several exit rotations. So a UUID taken
    from a seized or hostile exit stays valid on every *future* exit too. The only
    thing that invalidates it is `fleet client revoke <name>`, which DELETES the
    client and re-syncs the pool without it. It does not re-key: there is no
    rotate-this-credential operation, so giving a device a fresh UUID means
    `revoke` then `add` under the same name, and re-issuing its share link.
    Rotate exits to change your addresses; revoke clients to change your
    credentials. They are separate operations.
    """
    tr = node.get("transport", "xhttp")
    if inv.cascade:
        inbound_tag = "uplink-in"
        vless_clients = [{"id": node["uplink_uuid"], "email": "entry", "level": 0,
                          "flow": _flow(tr)}]
        short_ids = [node["uplink_short_id"]]
    else:
        clients = clients or []
        if not clients:
            raise FleetError(
                "direct mode needs at least one client — run `fleet client add <name>`")
        inbound_tag = "client-in"
        vless_clients = [{"id": c["uuid"], "email": c["name"], "level": 0,
                          "flow": _flow(tr)} for c in clients]
        short_ids = [c["short_id"] for c in clients]

    inbound = {
        "tag": inbound_tag,
        "listen": "0.0.0.0",
        "port": node["port"],
        "protocol": "vless",
        "settings": {"clients": vless_clients, "decryption": "none"},
        "streamSettings": _stream(
            inbound=True,
            host=node["xhttp_host"],
            path=node["xhttp_path"],
            mode=node["xhttp_mode"],
            sni=node["reality_sni"],
            dest=node["reality_dest"],
            private_key=node["reality_private"],
            short_ids=short_ids,
            transport=tr,
        ),
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"],
                     "routeOnly": True},
    }

    return {
        "log": {"loglevel": "warning", "dnsLog": False},
        "dns": {"servers": ["https://1.1.1.1/dns-query", "1.1.1.1", "9.9.9.9"],
                "queryStrategy": "UseIP"},
        "inbounds": [inbound],
        "outbounds": [
            {"tag": "out", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                # Never let the tunnel reach the provider's internal network or metadata
                # service. Providers terminate accounts for this and it is a trivial SSRF
                # pivot if a client is compromised.
                {"type": "field", "ip": ["geoip:private", "169.254.0.0/16"],
                 "outboundTag": "block"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                {"type": "field", "inboundTag": [inbound_tag], "outboundTag": "out"},
            ],
        },
        "policy": {
            "levels": {"0": {"handshake": 4, "connIdle": 300}},
            "system": {"statsInboundUplink": False, "statsInboundDownlink": False},
        },
    }


# ------------------------------------------------------------------- client share


def client_uri(inv: Inventory, state: State, client: dict, node: dict | None = None) -> str:
    """A vless:// share link. `node` selects an exit (direct mode); omit for the entry."""
    from urllib.parse import quote, urlencode

    if node is None:
        if not inv.cascade:
            raise FleetError("direct mode: client_uri needs an exit node")
        entry = state.entry or {}
        host, port, label = inv.entry.host, inv.entry.port, inv.entry.name
        pbk = entry["reality_public"]
        sni, fp = inv.entry.reality_sni[0], inv.entry.fingerprint
        xmode, xpath, xhost = inv.entry.xhttp_mode, inv.entry.xhttp_path, inv.entry.xhttp_host
        tr = inv.entry.transport
    else:
        host, port, label = node["ipv4"], node["port"], node["name"]
        pbk = node["reality_public"]
        sni, fp = node["reality_sni"], node.get("fingerprint", "firefox")
        xmode, xpath, xhost = node["xhttp_mode"], node["xhttp_path"], node["xhttp_host"]
        tr = node.get("transport", "xhttp")

    params = {
        "encryption": "none", "security": "reality", "sni": sni, "fp": fp, "pbk": pbk,
        "sid": client["short_id"], "spx": "/",
    }
    if tr == "tcp":
        params.update({"type": "tcp", "flow": "xtls-rprx-vision", "headerType": "none"})
    else:
        params.update({"type": "xhttp", "mode": xmode, "path": xpath, "host": xhost})
    tag = quote(f"{label}-{client['name']}")
    return f"vless://{client['uuid']}@{host}:{port}?{urlencode(params)}#{tag}"


def client_uris(inv: Inventory, state: State, client: dict) -> list[str]:
    """Every server this client should know about."""
    if inv.cascade:
        return [client_uri(inv, state, client)]
    return [client_uri(inv, state, client, n) for n in state.serving_exits()]


def subscription(inv: Inventory, state: State, client: dict) -> str:
    """A base64 subscription payload, the format every modern client understands.

    This is the answer to "how do clients follow a rotation without me touching each
    device": they poll a URL, not DNS. Point v2rayTun / Happ / NekoBox / podkop at it
    and `fleet rotate` becomes invisible to them again — the property the cascade gives
    you for free and direct mode otherwise takes away.
    """
    import base64

    uris = client_uris(inv, state, client)
    if not uris:
        raise FleetError("no serving exits — nothing to put in a subscription")
    return base64.b64encode("\n".join(uris).encode()).decode()


def parse_subscription(blob: str) -> list[str]:
    """Extract `vless://` URIs from a subscription, in whatever shape it arrived.

    Accepts what `subscription()` above produces — base64 of newline-joined URIs,
    the format every modern client speaks — and also a plain list, because the
    thing a human actually has to hand is often a pasted file or a client export.
    Being forgiving here costs nothing; guessing wrong about the format in front of
    someone whose internet is already down costs a lot.

    Deliberately NOT forgiving about what it returns: only well-formed `vless://`
    URIs, de-duplicated, order preserved. Anything else in the file is ignored
    rather than half-understood.
    """
    import base64 as _b64
    import re as _re

    text = (blob or "").strip()
    if "vless://" not in text:
        # Padding is often stripped from subscription blobs in transit.
        try:
            decoded = _b64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace")
            if "vless://" in decoded:
                text = decoded
        except Exception:
            pass

    seen: set[str] = set()
    out: list[str] = []
    for m in _re.findall(r"vless://[^\s\"'<>]+", text):
        uri = m.rstrip(",;")
        if uri not in seen:
            seen.add(uri)
            out.append(uri)
    return out


def describe_uri(uri: str) -> dict[str, str]:
    """The fields worth showing before a URI is allowed to reconfigure a router.

    The UUID is truncated on purpose: it is the client credential, and this ends up
    on a web page.
    """
    from urllib.parse import parse_qs as _qs, unquote as _unq, urlparse as _up

    p = _up(uri)
    q = _qs(p.query)
    first = lambda k, d="—": (q.get(k) or [d])[0]  # noqa: E731
    return {
        "host": p.hostname or "?",
        "port": str(p.port or "?"),
        "sni": first("sni", first("host")),
        "security": first("security"),
        "flow": first("flow"),
        "transport": first("type", "tcp"),
        "id": (p.username[:8] + "…") if p.username else "—",
        "label": _unq(p.fragment or "") or "—",
    }


def dumps(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def client_config(inv: Inventory, state: State, client: dict, *,
                  socks_port: int = 10808, http_port: int = 10809,
                  local_split: bool = True) -> dict[str, Any]:
    """A full Xray client config — for a desktop, or for xray-core on OpenWrt.

    What `local_split` means depends on the mode, and the difference matters:

      * **cascade** — the geography split lives on the RU entry node, so it covers
        every device including phones on mobile data. Here it is only a
        *failure-domain* split: if the tunnel is down, Russian destinations still work
        via the local ISP instead of blackholing.

      * **direct** — there is no Russian node, so this *is* the geography split, and
        it is the only one. Russian traffic leaves via your home ISP, which for a
        Russian home connection is a genuinely better source address than a datacenter
        one: it is residential, it is stable, and bank anti-fraud likes it. Turn this
        off and your bank sees a German IP.
    """
    entry = state.entry or {}
    if not entry and inv.cascade:
        raise FleetError("entry node has no state yet — run `fleet entry init` first")
    spec = inv.entry

    outbounds: list[dict[str, Any]] = []
    bypass_ips: list[str] = []

    if inv.cascade:
        outbounds.append({
            "tag": "proxy",
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": spec.host, "port": spec.port,
                "users": [{"id": client["uuid"], "encryption": "none",
                           "flow": _flow(spec.transport)}],
            }]},
            "streamSettings": _stream(
                inbound=False, host=spec.xhttp_host, path=spec.xhttp_path,
                mode=spec.xhttp_mode, sni=spec.reality_sni,
                fingerprint=spec.fingerprint, public_key=entry["reality_public"],
                short_id=client["short_id"], transport=spec.transport,
            ),
        })
        bypass_ips.append(spec.host)
        proxy_target: dict[str, Any] = {"outboundTag": "proxy"}
    else:
        serving = state.serving_exits()
        if not serving:
            raise FleetError("no serving exits — run `fleet up` first")
        for node in serving:
            outbounds.append({
                "tag": exit_tag(node),
                "protocol": "vless",
                "settings": {"vnext": [{
                    "address": node["ipv4"], "port": node["port"],
                    "users": [{"id": client["uuid"], "encryption": "none",
                               "flow": _flow(node.get("transport", "xhttp"))}],
                }]},
                "streamSettings": _stream(
                    inbound=False, host=node["xhttp_host"], path=node["xhttp_path"],
                    mode=node["xhttp_mode"], sni=node["reality_sni"],
                    fingerprint=node.get("fingerprint", "firefox"),
                    public_key=node["reality_public"], short_id=client["short_id"],
                    transport=node.get("transport", "xhttp"),
                ),
            })
            bypass_ips.append(node["ipv4"])
        # The balancer moves here from the entry node: with no Russian node to hide
        # rotation behind, the client itself has to survive an exit disappearing.
        proxy_target = {"balancerTag": "exits"}

    outbounds.append(
        {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}})
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    rules: list[dict[str, Any]] = [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
        # Never tunnel the tunnel.
        {"type": "field", "ip": bypass_ips, "outboundTag": "direct"},
    ]
    if local_split:
        rules.append({"type": "field", "domain": list(spec.split.direct_domains),
                      "outboundTag": "direct"})
        if not inv.cascade:
            # Direct mode: this is the real geography split, so catch bare-IP
            # connections to Russian hosts too, exactly as the entry node would.
            rules.append({"type": "field", "ip": list(spec.split.direct_ips),
                          "outboundTag": "direct"})
    rules.append({"type": "field", "network": "tcp,udp", **proxy_target})

    config: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks", "listen": "127.0.0.1", "port": socks_port,
                "protocol": "socks", "settings": {"udp": True, "auth": "noauth"},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            },
            {
                "tag": "http", "listen": "127.0.0.1", "port": http_port,
                "protocol": "http",
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }
    if not inv.cascade:
        config["routing"]["balancers"] = [
            {"tag": "exits", "selector": [EXIT_TAG_PREFIX], "strategy": {"type": "leastPing"}}]
        config["observatory"] = {
            "subjectSelector": [EXIT_TAG_PREFIX], "probeURL": PROBE_URL,
            "probeInterval": "30s", "enableConcurrency": True,
        }
    return config


# ------------------------------------------------------------------------- DNS

#: Yandex Basic, verified 2026-09-26. Basic on purpose — Safe and Family apply their
#: own filtering, which is just a second censor.
YANDEX_DOT_IP = "77.88.8.8"
YANDEX_DOT_IP2 = "77.88.8.1"
YANDEX_DOT_NAME = "common.dot.dns.yandex.net"


def _ru_suffixes(inv: Inventory) -> list[str]:
    """The split list reduced to bare domain suffixes dnsmasq can route on."""
    out = []
    for entry in inv.entry.split.direct_domains:
        if entry.startswith("domain:") or entry.startswith("full:"):
            out.append(entry.split(":", 1)[1])
        elif ":" not in entry:           # a plain substring rule
            out.append(entry)
        # geosite:/regexp: have no dnsmasq equivalent — skipped deliberately.
    return sorted(set(out))


def dnsmasq_split(inv: Inventory, *, stubby_port: int = 5453) -> str:
    """dnsmasq / Pi-hole config: Russian names to a domestic resolver, nothing else.

    The deliberate omission is a default upstream. Foreign names must NOT resolve here
    — they travel as names inside the tunnel and are resolved at the exit, which is the
    only path that never touches Russian DNS infrastructure. See docs/07-dns.md.
    """
    lines = [
        "# Generated by `fleet dns` — do not edit by hand, regenerate instead.",
        "#",
        "# Russian names go to a domestic resolver (via stubby, DoT). Everything else",
        "# is deliberately NOT resolved here: the name travels inside the tunnel and",
        "# the exit node resolves it. Since August 2026 TSPU rewrites queries aimed at",
        "# 8.8.8.8 / 1.1.1.1 to the state resolver, silently, so resolving foreign",
        "# names locally means asking the censor. See docs/07-dns.md.",
        "",
        f"# Domestic names -> stubby -> DoT {YANDEX_DOT_NAME}",
    ]
    for suffix in _ru_suffixes(inv):
        lines.append(f"server=/{suffix}/127.0.0.1#{stubby_port}")
    lines += [
        "",
        "# Reverse lookups for RFC1918 stay local.",
        "server=/10.in-addr.arpa/",
        "server=/168.192.in-addr.arpa/",
        "",
        "# Hygiene.",
        "domain-needed",
        "bogus-priv",
        "no-resolv          # never inherit upstreams from /etc/resolv.conf",
        "cache-size=10000",
        "min-cache-ttl=60",
        "",
        "# NOTE: with `no-resolv` and no `server=` default, anything not matched above",
        "# gets no answer from this resolver. That is intended when podkop FakeIP or a",
        "# proxy-aware client handles foreign names. If some device genuinely needs a",
        "# local answer for foreign names, add ONE line below and understand that those",
        "# queries are then visible to, and answerable by, the network:",
        f"#   server=127.0.0.1#{stubby_port}",
    ]
    return "\n".join(lines) + "\n"


def stubby_config(inv: Inventory, *, listen_port: int = 5453) -> str:
    """stubby: DoT to the domestic resolver.

    stubby rather than dnscrypt-proxy because it takes a hostname, an address and a
    port directly — no DNS stamp to source or fabricate — and DoT to a domestic
    resolver is the whole job here.
    """
    return f"""# Generated by `fleet dns` — do not edit by hand, regenerate instead.
#
# DNS-over-TLS to a Russian resolver, for Russian names only. Foreign names are not
# resolved on this box at all; see docs/07-dns.md.

resolution_type: GETDNS_RESOLUTION_STUB
dns_transport_list:
  - GETDNS_TRANSPORT_TLS
tls_authentication: GETDNS_AUTHENTICATION_REQUIRED
tls_query_padding_blocksize: 128
edns_client_subnet_private: 1
round_robin_upstreams: 1
idle_timeout: 10000

listen_addresses:
  - 127.0.0.1@{listen_port}
  - 0::1@{listen_port}

upstream_recursive_servers:
  - address_data: {YANDEX_DOT_IP}
    tls_auth_name: "{YANDEX_DOT_NAME}"
    tls_port: 853
  - address_data: {YANDEX_DOT_IP2}
    tls_auth_name: "{YANDEX_DOT_NAME}"
    tls_port: 853
"""
