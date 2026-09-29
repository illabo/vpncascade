"""Hetzner Cloud."""
from __future__ import annotations

from ..util import FleetError, http
from . import register
from .base import Provider, ServerHandle, ServerSpec

API = "https://api.hetzner.cloud/v1"


@register
class Hetzner(Provider):
    name = "hetzner"
    token_env = "HCLOUD_TOKEN"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _handle(self, s: dict) -> ServerHandle:
        net = s.get("public_net") or {}
        return ServerHandle(
            provider=self.name,
            provider_id=str(s["id"]),
            name=s.get("name", ""),
            ipv4=(net.get("ipv4") or {}).get("ip", "") or "",
            ipv6=((net.get("ipv6") or {}).get("ip", "") or "").split("/")[0],
            region=((s.get("datacenter") or {}).get("location") or {}).get("name", ""),
            status="live" if s.get("status") == "running" else s.get("status", "unknown"),
        )

    def create(self, spec: ServerSpec) -> ServerHandle:
        body = {
            "name": spec.name,
            "server_type": spec.server_type or "cx22",
            "image": spec.image or "debian-12",
            "location": spec.region or "hel1",
            "user_data": spec.user_data,
            "start_after_create": True,
            "labels": spec.labels,
            "public_net": {
                "enable_ipv4": True,
                "enable_ipv6": bool(spec.opts.get("ipv6", True)),
            },
        }
        key_names = spec.opts.get("ssh_key_names")
        if key_names:
            body["ssh_keys"] = list(key_names)
        resp = http("POST", f"{API}/servers", headers=self._headers(), body=body)
        return self._handle(resp["server"])

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{API}/servers/{provider_id}", headers=self._headers())
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle(resp["server"])

    def destroy(self, provider_id: str) -> None:
        try:
            http(
                "DELETE",
                f"{API}/servers/{provider_id}",
                headers=self._headers(),
                expect=(200, 202, 204, 404),
            )
        except FleetError as e:
            if "404" not in str(e):
                raise

    def list_fleet(self) -> list[ServerHandle]:
        resp = http(
            "GET",
            f"{API}/servers?label_selector=fleet%3Dexit&per_page=50",
            headers=self._headers(),
        )
        return [self._handle(s) for s in resp.get("servers", [])]
