"""Small shared helpers: HTTP, shell, logging."""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

VERBOSE = os.environ.get("FLEET_VERBOSE", "") not in ("", "0")


class FleetError(RuntimeError):
    """Anything that should stop the current command with a readable message."""


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"\033[2m{_stamp()}\033[0m {msg}", file=sys.stderr)


def step(msg: str) -> None:
    print(f"\033[2m{_stamp()}\033[0m \033[1;36m▸\033[0m {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"\033[2m{_stamp()}\033[0m \033[32m✓\033[0m {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"\033[2m{_stamp()}\033[0m \033[33m!\033[0m {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"\033[2m{_stamp()}\033[0m \033[31m✗\033[0m {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if VERBOSE:
        print(f"\033[2m{_stamp()}   {msg}\033[0m", file=sys.stderr)


def redact_url(url: str) -> str:
    """Strip bearer-in-path secrets before a URL reaches a log or an exception.

    SporeStack puts the API token — which is also the WALLET — in the URL path,
    so an unredacted error message hands anyone who can read the log the ability
    to spend the balance. Also covers `?token=`/`?key=` style query secrets.
    """
    url = re.sub(r"(/token/)[^/?#]+", r"\1<redacted>", url)
    url = re.sub(r"((?:api[_-]?key|token|key|secret|password)=)[^&#]+",
                 r"\1<redacted>", url, flags=re.I)
    return url


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 60,
    expect: tuple[int, ...] = (200, 201, 202, 204),
) -> Any:
    """Minimal JSON HTTP client. Returns parsed JSON, or None for empty bodies."""
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "fleet/0.1")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    debug(f"{method} {redact_url(url)}")
    opener = None
    proxy = os.environ.get("FLEET_API_PROXY", "").strip()
    if proxy:
        # Only fleet's own HTTP goes this way. SSH — health probes, deploys — is
        # untouched and stays on the bare line, which is the point.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        _open = opener.open if opener else urllib.request.urlopen
        with _open(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status not in expect:
                raise FleetError(f"{method} {redact_url(url)} -> HTTP {resp.status}: {raw[:400]!r}")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read()[:600].decode(errors="replace")
        raise FleetError(f"{method} {redact_url(url)} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise FleetError(f"{method} {redact_url(url)} -> {e.reason}") from None


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: int = 300,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a local command."""
    debug("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
            input=stdin,
        )
    except subprocess.TimeoutExpired as e:
        # Usually a remote command that left a child holding stdout open, so ssh is
        # still waiting on the channel. Report it as an error rather than a traceback.
        raise FleetError(
            f"timed out after {timeout}s: {' '.join(cmd[:3])}…\n"
            f"  {(e.stdout or b'').decode(errors='replace')[-400:].strip()}"
        ) from None
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise FleetError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{err}")
    return proc


def retry(fn, *, attempts: int = 30, delay: float = 5.0, what: str = "operation"):
    """Poll until fn() returns something truthy, or give up."""
    last = None
    for i in range(attempts):
        try:
            result = fn()
            if result:
                return result
        except FleetError as e:
            last = e
            debug(f"{what}: attempt {i + 1} failed: {e}")
        time.sleep(delay)
    raise FleetError(f"{what} did not succeed after {attempts} attempts ({last or 'no result'})")


def now() -> int:
    return int(time.time())


def human_age(ts: int) -> str:
    secs = max(0, now() - ts)
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"
