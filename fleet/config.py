"""Inventory loading and validation (TOML — stdlib tomllib, no deps)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .util import FleetError

DEFAULT_INVENTORY = "inventory.toml"

# Domains that should leave the RU entry node directly, with a Russian source IP.
#
# These are explicit `domain:` entries rather than `geosite:` tags on purpose. A
# geosite tag that is missing from the geosite.dat shipped with your Xray build makes
# Xray refuse to start, which on a remote node means a fleet-wide outage at 3am. Plain
# domain rules can never do that. If your geosite.dat does carry `category-ru`, add it
# in inventory.toml — `fleet entry deploy` runs `xray -test` before swapping configs,
# so a bad tag is caught before it can take the node down.
DEFAULT_DIRECT_DOMAINS = [
    # State / identity
    "domain:gosuslugi.ru", "domain:nalog.ru", "domain:nalog.gov.ru", "domain:pfr.gov.ru",
    "domain:mos.ru", "domain:gov.ru", "domain:rt.ru",
    # Banking and payments
    "domain:sber.ru", "domain:sberbank.ru", "domain:sbrf.ru", "domain:tbank.ru",
    "domain:tinkoff.ru", "domain:alfabank.ru", "domain:vtb.ru", "domain:psbank.ru",
    "domain:raiffeisen.ru", "domain:gazprombank.ru", "domain:open.ru", "domain:mkb.ru",
    "domain:sovcombank.ru", "domain:rshb.ru", "domain:mironline.ru", "domain:nspk.ru",
    # Yandex / VK / Mail.ru ecosystems
    "domain:yandex.ru", "domain:yandex.net", "domain:yandex.com", "domain:ya.ru",
    "domain:yastatic.net", "domain:yandexcloud.net", "domain:kinopoisk.ru",
    "domain:vk.com", "domain:vk.ru", "domain:userapi.com", "domain:vkuser.net",
    "domain:vk-cdn.net", "domain:mail.ru", "domain:imgsmail.ru", "domain:mradx.net",
    "domain:ok.ru", "domain:odnoklassniki.ru",
    # Commerce and services
    "domain:ozon.ru", "domain:wildberries.ru", "domain:wb.ru", "domain:avito.ru",
    "domain:avito.st", "domain:dns-shop.ru", "domain:mvideo.ru", "domain:citilink.ru",
    "domain:2gis.ru", "domain:2gis.com", "domain:hh.ru", "domain:drom.ru",
    "domain:aviasales.ru", "domain:tutu.ru", "domain:rzd.ru", "domain:pochta.ru",
    "domain:cdek.ru", "domain:delivery-club.ru", "domain:sbermarket.ru",
    # Media and telecom
    "domain:rutube.ru", "domain:smotrim.ru", "domain:1tv.ru", "domain:ria.ru",
    "domain:lenta.ru", "domain:rbc.ru", "domain:kommersant.ru", "domain:habr.com",
    "domain:mts.ru", "domain:beeline.ru", "domain:megafon.ru", "domain:tele2.ru",
    # Infrastructure / CDN that only serves RU
    "domain:selectel.ru", "domain:vk.cloud", "domain:cdnvideo.ru", "domain:ngenix.net",
]
DEFAULT_DIRECT_IPS = ["geoip:private", "geoip:ru"]
# Yandex + Rostelecom resolvers: answers that place you on the right RU CDN edge.
DEFAULT_RU_DNS = ["77.88.8.8", "77.88.8.1"]


@dataclass
class Defaults:
    ssh_key: str = "~/.ssh/id_ed25519"
    ssh_user: str = "root"
    ssh_port: int = 2222
    xray_version: str = "latest"
    drain_seconds: int = 120
    connect_timeout: int = 20
    bootstrap_timeout: int = 900


@dataclass
class SplitPolicy:
    direct_domains: list[str] = field(default_factory=lambda: list(DEFAULT_DIRECT_DOMAINS))
    direct_ips: list[str] = field(default_factory=lambda: list(DEFAULT_DIRECT_IPS))
    block_domains: list[str] = field(default_factory=list)
    ru_dns: list[str] = field(default_factory=lambda: list(DEFAULT_RU_DNS))
    enabled: bool = True


@dataclass
class EntrySpec:
    name: str = "entry-ru"
    provider: str = "manual"
    host: str = ""
    port: int = 443
    reality_dest: str = ""
    reality_sni: list[str] = field(default_factory=list)
    xhttp_path: str = "/api/v1/update"
    xhttp_host: str = ""
    xhttp_mode: str = "stream-one"
    fingerprint: str = "firefox"
    transport: str = "tcp"
    provider_opts: dict[str, Any] = field(default_factory=dict)
    split: SplitPolicy = field(default_factory=SplitPolicy)


@dataclass
class ProviderSpec:
    #: the driver to use (hetzner, vultr, sporestack, …)
    name: str
    #: identity for diversity purposes. Two entries can share a driver but reach very
    #: different networks — SporeStack fronting its own Amsterdam metal is not the same
    #: ASN as SporeStack reselling DigitalOcean — so they must count as separate
    #: providers when fleet is spreading exits across autonomous systems.
    label: str = ""
    weight: int = 1
    regions: list[str] = field(default_factory=list)
    server_type: str = ""
    image: str = ""
    opts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitPolicy:
    #: acknowledge the sing-box/XHTTP incompatibility and use XHTTP anyway
    opts_allow_xhttp: bool = False
    pool_size: int = 2
    #: Hard ceiling on how many exits may exist at once, across every provider.
    #: 0 means "auto" = 2x pool_size, which leaves room for make-before-break
    #: rotation while still capping a runaway. A provisioning loop that ignores
    #: this can bill you indefinitely: a bug or a rotation storm between two
    #: deployments creates servers as fast as the API allows. This is the
    #: backstop that makes such a bug cost cents instead of a month's rent.
    max_total_exits: int = 0
    max_age_hours: int = 72
    min_age_hours: int = 6
    #: Runway bought beyond max_age on self-expiring providers (SporeStack). This is
    #: how long the fleet survives with the orchestrator switched off. Too small and a
    #: laptop closed over a long weekend takes the whole VPN down with it.
    expiry_buffer_hours: int = 120
    port: int = 443
    reality_dest: str = "www.wikipedia.org:443"
    reality_sni: list[str] = field(default_factory=lambda: ["www.wikipedia.org"])
    xhttp_path: str = "/api/v1/update"
    xhttp_mode: str = "stream-one"
    fingerprint: str = "firefox"
    transport: str = "tcp"
    providers: list[ProviderSpec] = field(default_factory=list)


@dataclass
class RouterSpec:
    """The router on the same LAN as the orchestrator, if fleet should update it."""
    enabled: bool = False
    host: str = ""            # user@host, e.g. root@192.168.8.1
    ssh_port: int = 22
    ssh_key: str = ""
    client: str = "beryl"     # which fleet client this router uses
    strict_host_key: bool = False
    #: LAN CIDR whose traffic podkop sends through the tunnel (`fully_routed_ips`).
    #: Empty = derive a /24 from `host`. Everything from here is tunnelled EXCEPT
    #: the RU exclusion list, which is the inverse of podkop's usual opt-in model.
    lan_subnet: str = ""
    #: Where the RU direct-list is written on the router.
    ru_list_path: str = "/etc/podkop-ru-direct.lst"
    #: Keep the orchestrator itself OUT of the tunnel. Essential, not cosmetic: the
    #: orchestrator sits inside `fully_routed_ips`, so without this its probes to an
    #: exit travel through the *other* exit — `fleet health` then measures the tunnel
    #: rather than the ISP, and `provision_exit_with_reroll` cannot see that a new
    #: exit landed in a blocked range. See HANDOFF #53.
    exclude_orchestrator: bool = True
    #: Proxy for fleet's OWN HTTP calls (provider APIs, RIPEstat, blocklist).
    #: Why this exists: the orchestrator is deliberately outside the tunnel so its
    #: reachability probes measure the ISP rather than the tunnel (#53). But TSPU
    #: freezes TLS uploads past ~12-20 KB, and a server-create request carries ~11 KB
    #: of cloud-init — so provisioning fails from the bare line while probing must
    #: stay on it. Pointing only fleet's HTTP at podkop's mixed inbound resolves
    #: both: management traffic is tunnelled, SSH probes stay direct.
    #: Enable with `mixed_proxy_enabled=1` + `mixed_proxy_port` on the router's
    #: proxy section; then set e.g. "http://192.168.8.1:1080".
    api_proxy: str = ""
    #: LAN address to exclude. Set it explicitly: auto-detection uses whichever
    #: machine happens to run `fleet`, so running it from a laptop would quietly
    #: take the laptop out of the tunnel instead of the orchestrator.
    orchestrator_ip: str = ""


@dataclass
class Inventory:
    defaults: Defaults
    entry: EntrySpec
    exits: ExitPolicy
    clients: list[str]
    root: str
    path: str
    #: "cascade" — clients → RU entry node → rotating foreign exit.
    #: "direct"  — clients → rotating foreign exit, no Russian node at all.
    mode: str = "cascade"
    router: "RouterSpec" = field(default_factory=RouterSpec)

    @property
    def cascade(self) -> bool:
        return self.mode == "cascade"

    @property
    def site(self) -> str:
        """Site name, taken from the inventory filename.

        `inventory.toml` is the unnamed default; anything else names a site. This is
        what keeps several sites in one checkout from sharing state — without it,
        `site-b.toml` and `site-c.toml` in the same directory would both write
        `state/fleet.json` and silently destroy each other's fleet.
        """
        stem = os.path.splitext(os.path.basename(self.path))[0]
        return "" if stem == "inventory" else stem

    @property
    def state_dir(self) -> str:
        base = os.path.join(self.root, "state")
        return os.path.join(base, self.site) if self.site else base

    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, "fleet.json")

    @property
    def known_hosts(self) -> str:
        return os.path.join(self.state_dir, "known_hosts")

    def provider_by_name(self, name: str) -> ProviderSpec | None:
        for p in self.exits.providers:
            if name in (p.label, p.name):
                return p
        return None


def _typed(d: dict, key: str, default, kind):
    val = d.get(key, default)
    if val is None:
        return default
    if kind is list and not isinstance(val, list):
        raise FleetError(f"inventory: `{key}` must be a list")
    if kind is int and not isinstance(val, int):
        raise FleetError(f"inventory: `{key}` must be an integer")
    if kind is str and not isinstance(val, str):
        raise FleetError(f"inventory: `{key}` must be a string")
    return val


def load(path: str | None = None) -> Inventory:
    path = path or os.environ.get("FLEET_INVENTORY") or DEFAULT_INVENTORY
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise FleetError(
            f"no inventory at {path}\n"
            "  copy inventory.example.toml to inventory.toml and edit it"
        )
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    root = os.path.dirname(path)
    d = raw.get("defaults", {})
    defaults = Defaults(
        ssh_key=_typed(d, "ssh_key", Defaults.ssh_key, str),
        ssh_user=_typed(d, "ssh_user", Defaults.ssh_user, str),
        ssh_port=_typed(d, "ssh_port", Defaults.ssh_port, int),
        xray_version=_typed(d, "xray_version", Defaults.xray_version, str),
        drain_seconds=_typed(d, "drain_seconds", Defaults.drain_seconds, int),
        connect_timeout=_typed(d, "connect_timeout", Defaults.connect_timeout, int),
        bootstrap_timeout=_typed(d, "bootstrap_timeout", Defaults.bootstrap_timeout, int),
    )

    e = raw.get("entry", {})
    sp = e.get("split", {})
    split = SplitPolicy(
        direct_domains=_typed(sp, "direct_domains", DEFAULT_DIRECT_DOMAINS, list),
        direct_ips=_typed(sp, "direct_ips", DEFAULT_DIRECT_IPS, list),
        block_domains=_typed(sp, "block_domains", [], list),
        ru_dns=_typed(sp, "ru_dns", DEFAULT_RU_DNS, list),
        enabled=bool(sp.get("enabled", True)),
    )
    sni = _typed(e, "reality_sni", [], list)
    dest = _typed(e, "reality_dest", "", str)
    if dest and not sni:
        sni = [dest.rsplit(":", 1)[0]]
    entry = EntrySpec(
        name=_typed(e, "name", "entry-ru", str),
        provider=_typed(e, "provider", "manual", str),
        host=_typed(e, "host", "", str),
        port=_typed(e, "port", 443, int),
        reality_dest=dest,
        reality_sni=sni,
        xhttp_path=_typed(e, "xhttp_path", "/api/v1/update", str),
        xhttp_host=_typed(e, "xhttp_host", sni[0] if sni else "", str),
        xhttp_mode=_typed(e, "xhttp_mode", "stream-one", str),
        fingerprint=_typed(e, "fingerprint", "firefox", str),
        transport=_typed(e, "transport", "tcp", str),
        provider_opts={k: v for k, v in e.items() if k.startswith("provider_")},
        split=split,
    )

    x = raw.get("exits", {})
    providers = []
    for p in x.get("providers", []):
        if "name" not in p:
            raise FleetError("inventory: each [[exits.providers]] needs a `name`")
        providers.append(
            ProviderSpec(
                name=p["name"],
                label=str(p.get("label") or
                          (f"{p['name']}-{p['upstream_provider']}"
                           if p.get("upstream_provider") else p["name"])),
                weight=int(p.get("weight", 1)),
                regions=list(p.get("regions", [])),
                server_type=str(p.get("server_type", "")),
                image=str(p.get("image", "")),
                opts={
                    k: v
                    for k, v in p.items()
                    if k not in ("name", "label", "weight", "regions", "server_type",
                                 "image")
                },
            )
        )
    ex_sni = _typed(x, "reality_sni", [], list)
    ex_dest = _typed(x, "reality_dest", ExitPolicy.reality_dest, str)
    if not ex_sni:
        ex_sni = [ex_dest.rsplit(":", 1)[0]]
    exits = ExitPolicy(
        opts_allow_xhttp=bool(x.get("allow_xhttp", False)),
        pool_size=_typed(x, "pool_size", 2, int),
        max_age_hours=_typed(x, "max_age_hours", 72, int),
        min_age_hours=_typed(x, "min_age_hours", 6, int),
        expiry_buffer_hours=_typed(x, "expiry_buffer_hours", 120, int),
        port=_typed(x, "port", 443, int),
        reality_dest=ex_dest,
        reality_sni=ex_sni,
        xhttp_path=_typed(x, "xhttp_path", "/api/v1/update", str),
        xhttp_mode=_typed(x, "xhttp_mode", "stream-one", str),
        fingerprint=_typed(x, "fingerprint", "firefox", str),
        transport=_typed(x, "transport", "tcp", str),
        providers=providers,
    )

    clients = [c["name"] if isinstance(c, dict) else str(c) for c in raw.get("clients", [])]

    mode = _typed(raw, "mode", "cascade", str)
    if mode not in ("cascade", "direct"):
        raise FleetError(f"mode must be \"cascade\" or \"direct\", not {mode!r}")

    rt = raw.get("router", {})
    router = RouterSpec(
        enabled=bool(rt.get("enabled", bool(rt.get("host")))),
        host=_typed(rt, "host", "", str),
        ssh_port=_typed(rt, "ssh_port", 22, int),
        ssh_key=_typed(rt, "ssh_key", "", str),
        client=_typed(rt, "client", "beryl", str),
        strict_host_key=bool(rt.get("strict_host_key", False)),
        lan_subnet=_typed(rt, "lan_subnet", "", str),
        ru_list_path=_typed(rt, "ru_list_path", "/etc/podkop-ru-direct.lst", str),
        exclude_orchestrator=bool(rt.get("exclude_orchestrator", True)),
        orchestrator_ip=_typed(rt, "orchestrator_ip", "", str),
        api_proxy=_typed(rt, "api_proxy", "", str),
    )

    inv = Inventory(
        defaults=defaults, entry=entry, exits=exits, clients=clients, root=root,
        path=path, mode=mode, router=router,
    )
    _validate(inv)
    return inv


def _validate(inv: Inventory) -> None:
    problems = []
    if inv.cascade:
        if not inv.entry.host and inv.entry.provider == "manual":
            problems.append("entry.host is empty — set the IP of your hand-bought RU VPS")
        if not inv.entry.reality_dest:
            problems.append("entry.reality_dest is empty (e.g. \"corp.ozon.ru:443\")")
        if ":" not in inv.entry.reality_dest and inv.entry.reality_dest:
            problems.append("entry.reality_dest must include a port, e.g. \"example.ru:443\"")
        if inv.entry.xhttp_mode not in ("auto", "packet-up", "stream-up", "stream-one"):
            problems.append(
                f"entry.xhttp_mode `{inv.entry.xhttp_mode}` is not a valid XHTTP mode")
    else:
        # In direct mode clients hold every exit's address, so a single exit means a
        # rotation is a hard cutover for every device. Two is the minimum that lets
        # the client's own balancer carry you across a replacement.
        if inv.exits.pool_size < 2:
            problems.append(
                "mode = \"direct\" with exits.pool_size < 2: rotating the only exit "
                "breaks every client at once. Use pool_size >= 2 so the client-side "
                "balancer has somewhere to fail over to.")
    if not inv.exits.providers:
        problems.append("no [[exits.providers]] configured — nowhere to create exits")
    labels = [p.label for p in inv.exits.providers]
    dupes = {x for x in labels if labels.count(x) > 1}
    if dupes:
        problems.append(
            f"duplicate provider label(s) {sorted(dupes)} — give each "
            "[[exits.providers]] entry a distinct `label` so fleet can tell them apart")
    if inv.exits.pool_size < 1:
        problems.append("exits.pool_size must be >= 1")
    if inv.exits.min_age_hours >= inv.exits.max_age_hours:
        problems.append("exits.min_age_hours must be < exits.max_age_hours")
    if inv.exits.expiry_buffer_hours < 24:
        problems.append(
            "exits.expiry_buffer_hours < 24: on a self-expiring provider the fleet "
            "would evaporate within a day of the orchestrator going quiet")
    for label, t in (("entry", inv.entry.transport), ("exits", inv.exits.transport)):
        if t not in ("tcp", "xhttp"):
            problems.append(f"{label}.transport must be \"tcp\" or \"xhttp\", not {t!r}")
    if inv.exits.transport == "xhttp":
        problems.append(
            "exits.transport = \"xhttp\" — note that upstream sing-box does not implement "
            "XHTTP, so podkop and most OpenWrt/mobile tooling built on sing-box cannot "
            "connect. Use \"tcp\" (XTLS-Vision) unless every client runs xray-core. "
            "Set exits.allow_xhttp = true to silence this.")
    if inv.exits.opts_allow_xhttp:
        problems = [x for x in problems if "sing-box does not implement" not in x]
    # The mistake in the reference config: a foreign node masking as a .ru host.
    ex_host = inv.exits.reality_sni[0] if inv.exits.reality_sni else ""
    if ex_host.endswith(".ru"):
        problems.append(
            f"exits.reality_sni is `{ex_host}` — a foreign exit presenting a .ru SNI is an "
            "SNI/IP-geography mismatch and the dest may be unreachable from abroad. "
            "See docs/02-architecture.md §1."
        )
    if problems:
        raise FleetError("inventory problems:\n  - " + "\n  - ".join(problems))
