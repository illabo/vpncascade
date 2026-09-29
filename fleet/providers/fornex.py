"""Fornex (AS48040).

Why bother with a provider this awkward: AS48040 is a **22-prefix ASN**, small
enough that RKN's sweeps have not bothered with it, and from the Russian Far East
it measures **167 ms against 293 ms to Singapore** — European exits are *faster*
from there because the traffic transits Moscow either way. It is a third ASN
for a fleet that would otherwise sit on two.

**Read this before enabling it.** Fornex is not a drop-in cloud provider, and three
of its quirks will cost you money or a broken node if you meet them by surprise:

1. **`POST /orders/vps/` is disabled by default.** The account needs a grant from
   their support before it works at all; until then every create returns
   `403 {"detail": "You do not have permission to perform this action."}`.
   That 403 is *not* a bad token — the API returns **401** for a missing, invalid
   or unprefixed key, so 401-vs-403 tells you which you are looking at.

2. **Billing is whole months and there is no early cancel.** `term` is in months
   (1/3/6/12), and `POST /orders/{order_id}/cancel/` defaults to `when=expired`,
   which only blocks autorenewal; `when=now` is documented as *"not yet
   supported"*. So a node destroyed after three days still costs a full month.
   **Never put Fornex in a fast-rotating pool** — at `max_age_hours = 72` it bills
   roughly ten fresh months a month. It belongs on a slow rotation, as a stable
   diversity anchor beside providers that bill hourly.

3. **There is no `user_data` / cloud-init field.** The only lever at create time is
   `ssh_keys`, so provisioning is two-phase: order with a key, then push
   `deploy.bootstrap_script()` over SSH. `needs_ssh_bootstrap` below is what tells
   `ops.provision_exit` to do that.

Two smaller traps, both handled here:

* `ssh_keys` must be passed **explicitly**. Their docs: *"By default, an empty list
  is passed, even if there are keys marked as 'add by default' in your dashboard."*
  Combined with the absent cloud-init, forgetting it yields a running server with
  no way in at all — a paid brick.
* `save_backup` on cancel **defaults to true**, snapshotting the disk before
  deletion. Snapshots are chargeable, so every teardown would quietly accrue
  storage. We send `false`.

Capacity is scarce and moves: `disabled_by_slots` on the locations endpoint flips
within minutes, and it disagreed with the control panel at least once, so treat it
as advisory and let the caller re-roll.
"""
from __future__ import annotations

import json

from ..util import FleetError, http, debug
from . import register
from .base import Provider, ServerHandle, ServerSpec

API = "https://fornex.com/api"

#: Order states Fornex reports once the machine is usable.
LIVE_STATES = {"active", "running", "enabled", "work", "working"}


@register
class Fornex(Provider):
    name = "fornex"
    token_env = "FORNEX_API_KEY"

    #: No user_data field exists. `ops.provision_exit` must push the bootstrap over
    #: SSH after the node boots. See the module docstring.
    needs_ssh_bootstrap = True

    # ------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        key = self.token()
        if " " in key or not key:
            raise FleetError(
                f"${self.token_env} looks wrong: it should be the raw key from the\n"
                f"  Fornex client area, of the form `<prefix>.<secret>`, with no\n"
                f"  `Api-Key ` prefix — this driver adds that."
            )
        return {"Authorization": f"Api-Key {key}", "Content-Type": "application/json"}

    def _explain(self, e: FleetError) -> FleetError:
        """Turn Fornex's terse 403 into the answer, not a scavenger hunt.

        The support grant gates a whole CLUSTER of endpoints, not just the create
        call — measured 2026-09-29:

            gated : POST /orders/vps/, /orders/vps/templates/, /orders/vps/apps/,
                    /orders/vps/snapshots/, /orders/vps/services/
            open  : /orders/vps/locations/, /orders/vps/disks/, /plans/vps,
                    GET /orders/vps/, /ssh_keys/, /account/*, /project/

        So a 403 while merely *resolving an image* is the same missing grant, and
        saying so beats letting the operator debug a template lookup.
        """
        if "403" in str(e):
            return FleetError(
                "fornex: HTTP 403 — the VPS-creation API is not enabled on this account.\n"
                "  It is DISABLED BY DEFAULT. Ask support to enable it:\n"
                "    https://fornex.com/my/tickets/new/\n"
                "  The grant covers creation AND the catalogue endpoints it needs\n"
                "  (templates, apps, snapshots, services), so image lookups fail too.\n"
                "  This is NOT an auth problem: a bad, missing or unprefixed key returns\n"
                "  401, not 403. Nor is it billing, if the account balance is positive.\n"
                "  Workaround until then: set `image` and `server_type` in inventory.toml\n"
                "  to numeric ids, which skips the gated lookups — though create itself\n"
                "  stays blocked.\n"
                f"  Original: {e}"
            )
        return e

    def _get(self, path: str):
        """GET with the 403 translated. Every catalogue read goes through this."""
        try:
            return http("GET", f"{API}{path}", headers=self._headers())
        except FleetError as e:
            raise self._explain(e) from None

    @staticmethod
    def _handle_from(order: dict, name: str = "") -> ServerHandle:
        ip = ""
        # Addresses appear under different keys depending on which endpoint answered.
        for key in ("ip", "ipv4", "main_ip", "ip_address"):
            v = order.get(key)
            if isinstance(v, str) and v:
                ip = v
                break
        if not ip:
            addrs = order.get("ip_addresses") or order.get("ips") or []
            for a in addrs:
                cand = a.get("ip") if isinstance(a, dict) else a
                if isinstance(cand, str) and ":" not in cand:
                    ip = cand
                    break
        state = str(order.get("status") or order.get("state") or "").lower()
        return ServerHandle(
            provider="fornex",
            provider_id=str(order.get("order_id") or order.get("id") or ""),
            name=order.get("name") or name,
            ipv4=ip,
            ipv6=str(order.get("ipv6") or ""),
            region=str(order.get("location") or ""),
            status="live" if (state in LIVE_STATES and ip) else (state or "provisioning"),
            extra={"state": state},
        )

    # ------------------------------------------------------------ catalogue

    def _free_locations(self) -> list[str]:
        """Country codes with a free slot right now.

        Advisory only: the endpoint takes no parameters, and it has been observed
        reporting a location free while the control panel showed it sold out — real
        availability is probably per plan and frequency. A create can still fail on
        a location listed here; that is what the caller's re-roll is for.
        """
        resp = self._get("/orders/vps/locations/")
        rows = resp if isinstance(resp, list) else (resp.get("results") or [])
        return [r["value"] for r in rows if not r.get("disabled_by_slots")]

    def _resolve_template(self, image: str) -> int:
        """Map an image name like `debian-13` to a template id."""
        if str(image).isdigit():
            return int(image)
        resp = self._get("/orders/vps/templates/")
        rows = resp if isinstance(resp, list) else (resp.get("results") or [])
        wanted = [w for w in str(image or "debian-13").lower().replace("-", " ").split() if w]
        matches = [r for r in rows if all(w in str(r.get("name", "")).lower() for w in wanted)]
        if not matches:
            avail = ", ".join(sorted(str(r.get("name", "")) for r in rows)[:8])
            raise FleetError(
                f"fornex: no template matching `{image}`.\n"
                f"  available include: {avail}\n"
                f"  or set `image` to a numeric template id in inventory.toml"
            )
        # Prefer the plainest match: a bare OS over one bundled with a control panel.
        matches.sort(key=lambda r: len(str(r.get("name", ""))))
        return int(matches[0]["id"])

    def _resolve_tariff(self, server_type: str) -> int:
        if str(server_type).isdigit():
            return int(server_type)
        resp = self._get("/plans/vps")
        rows = resp if isinstance(resp, list) else (resp.get("results") or [])
        want = str(server_type or "").strip().lower()
        matches = [r for r in rows if str(r.get("name", "")).strip().lower() == want]
        if not matches:
            matches = [r for r in rows if want and want in str(r.get("name", "")).lower()]
        if not matches:
            cheapest = sorted(rows, key=lambda r: float(r.get("price") or 1e9))[:6]
            avail = ", ".join(f"{r.get('name')} (EUR {r.get('price')})" for r in cheapest)
            raise FleetError(
                f"fornex: no plan matching `{server_type}`.\n"
                f"  cheapest available: {avail}\n"
                f"  set `server_type` to a plan name or numeric tariff id"
            )
        matches.sort(key=lambda r: float(r.get("price") or 1e9))
        return int(matches[0]["id"])

    def _ssh_key_id(self, pubkey: str) -> int:
        """Return the id of this public key at Fornex, registering it if new.

        Matched on the key body rather than the comment, because the comment
        differs between machines while the key does not.
        """
        body = pubkey.strip().split()
        fingerprintable = body[1] if len(body) > 1 else pubkey.strip()
        resp = self._get("/ssh_keys/")
        rows = resp if isinstance(resp, list) else (resp.get("results") or [])
        for r in rows:
            existing = str(r.get("key", "")).strip().split()
            if len(existing) > 1 and existing[1] == fingerprintable:
                return int(r["id"])
        created = http(
            "POST", f"{API}/ssh_keys/", headers=self._headers(),
            body={"key": pubkey.strip(), "title": "fleet"},
        )
        return int(created["id"])

    # -------------------------------------------------------------- the API

    def create(self, spec: ServerSpec) -> ServerHandle:
        pubkey = spec.opts.get("ssh_key")
        if not pubkey:
            raise FleetError(
                "fornex: no SSH public key was supplied, and Fornex has no cloud-init.\n"
                "  Without a key the node would boot with no way to reach it at all.\n"
                "  Set `ssh_key` in [defaults] so fleet can pass it."
            )

        # Prefer a region that currently has a slot. Availability churns by the
        # minute, so a static inventory `regions` list goes stale fast.
        region = spec.region
        try:
            free = self._free_locations()
            if free and region not in free:
                chosen = self.pick_region(free)
                if region:
                    debug(f"fornex: {region} has no free slot; using {chosen}")
                region = chosen
            elif not free:
                debug("fornex: no location reports a free slot; trying anyway")
        except FleetError as e:
            debug(f"fornex: could not list locations ({e}); using {region} as configured")

        body: dict = {
            "location": region or "SE",
            "tariff": self._resolve_tariff(spec.server_type),
            "template": self._resolve_template(spec.image),
            # Months. 1 is the minimum Fornex sells; see the module docstring on why
            # this provider must not be rotated fast.
            "term": int(spec.opts.get("term") or 1),
            # MUST be explicit — "add by default" keys in the dashboard are ignored
            # by the API, and with no cloud-init an empty list means no way in.
            "ssh_keys": [self._ssh_key_id(pubkey)],
            "name": spec.name[:25],
        }
        disk_class = spec.opts.get("disk_class")
        if disk_class:
            body["disk_class"] = int(disk_class)
        if spec.labels:
            # Fornex has no label map, only a flat tag list.
            body["tags"] = [f"{k}={v}" for k, v in spec.labels.items()]

        try:
            resp = http("POST", f"{API}/orders/vps/", headers=self._headers(), body=body)
        except FleetError as e:
            raise self._explain(e) from None

        order_id = str((resp or {}).get("order_id") or "")
        if not order_id:
            raise FleetError(f"fornex: create returned no order_id: {json.dumps(resp)[:200]}")
        # The create response carries only the id; the address arrives later.
        return ServerHandle(provider=self.name, provider_id=order_id, name=spec.name,
                            region=region, status="provisioning")

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{API}/orders/vps/{provider_id}/", headers=self._headers())
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle_from(resp or {})

    def destroy(self, provider_id: str) -> None:
        """Cancel the order.

        Note what this does NOT do: it does not stop the billing you have already
        incurred. `when=now` is documented as "not yet supported", so the order runs
        to the end of its paid month regardless. We still send it, so that the day
        Fornex implements it this driver starts benefiting without a change.

        `save_backup` is forced off: it defaults to TRUE, and a snapshot per
        teardown is a charge nobody asked for.
        """
        try:
            http(
                "POST", f"{API}/orders/{provider_id}/cancel/", headers=self._headers(),
                body={"when": "now", "save_backup": False},
                expect=(200, 201, 202, 204, 404),
            )
        except FleetError as e:
            if "404" in str(e):
                return
            raise

    # ------------------------------------------------------------- orphans

    def list_fleet(self) -> list[ServerHandle]:
        """Our servers, identified by the tag `create()` sets.

        Fornex has no server-side tag filter, so this filters client-side. Anything
        without our tag is left alone — this account may hold servers that are
        nothing to do with fleet, and reaping those would be unforgivable.
        """
        resp = self._get("/orders/vps/")
        rows = resp if isinstance(resp, list) else (resp.get("results") or [])
        out = []
        for r in rows:
            tags = [str(t) for t in (r.get("tags") or [])]
            if "fleet=exit" not in tags and "role=exit" not in tags:
                continue
            out.append(self._handle_from(r))
        return out
