"""Provider drivers. Each one wraps three operations: create, wait, destroy."""
from __future__ import annotations

from ..util import FleetError
from .base import Provider, ServerSpec

_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str, opts: dict | None = None) -> Provider:
    from . import digitalocean, fornex, hetzner, manual, sporestack, upcloud, vultr  # noqa: F401

    if name not in _REGISTRY:
        raise FleetError(
            f"unknown provider `{name}` — available: {', '.join(sorted(_REGISTRY))}"
        )
    return _REGISTRY[name](opts or {})


def available() -> list[str]:
    from . import digitalocean, fornex, hetzner, manual, sporestack, upcloud, vultr  # noqa: F401

    return sorted(_REGISTRY)


__all__ = ["Provider", "ServerSpec", "get", "register", "available"]
