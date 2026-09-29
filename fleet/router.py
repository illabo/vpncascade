"""Pushing rotated exits to the router.

This closes the loop that direct mode otherwise leaves open. When `fleet rotate`
replaces an exit, the router is holding the *old* address — and in direct mode there
is no entry node to hide that behind.

The fix is not a hosted subscription, a DNS record, or a hole in anyone's firewall:
**the orchestrator and the router are on the same LAN.** The Pi already has fleet,
already knows the new addresses, and can simply SSH across the living room and update
podkop. No internet path, nothing to host, nothing that CGNAT or SORM can sit in the
middle of. Rotation becomes invisible to every device again, the same property the
cascade gives you for free.

Requires a `[router]` section in inventory.toml and the orchestrator's SSH key in the
router's authorized_keys.
"""
from __future__ import annotations

import hashlib
import shlex

from .config import Inventory
from .render import client_uris
from .state import State
from .util import FleetError, log, ok, run, step, warn


def _ssh(inv: Inventory, *args: str, check: bool = True, timeout: int = 90):
    r = inv.router
    base = [
        "ssh", "-p", str(r.ssh_port),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", f"StrictHostKeyChecking={'yes' if r.strict_host_key else 'accept-new'}",
    ]
    if r.ssh_key:
        import os
        base += ["-i", os.path.expanduser(r.ssh_key)]
    return run(base + [r.host, *args], check=check, timeout=timeout)


def fingerprint(uris: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(uris)).encode()).hexdigest()[:16]


def lan_subnet(inv: Inventory) -> str:
    """The LAN CIDR whose traffic gets tunnelled. Derived from the router address."""
    if inv.router.lan_subnet:
        return inv.router.lan_subnet
    host = inv.router.host.split("@")[-1]
    parts = host.split(".")
    if len(parts) == 4 and all(x.isdigit() for x in parts):
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    raise FleetError(
        f"cannot derive the LAN subnet from router host `{host}`.\n"
        "  Set [router] lan_subnet = \"192.168.8.0/24\" in inventory.toml"
    )


def orchestrator_ip(inv: Inventory) -> str:
    """This machine's LAN address, as the router sees it.

    Found by opening a UDP socket toward the router and reading back the local
    address the kernel chose — no packet is sent, and it works regardless of how
    many interfaces the box has (the Pi is dual-homed).
    """
    if inv.router.orchestrator_ip:
        return inv.router.orchestrator_ip
    import socket
    host = inv.router.host.split("@")[-1]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def ru_direct_domains(inv: Inventory) -> list[str]:
    """Russian domains that must bypass the tunnel, as bare names."""
    out = []
    for d in inv.entry.split.direct_domains:
        d = d.strip()
        if not d:
            continue
        out.append(d.split(":", 1)[-1] if ":" in d else d)
    return sorted(set(out))


def push(inv: Inventory, state: State, *, force: bool = False,
         uris: list[str] | None = None) -> bool:
    """Send an exit list to the router. Returns True if anything changed.

    `uris` overrides the list derived from state, for configs that came from
    somewhere else — an imported subscription from another site, say. Everything
    downstream is shared deliberately: the podkop 0.7 schema below is emitted in
    exactly one place, because maintaining a second copy is what produced four
    silently-fatal bugs at once (HANDOFF #32). An import must not become copy five.

    Emits podkop **0.7** config. The pre-0.7 schema this used to write was silently
    fatal in four ways at once (see HANDOFF #32), so the shape below is deliberate:

      * section TYPE is `section`, not `main` — podkop iterates
        `config_foreach ... "section"` and a `main`-typed section is simply invisible,
        producing "Outbound section not found" and no tunnel at all;
      * `proxy_config_type` selects the branch: `urltest` reads a LIST of
        `urltest_proxy_links`, `url` reads a single `proxy_string` OPTION;
      * a second `exclusion` section carries the RU list, which is how you invert
        podkop's opt-in model into "tunnel everything EXCEPT these".
    """
    r = inv.router
    if not r.enabled or not r.host:
        return False

    if uris is None:
        client = state.get_client(r.client)
        if not client:
            raise FleetError(
                f"[router].client is `{r.client}` but there is no such client.\n"
                f"  Run: fleet client add {r.client}"
            )
        uris = client_uris(inv, state, client)
    if not uris:
        warn("no serving exits — not touching the router "
             "(it keeps its current config and podkop's fail-open keeps RU working)")
        return False
    for u in uris:
        if "type=xhttp" in u:
            raise FleetError(
                "these URIs use XHTTP, which podkop's sing-box cannot speak.\n"
                '  Set transport = "tcp" in inventory.toml and run `fleet sync`.'
            )

    subnet = lan_subnet(inv)
    domains = ru_direct_domains(inv)
    fp = fingerprint(uris + [subnet, str(len(domains))])
    if not force and state.data.get("router_fingerprint") == fp:
        log(f"router already has these {len(uris)} exit(s)")
        return False

    step(f"pushing {len(uris)} exit(s) to the router at {r.host}")
    _ssh(inv, "true")  # fail early and clearly if the router is unreachable

    # The RU direct list, as a file podkop reads via local_domain_lists. Pipe it in:
    # OpenWrt has no sftp-server, so scp does not work here (HANDOFF #12).
    _ssh_stdin(inv, "\n".join(domains) + "\n", f"cat > {shlex.quote(r.ru_list_path)}")

    multi = len(uris) > 1
    cmds = [
        # --- section 'main': everything from the LAN goes out through the exits ---
        "uci set podkop.main=section",
        "uci set podkop.main.connection_type='proxy'",
        f"uci set podkop.main.proxy_config_type='{'urltest' if multi else 'url'}'",
        # scrub every key from the old schema, and the branch we are not using
        "uci -q delete podkop.main.proxy_type",
        "uci -q delete podkop.main.community_lists",
        "uci -q delete podkop.main.proxy_string",
        "uci -q delete podkop.main.urltest_proxy_links",
        "uci -q delete podkop.main.fully_routed_ips",
    ]
    if multi:
        for u in uris:
            cmds.append(f"uci add_list podkop.main.urltest_proxy_links={shlex.quote(u)}")
    else:
        cmds.append(f"uci set podkop.main.proxy_string={shlex.quote(uris[0])}")
    cmds.append(f"uci add_list podkop.main.fully_routed_ips={shlex.quote(subnet)}")

    # --- keep the orchestrator out of the tunnel ---
    # Without this its health probes leave through an exit, so they measure the
    # tunnel instead of the ISP and a blocked exit looks healthy (HANDOFF #53).
    if r.exclude_orchestrator:
        me = orchestrator_ip(inv)
        if me:
            cmds += ["uci -q delete podkop.settings.routing_excluded_ips",
                     f"uci add_list podkop.settings.routing_excluded_ips={shlex.quote(me)}"]
        else:
            warn("could not determine this machine's LAN address; the orchestrator "
                 "will stay inside the tunnel and `fleet health` will be misleading")

    # --- section 'ru': Russian destinations leave via the home ISP ---
    cmds += [
        "uci set podkop.ru=section",
        "uci set podkop.ru.connection_type='exclusion'",
        "uci -q delete podkop.ru.community_lists",
        "uci -q delete podkop.ru.local_domain_lists",
        f"uci add_list podkop.ru.local_domain_lists={shlex.quote(r.ru_list_path)}",
        "uci commit podkop",
        # Detach the restart from our SSH channel. If the init script leaves a child
        # holding stdout, ssh waits for that fd to close and the push hangs until the
        # timeout — which is exactly what happened the first time this was tested.
        "/etc/init.d/podkop restart </dev/null >/dev/null 2>&1",
    ]
    _ssh(inv, "; ".join(cmds), timeout=180)

    import time
    time.sleep(6)
    _verify(inv, len(uris))

    state.data["router_fingerprint"] = fp
    state.save()
    ok(f"router updated and podkop restarted ({fp}; {len(domains)} RU domains direct)")
    return True


def _ssh_stdin(inv: Inventory, payload: str, remote_cmd: str) -> None:
    """Feed stdin to a remote command. OpenWrt has no sftp-server, so scp is out."""
    r = inv.router
    base = ["ssh", "-p", str(r.ssh_port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", f"StrictHostKeyChecking={'yes' if r.strict_host_key else 'accept-new'}"]
    if r.ssh_key:
        import os
        base += ["-i", os.path.expanduser(r.ssh_key)]
    run(base + [r.host, remote_cmd], check=True, timeout=90, stdin=payload)


def _verify(inv: Inventory, want_uris: int) -> None:
    """A running sing-box is NOT proof of a working tunnel (HANDOFF #33).

    podkop will happily start, install nft rules and run sing-box while routing
    nothing — that is exactly how this went unnoticed for hours. So check the two
    things that actually fail silently: that podkop found the outbound section, and
    that it is not aborting.
    """
    probe = _ssh(
        inv,
        "ps w | grep -c '[s]ing-box'; "
        "nft list ruleset 2>/dev/null | grep -c podkop; "
        "logread -e podkop | tail -30 | grep -ciE 'Aborted|not found|fatal'",
        check=False, timeout=60,
    )
    lines = (probe.stdout or "").split()
    procs = int(lines[0]) if len(lines) > 0 and lines[0].isdigit() else 0
    rules = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 0
    fatals = int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 0

    if procs < 1 or rules < 1 or fatals > 0:
        logs = _ssh(inv, "logread -e podkop | tail -15", check=False, timeout=60)
        raise FleetError(
            "podkop is not healthy after the push "
            f"(sing-box procs={procs}, nft rules={rules}, recent fatal lines={fatals}):\n"
            f"{(logs.stdout or '').strip()}"
        )


def status(inv: Inventory) -> str:
    r = inv.router
    if not r.enabled or not r.host:
        return "router push disabled (no [router] section in inventory.toml)"
    got = _ssh(
        inv,
        "echo -n 'exits='; uci -q get podkop.main.urltest_proxy_links | wc -w; "
        "echo -n 'mode='; uci -q get podkop.main.proxy_config_type; "
        "echo -n 'ru_list='; uci -q get podkop.ru.local_domain_lists; "
        "echo -n 'sing-box='; ps w | grep -c '[s]ing-box'; "
        "echo -n 'nft='; nft list ruleset 2>/dev/null | grep -c podkop",
        check=False, timeout=60)
    return (got.stdout or got.stderr or "").strip()
