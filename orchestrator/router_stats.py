#!/usr/bin/env python3
"""Collect tunnel/DNS/podkop stats from the router for the LAN panel.

Two sources, deliberately:

  * **sing-box's Clash API** over plain HTTP on the LAN. podkop enables it by
    default (`external_controller`), and it is the only place that knows which exit
    the urltest selector actually picked, what each exit's measured latency is, and
    how many bytes have moved. No SSH, so it is fast enough to poll often.
  * **one SSH call** for the things the API cannot see: whether podkop installed its
    nftables rules, whether the local resolver is alive, which DNS the router uses.

Samples land in **tmpfs**, not on the SD card. A sample every few minutes is ~100 k
writes a year, and this project has already lost one card — see HANDOFF #43, where
the likely culprit was undervoltage during writes. Losing history on reboot is a fair
trade for not eating the rootfs.

stdlib only, like the rest of fleet.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

#: tmpfs on any systemd box; falls back to /tmp if /run is not writable.
STATS_DIR = "/run/vpncascade"
STATS_FILE = "stats.jsonl"
#: 288 samples = 24 h at 5-minute intervals. Bounded so the file cannot grow.
MAX_SAMPLES = 288
HTTP_TIMEOUT = 6
SSH_TIMEOUT = 15


def _stats_path() -> str:
    d = STATS_DIR
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".w")
        with open(probe, "w") as fh:
            fh.write("")
        os.unlink(probe)
    except OSError:
        d = "/tmp/vpncascade"
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, STATS_FILE)


def _get_json(url: str):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def clash_snapshot(base: str) -> dict:
    """Live tunnel state from sing-box. Empty dict if the router is unreachable."""
    out: dict = {"reachable": False, "exits": [], "selected": "",
                 "conns": 0, "up_total": 0, "down_total": 0}

    proxies = _get_json(f"{base}/proxies")
    if proxies is None:
        return out
    out["reachable"] = True

    p = proxies.get("proxies", {})
    for name, v in p.items():
        # The VLESS outbounds are the exits; everything else is plumbing.
        if v.get("type") != "VLESS":
            continue
        hist = v.get("history") or []
        out["exits"].append({
            "name": name,
            # 0 in Clash's history means "probe failed", which is not the same as
            # "0 ms" — surface it as unreachable rather than as a suspiciously
            # excellent latency.
            "delay_ms": (hist[-1].get("delay") or 0) if hist else None,
        })
    out["exits"].sort(key=lambda e: e["name"])

    # Which one the urltest actually chose. `now` on the urltest node is the answer;
    # the selector above it just points at the urltest.
    for name, v in p.items():
        if v.get("type") == "URLTest" and v.get("now"):
            out["selected"] = v["now"]
            break

    conns = _get_json(f"{base}/connections") or {}
    rows = conns.get("connections") or []
    out["conns"] = len(rows)
    out["up_total"] = int(conns.get("uploadTotal") or 0)
    out["down_total"] = int(conns.get("downloadTotal") or 0)

    # Split by which outbound actually carried it. chains[0] is the leaf: the
    # exclusion section routes RU destinations to `direct-out`, everything else
    # goes to a VLESS exit. This is the honest answer to "is the split working",
    # measured from real traffic rather than from config.
    #
    # NOTE: counts and bytes only. Hostnames are deliberately NOT returned here,
    # because this dict is what gets persisted — and a rolling list of every site
    # the household visits is exactly the artefact this project exists to avoid.
    # The panel reads hostnames live, for display, and never stores them.
    d_n = t_n = 0
    d_up = d_dn = t_up = t_dn = 0
    for c in rows:
        ch = (c.get("chains") or [""])[0]
        up, dn = int(c.get("upload") or 0), int(c.get("download") or 0)
        if ch == "direct-out":
            d_n += 1; d_up += up; d_dn += dn
        else:
            t_n += 1; t_up += up; t_dn += dn
    out.update(direct_conns=d_n, tunnel_conns=t_n,
               direct_bytes=d_up + d_dn, tunnel_bytes=t_up + t_dn)
    # Connection ids, classified. Used to count each connection ONCE across
    # samples: RU page loads finish in well under the polling interval, so
    # instantaneous counts almost never see them and the split looks like 0%
    # direct even when it is working. Ids are opaque UUIDs — no host data.
    out["_ids"] = {str(c.get("id")): ("direct" if (c.get("chains") or [""])[0] == "direct-out"
                                      else "tunnel")
                   for c in rows if c.get("id")}
    return out


def live_hosts(base: str, limit: int = 8) -> dict:
    """Current destinations, split direct vs tunnelled. NEVER persisted.

    Read on page load and discarded. See the note in clash_snapshot: aggregate
    counts are safe to keep, a history of hostnames is not.
    """
    out = {"direct": [], "tunnel": []}
    conns = _get_json(f"{base}/connections") or {}
    agg: dict = {}
    for c in conns.get("connections") or []:
        md = c.get("metadata") or {}
        host = md.get("host") or md.get("destinationIP") or "?"
        ch = (c.get("chains") or [""])[0]
        key = ("direct" if ch == "direct-out" else "tunnel", host)
        agg[key] = agg.get(key, 0) + int(c.get("upload") or 0) + int(c.get("download") or 0)
    for (kind, host), b in sorted(agg.items(), key=lambda kv: -kv[1]):
        if len(out[kind]) < limit:
            out[kind].append({"host": host, "bytes": b})
    return out


def router_probe(ssh_target: str, ssh_key: str = "") -> dict:
    """The bits the Clash API cannot see. One SSH round trip, fail-soft."""
    out = {"ok": False, "singbox": 0, "nft_rules": 0, "dnsproxy": 0,
           "dns_type": "", "dns_server": "", "podkop": ""}
    if not ssh_target:
        return out
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
           "-o", "StrictHostKeyChecking=accept-new"]
    if ssh_key:
        cmd += ["-i", os.path.expanduser(ssh_key)]
    remote = (
        "ps w | grep -c '[s]ing-box'; "
        "nft list ruleset 2>/dev/null | grep -c podkop; "
        "ps w | grep -c '[d]nsproxy'; "
        "uci -q get podkop.settings.dns_type; "
        "uci -q get podkop.settings.dns_server; "
        "podkop show_version 2>/dev/null | head -1; "
        # The exit list, in the order podkop feeds it to sing-box. That order IS
        # the outbound numbering: index N becomes `main-N-out`. Verified against a
        # live router by pinning a tag via the Clash API and watching which exit
        # the traffic actually left from.
        "uci -q get podkop.main.urltest_proxy_links; "
        "uci -q get podkop.main.proxy_string"
    )
    try:
        r = subprocess.run(cmd + [ssh_target, remote], capture_output=True,
                           text=True, timeout=SSH_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return out
    lines = (r.stdout or "").splitlines()
    def _i(n):
        try:
            return int(lines[n].strip())
        except (IndexError, ValueError):
            return 0
    def _s(n):
        try:
            return lines[n].strip()
        except IndexError:
            return ""
    out.update(ok=r.returncode == 0, singbox=_i(0), nft_rules=_i(1),
               dnsproxy=_i(2), dns_type=_s(3), dns_server=_s(4), podkop=_s(5))
    out["outbound_names"] = _outbound_names(_s(6) + " " + _s(7))
    return out


def _outbound_names(uri_blob: str) -> dict[str, str]:
    """Map sing-box's `main-N-out` tags to the exit names they actually are.

    podkop numbers outbounds by position in its URI list and throws the `#fragment`
    away, so the Clash API reports `main-1-out` where a human wants
    `exit-hetzne-sin-6ae24d`. The fragment we generate is `<exit-name>-<client>`,
    so the exit name is everything before the last dash.

    Falls back to `host:port` when a URI has no fragment, and to the bare tag when
    there is nothing at all — a missing label must never hide a live exit from the
    panel.
    """
    import re as _re
    from urllib.parse import unquote as _unq, urlparse as _up

    names: dict[str, str] = {}
    for i, uri in enumerate(_re.findall(r"vless://[^\s'\"]+", uri_blob or ""), start=1):
        tag = f"main-{i}-out"
        frag = _unq(_up(uri).fragment or "")
        if frag:
            names[tag] = frag.rsplit("-", 1)[0] if "-" in frag else frag
        else:
            p = _up(uri)
            names[tag] = f"{p.hostname or '?'}:{p.port or '?'}"
    return names


def collect(clash_base: str, ssh_target: str, ssh_key: str = "") -> dict:
    s = {"t": int(time.time())}
    s.update(clash_snapshot(clash_base))
    s["router"] = router_probe(ssh_target, ssh_key)

    # Attach human names to the outbound tags, so the panel can say
    # "exit-upclou-sg-sin1-b8df9c" instead of "main-2-out".
    names = (s["router"] or {}).get("outbound_names") or {}
    for e in s.get("exits", []):
        e["label"] = names.get(e["name"], e["name"])
    s["selected_label"] = names.get(s.get("selected", ""), s.get("selected", ""))
    return s


COUNTER_FILE = "counters.json"


def _counter_path() -> str:
    return os.path.join(os.path.dirname(_stats_path()), COUNTER_FILE)


def bump_counters(sample: dict) -> dict:
    """Fold this sample's connection ids into cumulative direct/tunnel totals.

    Keeps a bounded set of ids already counted, so a connection that survives
    several polls is not counted twice. Ids only — never hostnames.
    """
    path = _counter_path()
    try:
        with open(path) as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        c = {"direct": 0, "tunnel": 0, "since": int(time.time()), "seen": []}
    seen = set(c.get("seen") or [])
    for cid, kind in (sample.get("_ids") or {}).items():
        if cid in seen:
            continue
        seen.add(cid)
        c[kind] = int(c.get(kind, 0)) + 1
    # Bound the id set; oldest-first is fine because ids age out of the API anyway.
    c["seen"] = list(seen)[-4000:]
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(c, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return c


def counters() -> dict:
    try:
        with open(_counter_path()) as fh:
            c = json.load(fh)
        c.pop("seen", None)
        return c
    except (OSError, ValueError):
        return {"direct": 0, "tunnel": 0, "since": 0}


def append(sample: dict) -> None:
    """Append one sample, trimming to MAX_SAMPLES. Never raises."""
    bump_counters(sample)
    sample = {k: v for k, v in sample.items() if k != "_ids"}   # never persist ids
    path = _stats_path()
    try:
        rows = load()
        rows.append(sample)
        rows = rows[-MAX_SAMPLES:]
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass


def load() -> list[dict]:
    try:
        with open(_stats_path()) as fh:
            out = []
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
            return out
    except OSError:
        return []


def deltas(rows: list[dict]) -> list[dict]:
    """Per-interval byte deltas.

    The Clash totals are cumulative since sing-box started, so a restart makes them
    go backwards. Treat any decrease as a counter reset and drop that interval
    rather than reporting a negative or a wild spike.
    """
    out = []
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        up = b.get("up_total", 0) - a.get("up_total", 0)
        dn = b.get("down_total", 0) - a.get("down_total", 0)
        if up < 0 or dn < 0:
            continue  # sing-box restarted
        out.append({"t": b["t"], "dt": dt, "up": up, "down": dn,
                    "up_bps": up * 8 / dt, "down_bps": dn * 8 / dt})
    return out


def human_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def human_bps(n: float) -> str:
    for u in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if abs(n) < 1000 or u == "Gbit/s":
            return f"{n:.0f} {u}" if u == "bit/s" else f"{n:.1f} {u}"
        n /= 1000
    return f"{n:.1f} Gbit/s"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="collect one router stats sample")
    ap.add_argument("--clash", default="http://192.168.8.1:9090")
    ap.add_argument("--router", default="root@192.168.8.1")
    ap.add_argument("--ssh-key", default="")
    ap.add_argument("--store", action="store_true", help="append to the ring buffer")
    a = ap.parse_args()
    sample = collect(a.clash, a.router, a.ssh_key)
    if a.store:
        append(sample)
    print(json.dumps(sample, indent=2))
