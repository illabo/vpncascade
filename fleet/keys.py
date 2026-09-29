"""Key material: REALITY X25519 pairs, UUIDs, shortIds, SSH host keys, WireGuard keys.

Deliberately dependency-free — this runs from cron on whatever box is handy, so it
uses only the stdlib plus the system `ssh-keygen`. X25519 is implemented inline per
RFC 7748; it is used only for keygen, never on a data path, so speed is irrelevant.
"""
from __future__ import annotations

import base64
import os
import secrets
import subprocess
import tempfile
import uuid

# --------------------------------------------------------------------------- X25519

_P = 2**255 - 19
_A24 = 121665


def _cswap(swap: int, a: int, b: int) -> tuple[int, int]:
    dummy = (-swap) & (a ^ b)
    return a ^ dummy, b ^ dummy


def _clamp(k: bytes) -> bytes:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return bytes(b)


def _x25519(scalar: bytes, u_coord: bytes) -> bytes:
    """RFC 7748 X25519 scalar multiplication."""
    k = int.from_bytes(_clamp(scalar), "little")
    u = int.from_bytes(u_coord, "little") & ((1 << 255) - 1)

    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        x2, x3 = _cswap(swap, x2, x3)
        z2, z3 = _cswap(swap, z2, z3)
        swap = kt

        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow((da + cb) % _P, 2, _P)
        z3 = x1 * pow((da - cb) % _P, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P

    x2, x3 = _cswap(swap, x2, x3)
    z2, z3 = _cswap(swap, z2, z3)
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASEPOINT = (9).to_bytes(32, "little")


def _derive_public(private_raw: bytes) -> bytes:
    return _x25519(private_raw, _BASEPOINT)


# ------------------------------------------------------------------- encodings

def b64url(raw: bytes) -> str:
    """Base64 raw-urlsafe, unpadded — the encoding `xray x25519` emits."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def reality_keypair() -> tuple[str, str]:
    """(privateKey, publicKey) in Xray REALITY format."""
    priv = _clamp(secrets.token_bytes(32))
    return b64url(priv), b64url(_derive_public(priv))


def reality_public_from_private(private_b64url: str) -> str:
    pad = "=" * (-len(private_b64url) % 4)
    raw = base64.urlsafe_b64decode(private_b64url + pad)
    return b64url(_derive_public(raw))


def wireguard_keypair() -> tuple[str, str]:
    """(privateKey, publicKey) base64-std — WireGuard / AmneziaWG format."""
    priv = _clamp(secrets.token_bytes(32))
    return base64.b64encode(priv).decode(), base64.b64encode(_derive_public(priv)).decode()


def wireguard_psk() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


# ------------------------------------------------------------------- identifiers

def new_uuid() -> str:
    return str(uuid.uuid4())


def short_id(nbytes: int = 8) -> str:
    """REALITY shortId: 1..8 bytes rendered as lowercase hex."""
    if not 1 <= nbytes <= 8:
        raise ValueError("shortId must be 1..8 bytes")
    return secrets.token_hex(nbytes)


def node_suffix() -> str:
    """Short random tag so hostnames never repeat across rotations."""
    return secrets.token_hex(3)


# ------------------------------------------------------------------- SSH

def ssh_host_keypair() -> tuple[str, str]:
    """Generate an ed25519 SSH host key via ssh-keygen. Returns (private, public)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hostkey")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fleet-node", "-f", path],
            check=True,
            capture_output=True,
        )
        with open(path) as fh:
            private = fh.read()
        with open(path + ".pub") as fh:
            public = fh.read().strip()
    return private, public


def read_ssh_pubkey(path: str) -> str:
    """Read the operator's SSH public key, accepting either the .pub or private path."""
    expanded = os.path.expanduser(path)
    if not expanded.endswith(".pub") and os.path.exists(expanded + ".pub"):
        expanded += ".pub"
    if not os.path.exists(expanded):
        raise FileNotFoundError(
            f"no SSH public key at {expanded} — set ssh_key in inventory.toml"
        )
    with open(expanded) as fh:
        return fh.read().strip()
