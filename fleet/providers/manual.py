"""A server you bought by hand and will never let a script destroy.

This is the RU entry node. It cannot be anonymous (Russian hosts must identify
customers via Gosuslugi/ESIA or a passport) and it must not be churned, so the
driver refuses to create or destroy anything and only reports what the inventory
already told us. See docs/01-research-findings.md §4.
"""
from __future__ import annotations

from ..util import FleetError
from . import register
from .base import Provider, ServerHandle, ServerSpec


@register
class Manual(Provider):
    name = "manual"
    token_env = ""

    def create(self, spec: ServerSpec) -> ServerHandle:
        raise FleetError(
            "provider `manual` cannot create servers.\n"
            "  Buy the node by hand, then put its IP in inventory.toml as entry.host\n"
            "  and run `fleet entry deploy`."
        )

    def get(self, provider_id: str) -> ServerHandle | None:
        return ServerHandle(
            provider=self.name, provider_id=provider_id, name=provider_id,
            ipv4=provider_id, status="live",
        )

    def destroy(self, provider_id: str) -> None:
        raise FleetError(
            f"refusing to destroy manual node {provider_id} — delete it in the provider's "
            "panel if you really mean it."
        )
