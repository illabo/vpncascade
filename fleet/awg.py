"""AmneziaWG: the UDP fast path that runs alongside VLESS on the entry node.

Why both: VLESS/REALITY is TCP+TLS and survives networks that suppress UDP wholesale
(routine on Russian mobile during "shutdown" events), but it costs real CPU. AmneziaWG
is UDP, far cheaper, and better for anything latency-sensitive — and it dies the moment
UDP is being throttled. They fail in different conditions, so running both gives you a
fallback that is not correlated with the thing that broke.

Generations, because the right answer changed recently and the wrong one silently
fails to connect:

* **1.0** — Jc/Jmin/Jmax, S1/S2, H1-H4. What GL.iNet's 4.8.2 *beta* spoke.
* **1.5** — adds I1-I5, the Custom Protocol Signature: the first packet carries a hex
  snapshot of a real protocol (a QUIC Initial, a DNS query) so it *is* a plausible
  packet rather than merely random.
* **2.0** — adds S3/S4 (random prefixes on Cookie and Data packets, where 1.0 only
  masked Init and Response) and dynamic header ranges.

**GL.iNet firmware v4.9 and later supports AmneziaWG 2.0**, per their own docs, and
4.9.0 went stable for the Beryl AX on 2026-09-04. So 2.0 is the default here. On
anything still running 4.8.x, pin `generation="1.0"` or the peer will not connect —
every peer on an interface must agree on these values.

I1-I5 are deliberately **not** auto-generated: a signature is only useful if it mimics
a protocol that is plausible on your path, which is a judgement call, and GL.iNet
notes they must be set by hand. Supply them explicitly if you want them.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field

from . import keys


@dataclass
class ObfuscationParams:
    """AmneziaWG 1.0 obfuscation knobs. Every peer on an interface must match these."""

    jc: int          # junk packet count sent before the handshake
    jmin: int        # min junk packet size
    jmax: int        # max junk packet size
    s1: int          # random prefix prepended to the Init packet
    s2: int          # random prefix prepended to the Response packet
    h1: int          # custom message type for Init
    h2: int          # ... Response
    h3: int          # ... Cookie
    h4: int          # ... Data
    s3: int = 0      # 2.0: random prefix on the Cookie packet
    s4: int = 0      # 2.0: random prefix on the Data packet
    #: 1.5+: Custom Protocol Signature packets. Never auto-generated — see the module
    #: docstring. Supply hex snapshots of a protocol plausible on your path.
    i1: str = ""
    i2: str = ""
    i3: str = ""
    i4: str = ""
    i5: str = ""

    def as_conf(self, *, generation: str = "2.0") -> list[str]:
        lines = [
            f"Jc = {self.jc}", f"Jmin = {self.jmin}", f"Jmax = {self.jmax}",
            f"S1 = {self.s1}", f"S2 = {self.s2}",
        ]
        if generation >= "2.0" and (self.s3 or self.s4):
            lines += [f"S3 = {self.s3}", f"S4 = {self.s4}"]
        lines += [f"H1 = {self.h1}", f"H2 = {self.h2}",
                  f"H3 = {self.h3}", f"H4 = {self.h4}"]
        if generation >= "1.5":
            for n, v in ((1, self.i1), (2, self.i2), (3, self.i3),
                         (4, self.i4), (5, self.i5)):
                if v:
                    lines.append(f"I{n} = {v}")
        return lines


def generate_params(generation: str = "2.0") -> ObfuscationParams:
    """Random but valid obfuscation parameters for the requested generation.

    Constraints that matter and are easy to get wrong:
      * S1 + 56 must not equal S2, or an obfuscated Init becomes indistinguishable
        from a Response and the handshake breaks.
      * H1..H4 must be distinct and must avoid 1..4, which are WireGuard's real
        message types — reusing them defeats the point.
      * Jmax must stay well inside the MTU or the junk packets fragment, which is
        itself a signal.
    """
    jmin = secrets.choice(range(24, 65))
    jmax = jmin + secrets.choice(range(20, 120))

    while True:
        s1 = secrets.choice(range(15, 133))
        s2 = secrets.choice(range(15, 133))
        if s1 + 56 != s2:
            break

    headers: set[int] = set()
    while len(headers) < 4:
        headers.add(secrets.randbelow(2_147_483_000) + 5)
    h1, h2, h3, h4 = sorted(headers)

    s3 = s4 = 0
    if generation >= "2.0":
        # 1.0 masked only Init and Response; 2.0 extends the same trick to the Cookie
        # and Data packets, which is where the residual length signature lived.
        s3 = secrets.choice(range(15, 133))
        s4 = secrets.choice(range(15, 133))

    return ObfuscationParams(
        jc=secrets.choice(range(3, 11)),
        jmin=jmin, jmax=min(jmax, 1180),
        s1=s1, s2=s2, s3=s3, s4=s4, h1=h1, h2=h2, h3=h3, h4=h4,
    )


@dataclass
class AwgInterface:
    listen_port: int = 51820
    subnet_v4: str = "10.13.13.0/24"
    server_ip: str = "10.13.13.1"
    mtu: int = 1320
    private_key: str = ""
    public_key: str = ""
    generation: str = "2.0"
    params: ObfuscationParams = field(default_factory=generate_params)
    peers: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, generation: str = "2.0", **kw) -> "AwgInterface":
        priv, pub = keys.wireguard_keypair()
        return cls(private_key=priv, public_key=pub, generation=generation,
                   params=generate_params(generation), **kw)

    def next_ip(self) -> str:
        base = self.subnet_v4.split("/")[0].rsplit(".", 1)[0]
        used = {p["address"].split("/")[0] for p in self.peers} | {self.server_ip}
        for host in range(2, 255):
            candidate = f"{base}.{host}"
            if candidate not in used:
                return candidate
        from .util import FleetError
        raise FleetError("AmneziaWG subnet is full")

    def add_peer(self, name: str) -> dict:
        priv, pub = keys.wireguard_keypair()
        peer = {
            "name": name,
            "private_key": priv,
            "public_key": pub,
            "preshared_key": keys.wireguard_psk(),
            "address": f"{self.next_ip()}/32",
        }
        self.peers.append(peer)
        return peer

    # ---------------------------------------------------------------- rendering

    def server_conf(self, *, generation: str | None = None) -> str:
        generation = generation or self.generation
        lines = [
            "[Interface]",
            f"Address = {self.server_ip}/{self.subnet_v4.split('/')[1]}",
            f"ListenPort = {self.listen_port}",
            f"PrivateKey = {self.private_key}",
            f"MTU = {self.mtu}",
            *self.params.as_conf(generation=generation),
            "",
            "# Traffic from this interface is handed to Xray by nftables tproxy, so it",
            "# follows the same split and the same cascade as VLESS clients. No NAT here.",
            "",
        ]
        for p in self.peers:
            lines += [
                f"# {p['name']}",
                "[Peer]",
                f"PublicKey = {p['public_key']}",
                f"PresharedKey = {p['preshared_key']}",
                f"AllowedIPs = {p['address']}",
                "",
            ]
        return "\n".join(lines)

    def peer_conf(self, peer: dict, endpoint: str, *, generation: str | None = None,
                  dns: str = "") -> str:
        generation = generation or self.generation
        lines = [
            f"# {peer['name']} — AmneziaWG {generation}",
            "[Interface]",
            f"PrivateKey = {peer['private_key']}",
            f"Address = {peer['address']}",
            f"MTU = {self.mtu}",
        ]
        if dns:
            lines.append(f"DNS = {dns}")
        lines += self.params.as_conf(generation=generation)
        lines += [
            "",
            "[Peer]",
            f"PublicKey = {self.public_key}",
            f"PresharedKey = {peer['preshared_key']}",
            f"Endpoint = {endpoint}:{self.listen_port}",
            "AllowedIPs = 0.0.0.0/0, ::/0",
            "PersistentKeepalive = 25",
        ]
        return "\n".join(lines) + "\n"

    def to_state(self) -> dict:
        d = asdict(self)
        d["params"] = asdict(self.params)
        return d

    @classmethod
    def from_state(cls, d: dict) -> "AwgInterface":
        d = dict(d)
        d["params"] = ObfuscationParams(**d["params"])
        return cls(**d)
