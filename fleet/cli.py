"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import collections
import shutil
import subprocess
import sys

from . import asn as asnmod
from . import router as routermod
from . import awg as awgmod
from . import config as cfgmod
from . import keys, ops, providers, render
from .deploy import (
    cloud_init,
    pin_host_key,
    push_xray_config,
    resolve_xray_version,
    ssh_run,
    wait_ssh,
)
from .state import State
from .util import FleetError, fail, human_age, log, ok, step, warn

BANNER = "fleet — obfuscated VPN cascade control"


def _load(args) -> tuple[cfgmod.Inventory, State]:
    inv = cfgmod.load(args.inventory)
    return inv, State(inv.state_path)


# --------------------------------------------------------------------- output

def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    head = "  " + "  ".join(c.upper().ljust(widths[c]) for c in columns)
    sep = "  " + "  ".join("-" * widths[c] for c in columns)
    body = [
        "  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows
    ]
    return "\n".join([head, sep, *body])


# -------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    target = args.inventory or cfgmod.DEFAULT_INVENTORY
    if os.path.exists(target) and not args.force:
        fail(f"{target} already exists (use --force to overwrite)")
        return 1
    example = os.path.join(os.path.dirname(__file__), "..", "inventory.example.toml")
    shutil.copyfile(example, target)
    ok(f"wrote {target}")
    print("\nNext:\n"
          "  1. edit inventory.toml — at minimum entry.host and [[exits.providers]]\n"
          "  2. export provider credentials (see .env.example)\n"
          "  3. fleet entry init && fleet client add beryl\n"
          "  4. fleet entry deploy && fleet up\n")
    return 0


def _dest_host(node: dict) -> str:
    """The hostname out of `reality_dest`, which is stored as host:port."""
    d = node.get("reality_dest") or ""
    return d.rsplit(":", 1)[0] if ":" in d else d


def _sni_cell(node: dict) -> str:
    """The masking identity, flagged when it disagrees with dest."""
    sni = node.get("reality_sni") or "—"
    dest = _dest_host(node)
    return f"{sni} !=dest" if (dest and sni != "—" and dest != sni) else sni


def cmd_status(args) -> int:
    inv, state = _load(args)
    print(f"\n\033[1m{BANNER}\033[0m")
    print(f"  inventory: {inv.path}")
    if inv.site:
        print(f"  site: \033[1m{inv.site}\033[0m   state: {inv.state_dir}")
    shape = ("clients → RU entry → rotating exit"
             if inv.cascade else "clients → rotating exit (no Russian node)")
    print(f"  mode: \033[1m{inv.mode}\033[0m — {shape}")

    entry = state.entry
    if entry and inv.cascade:
        print(f"\n\033[1mENTRY\033[0m  {entry['name']}  {inv.entry.host}:{inv.entry.port}")
        print(f"  masking as {inv.entry.reality_sni[0]} (dest {inv.entry.reality_dest})")
        print(f"  split: {'on' if inv.entry.split.enabled else 'OFF'}, "
              f"{len(inv.entry.split.direct_domains)} direct domains, "
              f"{len(inv.entry.split.direct_ips)} direct IP rules")
        print(f"  public key {entry['reality_public']}")
    elif inv.cascade:
        print("\n\033[1mENTRY\033[0m  not initialised — run `fleet entry init`")
    else:
        print(f"\n\033[1mSPLIT\033[0m  on the client/router: "
              f"{len(inv.entry.split.direct_domains)} RU domains go out your own ISP")

    print(f"\n\033[1mEXITS\033[0m  ({len(state.serving_exits())} serving / "
          f"{len(state.exits)} total, target {inv.exits.pool_size})")
    rows = [
        {
            "name": n["name"], "provider": n["provider"], "region": n["region"],
            "ip": n["ipv4"], "port": n["port"], "age": human_age(n["created_at"]),
            "status": n["status"], "serving": "yes" if n.get("serving") else "-",
            "asn": (f"AS{n['asn']}" if n.get("asn") else "?"),
            # The identity the node presents to anyone who connects. Grouped with
            # ip/asn because these three are what an outside observer actually sees.
            "sni": _sni_cell(n),
        }
        for n in state.exits
    ]
    print(_table(rows, ["name", "provider", "region", "ip", "asn", "sni", "age",
                        "status", "serving"]))
    distinct = {n.get("asn") for n in state.exits if n.get("asn")}
    if len(state.exits) > 1 and len(distinct) < 2:
        print("\n  \033[33mAll exits share one ASN — a single sweep takes the whole "
              "pool. Add a provider on a different ASN.\033[0m")

    # A node whose sni does not match its dest serves a certificate for the wrong
    # name. That is remotely checkable by anyone who connects, and it is the exact
    # tell REALITY exists to avoid, so it is worth more than a quiet column.
    bad = [n for n in state.exits if _dest_host(n) and n.get("reality_sni")
           and _dest_host(n) != n["reality_sni"]]
    for n in bad:
        print(f"\n  \033[33m{n['name']}: sni is {n['reality_sni']} but dest is "
              f"{_dest_host(n)} — a probe gets a certificate for the wrong name. "
              f"Redeploy this exit.\033[0m")

    dup = collections.Counter(n["reality_sni"] for n in state.serving_exits()
                              if n.get("reality_sni"))
    for sni, c in dup.items():
        if c > 1:
            print(f"\n  \033[33m{c} serving exits both mask as {sni} — "
                  f"that correlates them. Widen reality_sni in inventory.toml.\033[0m")

    print(f"\n\033[1mCLIENTS\033[0m  ({len(state.clients)})")
    print(_table(
        [{"name": c["name"], "uuid": c["uuid"], "shortid": c["short_id"],
          "added": human_age(c["created_at"])} for c in state.clients],
        ["name", "uuid", "shortid", "added"],
    ))

    stale = [n for n in state.live_exits() if state.age_hours(n) >= inv.exits.max_age_hours]
    if stale:
        print(f"\n  \033[33m{len(stale)} exit(s) past max_age_hours — "
              f"run `fleet rotate`\033[0m")
    print()
    return 0


def _require_cascade(inv) -> None:
    if not inv.cascade:
        raise FleetError(
            'mode = "direct" — there is no entry node.\n'
            "  Set mode = \"cascade\" in inventory.toml to add one.")


def cmd_entry_init(args) -> int:
    inv, state = _load(args)
    _require_cascade(inv)
    ops.entry_init(inv, state)
    return 0


def cmd_entry_deploy(args) -> int:
    """First-time provisioning of the hand-bought RU node, over SSH."""
    inv, state = _load(args)
    _require_cascade(inv)
    ops.entry_init(inv, state)
    if not state.clients:
        fail("add at least one client first: fleet client add <name>")
        return 1

    host = inv.entry.host

    # A wrong SSH allowlist on an exit costs you a `fleet destroy`. On the hand-bought
    # entry node it costs you the node, so check before we firewall ourselves out.
    allow = os.environ.get("FLEET_SSH_ALLOW_V4", "").strip()
    if allow:
        try:
            from .util import http
            mine = (http("GET", "https://api.ipify.org?format=json") or {}).get("ip", "")
        except FleetError:
            mine = ""
        if mine and mine not in allow:
            warn(f"FLEET_SSH_ALLOW_V4 is `{allow}` but you appear to be at {mine}.")
            warn("If that address is not inside the allowlist you will be locked out of "
                 "the entry node permanently.")
            if input("Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                log("aborted")
                return 1

    version = resolve_xray_version(inv.defaults.xray_version)
    step(f"bootstrapping entry node {host} with Xray v{version}")

    with open(os.path.join(os.path.dirname(__file__), "..", "deploy", "bootstrap.sh")) as fh:
        bootstrap = fh.read()

    # shlex.quote every value: the remote end does `set -a; . /etc/fleet.env`, so an
    # unquoted value is shell. SSH_ALLOW_V4 is the live hazard — the documented form
    # is `203.0.113.4/32, 198.51.100.0/24`, and that space turns the second CIDR into
    # a command. deploy.py already quotes; this path did not.
    env = (
        f"XRAY_VERSION={shlex.quote(version)}\nXRAY_SHA256=\n"
        f"SSH_PORT={inv.defaults.ssh_port}\n"
        f"NODE_ROLE=entry\nSERVICE_PORT={inv.entry.port}\n"
        f"SSH_ALLOW_V4={shlex.quote(os.environ.get('FLEET_SSH_ALLOW_V4', '0.0.0.0/0'))}\n"
    )
    # Before the bootstrap runs, sshd is wherever it already is (22 unless you moved
    # it). Afterwards it is on inv.defaults.ssh_port. Everything below has to respect
    # that changeover or it will hang on a port nothing is listening on any more.
    port = args.port or (22 if args.first_run else inv.defaults.ssh_port)
    if args.first_run:
        warn(f"first run: connecting on port {port} and accepting the node's existing "
             "host key (it predates us, so it cannot be pre-pinned)")
        from .util import run as _run
        _run(
            [
                "ssh", "-p", str(port), "-i", os.path.expanduser(inv.defaults.ssh_key),
                "-o", f"UserKnownHostsFile={inv.known_hosts}",
                "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={inv.defaults.connect_timeout}",
                f"{inv.defaults.ssh_user}@{host}", "true",
            ],
            timeout=40,
        )

    def sh(cmd: str, **kw):
        return ssh_run(inv, host, cmd, port=port, **kw)

    sh("cat > /usr/local/sbin/fleet-bootstrap.sh && chmod 0700 "
       "/usr/local/sbin/fleet-bootstrap.sh", stdin=bootstrap, timeout=60)
    sh("cat > /etc/fleet.env && chmod 0600 /etc/fleet.env", stdin=env, timeout=60)

    # Grab the host key now, over the connection we already have. After the bootstrap
    # restarts sshd on a new port we would have no trusted way to ask for it.
    scan = sh("cat /etc/ssh/ssh_host_ed25519_key.pub", check=False, timeout=60)

    step("running bootstrap (installs Xray, tunes the kernel, moves SSH to "
         f"{inv.defaults.ssh_port} — a minute or two)")
    # sshd restarts inside this command. The existing session survives it (sshd forks
    # per connection), but nothing may reuse `port` afterwards, so the cleanup is
    # chained into the same invocation rather than issued as a second one.
    sh("set -a; . /etc/fleet.env; set +a; "
       "bash /usr/local/sbin/fleet-bootstrap.sh 2>&1 | tail -20; "
       "shred -u /etc/fleet.env 2>/dev/null || rm -f /etc/fleet.env", timeout=1200)

    if scan.returncode == 0 and scan.stdout.strip():
        pin_host_key(inv, host, inv.defaults.ssh_port, scan.stdout.strip())
        ok(f"pinned entry host key for port {inv.defaults.ssh_port}")
    else:
        warn("could not read the host key; the next connection will fail strict "
             "checking. Add it by hand:\n"
             f"    ssh-keyscan -p {inv.defaults.ssh_port} {host} >> {inv.known_hosts}")

    # From here on, use the new port.
    probe = ssh_run(inv, host, "true", port=inv.defaults.ssh_port, check=False, timeout=40)
    if probe.returncode != 0:
        fail(f"cannot reach {host} on the new SSH port {inv.defaults.ssh_port}:\n"
             f"{(probe.stderr or '').strip()}")
        return 1
    ok(f"SSH confirmed on port {inv.defaults.ssh_port}")

    state.entry["xray_version"] = version
    state.save()
    ops.sync(inv, state)
    ok("entry node deployed")
    print(f"\n  SSH now listens on port {inv.defaults.ssh_port}. Future commands use it "
          f"automatically.\n")
    return 0


def cmd_entry_sync(args) -> int:
    inv, state = _load(args)
    ops.sync(inv, state)
    return 0


def cmd_sync(args) -> int:
    inv, state = _load(args)
    ops.sync(inv, state)
    return 0


def cmd_subscription(args) -> int:
    """Emit a subscription payload — how clients follow rotation without DNS."""
    inv, state = _load(args)
    targets = [c for c in state.clients if not args.name or c["name"] == args.name]
    if not targets:
        fail(f"no client named `{args.name}`")
        return 1
    for c in targets:
        payload = render.subscription(inv, state, c)
        n = len(render.client_uris(inv, state, c))
        if args.out == "-":
            # stdout, so it can be piped straight into qrencode/pbcopy without
            # leaving a copy of working credentials lying around in state/.
            print(payload)
            continue
        path = args.out or os.path.join(inv.state_dir, f"sub-{c['name']}.txt")
        with open(path, "w") as fh:
            fh.write(payload + "\n")
        os.chmod(path, 0o600)
        ok(f"{c['name']}: {n} server(s) → {path}")
    if args.out == "-":
        return 0
    print("\n  Host these files somewhere your clients can fetch over HTTPS, then point\n"
          "  v2rayTun / Happ / NekoBox / podkop at the URL. `fleet rotate` rewrites them,\n"
          "  so a rotation stops being a per-device chore.\n"
          "  Anyone with the URL gets working credentials — treat it like a password.\n")
    return 0


def cmd_client_add(args) -> int:
    inv, state = _load(args)
    if inv.cascade:
        ops.entry_init(inv, state)
    from .util import now
    client = {
        "name": args.name,
        "uuid": keys.new_uuid(),
        "short_id": keys.short_id(4),
        "created_at": now(),
    }
    state.add_client(client)
    state.save()
    ok(f"client `{args.name}` added")
    if args.sync:
        ops.sync(inv, state)
    else:
        warn("run `fleet sync` to activate it"
             + (" on the entry node" if inv.cascade else " on every serving exit"))
    try:
        for uri in render.client_uris(inv, state, client):
            print("\n" + uri)
        print()
    except FleetError as e:
        warn(f"no share link yet: {e}")
    return 0


def cmd_client_revoke(args) -> int:
    inv, state = _load(args)
    if not state.get_client(args.name):
        fail(f"no client named `{args.name}`")
        return 1
    state.drop_client(args.name)
    state.save()
    ok(f"client `{args.name}` revoked")
    if not state.clients:
        warn("that was the last client; the entry node cannot be synced without one")
        return 0
    ops.sync(inv, state)
    return 0


def cmd_client_uri(args) -> int:
    inv, state = _load(args)
    targets = [c for c in state.clients if not args.name or c["name"] == args.name]
    if not targets:
        fail(f"no client named `{args.name}`")
        return 1
    for c in targets:
        uris = render.client_uris(inv, state, c)
        if not uris:
            fail("no serving exits yet — run `fleet up`")
            return 1
        if len(uris) > 1:
            print(f"\n\033[1m{c['name']}\033[0m  \033[2m({len(uris)} servers — import "
                  f"all of them so the client can fail over)\033[0m")
        for uri in uris:
            if args.qr and shutil.which("qrencode"):
                # Through stdin, never argv: a command line is world-readable in
                # `ps`, and this one would carry the client's UUID.
                # No string argument: qrencode then reads stdin. Not `-r -`,
                # which it rejects with "Cannot read input file -."
                subprocess.run(["qrencode", "-t", "ANSIUTF8"],
                               input=uri.encode(), check=False)
            elif args.qr:
                warn("qrencode not installed — printing the URI instead.\n"
                     "  macOS: brew install qrencode   Debian/Pi: sudo apt install qrencode")
            print(uri if len(targets) == 1 and len(uris) == 1 else f"  {uri}")
    if not inv.cascade:
        print("\n  \033[2mThese addresses change when exits rotate. `fleet subscription`\n"
              "  avoids reissuing them by hand.\033[0m")
    return 0


def cmd_client_config(args) -> int:
    inv, state = _load(args)
    client = state.get_client(args.name)
    if not client:
        fail(f"no client named `{args.name}`")
        return 1
    cfg = render.client_config(inv, state, client, socks_port=args.socks_port,
                               local_split=not args.no_local_split)
    out = render.dumps(cfg)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out)
        os.chmod(args.out, 0o600)
        ok(f"wrote {args.out}")
    else:
        print(out)
    return 0


def cmd_exit_new(args) -> int:
    inv, state = _load(args)
    node = ops.provision_exit(inv, state, want_provider=args.provider, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    if args.serve:
        node["serving"] = True
        state.save()
        ops.sync(inv, state)
    return 0


def cmd_up(args) -> int:
    inv, state = _load(args)
    created = ops.ensure_pool(inv, state, dry_run=args.dry_run)
    if created:
        ok(f"added {created} exit(s)")
    return 0


def cmd_rotate(args) -> int:
    inv, state = _load(args)
    ops.rotate(inv, state, node_id=args.node, force=args.force)
    return 0


def cmd_reap(args) -> int:
    inv, state = _load(args)
    ops.reap(inv, state)
    return 0


def cmd_destroy(args) -> int:
    inv, state = _load(args)
    if args.all:
        victims = list(state.exits)
        if not victims:
            log("nothing to destroy")
            return 0
        print("\nAbout to destroy ALL exits:")
        for n in victims:
            print(f"  {n['name']}  {n['provider']}  {n['ipv4']}")
        print("\nThe entry node is a `manual` provider and will NOT be touched.")
        if not args.yes:
            reply = input("\nType 'destroy' to confirm: ").strip()
            if reply != "destroy":
                log("aborted")
                return 1
        for n in victims:
            try:
                ops.retire_exit(inv, state, n, reason="teardown", drain=False)
            except FleetError as e:
                fail(str(e))
        ops.sync(inv, state)
        return 0

    if not args.node:
        fail("name an exit, or pass --all")
        return 1
    node = state.get_exit(args.node)
    if not node:
        fail(f"no exit named `{args.node}`")
        return 1
    ops.retire_exit(inv, state, node, reason="manual")
    return 0


def cmd_health(args) -> int:
    inv, state = _load(args)
    report = ops.health(inv, state)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\nentry: {report['entry'].get('xray', 'unknown')} "
              f"({report['entry'].get('host', '-')})")
        reach = "from_entry" if inv.cascade else "from_here"
        print(_table(report["exits"],
                     ["name", "provider", "ip", "age", "status", "serving", reach]))
        print(f"\n  {report['healthy']} healthy, {report['unhealthy']} unhealthy\n")
    return 0 if report["unhealthy"] == 0 else 2


def cmd_cron(args) -> int:
    """One entry point for the scheduler: reap, top up, rotate, refill, report."""
    inv, state = _load(args)
    failures = 0
    for name, fn in (
        ("reap", lambda: ops.reap(inv, state)),
        ("extend", lambda: ops.extend_self_expiring(inv, state)),
        ("rotate", lambda: ops.rotate(inv, state)),
        ("ensure_pool", lambda: ops.ensure_pool(inv, state)),
    ):
        try:
            step(f"cron: {name}")
            fn()
        except FleetError as e:
            fail(f"cron: {name} failed: {e}")
            failures += 1

    try:
        report = ops.health(inv, state)
        # An exit that is unreachable from Russia is useless no matter how healthy it
        # looks from here, so replace it now rather than at its scheduled rotation.
        for row in report["exits"]:
            if row.get("from_entry") == "BLOCKED FROM RU" or \
               row.get("from_here") == "UNREACHABLE":
                node = state.get_exit(row["name"])
                if node:
                    warn(f"replacing {row['name']}: blocked from the entry node")
                    ops.rotate(inv, state, node_id=node["id"], force=True)
        log(f"cron: {report['healthy']} healthy, {report['unhealthy']} unhealthy")
    except FleetError as e:
        fail(f"cron: health failed: {e}")
        failures += 1
    return 1 if failures else 0


def _awg_requires_entry(inv) -> None:
    """AmneziaWG currently attaches to the entry node only.

    In direct mode there is no entry node, and the naive fix — deploy AmneziaWG to
    each exit — has a rotation problem VLESS does not: a WireGuard peer has exactly
    one Endpoint and no client-side balancer, so every rotation would break every
    peer. Solving it properly means sharing one interface identity across all exits
    and rewriting Endpoint on rotation. That is a real design, not a one-liner, so
    the command refuses rather than half-working against an empty host.
    """
    if not inv.cascade:
        raise FleetError(
            'AmneziaWG is only wired up for mode = "cascade" (it attaches to the '
            "entry node).\n"
            "  In direct mode, use VLESS+REALITY+Vision — `transport = \"tcp\"` is "
            "already the default\n"
            "  and Vision is the faster of the two userspace paths. See "
            "docs/02-architecture.md."
        )


def cmd_awg_init(args) -> int:
    inv, state = _load(args)
    _awg_requires_entry(inv)
    if state.data.get("awg") and not args.force:
        fail("AmneziaWG is already initialised (use --force to regenerate — this "
             "invalidates every existing peer config)")
        return 1
    iface = awgmod.AwgInterface.new(generation=args.generation,
                                    listen_port=args.port, mtu=args.mtu)
    data = iface.to_state()
    data["tproxy_port"] = args.tproxy_port
    data["generation"] = args.generation
    state.data["awg"] = data
    state.save()
    ok(f"AmneziaWG interface created on UDP/{args.port} (generation {args.generation})")
    sx = f" S3={iface.params.s3} S4={iface.params.s4}" if iface.params.s3 else ""
    print(f"\n  obfuscation: Jc={iface.params.jc} Jmin={iface.params.jmin} "
          f"Jmax={iface.params.jmax} S1={iface.params.s1} S2={iface.params.s2}{sx}")
    print(f"  headers:     H1={iface.params.h1} H2={iface.params.h2} "
          f"H3={iface.params.h3} H4={iface.params.h4}\n")
    if args.generation == "1.0":
        log("generation 1.0: only needed for GL.iNet firmware 4.8.x. Firmware 4.9+ "
            "speaks AmneziaWG 2.0 — rerun with --generation 2.0 --force once upgraded.")
    else:
        log(f"generation {args.generation}: needs GL.iNet firmware >= 4.9 on the "
            "router, or the Amnezia client elsewhere")
    return 0


def cmd_awg_peer(args) -> int:
    inv, state = _load(args)
    _awg_requires_entry(inv)
    data = state.data.get("awg")
    if not data:
        fail("run `fleet awg init` first")
        return 1
    iface = awgmod.AwgInterface.from_state({k: v for k, v in data.items()
                                            if k != "tproxy_port"})
    peer = iface.add_peer(args.name)
    data.update(iface.to_state())
    state.data["awg"] = data
    state.save()
    conf = iface.peer_conf(peer, inv.entry.host)
    ok(f"peer `{args.name}` added at {peer['address']}")
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(conf)
        os.chmod(args.out, 0o600)
        ok(f"wrote {args.out}")
    else:
        print("\n" + conf)
    warn("run `fleet awg deploy` to activate it on the entry node")
    return 0


def cmd_awg_deploy(args) -> int:
    from .deploy import scp_put, ssh_run as _sr
    inv, state = _load(args)
    _awg_requires_entry(inv)
    data = state.data.get("awg")
    if not data:
        fail("run `fleet awg init` first")
        return 1
    iface = awgmod.AwgInterface.from_state({k: v for k, v in data.items()
                                            if k != "tproxy_port"})
    host = inv.entry.host

    if args.install:
        with open(os.path.join(os.path.dirname(__file__), "..", "deploy", "amneziawg",
                               "install-awg.sh")) as fh:
            script = fh.read()
        step("installing AmneziaWG on the entry node (compiles a kernel module; slow)")
        _sr(inv, host, "cat > /root/install-awg.sh && chmod 0700 /root/install-awg.sh",
            stdin=script, timeout=120)
        _sr(inv, host,
            f"AWG_PORT={iface.listen_port} TPROXY_PORT={data.get('tproxy_port', 12345)} "
            f"bash /root/install-awg.sh 2>&1 | tail -20", timeout=1800)
        ok("AmneziaWG installed")

    conf = iface.server_conf()
    scp_put(inv, host, conf, "/etc/amnezia/amneziawg/awg0.conf", mode="0600")
    _sr(inv, host, "systemctl enable awg-quick@awg0 >/dev/null 2>&1; "
                   "systemctl restart awg-quick@awg0", timeout=120)
    active = _sr(inv, host, "awg show awg0 2>&1 | head -5", check=False, timeout=60)
    print(active.stdout or active.stderr)
    ops.sync(inv, state)   # adds the tproxy inbound to the Xray config
    ok(f"AmneziaWG live with {len(iface.peers)} peer(s)")
    return 0


def cmd_render(args) -> int:
    inv, state = _load(args)
    if args.what == "entry":
        _require_cascade(inv)
        print(render.dumps(render.entry_config(inv, state)))
    else:
        node = state.get_exit(args.name) if args.name else (state.exits or [None])[0]
        if not node:
            fail("no exit to render; pass a name")
            return 1
        # In direct mode the exit inbound carries the client credentials.
        print(render.dumps(render.exit_config(inv, node, state.clients)))
    return 0


def cmd_dns(args) -> int:
    """Emit resolver config for the always-on box, from the same split list."""
    inv, state = _load(args)
    outdir = args.out or os.path.join(inv.root, "orchestrator",
                                      f"dns-{inv.site}" if inv.site else "dns")
    os.makedirs(outdir, exist_ok=True)
    files = {
        "dnsmasq-split.conf": render.dnsmasq_split(inv, stubby_port=args.stubby_port),
        "stubby.yml": render.stubby_config(inv, listen_port=args.stubby_port),
    }
    for name, body in files.items():
        path = os.path.join(outdir, name)
        with open(path, "w") as fh:
            fh.write(body)
        ok(f"wrote {path}")
    n = body.count("server=/")
    print(f"""
  Install on the always-on box:
    dnsmasq-split.conf -> /etc/dnsmasq.d/99-fleet.conf   (or Pi-hole's /etc/dnsmasq.d/)
    stubby.yml         -> /etc/stubby/stubby.yml
    systemctl restart stubby && systemctl restart dnsmasq   # or pihole-FTL

  Russian names resolve over DoT to {render.YANDEX_DOT_NAME}. Foreign names are
  deliberately NOT resolved here — they travel inside the tunnel. Verify with:
    tests/t11_dns_integrity.sh 127.0.0.1:10808
""")
    return 0


def cmd_router_push(args) -> int:
    inv, state = _load(args)
    if not inv.router.enabled or not inv.router.host:
        fail('no [router] section in inventory.toml — nothing to push to.\n'
             '  Add:  [router]\n        host = "root@192.168.8.1"\n'
             '        client = "beryl"')
        return 1

    uris = None
    if getattr(args, "from_file", ""):
        path = os.path.expanduser(args.from_file)
        try:
            blob = open(path).read() if path != "-" else sys.stdin.read()
        except OSError as e:
            fail(f"cannot read {path}: {e}")
            return 1
        uris = render.parse_subscription(blob)
        if not uris:
            fail(f"no vless:// URIs found in {path}.\n"
                 "  Expected a `fleet subscription` blob (base64) or a plain list.")
            return 1
        log(f"imported {len(uris)} exit(s) from {path}")
        for u in uris:
            d = render.describe_uri(u)
            log(f"  {d['host']}:{d['port']}  sni={d['sni']}  "
                f"flow={d['flow']}  id={d['id']}  {d['label']}")
        # An imported list is not in state, so the next provisioning run pushes
        # state's exits over the top. Say so now rather than let it look flaky.
        if state.serving_exits():
            warn(f"this orchestrator has {len(state.serving_exits())} serving exit(s) "
                 f"of its own — a rotation or `fleet cron` will overwrite this import")
    # This check used to live inside the --from-file branch above, so a plain
    # `router push --dry-run` skipped it entirely and did a REAL push. A flag
    # named --dry-run must never touch the router, whichever path reached it;
    # this is the most destructive command in the tool and it runs against a
    # household's only uplink.
    if args.dry_run:
        if uris is None:
            uris = routermod.resolve_uris(inv, state)
            for u in uris:
                d = render.describe_uri(u)
                log(f"  {d['host']}:{d['port']}  sni={d['sni']}  "
                    f"flow={d['flow']}  id={d['id']}  {d['label']}")
        log(f"dry run — would push {len(uris)} exit(s) and "
            f"{len(routermod.ru_direct_domains(inv))} direct domains; router not touched")
        return 0

    changed = routermod.push(inv, state, force=args.force, uris=uris)
    if not changed:
        log("nothing to do")
    return 0


def cmd_router_status(args) -> int:
    inv, state = _load(args)
    print(routermod.status(inv))
    return 0


def cmd_check(args) -> int:
    """Assess an address the way RKN's targeting works: by ASN, not by hostname."""
    inv, state = _load(args)
    cache = inv.state_dir
    targets = args.ip or [n["ipv4"] for n in state.exits if n.get("ipv4")]
    if not targets:
        fail("no addresses to check — pass one, or provision an exit first")
        return 1
    bad = 0
    for ip in targets:
        v = asnmod.assess(ip, cache)
        asn_s = f"AS{v['asn']}" if v["asn"] else "AS?"
        print(f"\n\033[1m{ip}\033[0m  {asn_s}  {v['holder'] or '(unknown holder)'}")
        if v["prefix"]:
            print(f"  prefix          {v['prefix']}")
        if v["blocklist_age_hours"] is not None:
            print(f"  blocklist age   {v['blocklist_age_hours']:.1f}h")
        if v["reasons"]:
            bad += 1
            for r in v["reasons"]:
                warn(f"  {r}")
        else:
            ok("  nothing flagged")
    print("\n  \033[2mAdvisory only. The aggregate list covers whole cloud allocations\n"
          "  that are candidates for sweeping, not confirmed blocks — it flags 1.1.1.1\n"
          "  and 8.8.8.8 too. Ground truth is whether a box on that address answers\n"
          "  from inside Russia, which is what `fleet health` measures.\033[0m\n")
    return 0 if bad == 0 else 2


def cmd_providers(args) -> int:
    print("\ndrivers: " + ", ".join(providers.available()))
    if args.catalogue:
        drv = providers.get("sporestack", {})
        for kind in ("flavors", "regions", "os"):
            try:
                items = drv.slugs(kind, args.upstream)
                print(f"\nsporestack {kind} ({args.upstream or 'all'}):")
                for it in items[:40]:
                    print("  " + json.dumps(it))
            except FleetError as e:
                fail(f"{kind}: {e}")
    print()
    return 0


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description=BANNER)
    p.add_argument("-i", "--inventory", default=None, help="path to inventory.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a starter inventory.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("status", help="show the fleet").set_defaults(fn=cmd_status)

    e = sub.add_parser("entry", help="the stable RU anchor node")
    esub = e.add_subparsers(dest="sub", required=True)
    esub.add_parser("init", help="generate the entry REALITY identity").set_defaults(
        fn=cmd_entry_init)
    ed = esub.add_parser("deploy", help="install and configure the entry node over SSH")
    ed.add_argument("--first-run", action="store_true",
                    help="node is untouched: connect on port 22 and accept its host key")
    ed.add_argument("--port", type=int, default=None)
    ed.set_defaults(fn=cmd_entry_deploy)
    esub.add_parser("sync", help="regenerate and push the entry config").set_defaults(
        fn=cmd_entry_sync)

    c = sub.add_parser("client", help="per-device credentials")
    csub = c.add_subparsers(dest="sub", required=True)
    ca = csub.add_parser("add")
    ca.add_argument("name")
    ca.add_argument("--sync", action="store_true", help="push to the entry node immediately")
    ca.set_defaults(fn=cmd_client_add)
    cr = csub.add_parser("revoke")
    cr.add_argument("name")
    cr.set_defaults(fn=cmd_client_revoke)
    cu = csub.add_parser("uri", help="print share links")
    cu.add_argument("name", nargs="?")
    cu.add_argument("--qr", action="store_true")
    cu.set_defaults(fn=cmd_client_uri)
    cc = csub.add_parser("config", help="emit a full Xray client config (desktop / OpenWrt)")
    cc.add_argument("name")
    cc.add_argument("-o", "--out")
    cc.add_argument("--socks-port", type=int, default=10808)
    cc.add_argument("--no-local-split", action="store_true",
                    help="omit the client-side failure-domain split")
    cc.set_defaults(fn=cmd_client_config)

    x = sub.add_parser("exit", help="ephemeral exit nodes")
    xsub = x.add_subparsers(dest="sub", required=True)
    xn = xsub.add_parser("new")
    xn.add_argument("--provider")
    xn.add_argument("--serve", action="store_true", default=True)
    xn.add_argument("--dry-run", action="store_true")
    xn.set_defaults(fn=cmd_exit_new)

    sy = sub.add_parser("sync", help="make the fleet match state (entry, or all exits)")
    sy.set_defaults(fn=cmd_sync)

    sb = sub.add_parser("subscription",
                        help="write subscription payloads so clients follow rotation")
    sb.add_argument("name", nargs="?")
    sb.add_argument("-o", "--out",
                    help='file to write (default state/sub-<name>.txt), '
                         'or "-" for stdout')
    sb.set_defaults(fn=cmd_subscription)

    u = sub.add_parser("up", help="bring the exit pool to its configured size")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(fn=cmd_up)

    r = sub.add_parser("rotate", help="replace exits past max_age_hours")
    r.add_argument("--node", help="rotate one specific exit")
    r.add_argument("--force", action="store_true", help="ignore min_age_hours")
    r.set_defaults(fn=cmd_rotate)

    sub.add_parser("reap", help="destroy expired and orphaned servers").set_defaults(fn=cmd_reap)

    d = sub.add_parser("destroy", help="tear down exits")
    d.add_argument("node", nargs="?")
    d.add_argument("--all", action="store_true")
    d.add_argument("--yes", action="store_true")
    d.set_defaults(fn=cmd_destroy)

    h = sub.add_parser("health", help="probe exits from the entry node")
    h.add_argument("--json", action="store_true")
    h.set_defaults(fn=cmd_health)

    sub.add_parser("cron", help="the scheduled maintenance pass").set_defaults(fn=cmd_cron)

    rn = sub.add_parser("render", help="print a generated config without deploying")
    rn.add_argument("what", choices=["entry", "exit"])
    rn.add_argument("name", nargs="?")
    rn.set_defaults(fn=cmd_render)

    a = sub.add_parser("awg", help="AmneziaWG fast path on the entry node")
    asub = a.add_subparsers(dest="sub", required=True)
    ai = asub.add_parser("init", help="create the interface and its obfuscation params")
    ai.add_argument("--port", type=int, default=51820)
    ai.add_argument("--mtu", type=int, default=1320)
    ai.add_argument("--tproxy-port", type=int, default=12345)
    ai.add_argument("--generation", choices=["1.0", "1.5", "2.0"], default="2.0",
                    help="2.0 needs GL.iNet firmware >= 4.9; use 1.0 on 4.8.x")
    ai.add_argument("--force", action="store_true")
    ai.set_defaults(fn=cmd_awg_init)
    ap = asub.add_parser("peer", help="add a device")
    ap.add_argument("name")
    ap.add_argument("-o", "--out")
    ap.set_defaults(fn=cmd_awg_peer)
    ad = asub.add_parser("deploy", help="push the interface to the entry node")
    ad.add_argument("--install", action="store_true", help="also compile and install AmneziaWG")
    ad.set_defaults(fn=cmd_awg_deploy)

    dn = sub.add_parser("dns", help="emit resolver config for the always-on box")
    dn.add_argument("-o", "--out")
    dn.add_argument("--stubby-port", type=int, default=5453)
    dn.set_defaults(fn=cmd_dns)

    rt = sub.add_parser("router", help="the router on your LAN that fleet keeps updated")
    rtsub = rt.add_subparsers(dest="sub", required=True)
    rtp = rtsub.add_parser("push", help="send the current exit list to the router")
    rtp.add_argument("--force", action="store_true", help="push even if unchanged")
    rtp.add_argument("--from-file", default="", metavar="PATH",
                     help="push exits from a subscription file instead of state "
                          "(base64 or plain vless:// list; '-' for stdin)")
    rtp.add_argument("--dry-run", action="store_true",
                     help="show what would be pushed and stop, touching nothing")
    rtp.set_defaults(fn=cmd_router_push)
    rtsub.add_parser("status", help="what the router currently has").set_defaults(
        fn=cmd_router_status)

    ck = sub.add_parser("check", help="assess an address by ASN and blocklist presence")
    ck.add_argument("ip", nargs="*")
    ck.set_defaults(fn=cmd_check)

    pr = sub.add_parser("providers", help="list drivers, optionally the SporeStack catalogue")
    pr.add_argument("--catalogue", action="store_true")
    pr.add_argument("--upstream", help="digitalocean | vultr | ...")
    pr.set_defaults(fn=cmd_providers)

    return p



#: Subcommands that create, destroy or reconfigure real resources. These must not
#: run concurrently with each other or with `cron/fleet-cron.sh`, which takes the
#: same lock. Without this, a manual `fleet up` racing the systemd timer each sees
#: an under-target pool and both provision — which bills you twice and orphans a
#: server, because the loser's state write is overwritten.
_MUTATING = {"up", "rotate", "reap", "sync", "destroy", "cron", "entry", "awg", "client"}


class _FleetLock:
    """Cross-process lock sharing cron's lockfile. flock where available."""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise FleetError(
                "another fleet run holds the lock (the systemd timer, or another "
                "shell).\n"
                "  Wait for it, or: sudo systemctl stop fleet.timer\n"
                f"  lock: {self.path}"
            )
        return self

    def __exit__(self, *exc):
        if self.fh:
            import fcntl
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        os.environ["FLEET_VERBOSE"] = "1"
        import importlib
        from . import util
        importlib.reload(util)
    # Route fleet's own HTTP through the tunnel if the inventory says so. Must be
    # set before any provider call; SSH is unaffected. See RouterSpec.api_proxy.
    try:
        from . import config as _cfg
        _proxy = _cfg.load(args.inventory).router.api_proxy
        if _proxy and not os.environ.get("FLEET_API_PROXY"):
            os.environ["FLEET_API_PROXY"] = _proxy
    except Exception:                                   # noqa: BLE001
        pass

    cmd = getattr(args, "_cmd", None) or (argv or sys.argv[1:] or [""])[0]
    # cron/fleet-cron.sh takes this same lock before invoking us, so locking again
    # here would deadlock against ourselves — which is exactly what happened the
    # first time the timer fired after the lock was added. The wrapper exports
    # FLEET_LOCK_HELD to say "already covered".
    needs_lock = cmd in _MUTATING and not os.environ.get("FLEET_LOCK_HELD")
    try:
        if needs_lock:
            lock_path = os.environ.get("FLEET_LOCK") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "state", ".cron.lock")
            with _FleetLock(lock_path):
                return args.fn(args)
        return args.fn(args)
    except FleetError as e:
        fail(str(e))
        return 1
    except KeyboardInterrupt:
        fail("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
