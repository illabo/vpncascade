"""SporeStack — anonymous reseller in front of DigitalOcean / Vultr / Slow Servers.

The interesting property for us is that servers carry an expiry: you buy N days and
the server is powered off and then deleted when the balance runs out. Self-destruct
is the default rather than something cron has to remember, so a dead laptop still
means a dead fleet. We always launch with autorenew disabled for that reason.

The API token is a bearer credential *and* the wallet — it goes in the URL path, so
never log a SporeStack URL at anything but debug level.
"""
from __future__ import annotations

import math
import os

from ..util import FleetError, http, now
from . import register
from .base import Provider, ServerHandle, ServerSpec

CLEARNET = "https://api.sporestack.com"
TOR = "http://api.spore64i5sofqlfz5gq2ju4msgzojjwifls7rok2cti624zyq3fcelad.onion"


@register
class SporeStack(Provider):
    name = "sporestack"
    token_env = "SPORESTACK_TOKEN"
    self_expiring = True

    @property
    def api(self) -> str:
        if os.environ.get("SPORESTACK_USE_TOR_ENDPOINT", "") not in ("", "0"):
            return TOR  # requires a local SOCKS proxy / torsocks around the process
        return self.opts.get("endpoint", CLEARNET)

    def _base(self) -> str:
        return f"{self.api}/token/{self.token()}"

    def _handle(self, s: dict) -> ServerHandle:
        ipv4 = s.get("ipv4", "") or ""
        return ServerHandle(
            provider=self.name,
            provider_id=str(s["machine_id"]),
            name=s.get("hostname", ""),
            ipv4=ipv4,
            ipv6=s.get("ipv6", "") or "",
            region=s.get("region", "") or "",
            status="live" if s.get("running") and ipv4 else "provisioning",
            expires_at=s.get("expiration"),
            extra={"autorenew": s.get("autorenew"), "provider": s.get("provider")},
        )

    # ------------------------------------------------------------ catalogue

    def slugs(self, kind: str, upstream: str | None = None) -> list[dict]:
        if kind not in ("os", "flavors", "regions"):
            raise FleetError("kind must be one of: os, flavors, regions")
        url = f"{self.api}/slugs/{kind}"
        if upstream:
            url += f"?provider={upstream}"
        return http("GET", url) or []

    def balance(self) -> dict:
        return http("GET", f"{self._base()}/balance") or {}

    # ----------------------------------------------------------------- api

    def create(self, spec: ServerSpec) -> ServerHandle:
        days = int(spec.opts.get("days") or 1)
        body = {
            "days": max(1, days),
            "flavor": spec.server_type or "vps-1vcpu-1gb",
            "operating_system": spec.image or "debian-12",
            "region": spec.region or None,
            "provider": spec.opts.get("upstream_provider") or "digitalocean",
            "hostname": spec.name,
            "autorenew": False,  # the point of using SporeStack: it dies on its own
            "user_data": spec.user_data,
        }
        ssh_key = spec.opts.get("ssh_key")
        if ssh_key:
            body["ssh_key"] = ssh_key
        body = {k: v for k, v in body.items() if v is not None}

        resp = http("POST", f"{self._base()}/servers", body=body)
        machine_id = resp["machine_id"]
        # The launch response carries no addresses; poll for them.
        return ServerHandle(
            provider=self.name,
            provider_id=str(machine_id),
            name=spec.name,
            status="provisioning",
        )

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{self._base()}/servers/{provider_id}")
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle(resp)

    def destroy(self, provider_id: str) -> None:
        try:
            http("DELETE", f"{self._base()}/servers/{provider_id}", expect=(200, 204, 404))
        except FleetError as e:
            if "404" not in str(e):
                raise

    def list_fleet(self) -> list[ServerHandle]:
        resp = http("GET", f"{self._base()}/servers")
        servers = resp.get("servers", []) if isinstance(resp, dict) else (resp or [])
        return [self._handle(s) for s in servers]

    def extend(self, provider_id: str, hours: int) -> None:
        days = max(1, min(90, math.ceil(hours / 24)))  # API caps a topup at 90 days
        http("POST", f"{self._base()}/servers/{provider_id}/topup", body={"days": days})

    def expiry_slack_hours(self, handle: ServerHandle) -> float:
        if not handle.expires_at:
            return float("inf")
        return (handle.expires_at - now()) / 3600.0
