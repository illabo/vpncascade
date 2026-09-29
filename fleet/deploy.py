"""Turning a bare IP into a working node: cloud-init, SSH, config pushes.

SSH host keys are generated here and injected through cloud-init, then pinned in
state/known_hosts. That closes the trust-on-first-use window, which matters because
these servers get created unattended from cron — there is nobody at the keyboard to
eyeball a fingerprint.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import textwrap

from .config import Inventory
from .util import FleetError, debug, http, log, run, step

XRAY_LATEST = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"


def resolve_xray_version(pin: str) -> str:
    """Pin `latest` to a concrete version so every node in a rotation matches.

    XHTTP is still moving fast and a client/entry/exit version skew produces silent
    breakage, so the fleet must never end up with mixed versions by accident.
    """
    if pin and pin != "latest":
        return pin.lstrip("v")
    resp = http("GET", XRAY_LATEST, headers={"Accept": "application/vnd.github+json"})
    tag = (resp or {}).get("tag_name", "")
    if not tag:
        raise FleetError("could not resolve the latest Xray release; pin xray_version")
    return tag.lstrip("v")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _write_file(path: str, content: str, *, mode: str = "0600", owner: str = "root:root") -> str:
    return textwrap.dedent(
        f"""\
        - path: {path}
          owner: {owner}
          permissions: '{mode}'
          encoding: b64
          content: {_b64(content)}
        """
    ).rstrip()


def node_env(
    inv: Inventory,
    *,
    role: str,
    service_port: int,
    xray_version: str,
    xray_sha256: str = "",
) -> str:
    """The `/etc/fleet.env` contents that `bootstrap.sh` sources.

    Shared by BOTH delivery mechanisms — cloud-init for providers that accept
    user_data, and `bootstrap_script()` for those that do not (Fornex). Keeping one
    builder is the point: this project has already shipped four silently-fatal bugs
    from maintaining two copies of the same config (see HANDOFF #32).

    Every value is shlex-quoted because the remote end does `set -a; . /etc/fleet.env`
    — an unquoted value is shell. `SSH_ALLOW_V4` is the live hazard: its documented
    form is `203.0.113.4/32, 198.51.100.0/24`, and that space would turn the second
    CIDR into a command.
    """
    ssh_allow = os.environ.get("FLEET_SSH_ALLOW_V4", "0.0.0.0/0").strip() or "0.0.0.0/0"
    return "\n".join(
        [
            f"XRAY_VERSION={shlex.quote(xray_version)}",
            f"XRAY_SHA256={shlex.quote(xray_sha256)}",
            f"SSH_PORT={inv.defaults.ssh_port}",
            f"NODE_ROLE={shlex.quote(role)}",
            f"SERVICE_PORT={service_port}",
            f"SSH_ALLOW_V4={shlex.quote(ssh_allow)}",
        ]
    )


def cloud_init(
    inv: Inventory,
    *,
    hostname: str,
    role: str,
    xray_config: dict,
    xray_version: str,
    host_key_private: str,
    host_key_public: str,
    authorized_key: str,
    service_port: int,
    xray_sha256: str = "",
) -> str:
    """Build the cloud-config user-data for a fresh node."""
    with open(os.path.join(os.path.dirname(__file__), "..", "deploy", "bootstrap.sh")) as fh:
        bootstrap = fh.read()

    env = node_env(inv, role=role, service_port=service_port,
                   xray_version=xray_version, xray_sha256=xray_sha256)

    indented_key = textwrap.indent(host_key_private.rstrip() + "\n", "      ")

    files = "\n".join(
        [
            _write_file("/usr/local/etc/xray/config.json", json.dumps(xray_config, indent=2),
                        mode="0640", owner="root:root"),
            _write_file("/usr/local/sbin/fleet-bootstrap.sh", bootstrap, mode="0700"),
            _write_file("/etc/fleet.env", env, mode="0600"),
        ]
    )

    return f"""#cloud-config
hostname: {hostname}
preserve_hostname: false
manage_etc_hosts: true

ssh_pwauth: false
disable_root: false
users:
  - name: root
    ssh_authorized_keys:
      - {authorized_key}

# Hetzner's Debian images ship root with an ALREADY-EXPIRED password. PAM then
# refuses every session — even key-authenticated, non-interactive ones — with
# "password change required but no TTY available". That locks us out of our own
# node and, worse, makes it undebuggable. Clear it both ways: chpasswd covers
# passwords cloud-init sets, bootcmd clears expiry the image baked in, early and
# on every boot.
chpasswd:
  expire: false

bootcmd:
  - [ bash, -c, "chage -M -1 -E -1 -d $(date +%Y-%m-%d) root 2>/dev/null || true" ]
  - [ bash, -c, "passwd -u root 2>/dev/null || true" ]

ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
{indented_key}  ed25519_public: {host_key_public}

write_files:
{files}

runcmd:
  - [ bash, -c, "set -a; . /etc/fleet.env; set +a; bash /usr/local/sbin/fleet-bootstrap.sh 2>&1 | tee /var/log/fleet-bootstrap.log" ]
  - [ bash, -c, "chown xray:xray /usr/local/etc/xray/config.json && systemctl enable --now xray" ]
  - [ bash, -c, "shred -u /etc/fleet.env 2>/dev/null || rm -f /etc/fleet.env" ]

power_state:
  mode: noop
"""



def bootstrap_script(
    inv: Inventory,
    *,
    hostname: str,
    role: str,
    xray_config: dict,
    xray_version: str,
    host_key_private: str,
    host_key_public: str,
    authorized_key: str,
    service_port: int,
    xray_sha256: str = "",
) -> str:
    """The same node build as `cloud_init()`, rendered as a shell script.

    For providers with **no `user_data` field at all** — Fornex is the one in this
    tree. There the only lever at create time is an SSH key, so provisioning is
    two-phase: order the machine with a key, then pipe this into `bash -s` over SSH.

    It takes the identical inputs and reuses the identical `bootstrap.sh` and
    `node_env()`, so the two paths cannot drift. Everything is base64-embedded
    rather than heredoc'd: an xray config or a private key contains characters that
    would otherwise need escaping, and one missed quote here is a paid brick.

    Ordering matters and mirrors cloud-init's:

      1. hostname, authorized key, root password expiry — the expiry clear is the
         Hetzner lesson (an already-expired root password makes PAM refuse even
         key-authenticated sessions), applied defensively everywhere.
      2. **host key replaced before `bootstrap.sh` runs.** fleet pins the key it
         generated via `pin_host_key()` *before* it ever connects on the final
         port, so if the node kept its own random key, every later connection
         would fail on a host-key mismatch that looks exactly like a MITM.
         Replacing an in-use host key does not drop the current session.
      3. `bootstrap.sh` — which moves sshd to `SSH_PORT`, installs nftables and
         xray, and is the single implementation of all of that.
    """
    with open(os.path.join(os.path.dirname(__file__), "..", "deploy", "bootstrap.sh")) as fh:
        bootstrap = fh.read()

    env = node_env(inv, role=role, service_port=service_port,
                   xray_version=xray_version, xray_sha256=xray_sha256)

    def emit(path: str, content: str, mode: str) -> str:
        return (
            f'install -d -m0755 "$(dirname {shlex.quote(path)})"\n'
            f'printf %s {shlex.quote(_b64(content))} | base64 -d > {shlex.quote(path)}\n'
            f'chmod {mode} {shlex.quote(path)}\n'
        )

    files = "".join([
        emit("/usr/local/etc/xray/config.json", json.dumps(xray_config, indent=2), "0640"),
        emit("/usr/local/sbin/fleet-bootstrap.sh", bootstrap, "0700"),
        emit("/etc/fleet.env", env, "0600"),
        emit("/etc/ssh/ssh_host_ed25519_key", host_key_private.rstrip() + "\n", "0600"),
        emit("/etc/ssh/ssh_host_ed25519_key.pub", host_key_public.rstrip() + "\n", "0644"),
    ])

    return f"""#!/usr/bin/env bash
# Generated by fleet for a provider without cloud-init. Do not edit on the node.
set -euo pipefail

hostnamectl set-hostname {shlex.quote(hostname)} 2>/dev/null || \
  echo {shlex.quote(hostname)} > /etc/hostname

# See cloud_init(): an expired root password makes PAM refuse key-authenticated
# non-interactive sessions with "password change required but no TTY available".
chage -M -1 -E -1 -d "$(date +%Y-%m-%d)" root 2>/dev/null || true
passwd -u root 2>/dev/null || true

install -d -m0700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
grep -qxF {shlex.quote(authorized_key)} /root/.ssh/authorized_keys || \
  echo {shlex.quote(authorized_key)} >> /root/.ssh/authorized_keys

{files}
# Drop the provider's own host keys so only the pinned ed25519 one remains, then
# reload sshd. Existing sessions survive a reload, including this one.
rm -f /etc/ssh/ssh_host_rsa_key* /etc/ssh/ssh_host_ecdsa_key* /etc/ssh/ssh_host_dsa_key*
systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || true

set -a; . /etc/fleet.env; set +a
bash /usr/local/sbin/fleet-bootstrap.sh 2>&1 | tee /var/log/fleet-bootstrap.log
chown xray:xray /usr/local/etc/xray/config.json
systemctl enable --now xray
shred -u /etc/fleet.env 2>/dev/null || rm -f /etc/fleet.env
echo "[fleet] ssh-bootstrap complete"
"""


# ------------------------------------------------------------------------- SSH


def pin_host_key(inv: Inventory, host: str, port: int, host_key_public: str) -> None:
    """Record the injected host key so SSH never has to trust on first use."""
    os.makedirs(os.path.dirname(inv.known_hosts), exist_ok=True)
    entry_host = f"[{host}]:{port}" if port != 22 else host
    line = f"{entry_host} {host_key_public.strip()}\n"

    existing = []
    if os.path.exists(inv.known_hosts):
        with open(inv.known_hosts) as fh:
            existing = [ln for ln in fh if not ln.startswith(entry_host + " ")]
    with open(inv.known_hosts, "w") as fh:
        fh.writelines(existing)
        fh.write(line)
    os.chmod(inv.known_hosts, 0o600)
    debug(f"pinned host key for {entry_host}")


def unpin_host_key(inv: Inventory, host: str, port: int) -> None:
    if not os.path.exists(inv.known_hosts):
        return
    entry_host = f"[{host}]:{port}" if port != 22 else host
    with open(inv.known_hosts) as fh:
        kept = [ln for ln in fh if not ln.startswith(entry_host + " ")]
    with open(inv.known_hosts, "w") as fh:
        fh.writelines(kept)


def ssh_base(inv: Inventory, host: str, port: int | None = None) -> list[str]:
    port = port or inv.defaults.ssh_port
    return [
        "ssh",
        "-p", str(port),
        "-i", os.path.expanduser(inv.defaults.ssh_key),
        "-o", f"UserKnownHostsFile={inv.known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", f"ConnectTimeout={inv.defaults.connect_timeout}",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        f"{inv.defaults.ssh_user}@{host}",
    ]


def ssh_run(
    inv: Inventory,
    host: str,
    command: str,
    *,
    port: int | None = None,
    check: bool = True,
    timeout: int = 120,
    stdin: str | None = None,
):
    return run(ssh_base(inv, host, port) + [command], check=check, timeout=timeout, stdin=stdin)


def scp_put(inv: Inventory, host: str, content: str, remote_path: str,
            *, mode: str = "0640", port: int | None = None) -> None:
    """Write a file remotely without a temp file on either side."""
    quoted = shlex.quote(remote_path)
    cmd = f"cat > {quoted}.new && chmod {mode} {quoted}.new && mv {quoted}.new {quoted}"
    ssh_run(inv, host, cmd, port=port, stdin=content, timeout=120)


def wait_reachable(inv: Inventory, host: str, *, budget: int = 240) -> None:
    """Fail fast when an address is unreachable, instead of after a 20-minute SSH wait.

    A swept IP range drops SYN to *every* port, so "nothing answers anywhere" is the
    signal. We accept any of three ports as proof of life, because which one is open
    depends on how far cloud-init has got: 22 before the bootstrap moves sshd, the
    configured port after, and the service port once Xray is up. Requiring a specific
    one would race the bootstrap and report a perfectly good server as blocked.
    """
    import socket
    import time as _t

    ports = sorted({22, inv.defaults.ssh_port, inv.exits.port})
    deadline = _t.time() + budget
    while _t.time() < deadline:
        for port in ports:
            try:
                with socket.create_connection((host, port), timeout=4):
                    debug(f"{host}:{port} answered — address is reachable")
                    return
            except OSError:
                continue
        _t.sleep(5)
    raise FleetError(
        f"nothing on {host} answered on any of {ports} within {budget}s. "
        "A range that drops SYN to every port is the signature of a blocked prefix, "
        "not a slow boot."
    )


def wait_ssh(inv: Inventory, host: str, *, port: int | None = None, attempts: int = 60,
             delay: float = 10.0) -> None:
    """Wait for sshd, then for cloud-init to have finished the bootstrap."""
    import time

    port = port or inv.defaults.ssh_port
    step(f"waiting for SSH on {host}:{port}")
    for i in range(attempts):
        proc = ssh_run(inv, host, "true", port=port, check=False, timeout=25)
        if proc.returncode == 0:
            debug(f"ssh up after {i + 1} attempts")
            break
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in (proc.stderr or ""):
            raise FleetError(
                f"host key mismatch for {host} — the injected key is not what answered. "
                "Destroy this node; do not trust it."
            )
        time.sleep(delay)
    else:
        raise FleetError(f"SSH on {host}:{port} never came up")

    step("waiting for cloud-init to finish")
    for _ in range(attempts):
        proc = ssh_run(inv, host, "test -f /var/lib/fleet-bootstrapped", port=port,
                       check=False, timeout=25)
        if proc.returncode == 0:
            return
        time.sleep(delay)
    tail = ssh_run(inv, host, "tail -40 /var/log/fleet-bootstrap.log 2>/dev/null || "
                              "tail -40 /var/log/cloud-init-output.log",
                   port=port, check=False, timeout=30)
    raise FleetError(f"bootstrap never completed on {host}:\n{tail.stdout or tail.stderr}")


def push_bootstrap(inv: Inventory, host: str, script: str, *, port: int = 22,
                   attempts: int = 40, delay: float = 10.0) -> None:
    """Phase two for providers with no cloud-init: run the build over SSH.

    Connects on the provider's DEFAULT ssh port (22) with the key the order was
    created with, and pipes `bootstrap_script()` into `bash -s`.

    **Host-key checking is deliberately off for this one connection**, and only
    this one. At this moment the node still carries the random host key its image
    generated; the script's whole job includes replacing it with the key fleet
    pinned. Pinning cannot happen earlier because we did not know that key, and
    strict checking here would simply refuse every first contact. The exposure is
    one connection on a fresh machine, and it closes immediately: every subsequent
    connection goes to `inv.defaults.ssh_port` under `StrictHostKeyChecking=yes`
    against the pinned key, and `wait_ssh()` treats a mismatch there as fatal.
    """
    import time

    base = [
        "ssh", "-p", str(port),
        "-i", os.path.expanduser(inv.defaults.ssh_key),
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", f"ConnectTimeout={inv.defaults.connect_timeout}",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        f"{inv.defaults.ssh_user}@{host}",
    ]

    step(f"waiting for SSH on {host}:{port} (pre-bootstrap)")
    for i in range(attempts):
        proc = run(base + ["true"], check=False, timeout=25)
        if proc.returncode == 0:
            debug(f"pre-bootstrap ssh up after {i + 1} attempts")
            break
        time.sleep(delay)
    else:
        raise FleetError(
            f"{host}:{port} never accepted SSH, so the bootstrap could not be pushed.\n"
            f"  This provider has no cloud-init, so there is no other way in — the\n"
            f"  node is unusable. Check the order's SSH key was attached at creation."
        )

    step("pushing the bootstrap over SSH (no cloud-init on this provider)")
    proc = run(base + ["bash -s"], check=False, timeout=1800, stdin=script)
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        raise FleetError(f"ssh bootstrap failed on {host}:\n{tail}")
    ok("bootstrap ran")


def push_xray_config(inv: Inventory, host: str, config: dict, *, port: int | None = None) -> None:
    """Validate remotely, then swap atomically. Never restart onto a config that fails."""
    payload = json.dumps(config, indent=2, ensure_ascii=False)
    scp_put(inv, host, payload, "/usr/local/etc/xray/config.candidate.json",
            mode="0640", port=port)
    test = ssh_run(
        inv, host,
        "/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.candidate.json",
        port=port, check=False, timeout=90,
    )
    if test.returncode != 0:
        ssh_run(inv, host, "rm -f /usr/local/etc/xray/config.candidate.json",
                port=port, check=False)
        raise FleetError(
            f"generated config was rejected by xray on {host}:\n"
            f"{(test.stderr or test.stdout).strip()[:1500]}"
        )
    ssh_run(
        inv, host,
        "cp -a /usr/local/etc/xray/config.json /usr/local/etc/xray/config.json.prev "
        "2>/dev/null; "
        "mv /usr/local/etc/xray/config.candidate.json /usr/local/etc/xray/config.json && "
        "chown xray:xray /usr/local/etc/xray/config.json && "
        "systemctl restart xray",
        port=port, timeout=90,
    )
    status = ssh_run(inv, host, "sleep 2; systemctl is-active xray", port=port, check=False,
                     timeout=60)
    if (status.stdout or "").strip() != "active":
        journal = ssh_run(inv, host, "journalctl -u xray -n 30 --no-pager", port=port,
                          check=False, timeout=60)
        rollback = ssh_run(
            inv, host,
            "test -f /usr/local/etc/xray/config.json.prev && "
            "mv /usr/local/etc/xray/config.json.prev /usr/local/etc/xray/config.json && "
            "systemctl restart xray",
            port=port, check=False, timeout=60,
        )
        raise FleetError(
            f"xray failed to start on {host} (rolled back: "
            f"{'yes' if rollback.returncode == 0 else 'NO — node is down'})\n"
            f"{(journal.stdout or '').strip()[:1500]}"
        )
    log(f"xray reloaded on {host}")
