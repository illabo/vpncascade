"""Orchestration: provision, rotate, reap, sync, health."""
from __future__ import annotations

import os
import random
import time
from typing import Any

from . import asn as asnmod
from . import keys, providers, render
from . import router as routermod
from .config import Inventory, ProviderSpec
from .deploy import (
    bootstrap_script,
    push_bootstrap,
    cloud_init,
    wait_reachable,
    pin_host_key,
    push_xray_config,
    resolve_xray_version,
    ssh_run,
    unpin_host_key,
    wait_ssh,
)
from .state import State
from .util import FleetError, fail, human_age, log, now, ok, step, warn


class UnreachableExit(FleetError):
    """Provisioned fine, but nothing answers from our vantage point — re-roll it."""


# ------------------------------------------------------------------ selection

def choose_provider(inv: Inventory, state: State, want: str | None = None) -> ProviderSpec:
    """Pick where the next exit goes.

    Diversity is not a preference here, it is the defence: RKN sweeps at AS
    granularity, so two exits sharing an ASN die together. An earlier version merely
    *weighted* towards an unused provider, which with two providers configured still
    left a coin-flip chance of stacking both exits on one of them. This version picks
    strict tiers and only falls through when a tier is empty:

      1. providers whose known ASN does not collide with a live exit's
      2. providers not currently in use  (ASN unknown — still better than reuse)
      3. anything

    Tier 1 exists because "different provider" and "different ASN" are not the same
    thing: resellers share upstreams, which is how one Dutch raid took out eight
    apparently independent hosts at once. See docs/06-choosing-providers.md.
    Configured weights apply *within* the chosen tier, so they still steer cost and
    preference — they just cannot override diversity.
    """
    if want:
        spec = inv.provider_by_name(want)
        if not spec:
            raise FleetError(f"provider `{want}` is not in inventory.toml [[exits.providers]]")
        return spec

    live = state.live_exits()
    in_use_names = {n.get("provider_label") or n["provider"] for n in live}
    in_use_asns = {n.get("asn") for n in live if n.get("asn")}
    known = state.data.get("provider_asns", {})   # provider -> [asns seen before]

    def clean(p: ProviderSpec) -> bool:
        return not (set(known.get(p.label, [])) & in_use_asns)

    unused = [p for p in inv.exits.providers if p.label not in in_use_names]
    for tier in ([p for p in unused if clean(p)], unused, list(inv.exits.providers)):
        if tier:
            pool: list[ProviderSpec] = []
            for p in tier:
                pool += [p] * max(1, p.weight)
            return random.choice(pool)
    raise FleetError("no providers configured in [[exits.providers]]")


def _remember_asn(state: State, provider: str, asn: int | None) -> None:
    """Record which ASNs a provider has handed us, so diversity can be judged by AS
    rather than by brand name."""
    if not asn:
        return
    seen = state.data.setdefault("provider_asns", {}).setdefault(provider, [])
    if asn not in seen:
        seen.append(asn)


def _slug(label: str) -> str:
    """Compress a provider label into something short but still distinguishing.

    Naive truncation turns both `sporestack-sporestack_eu` and
    `sporestack-slowservers` into "sporesta", which defeats the point of having
    separate labels. Keep the head and the tail instead: spo-eu, spo-slowse, het.
    """
    import re
    parts = [x for x in re.split(r"[-_]+", label) if x]
    if not parts:
        return "exit"
    if len(parts) == 1:
        return parts[0][:6]
    return f"{parts[0][:3]}-{parts[-1][:6]}"


def _exit_masking(inv: Inventory, pspec: ProviderSpec) -> tuple[str, str]:
    """(dest, sni) for an exit — per-provider override, else the global default.

    Worth setting per provider: REALITY proxies unauthenticated handshakes to the real
    dest, so the dest must be reachable *from that node* and plausible for its location.
    """
    dest = pspec.opts.get("reality_dest") or inv.exits.reality_dest
    sni = pspec.opts.get("reality_sni") or inv.exits.reality_sni or dest.rsplit(":", 1)[0]
    if isinstance(sni, list):
        if not sni:
            sni = dest.rsplit(":", 1)[0]
        else:
            # Pick per exit, so rotating does not reuse one masking host forever.
            # Every exit presenting the same SNI is a correlatable signature across
            # the fleet; varying it costs nothing.
            sni = random.choice(sni)
            # dest MUST follow the chosen sni. REALITY proxies unauthenticated
            # probes to dest, so if they disagree the prober gets a certificate for
            # the wrong name — a unique, remotely-scannable tell, worse than no
            # REALITY at all. (This is the mistake in the reference config that
            # `docs/02-architecture.md` flags.)
            port = dest.rsplit(":", 1)[-1] if ":" in dest else "443"
            dest = f"{sni}:{port}"
    return dest, sni


# ----------------------------------------------------------------- provision

def exit_ceiling(inv: Inventory) -> int:
    """How many exits may exist at once. 0 in config means 2x pool_size."""
    configured = getattr(inv.exits, "max_total_exits", 0) or 0
    return configured if configured > 0 else max(2, inv.exits.pool_size * 2)


def provision_exit(inv: Inventory, state: State, *, want_provider: str | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
    # Backstop against runaway creation. Checked here because this is the ONE
    # place a paid server comes into existence — ensure_pool, rotate and the
    # re-roll path all funnel through it, so capping here cannot be bypassed by
    # adding another caller.
    ceiling = exit_ceiling(inv)
    existing = len(state.exits)
    if existing >= ceiling:
        raise FleetError(
            f"refusing to create an exit: {existing} already exist and the ceiling "
            f"is {ceiling}.\n"
            f"  This is a safety stop, not a quota — something is looping, or state "
            f"has drifted from the provider.\n"
            f"  Check `fleet status`, reconcile with the provider console, then raise "
            f"exits.max_total_exits in inventory.toml if the number is genuinely wanted."
        )

    pspec = choose_provider(inv, state, want_provider)
    driver = providers.get(pspec.name, pspec.opts)

    in_use_regions = [n["region"] for n in state.live_exits() if n["provider"] == pspec.name]
    region = driver.pick_region(pspec.regions, avoid=in_use_regions)
    name = f"exit-{_slug(pspec.label)}-{region or 'any'}-{keys.node_suffix()}"
    dest, sni = _exit_masking(inv, pspec)

    if dry_run:
        log(f"would create {name} at {pspec.name}/{region} masking as {sni}")
        return {}

    step(f"provisioning {name} ({pspec.name}, {region or 'auto'}, mask={sni})")

    xray_version = resolve_xray_version(inv.defaults.xray_version)
    reality_priv, reality_pub = keys.reality_keypair()
    host_priv, host_pub = keys.ssh_host_keypair()

    node: dict[str, Any] = {
        "id": name,
        "name": name,
        "role": "exit",
        "provider": pspec.name,
        "provider_label": pspec.label,
        "provider_id": "",
        "region": region,
        "ipv4": "",
        "ipv6": "",
        "port": inv.exits.port,
        "created_at": now(),
        "expires_at": None,
        "status": "provisioning",
        "serving": False,
        "reality_private": reality_priv,
        "reality_public": reality_pub,
        "reality_dest": dest,
        "reality_sni": sni,
        "xhttp_host": sni,
        "xhttp_path": inv.exits.xhttp_path,
        "xhttp_mode": inv.exits.xhttp_mode,
        "transport": pspec.opts.get("transport") or inv.exits.transport,
        "uplink_uuid": keys.new_uuid(),
        "uplink_short_id": keys.short_id(),
        "host_key_public": host_pub,
        "xray_version": xray_version,
        "fingerprint": inv.exits.fingerprint,
    }

    opts = dict(pspec.opts)
    if driver.self_expiring:
        # Buy the rotation window plus the configured runway, so the provider's own
        # expiry is a backstop rather than a race with cron. This number is exactly
        # how long the fleet keeps working with the orchestrator switched off.
        total = inv.exits.max_age_hours + inv.exits.expiry_buffer_hours
        opts["days"] = max(1, -(-total // 24))
    opts.setdefault("ssh_key", keys.read_ssh_pubkey(inv.defaults.ssh_key))

    user_data = cloud_init(
        inv,
        hostname=name,
        role="exit",
        xray_config=render.exit_config(inv, node, state.clients),
        xray_version=xray_version,
        host_key_private=host_priv,
        host_key_public=host_pub,
        authorized_key=keys.read_ssh_pubkey(inv.defaults.ssh_key),
        service_port=inv.exits.port,
    )

    spec = providers.ServerSpec(
        name=name,
        user_data=user_data,
        region=region,
        server_type=pspec.server_type,
        image=pspec.image,
        labels={"fleet": "exit", "role": "exit"},
        opts=opts,
    )

    handle = driver.create(spec)
    node["provider_id"] = handle.provider_id
    node["ipv4"] = handle.ipv4
    node["expires_at"] = handle.expires_at
    state.add_exit(node)
    state.save()  # persist before waiting: a crash here must not orphan a paid server
    ok(f"created {name} (provider id {handle.provider_id})")

    step("waiting for an address")
    deadline = time.time() + 600
    while not node["ipv4"] and time.time() < deadline:
        time.sleep(8)
        live = driver.get(handle.provider_id)
        if live and live.ipv4:
            node["ipv4"] = live.ipv4
            node["ipv6"] = live.ipv6
            node["region"] = live.region or node["region"]
            node["expires_at"] = live.expires_at
            state.save()
    if not node["ipv4"]:
        raise FleetError(f"{name} never got an IPv4 address; destroy it with `fleet destroy {name}`")

    verdict = asnmod.assess(node["ipv4"], inv.state_dir)
    node["asn"] = verdict["asn"]
    node["asn_holder"] = verdict["holder"]
    _remember_asn(state, pspec.label, verdict["asn"])
    state.save()
    label = f"AS{verdict['asn']} {verdict['holder']}" if verdict["asn"] else "ASN unknown"
    ok(f"{name} is {node['ipv4']} ({label})")
    for reason in verdict["reasons"]:
        # Advisory only. The aggregate list flags most of the cloud, so gating on it
        # would reject nearly every rentable address. The live probe below decides.
        warn(f"  advisory: {reason}")

    pin_host_key(inv, node["ipv4"], inv.defaults.ssh_port, host_pub)

    # Cheap check first: is this address reachable at all from where we are? A swept
    # prefix shows up here in seconds, where waiting for SSH would take twenty minutes
    # before admitting the same thing.
    try:
        step("checking the address answers from here")
        wait_reachable(inv, node["ipv4"])
        ok("address is reachable")
    except FleetError as e:
        raise UnreachableExit(
            f"{name} ({node['ipv4']}, "
            f"{('AS' + str(node.get('asn')) + ' ' + (node.get('asn_holder') or '')).strip()}) "
            f"is unreachable.\n  {e}\n"
            f"  Running fleet from inside Russia, this is almost certainly a swept range."
        ) from None

    # Providers with no cloud-init (Fornex) get their build pushed over SSH now.
    # This must sit between reachability and wait_ssh: before it, nothing is
    # listening on inv.defaults.ssh_port at all, because moving sshd there is one
    # of the things the bootstrap does.
    if getattr(driver, "needs_ssh_bootstrap", False):
        push_bootstrap(
            inv,
            node["ipv4"],
            bootstrap_script(
                inv,
                hostname=name,
                role="exit",
                xray_config=render.exit_config(inv, node, state.clients),
                xray_version=xray_version,
                host_key_private=host_priv,
                host_key_public=host_pub,
                authorized_key=keys.read_ssh_pubkey(inv.defaults.ssh_key),
                service_port=inv.exits.port,
            ),
        )

    try:
        wait_ssh(inv, node["ipv4"])
    except FleetError as e:
        still_running = False
        try:
            remote = driver.get(handle.provider_id)
            still_running = bool(remote and remote.status in ("live", "running"))
        except FleetError:
            pass
        if still_running:
            # The provider says the box is up but nothing answers from here. On a
            # Russian vantage point that is the signature of an IP-range block — TSPU
            # drops SYN to every port on swept ranges — not a broken server.
            raise UnreachableExit(
                f"{name} ({node['ipv4']}) is running at {pspec.name} but unreachable "
                f"from here. {('AS' + str(node.get('asn')) + ' ' + (node.get('asn_holder') or '')).strip()}\n"
                f"  If you are running fleet from inside Russia this is almost "
                f"certainly a swept range, not a broken node."
            ) from None
        raise
    verify_node(inv, node)

    node["status"] = "live"
    state.save()
    ok(f"{name} is live")
    return node


def provision_exit_with_reroll(inv: Inventory, state: State, *,
                               want_provider: str | None = None,
                               attempts: int = 3) -> dict[str, Any]:
    """Provision, and if the address turns out to be unreachable, throw it away and
    try again.

    Cloud providers allocate from large pools, so a second roll often lands in a
    different prefix — and when it does not, `choose_provider` prefers a provider we
    are not already using, so the next attempt tends to move ASN entirely. This costs
    seconds and pennies; discovering the same thing from a client a day later costs
    an outage.
    """
    last: FleetError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return provision_exit(inv, state, want_provider=want_provider)
        except UnreachableExit as e:
            last = e
            fail(str(e))
            dead = [n for n in state.exits if n["status"] == "provisioning"]
            for n in dead:
                warn(f"discarding {n['name']} and re-rolling ({attempt}/{attempts})")
                try:
                    providers.get(n["provider"], {}).destroy(n["provider_id"])
                except FleetError as de:
                    fail(f"could not destroy {n['name']}: {de} — check the panel")
                if n.get("ipv4"):
                    unpin_host_key(inv, n["ipv4"], inv.defaults.ssh_port)
                state.drop_exit(n["id"])
                state.save()
    raise FleetError(
        f"could not get a reachable exit in {attempts} attempts. Last: {last}\n"
        "  Add a provider on a different ASN to [[exits.providers]] — the big "
        "datacenter ASNs (DigitalOcean, Hetzner, Vultr, OVH, AWS, Oracle) are the "
        "ones RKN sweeps. See docs/06-choosing-providers.md."
    )


def verify_node(inv: Inventory, node: dict) -> None:
    """Confirm xray is running and listening where we think it is."""
    host = node["ipv4"]
    active = ssh_run(inv, host, "systemctl is-active xray", check=False, timeout=60)
    if (active.stdout or "").strip() != "active":
        journal = ssh_run(inv, host, "journalctl -u xray -n 30 --no-pager", check=False)
        raise FleetError(f"xray is not active on {node['name']}:\n{journal.stdout}")
    listening = ssh_run(
        inv, host,
        f"ss -ltnp 2>/dev/null | grep -q ':{node['port']} ' && echo yes || echo no",
        check=False, timeout=60,
    )
    if "yes" not in (listening.stdout or ""):
        raise FleetError(f"{node['name']}: nothing listening on port {node['port']}")
    # REALITY's active-probe defence only works if the masked site is reachable from
    # *this* node. If it is not, probing gets a hang instead of a real certificate —
    # a unique, stable, remotely-testable fingerprint. Worse than no REALITY at all.
    dest_host, _, dest_port = node["reality_dest"].rpartition(":")
    probe = ssh_run(
        inv, host,
        f"timeout 8 openssl s_client -connect {shell_quote(dest_host)}:{dest_port} "
        f"-servername {shell_quote(node['reality_sni'])} </dev/null 2>&1 | head -30",
        check=False, timeout=60,
    )
    out = probe.stdout or ""
    if "CONNECTED" not in out or "Verify return code: 0" not in out:
        warn(
            f"{node['name']}: cannot complete a clean TLS handshake to its masking host "
            f"{node['reality_dest']} — REALITY's probe defence will be broken on this node.\n"
            f"    Pick a dest that is reachable from {node.get('region') or node['provider']}."
        )


def shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# --------------------------------------------------------------------- entry

def entry_init(inv: Inventory, state: State) -> dict:
    if state.entry:
        return state.entry
    priv, pub = keys.reality_keypair()
    state.entry = {
        "name": inv.entry.name,
        "host": inv.entry.host,
        "port": inv.entry.port,
        "reality_private": priv,
        "reality_public": pub,
        "created_at": now(),
        "xray_version": "",
    }
    state.save()
    ok(f"entry identity created (public key {pub})")
    return state.entry


def sync(inv: Inventory, state: State) -> None:
    """Make the fleet match state. What that means depends on the mode.

    cascade — push a regenerated config to the entry node. One push, and the rotation
              is live for every client without any of them noticing.
    direct  — push the client list to every serving exit, because with no entry node
              the exits are what hold client credentials. Clients still have to learn
              new exit addresses themselves; `fleet subscription` is how.
    """
    if not state.clients:
        raise FleetError("no clients — run `fleet client add <name>` first")
    serving = state.serving_exits()

    if inv.cascade:
        if not state.entry:
            raise FleetError("no entry node yet — run `fleet entry init`")
        if not serving:
            warn("no serving exits: the entry node will be pushed in fail-closed mode "
                 "(client traffic blocked rather than leaking out of a Russian datacenter)")
        config = render.entry_config(inv, state)
        step(f"pushing entry config ({len(serving)} exit(s), "
             f"{len(state.clients)} client(s)) to {inv.entry.host}")
        push_xray_config(inv, inv.entry.host, config)
        ok("entry node updated")
        return

    if not serving:
        warn("no serving exits — nothing to sync")
        return
    step(f"pushing {len(state.clients)} client credential(s) to "
         f"{len(serving)} exit(s)")
    failures = 0
    for node in serving:
        try:
            push_xray_config(inv, node["ipv4"], render.exit_config(inv, node, state.clients))
        except FleetError as e:
            fail(f"{node['name']}: {e}")
            failures += 1
    if failures:
        raise FleetError(f"{failures} of {len(serving)} exits failed to sync")
    ok("all serving exits updated")

    # The one client we can update ourselves: the router across the LAN.
    try:
        if not routermod.push(inv, state):
            if not inv.router.enabled:
                warn("clients hold exit addresses directly in this mode — reissue "
                     "their configs with `fleet client uri`, or configure [router] "
                     "in inventory.toml so fleet pushes to it automatically")
    except FleetError as e:
        fail(f"router push failed: {e}")
        warn("the exits are fine; the router is still on the previous list")


# Kept so older call sites and muscle memory keep working.
sync_entry = sync


# -------------------------------------------------------------------- retire

def retire_exit(inv: Inventory, state: State, node: dict, *, reason: str = "",
                drain: bool | None = None) -> None:
    name = node["name"]
    step(f"retiring {name}" + (f" ({reason})" if reason else ""))

    if node.get("serving"):
        node["serving"] = False
        node["status"] = "draining"
        state.save()
        sync(inv, state)
        seconds = inv.defaults.drain_seconds if drain is not False else 0
        if seconds:
            log(f"draining {name} for {seconds}s")
            time.sleep(seconds)

    driver = providers.get(node["provider"], {})
    try:
        driver.destroy(node["provider_id"])
        ok(f"destroyed {name} at {node['provider']}")
    except FleetError as e:
        fail(f"could not destroy {name}: {e}")
        node["status"] = "orphaned"
        state.save()
        raise

    if node.get("ipv4"):
        unpin_host_key(inv, node["ipv4"], inv.defaults.ssh_port)
    state.drop_exit(node["id"])
    state.save()


# ------------------------------------------------------------------- lifecycle

def ensure_pool(inv: Inventory, state: State, *, dry_run: bool = False) -> int:
    """Bring the serving pool up to pool_size. Returns how many were created."""
    serving = state.serving_exits()
    missing = inv.exits.pool_size - len(serving)
    if missing <= 0:
        log(f"pool is at size ({len(serving)}/{inv.exits.pool_size})")
        return 0
    created = 0
    for _ in range(missing):
        try:
            node = (provision_exit(inv, state, dry_run=True) if dry_run
                    else provision_exit_with_reroll(inv, state))
            if dry_run:
                created += 1
                continue
            node["serving"] = True
            state.save()
            created += 1
        except FleetError as e:
            fail(f"could not add an exit: {e}")
    if created and not dry_run:
        sync(inv, state)
    return created


def rotate(inv: Inventory, state: State, *, node_id: str | None = None,
           force: bool = False) -> None:
    """Make-before-break: stand the replacement up before taking anything down."""
    if node_id:
        victims = [n for n in state.exits if n["id"] == node_id or n["name"] == node_id]
        if not victims:
            raise FleetError(f"no exit named `{node_id}`")
    else:
        victims = [
            n for n in state.live_exits()
            if state.age_hours(n) >= inv.exits.max_age_hours
        ]
        if not victims:
            oldest = max(state.live_exits(), key=state.age_hours, default=None)
            log(
                "nothing is old enough to rotate"
                + (f" (oldest: {oldest['name']} at {state.age_hours(oldest):.1f}h of "
                   f"{inv.exits.max_age_hours}h)" if oldest else "")
            )
            return

    for victim in victims:
        age = state.age_hours(victim)
        if not force and age < inv.exits.min_age_hours:
            warn(f"{victim['name']} is only {age:.1f}h old (min_age_hours="
                 f"{inv.exits.min_age_hours}) — skipping; use --force to override")
            continue

        step(f"rotating {victim['name']} (age {age:.1f}h)")
        replacement = provision_exit_with_reroll(inv, state)
        replacement["serving"] = True
        state.save()
        sync(inv, state)          # both serving: no gap in coverage
        retire_exit(inv, state, victim, reason=f"age {age:.1f}h")
        ok(f"{victim['name']} → {replacement['name']}")


def reap(inv: Inventory, state: State) -> None:
    """Destroy anything expired, dead, or no longer present at its provider."""
    for node in list(state.exits):
        driver = providers.get(node["provider"], {})
        remote = None
        try:
            remote = driver.get(node["provider_id"])
        except FleetError as e:
            warn(f"{node['name']}: provider query failed ({e})")
            continue
        if remote is None:
            warn(f"{node['name']} no longer exists at {node['provider']} — dropping from state")
            if node.get("serving"):
                node["serving"] = False
            state.drop_exit(node["id"])
            state.save()
            sync(inv, state)
            continue
        if node["status"] == "orphaned":
            retire_exit(inv, state, node, reason="previously orphaned", drain=False)

    # Anything at a provider carrying our label but absent from state is a leak —
    # a crash between "create" and "save" — and it is costing money.
    known = {(n["provider"], n["provider_id"]) for n in state.exits}
    for pspec in inv.exits.providers:
        driver = providers.get(pspec.name, pspec.opts)
        try:
            for handle in driver.list_fleet():
                if (pspec.name, handle.provider_id) not in known:
                    warn(f"orphan at {pspec.name}: {handle.name} ({handle.provider_id}) "
                         f"— destroying")
                    driver.destroy(handle.provider_id)
        except (FleetError, NotImplementedError) as e:
            warn(f"{pspec.name}: could not list for orphans ({e})")


def extend_self_expiring(inv: Inventory, state: State) -> None:
    """Top up self-expiring nodes we still want, so they don't die mid-rotation."""
    for node in state.live_exits():
        driver = providers.get(node["provider"], {})
        if not driver.self_expiring or not node.get("expires_at"):
            continue
        hours_left = (node["expires_at"] - now()) / 3600.0
        age = state.age_hours(node)
        wanted_remaining = (max(0.0, inv.exits.max_age_hours - age)
                            + inv.exits.expiry_buffer_hours)
        if hours_left < wanted_remaining:
            step(f"topping up {node['name']} ({hours_left:.1f}h left)")
            driver.extend(node["provider_id"], int(wanted_remaining - hours_left))
            remote = driver.get(node["provider_id"])
            if remote:
                node["expires_at"] = remote.expires_at
                state.save()


# --------------------------------------------------------------------- health

def health(inv: Inventory, state: State) -> dict[str, Any]:
    """Check each exit *from the entry node* — reachability from Russia is the only
    measurement that matters, and your laptop is not in Russia."""
    report: dict[str, Any] = {"entry": {}, "exits": [], "healthy": 0, "unhealthy": 0}

    if inv.cascade and state.entry and inv.entry.host:
        probe = ssh_run(inv, inv.entry.host, "systemctl is-active xray", check=False, timeout=60)
        active = (probe.stdout or "").strip() == "active"
        report["entry"] = {"host": inv.entry.host, "xray": "active" if active else "DOWN"}
        if not active:
            fail(f"entry {inv.entry.host}: xray is not running")
    elif not inv.cascade:
        report["entry"] = {"host": "-", "xray": "n/a (direct mode)"}

    for node in state.exits:
        row: dict[str, Any] = {
            "name": node["name"], "provider": node["provider"], "region": node["region"],
            "ip": node["ipv4"], "age": human_age(node["created_at"]),
            "serving": bool(node.get("serving")), "status": node["status"],
        }
        reachable = None
        if inv.cascade and state.entry and inv.entry.host and node.get("ipv4"):
            cmd = (
                f"timeout 6 bash -c '</dev/tcp/{node['ipv4']}/{node['port']}' "
                f"&& echo REACHABLE || echo BLOCKED"
            )
            res = ssh_run(inv, inv.entry.host, cmd, check=False, timeout=60)
            reachable = "REACHABLE" in (res.stdout or "")
            row["from_entry"] = "reachable" if reachable else "BLOCKED FROM RU"
        elif not inv.cascade and node.get("ipv4"):
            # No Russian vantage point in direct mode, so this measures reachability
            # from wherever you are running fleet. If that is not the network the
            # clients use, it is a weaker signal than the cascade's check.
            import socket
            try:
                with socket.create_connection((node["ipv4"], node["port"]), timeout=6):
                    reachable = True
            except OSError:
                reachable = False
            row["from_here"] = "reachable" if reachable else "UNREACHABLE"
        if node.get("expires_at"):
            runway = (node["expires_at"] - now()) / 3600
            row["expires_in"] = f"{runway:.0f}h"
            if runway < 24:
                fail(f"{node['name']} expires in {runway:.0f}h — if the orchestrator "
                     f"stops now, this node is deleted")
            elif runway < inv.exits.expiry_buffer_hours / 2:
                warn(f"{node['name']} has only {runway:.0f}h of paid runway left")

        healthy = node["status"] == "live" and (reachable is not False)
        row["healthy"] = healthy
        report["healthy" if healthy else "unhealthy"] += 1
        if not healthy and reachable is False:
            where = "the entry node" if inv.cascade else "here"
            fail(f"{node['name']} ({node['ipv4']}) is not reachable from {where} — "
                 f"the {node['provider']} range is likely blocked; rotate it")
        report["exits"].append(row)
    return report
