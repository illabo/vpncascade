"""Provider interface.

Deliberately tiny. Renting a throwaway VPS is three operations, and keeping the
interface at three operations is what makes providers hot-swappable — which is the
actual security property we want (one provider's ban wave costs one exit, not the
fleet). See docs/01-research-findings.md §1d.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any

from ..util import FleetError


@dataclass
class ServerSpec:
    """What we ask a provider for."""

    name: str
    user_data: str
    region: str = ""
    server_type: str = ""
    image: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    opts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerHandle:
    """What a provider gives back."""

    provider: str
    provider_id: str
    name: str
    ipv4: str = ""
    ipv6: str = ""
    region: str = ""
    status: str = "provisioning"
    expires_at: int | None = None  # providers with built-in TTL (SporeStack)
    extra: dict[str, Any] = field(default_factory=dict)


class Provider:
    name: str = "base"
    #: env var holding the API credential
    token_env: str = ""
    #: does this provider destroy servers by itself when unpaid?
    self_expiring: bool = False

    def __init__(self, opts: dict[str, Any] | None = None):
        self.opts = opts or {}

    # ------------------------------------------------------------- helpers

    def token(self) -> str:
        env = self.opts.get("token_env") or self.token_env
        value = os.environ.get(env, "").strip()
        if not value:
            raise FleetError(
                f"provider `{self.name}` needs a credential in ${env}\n"
                f"  export {env}=... (or put it in .env and run `fleet` via `bin/fleet`)"
            )
        return value

    @staticmethod
    def pick_region(regions: list[str], avoid: list[str] | None = None) -> str:
        """Pick a region, preferring one not already in use so exits stay scattered."""
        if not regions:
            return ""
        avoid = avoid or []
        fresh = [r for r in regions if r not in avoid]
        return random.choice(fresh or regions)

    # -------------------------------------------------------------- the API

    def create(self, spec: ServerSpec) -> ServerHandle:
        raise NotImplementedError

    def get(self, provider_id: str) -> ServerHandle | None:
        raise NotImplementedError

    def destroy(self, provider_id: str) -> None:
        raise NotImplementedError

    def list_fleet(self) -> list[ServerHandle]:
        """Servers at this provider that carry our label. Used to find orphans."""
        return []

    def extend(self, provider_id: str, hours: int) -> None:
        """Only meaningful for self-expiring providers."""
        raise FleetError(f"provider `{self.name}` does not support extend")
