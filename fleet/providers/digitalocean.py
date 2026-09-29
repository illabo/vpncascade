"""DigitalOcean."""
from __future__ import annotations

from ..util import FleetError, http
from . import register
from .base import Provider, ServerHandle, ServerSpec

API = "https://api.digitalocean.com/v2"
TAG = "fleet-exit"


@register
class DigitalOcean(Provider):
    name = "digitalocean"
    token_env = "DIGITALOCEAN_TOKEN"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def _handle(self, d: dict) -> ServerHandle:
        v4 = v6 = ""
        for net in (d.get("networks") or {}).get("v4", []):
            if net.get("type") == "public":
                v4 = net.get("ip_address", "")
        for net in (d.get("networks") or {}).get("v6", []):
            if net.get("type") == "public":
                v6 = net.get("ip_address", "")
        return ServerHandle(
            provider=self.name,
            provider_id=str(d["id"]),
            name=d.get("name", ""),
            ipv4=v4,
            ipv6=v6,
            region=(d.get("region") or {}).get("slug", ""),
            status="live" if d.get("status") == "active" and v4 else d.get("status", "new"),
        )

    def create(self, spec: ServerSpec) -> ServerHandle:
        body = {
            "name": spec.name,
            "region": spec.region or "fra1",
            "size": spec.server_type or "s-1vcpu-1gb",
            "image": spec.image or "debian-12-x64",
            "user_data": spec.user_data,
            "ipv6": bool(spec.opts.get("ipv6", True)),
            "monitoring": False,
            "tags": [TAG] + [f"{k}-{v}" for k, v in spec.labels.items() if k != "fleet"],
        }
        keys = spec.opts.get("ssh_key_ids")
        if keys:
            body["ssh_keys"] = list(keys)
        resp = http("POST", f"{API}/droplets", headers=self._headers(), body=body)
        return self._handle(resp["droplet"])

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{API}/droplets/{provider_id}", headers=self._headers())
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle(resp["droplet"])

    def destroy(self, provider_id: str) -> None:
        try:
            http(
                "DELETE",
                f"{API}/droplets/{provider_id}",
                headers=self._headers(),
                expect=(202, 204, 404),
            )
        except FleetError as e:
            if "404" not in str(e):
                raise

    def list_fleet(self) -> list[ServerHandle]:
        resp = http("GET", f"{API}/droplets?tag_name={TAG}", headers=self._headers())
        return [self._handle(d) for d in resp.get("droplets", [])]
