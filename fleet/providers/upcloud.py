"""UpCloud (API 1.3).

Why this provider exists in the tree: as of 2026-09 every other driver here points
at an ASN that RKN has swept — DigitalOcean AS14061, Hetzner AS24940, Vultr
AS20473, and by extension most of the budget hosts (Linode AS63949, Contabo
AS51167, FranTech AS53667, Netcup AS197540). UpCloud is AS202053, which appears on
neither the 391-AS full block nor the 47-AS dynamic list, and it has a Singapore
zone — a short Pacific hop that does not transit Moscow. It also takes an ordinary
payment card, which SporeStack does not.

Absence from a published block list is weaker evidence than a successful probe, so
treat the ASN as promising rather than proven: `fleet health` measures reachability
from inside Russia, and that is the only verdict that counts.

Two API quirks drive the shape of this file:

  * Auth is a **Bearer token** (`ucat_...`), created in the control panel under
    Account -> API Tokens. Shown once at creation, max 365 days. This driver
    originally said "UpCloud has no token auth" and used HTTP Basic with
    `username:password`; that was true when written and is not any more. Basic is
    still accepted as a fallback so an older `.env` keeps working.
    Leave the token's IP allowlist OPEN: `fleet` sends provider API calls through
    `FLEET_API_PROXY` (TSPU freezes large TLS uploads on the bare line), so they
    egress from whichever exit is current — and exits rotate every <=72 h.
  * A server must be **stopped before it can be deleted**. `destroy()` therefore
    stops, waits, and only then deletes — which makes it the one slow operation in
    the driver. It is written to be idempotent so a retry after a timeout is safe.
"""
from __future__ import annotations

import base64
import os
import time

from ..util import FleetError, http, debug
from . import register
from .base import Provider, ServerHandle, ServerSpec

API = "https://api.upcloud.com/1.3"
LABEL_KEY = "fleet"
LABEL_VALUE = "exit"

#: how long to wait for a soft stop before giving up and forcing it
STOP_SOFT_TIMEOUT = 90
#: how long to wait for the hard stop after that
STOP_HARD_TIMEOUT = 60


@register
class UpCloud(Provider):
    name = "upcloud"
    #: Bearer token from the control panel: Account -> API Tokens. Starts `ucat_`.
    token_env = "UPCLOUD_API_TOKEN"
    #: Pre-token form, still honoured so an existing .env does not break.
    legacy_token_env = "UPCLOUD_API_CREDENTIALS"

    # ------------------------------------------------------------- plumbing

    def token(self) -> str:
        """Bearer token first, falling back to the old Basic credential."""
        env = self.opts.get("token_env") or self.token_env
        value = os.environ.get(env, "").strip()
        if not value:
            value = os.environ.get(self.legacy_token_env, "").strip()
        if not value:
            raise FleetError(
                f"provider `upcloud` needs a credential in ${env}\n"
                f"  Create one at Account -> API Tokens in the UpCloud control panel.\n"
                f"  It starts `ucat_` and is shown ONCE, so copy it straight into .env.\n"
                f"  Leave its IP allowlist open — fleet's API calls egress via the\n"
                f"  current exit, and exits rotate.\n"
                f"  (${self.legacy_token_env} with `username:password` also works.)"
            )
        return value

    def _headers(self) -> dict[str, str]:
        cred = self.token()
        if cred.startswith("ucat_"):
            return {"Authorization": f"Bearer {cred}"}
        if ":" in cred:
            # Legacy HTTP Basic. UpCloud recommends a dedicated API sub-account for
            # this form; a token is better because it can be revoked on its own.
            enc = base64.b64encode(cred.encode()).decode()
            return {"Authorization": f"Basic {enc}"}
        raise FleetError(
            f"${self.token_env} does not look like an UpCloud credential.\n"
            f"  Expected a Bearer token starting `ucat_` (Account -> API Tokens),\n"
            f"  or the legacy `username:password` form."
        )

    @staticmethod
    def _ips(server: dict) -> tuple[str, str]:
        """Pull the public v4 and v6 out of UpCloud's doubly-nested address list."""
        v4 = v6 = ""
        addrs = (server.get("ip_addresses") or {}).get("ip_address") or []
        for a in addrs:
            if a.get("access") != "public":
                continue  # "utility" is the private inter-server network
            if a.get("family") == "IPv4" and not v4:
                v4 = a.get("address", "") or ""
            elif a.get("family") == "IPv6" and not v6:
                v6 = a.get("address", "") or ""
        return v4, v6

    def _handle(self, s: dict) -> ServerHandle:
        v4, v6 = self._ips(s)
        state = s.get("state", "")
        return ServerHandle(
            provider=self.name,
            provider_id=str(s.get("uuid", "")),
            name=s.get("title", "") or s.get("hostname", ""),
            ipv4=v4,
            ipv6=v6,
            region=s.get("zone", "") or "",
            # "started" alone is not enough: the address can lag the state change
            status="live" if state == "started" and v4 else (state or "provisioning"),
            extra={"state": state},
        )

    # ------------------------------------------------------------ catalogue

    def resolve_template(self, image: str) -> str:
        """Map an image name like `debian-13` to a public template UUID.

        Accepts a UUID straight through, so inventory.toml can pin one if the
        fuzzy match ever picks the wrong release.
        """
        if image.count("-") == 4 and len(image) == 36:
            return image  # already a UUID
        resp = http("GET", f"{API}/storage/template", headers=self._headers())
        storages = (resp.get("storages") or {}).get("storage") or []
        wanted = image.lower().replace("-", " ").strip()
        public = [s for s in storages if s.get("access") == "public"]
        matches = [s for s in public if wanted in (s.get("title", "") or "").lower()]
        if not matches:
            # "debian-13" won't substring-match "Debian GNU/Linux 13 (Trixie)";
            # fall back to matching every word, which does.
            words = wanted.split()
            matches = [
                s
                for s in public
                if all(w in (s.get("title", "") or "").lower() for w in words)
            ]
        if not matches:
            avail = ", ".join(sorted(s.get("title", "") for s in public)[:8])
            raise FleetError(
                f"upcloud: no public template matching `{image}`.\n"
                f"  available include: {avail}\n"
                f"  set the template UUID directly as `image` in inventory.toml"
            )
        # Prefer the longest title: "Debian GNU/Linux 13" beats a "13 minimal" variant
        matches.sort(key=lambda s: len(s.get("title", "")), reverse=True)
        return str(matches[0]["uuid"])

    def _plan_storage(self, plan: str) -> int:
        """A cloned template must be sized to the plan, or creation is rejected."""
        try:
            resp = http("GET", f"{API}/plan", headers=self._headers())
            for p in (resp.get("plans") or {}).get("plan") or []:
                if p.get("name") == plan:
                    return int(p.get("storage_size") or 25)
        except FleetError as e:
            debug(f"upcloud: plan lookup failed ({e}); defaulting storage to 25 GB")
        return 25

    # -------------------------------------------------------------- the API

    def create(self, spec: ServerSpec) -> ServerHandle:
        plan = spec.server_type or "1xCPU-1GB"
        template = self.resolve_template(spec.image or "debian-13")
        size = int(spec.opts.get("storage_size") or self._plan_storage(plan))

        # Storage tier: DO NOT hardcode this. `maxiops` is only valid on the
        # general-purpose plans; the cheap DEV/STARTER/CLOUDNATIVE tiers reject it
        # outright with HTTP 409 TIER_INVALID ("New 'maxiops' storages cannot be
        # created with a server plan DEV-1xCPU-1GB-10GB"). Omitting the key lets
        # UpCloud pick whatever that plan actually supports, which is what we want
        # for an exit node — the bottleneck is the uplink, never the disk.
        # Override with `storage_tier` in the provider block if you ever care.
        storage: dict = {
            "action": "clone",
            "storage": template,
            "title": f"{spec.name}-root",
            "size": size,
        }
        tier = str(spec.opts.get("storage_tier") or "").strip()
        if tier:
            storage["tier"] = tier

        server: dict = {
            "zone": spec.region or "sg-sin1",
            "title": spec.name,
            "hostname": spec.name,
            "plan": plan,
            "storage_devices": {"storage_device": [storage]},
            # Without the metadata service the user_data is never delivered, and the
            # node comes up with no cloud-init and no keys — a silent paid brick.
            "metadata": "yes",
            # UpCloud is the only provider here that switches its PLATFORM firewall
            # on by default, with a 50-rule starter set. It is wrong for an exit in
            # both directions and it fails silently:
            #   inbound  accepts only 22/80/443/3389/8443/8880 — not 2222, where
            #            bootstrap.sh moves sshd, so fleet locks itself out the
            #            moment the bootstrap succeeds; and not any randomised
            #            exit port, which defeats port randomisation entirely.
            #   outbound accepts ~20 fixed ports. An exit must reach ARBITRARY
            #            destination ports for its clients; this quietly breaks
            #            everything that is not web traffic.
            # bootstrap.sh installs a full nftables ruleset on the node, so this
            # layer is redundant as well as harmful. Keep the firewall in ONE place.
            "firewall": "off",
            "user_data": spec.user_data,
        }

        # Belt and braces alongside cloud-init: if user_data ever fails to run we
        # still want a way in rather than a server we can only destroy.
        pubkey = spec.opts.get("ssh_key")
        if pubkey:
            server["login_user"] = {
                "username": "root",
                "ssh_keys": {"ssh_key": [pubkey]},
            }

        labels = [{"key": k, "value": v} for k, v in (spec.labels or {}).items()]
        labels.append({"key": LABEL_KEY, "value": LABEL_VALUE})
        # de-duplicate, last wins, since callers may already pass fleet=exit
        seen: dict[str, str] = {}
        for item in labels:
            seen[item["key"]] = item["value"]
        server["labels"] = {"label": [{"key": k, "value": v} for k, v in seen.items()]}

        resp = http("POST", f"{API}/server", headers=self._headers(), body={"server": server})
        return self._handle(resp.get("server") or {})

    def get(self, provider_id: str) -> ServerHandle | None:
        try:
            resp = http("GET", f"{API}/server/{provider_id}", headers=self._headers())
        except FleetError as e:
            if "404" in str(e):
                return None
            raise
        return self._handle(resp.get("server") or {})

    # ----------------------------------------------------------- destruction

    def _state(self, provider_id: str) -> str | None:
        h = self.get(provider_id)
        return None if h is None else str(h.extra.get("state") or "")

    def _wait_stopped(self, provider_id: str, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._state(provider_id)
            if st is None:
                return True  # already gone; nothing left to stop
            if st == "stopped":
                return True
            time.sleep(5)
        return False

    def destroy(self, provider_id: str) -> None:
        """Stop, wait, then delete with its storage.

        Idempotent: every step tolerates the server having already moved on, so a
        retry after a network timeout does not error out half way.
        """
        st = self._state(provider_id)
        if st is None:
            return  # already destroyed

        if st != "stopped":
            try:
                http(
                    "POST",
                    f"{API}/server/{provider_id}/stop",
                    headers=self._headers(),
                    body={"stop_server": {"stop_type": "soft", "timeout": "60"}},
                )
            except FleetError as e:
                # 409 = already stopping or stopped; anything else is real
                if "404" not in str(e) and "409" not in str(e):
                    raise

            if not self._wait_stopped(provider_id, STOP_SOFT_TIMEOUT):
                debug(f"upcloud: soft stop timed out on {provider_id}, forcing")
                try:
                    http(
                        "POST",
                        f"{API}/server/{provider_id}/stop",
                        headers=self._headers(),
                        body={"stop_server": {"stop_type": "hard"}},
                    )
                except FleetError as e:
                    if "404" not in str(e) and "409" not in str(e):
                        raise
                if not self._wait_stopped(provider_id, STOP_HARD_TIMEOUT):
                    raise FleetError(
                        f"upcloud: {provider_id} would not stop, so it cannot be "
                        "deleted and is still being billed — delete it in the "
                        "control panel."
                    )

        # storages=1 deletes the cloned root disk too. Without it the disk survives
        # the server, keeps costing money, and nothing in fleet ever looks at it.
        try:
            http(
                "DELETE",
                f"{API}/server/{provider_id}?storages=1&backups=delete",
                headers=self._headers(),
                expect=(200, 202, 204, 404),
            )
        except FleetError as e:
            if "404" not in str(e):
                raise

    # ------------------------------------------------------------- orphans

    def list_fleet(self) -> list[ServerHandle]:
        """Servers carrying our label.

        The list endpoint returns abbreviated objects — no addresses — which is
        fine, because orphan reaping keys on provider_id. Label filtering is done
        client-side as well as in the query, since older API versions ignore the
        parameter rather than erroring on it.
        """
        resp = http(
            "GET",
            f"{API}/server?label={LABEL_KEY}%3D{LABEL_VALUE}",
            headers=self._headers(),
        )
        servers = (resp.get("servers") or {}).get("server") or []
        out = []
        for s in servers:
            labels = (s.get("labels") or {}).get("label") or []
            if labels and not any(
                l.get("key") == LABEL_KEY and l.get("value") == LABEL_VALUE
                for l in labels
            ):
                continue
            out.append(self._handle(s))
        return out
