"""Vultr (API v2)."""
from __future__ import annotations

import base64

from ..util import FleetError, http
from . import register
from .base import Provider, ServerHandle, ServerSpec

API = "https://api.vultr.com/v2"
TAG = "fleet-exit"


@register
class Vultr(Provider):
    name = "vultr"
    token_env = "VULTR_API_KEY"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _handle(self, i: dict) -> ServerHandle:
        ip = i.get("main_ip", "") or ""
        if ip == "0.0.0.0":  # Vultr's placeholder while the instance is building
            ip = ""
        return ServerHandle(
            provider=self.name,
            provider_id=str(i["id"]),
            name=i.get("label", ""),
            ipv4=ip,
            ipv6=i.get("v6_main_ip", "") or "",
            region=i.get("region", ""),
            status="live" if i.get("server_status") == "ok" and ip else i.get("status", "pending"),
        )

    def resolve_os_id(self, image: str) -> int:
        """Vultr wants a numeric os_id. Accept either the number or a name fragment."""
        if image.isdigit():
            return int(image)
        resp = http("GET", f"{API}/os?per_page=500", headers=self._headers())
        wanted = image.lower().replace("-", " ")
        matches = [
            o
            for o in resp.get("os", [])
            if wanted in o.get("name", "").lower().replace("-", " ")
            and o.get("arch") == "x64"
        ]
        if not matches:
            raise FleetError(
                f"vultr: no OS matching `{image}`; set os_id explicitly in inventory.toml"
            )
        return int(matches[0]["id"])

    def create(self, spec: ServerSpec) -> ServerHandle:
        os_id = spec.opts.get("os_id") or self.resolve_os_id(spec.image or "debian 12")
        body = {
            "region": spec.region or "fra",
            "plan": spec.server_type or "vc2-1c-1gb",
            "os_id": int(os_id),
            "label": spec.name,
            "hostname": spec.name,
            "user_data": base64.b64encode(spec.user_data.encode()).decode(),
            "enable_ipv6": bool(spec.opts.get("ipv6", True)),
            "backups": "disabled",
            "ddos_protection": False,
            "tags": [TAG],
        }
        keys = spec.opts.get("ssh_key_ids")
        if keys:
            body["sshkey_id"] = list(keys)
        resp = http("POST", f"{API}/instances", headers=self._headers(), body=body)
        return self._handle(resp["instance"])

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{API}/instances/{provider_id}", headers=self._headers())
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle(resp["instance"])

    def destroy(self, provider_id: str) -> None:
        try:
            http(
                "DELETE",
                f"{API}/instances/{provider_id}",
                headers=self._headers(),
                expect=(204, 404),
            )
        except FleetError as e:
            if "404" not in str(e):
                raise

    def list_fleet(self) -> list[ServerHandle]:
        resp = http("GET", f"{API}/instances?tag={TAG}&per_page=100", headers=self._headers())
        return [self._handle(i) for i in resp.get("instances", [])]
