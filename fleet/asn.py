"""ASN lookup and RU-blocklist checking for freshly allocated IPs.

The premise, which is the whole point of this module: **you cannot pick a good
provider once and be done.** RKN works at AS granularity — a February 2026 sweep put
391 autonomous systems and >225 million addresses behind throttling, naming AS14061
(DigitalOcean), AS24940 (Hetzner), AS16509 (AWS), AS16276 (OVH), AS13335 (Cloudflare,
partial) and Oracle's ranges. Any hand-maintained "good hosts" list is stale in weeks.

So don't guess — measure, at the moment an address is allocated and before anything is
deployed onto it. Cloud providers hand out addresses from large pools, so an IP that
lands in blocked space can often be swapped simply by destroying and re-rolling, which
costs seconds and pennies and is much cheaper than discovering it from Russia later.

**A warning about the aggregate blocklist.** `russia-blocked-ips` merges ~146 sources,
including whole cloud and CDN allocations that are *candidates* for sweeping rather
than confirmed-blocked. Measured here, it flags 1.1.1.1, 8.8.8.8 and most of Hetzner —
useful as "this address sits in a range somebody has listed", useless as a hard gate,
because gating on it would reject essentially every cloud IP you can rent.

Treat everything in this module as **advisory context**. The authoritative test is a
live probe from inside Russia, which `ops.reachable_from_entry()` does over SSH on the
entry node. That is ground truth for the only question that matters: can the clients
actually reach this box.
"""
from __future__ import annotations

import ipaddress
import json
import os
import time
from bisect import bisect_right

from .util import FleetError, debug, http, warn

RIPESTAT_NET = "https://stat.ripe.net/data/network-info/data.json?resource={ip}"
RIPESTAT_AS = "https://stat.ripe.net/data/as-overview/data.json?resource=AS{asn}"
BLOCKLIST_URL = "https://raw.githubusercontent.com/eduard256/russia-blocked-ips/main/ip.txt"
MANIFEST_URL = "https://raw.githubusercontent.com/eduard256/russia-blocked-ips/main/manifest.json"

#: Datacenter ASNs named in RKN's AS-level actions. Landing here is not fatal — the
#: filtering is regional and inconsistent — but it means you are in the population
#: being targeted rather than outside it.
HOSTILE_ASNS: dict[int, str] = {
    14061: "DigitalOcean",
    24940: "Hetzner",
    16509: "AWS",
    14618: "AWS",
    16276: "OVH",
    20473: "Vultr / Choopa",
    13335: "Cloudflare (partial)",
    31898: "Oracle Cloud",
    8100:  "QuadraNet",
    63949: "Akamai / Linode",
    16625: "Akamai",
    15169: "Google Cloud",
    8075:  "Microsoft Azure",
    51167: "Contabo",
}


# ------------------------------------------------------------------ ASN lookup

def lookup(ip: str) -> dict:
    """Return {'asn': int|None, 'prefix': str, 'holder': str} for an address."""
    out: dict = {"asn": None, "prefix": "", "holder": ""}
    try:
        data = (http("GET", RIPESTAT_NET.format(ip=ip), timeout=25) or {}).get("data", {})
        asns = data.get("asns") or []
        out["prefix"] = data.get("prefix", "") or ""
        if asns:
            out["asn"] = int(asns[0])
    except (FleetError, ValueError, TypeError) as e:
        debug(f"RIPEstat network-info failed for {ip}: {e}")
        return out

    if out["asn"]:
        try:
            ov = (http("GET", RIPESTAT_AS.format(asn=out["asn"]), timeout=25) or {}).get("data", {})
            out["holder"] = ov.get("holder", "") or ""
        except FleetError:
            out["holder"] = HOSTILE_ASNS.get(out["asn"], "")
    return out


def hostile(asn: int | None) -> str:
    return HOSTILE_ASNS.get(asn or -1, "")


# ------------------------------------------------------------------ blocklist

class Blocklist:
    """The aggregated RU blocklist, cached on disk and queried by bisect.

    ~700 KB of CIDRs from ~146 sources (RKN's own dumps plus cloud/CDN ranges),
    refreshed upstream every 6 hours.
    """

    def __init__(self, cache_dir: str, ttl_hours: int = 6):
        self.path = os.path.join(cache_dir, "ru-blocklist.txt")
        self.ttl = ttl_hours * 3600
        self._v4: list[tuple[int, int]] = []
        self._v6: list[tuple[int, int]] = []
        self._starts4: list[int] = []
        self._starts6: list[int] = []
        self.count = 0
        self.age_hours: float | None = None

    # ---------------------------------------------------------------- fetch

    def _stale(self) -> bool:
        if not os.path.exists(self.path):
            return True
        return (time.time() - os.path.getmtime(self.path)) > self.ttl

    def refresh(self, force: bool = False) -> bool:
        """Download the list if the cache is stale. Returns True if it was updated."""
        if not force and not self._stale():
            return False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            import urllib.request
            req = urllib.request.Request(BLOCKLIST_URL, headers={"User-Agent": "fleet/0.1"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
        except Exception as e:  # network of any shape
            if os.path.exists(self.path):
                warn(f"could not refresh the RU blocklist ({e}); using the cached copy")
                return False
            raise FleetError(f"could not fetch the RU blocklist: {e}") from None
        tmp = self.path + ".new"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, self.path)
        return True

    # ----------------------------------------------------------------- load

    def load(self) -> "Blocklist":
        self.refresh()
        if not os.path.exists(self.path):
            raise FleetError("no RU blocklist cached and none could be fetched")
        v4: list[tuple[int, int]] = []
        v6: list[tuple[int, int]] = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    net = ipaddress.ip_network(line, strict=False)
                except ValueError:
                    continue
                span = (int(net.network_address), int(net.broadcast_address))
                (v4 if net.version == 4 else v6).append(span)
        # Merge overlaps so a bisect on start points is a correct containment test.
        self._v4 = _merge(v4)
        self._v6 = _merge(v6)
        self._starts4 = [a for a, _ in self._v4]
        self._starts6 = [a for a, _ in self._v6]
        self.count = len(self._v4) + len(self._v6)
        self.age_hours = (time.time() - os.path.getmtime(self.path)) / 3600
        return self

    # ---------------------------------------------------------------- query

    def contains(self, ip: str) -> str:
        """Return the matching CIDR if the address is listed, else ''."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        value = int(addr)
        spans, starts = ((self._v4, self._starts4) if addr.version == 4
                         else (self._v6, self._starts6))
        i = bisect_right(starts, value) - 1
        if i < 0:
            return ""
        lo, hi = spans[i]
        if lo <= value <= hi:
            return f"{ipaddress.ip_address(lo)}-{ipaddress.ip_address(hi)}"
        return ""


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans.sort()
    out = [spans[0]]
    for lo, hi in spans[1:]:
        plo, phi = out[-1]
        if lo <= phi + 1:
            out[-1] = (plo, max(phi, hi))
        else:
            out.append((lo, hi))
    return out


# ------------------------------------------------------------------ verdict

def assess(ip: str, cache_dir: str, *, use_blocklist: bool = True) -> dict:
    """Everything worth knowing about a freshly allocated address."""
    info = lookup(ip)
    verdict = {
        "ip": ip,
        "asn": info["asn"],
        "prefix": info["prefix"],
        "holder": info["holder"],
        "hostile_asn": hostile(info["asn"]),
        "blocked_range": "",
        "blocklist_age_hours": None,
        "clean": True,
        "reasons": [],
        #: advisory only — see the module docstring. Never gate provisioning on this.
        "advisory": True,
    }
    if verdict["hostile_asn"]:
        verdict["reasons"].append(
            f"AS{info['asn']} ({verdict['hostile_asn']}) is named in RKN's AS-level actions")
        verdict["clean"] = False

    if use_blocklist:
        try:
            bl = Blocklist(cache_dir).load()
            verdict["blocklist_age_hours"] = bl.age_hours
            hit = bl.contains(ip)
            if hit:
                verdict["blocked_range"] = hit
                verdict["reasons"].append(f"address falls inside a listed range ({hit})")
                verdict["clean"] = False
        except FleetError as e:
            warn(f"blocklist check skipped: {e}")
    return verdict
