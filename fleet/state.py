"""Fleet state: which servers exist, their keys, and when they were born."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .util import FleetError, now

SCHEMA = 1


class State:
    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, Any] = {"version": SCHEMA, "entry": None, "exits": [], "clients": []}
        self.load()

    # ------------------------------------------------------------------ io

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            try:
                loaded = json.load(fh)
            except json.JSONDecodeError as e:
                raise FleetError(f"state file {self.path} is corrupt: {e}") from None
        if loaded.get("version") != SCHEMA:
            raise FleetError(
                f"state file {self.path} has schema {loaded.get('version')}, expected {SCHEMA}"
            )
        self.data = loaded

    def save(self) -> None:
        """Atomic, 0600 — this file holds every private key in the fleet."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".fleet-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --------------------------------------------------------------- entry

    @property
    def entry(self) -> dict | None:
        return self.data.get("entry")

    @entry.setter
    def entry(self, node: dict | None) -> None:
        self.data["entry"] = node

    # ---------------------------------------------------------------- exits

    @property
    def exits(self) -> list[dict]:
        return self.data.setdefault("exits", [])

    def add_exit(self, node: dict) -> None:
        self.exits.append(node)

    def get_exit(self, node_id: str) -> dict | None:
        for n in self.exits:
            if n["id"] == node_id or n.get("name") == node_id:
                return n
        return None

    def drop_exit(self, node_id: str) -> None:
        self.data["exits"] = [
            n for n in self.exits if n["id"] != node_id and n.get("name") != node_id
        ]

    def live_exits(self) -> list[dict]:
        return [n for n in self.exits if n.get("status") == "live"]

    def serving_exits(self) -> list[dict]:
        """Exits currently in the entry node's balancer."""
        return [n for n in self.live_exits() if n.get("serving")]

    def age_hours(self, node: dict) -> float:
        return (now() - node.get("created_at", now())) / 3600.0

    # -------------------------------------------------------------- clients

    @property
    def clients(self) -> list[dict]:
        return self.data.setdefault("clients", [])

    def get_client(self, name: str) -> dict | None:
        for c in self.clients:
            if c["name"] == name:
                return c
        return None

    def add_client(self, client: dict) -> None:
        if self.get_client(client["name"]):
            raise FleetError(f"client `{client['name']}` already exists")
        self.clients.append(client)

    def drop_client(self, name: str) -> None:
        self.data["clients"] = [c for c in self.clients if c["name"] != name]
