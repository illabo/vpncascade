# Architecture

## Two modes, and which to start with

`mode` in `inventory.toml` picks the topology. They share everything else — the same
providers, rotation, tests and cron.

| | `direct` | `cascade` |
|---|---|---|
| Hops | client → exit | client → RU entry → exit |
| Russian node | **none** | required, hand-bought, identity-bound |
| Rotation visible to clients | yes (use `fleet subscription`) | **no** |
| Where the split lives | router / client config | the RU entry node |
| RU services see | **your home ISP address** | a Russian datacenter address |
| Covers phones on mobile data | only if they carry the config | yes, centrally |
| Survives the ~16 KB foreign-TLS freeze | **no** | yes |
| Exits hold client credentials | yes, all of them | no, only the entry's |
| Legal exposure | none in Russia | RKN can order the node offline |

**On a fixed-line home ISP, start with `direct`.** Three reasons:

1. **The main argument for the RU node evaporates at home.** Its job was to give
   Russian services a Russian source address. But your home connection *already is* a
   Russian address — and a residential one, which banks' anti-fraud prefers to a
   datacenter IP. Routing RU traffic direct out of the router gets you a strictly
   better source than the cascade would.
2. **You delete the riskiest component.** No identity-bound node, no RKN takedown
   surface, no Gosuslugi contract, one fewer machine to keep alive.
3. **It is strictly less machinery**, and it tells you empirically whether you need
   more.

**Switch to `cascade` when** phones on mobile data stall around 16 KB (see
`docs/05-detectability.md` — the freeze fires on *any* foreign TLS flow, so
obfuscation does not help), or when you want RU-source egress from devices away from
home, or when you get tired of exit addresses being client-visible.

**Migrating direct → cascade** costs one line and one deploy; nothing is thrown away:

```bash
$EDITOR inventory.toml      # mode = "cascade", fill in [entry]
fleet entry init && fleet entry deploy --first-run
fleet sync                  # exits are re-keyed to serve the entry, not clients
fleet client uri beryl      # hand out the new (and now permanent) address
```

### What direct mode costs you, stated plainly

Every ephemeral exit holds the UUIDs of all your devices. They are bearer tokens for
your own proxy and they die with the rotation, but a box you rented for 72 hours in a
jurisdiction you did not choose now has them. In cascade mode an exit knows exactly one
credential — the entry node's — and nothing about any client. That is a real downgrade,
not a free simplification, and it is the reason `fleet` refuses `pool_size = 1` in
direct mode: with one exit, a rotation is a hard cutover for every device at once.


## The topology

```
                                          ephemeral, rotated, never seen by clients
                                        ┌───────────────────────────────────────┐
                                        │                                       │
 ┌──────────┐                    ┌──────┴──────┐   VLESS/XHTTP   ┌───────────┐  │
 │ Beryl AX │  VLESS/XHTTP/      │  ENTRY (RU) │ ───REALITY────► │  EXIT #1  │──┼──► internet
 │ + phones │ ───REALITY───────► │   stable    │                 │  (abroad) │  │
 └──────────┘   443, SNI=RU      │   anchor    │ ───────────────► │  EXIT #2  │──┘
                                 └──────┬──────┘   (balancer +    └───────────┘
                                        │           observatory)
                                        │ freedom / direct
                                        ▼
                                  RU services (bank, gosuslugi, ozon…)
                                  — plain, from a Russian IP
```

**Three properties fall out of this, and they're why it's the right shape:**

1. **Clients have exactly one profile, forever.** They only ever know the entry node. Exit rotation, exit
   failure, exit provider bans — none of it touches a client config. This is what makes "tear down on
   schedule" actually practical instead of a support nightmare across four devices and a router.
2. **Split tunnelling is free.** The entry node is physically in Russia, so "send RU traffic direct" is a
   two-line routing rule on a box that already has a Russian IP. No podkop, no FakeIP, no router CPU.
3. **The blast radius of an exit is one country-hour.** Exits hold no client identity — just one inbound
   credential known only to the entry node.

## Where the tunnel terminates: the Beryl, not the home server

Two different boxes, two different jobs, and it is worth being explicit because they
are easy to conflate:

| | Beryl AX | home server |
|---|---|---|
| Routes LAN traffic | **yes** | no |
| **Terminates the tunnel** | **yes** | no |
| Applies the RU/foreign split | **yes** | no |
| Runs `fleet` (provision, rotate, reap) | no | **yes** |
| Serves split DNS | no | **yes** |
| In the data path | **yes** | **no** |

**The tunnel belongs on whichever box is already the router** — here the Beryl AX,
which is the reference hardware this was measured on. Keeping a second router in
front of it purely to route is a second power supply and a second failure point
bought for no speed, and some platforms cannot host the tunnel at all: RouterOS is
the common example, since it speaks no VLESS/REALITY. See
`docs/03-router-glinet-openwrt.md` for what the role requires and which hardware
fails it.

The decisive argument is not throughput, it is failure domains.

The Beryl is *already* a single point of failure — it is the router, and if it dies
nobody has internet regardless. Putting the tunnel there adds no new way to lose
connectivity. Put the tunnel on the home server instead and you have created a
**second** single point of failure: now a crashed Pi, a full SD card or a botched
`apt upgrade` takes the household offline.

That directly destroys the property `exits.expiry_buffer_hours` was built to give you.
The orchestrator can be off for five days and nothing breaks — *because it is not in
the data path*. DNS is the same: if the resolver dies you hardcode 77.88.8.8 on a
laptop and carry on. Make it the tunnel gateway and both of those escape hatches close.

So: **the home server does slow, out-of-band work** (cron, API calls, DNS) and the
**Beryl does fast, in-band work** (routing, split, tunnel). Neither depends on the
other being healthy.

### When to move the tunnel to the server anyway

One reason only: **the Beryl is measurably too slow for your line.** Measure, do not
guess — `tests/t06_throughput.sh` gives the number on your actual connection.

The ceiling is CPU crypto on the MT7981B. GL.iNet publish ~300 Mbit/s for WireGuard;
VLESS+REALITY+**Vision** should land somewhat under that (Vision splices rather than
re-encrypting, and the A53 has ARMv8 AES), and XHTTP considerably under it. So:

- **Line up to ~200 Mbit/s** — terminate on the Beryl and stop thinking about it.
- **Gigabit line, and you actually use it** — the Beryl will be the bottleneck.

### What moving it actually costs

If you do move it, two constraints bite, and neither is obvious:

1. **The Beryl has one LAN ethernet port** (1 GbE, plus a 2.5 GbE WAN). Give it to the
   server and you have no wired LAN port left — you need a switch.
2. **Traffic hairpins.** A single-NIC server acting as gateway receives on the same
   link it sends from, so tunnelled traffic crosses that 1 GbE link **twice**. A
   300 Mbit line means 600 Mbit on the link: fine. A gigabit line saturates it — which
   is self-defeating, since a too-slow Beryl was the reason you moved. Solve it with a
   second NIC (a USB 3.x dongle is genuinely fine here) or VLANs.

The tidier version, if you end up there, is to stop half-measuring: make the server the
router outright and demote the Beryl to a WiFi access point. That needs two NICs and is
a bigger change, but it removes the hairpin and the port shortage together.

## Could the orchestrator just run on the Beryl?

Asked directly, and worth recording because the answer differs by site and the
obvious reasoning is wrong in both directions.

**It fits.** Measured on a GL-MT3000 running OpenWrt 25.12.5, 2026-09-29:

    python3 on OpenWrt    +17.9 MiB, 25 packages   (279.2 -> 297.1 MiB installed)
    /overlay free         119.9 MB of 145.9 MB
    RAM available         228 MB of 492 MB
    orchestrator role     2.3 MB of code + state (936 KB of it state)

`fleet` is deliberately **stdlib-only Python plus `ssh` and `curl`**, which is what
makes this even a question. Capacity is not the objection, and any argument that
starts "the router is too small" is out of date.

### At the site that runs the fleet: keep them separate

Three reasons, in order of weight:

1. **The circular dependency.** `fleet sync` rewrites podkop's config on the router
   and restarts it. If the orchestrator *is* the router, a bad sync severs its own
   network and nothing remains to repair it. This is not hypothetical: this project
   has shipped four podkop schema bugs at once, factory-reset the router, and hit a
   stale host fingerprint needing `router push --force`. Each time the separate box
   was the thing that still worked and could push a correction.
2. **Resets and firmware upgrades wipe `/overlay`.** GL.iNet firmware rewrites
   config on its own — it has been observed overwriting `wireless.sta` from the
   repeater layer. `state/fleet.json` holds **every REALITY private key and every
   client UUID**; losing it means destroying every exit and reissuing every client.
   Putting it on the device most likely to be reset inverts the risk.
3. **The always-on box can be dual-homed**, and is here (eth0 + wlan0, so a pulled
   patch cord does not strand it). A router cannot be dual-homed to itself.

Flash wear is *not* on that list. UBI wear-levelling on NAND is far better than the
SD card that died earlier in this project; it is a real consideration, not a
blocker.

The honest case for merging is not nothing: one box, less power, and it would
delete two fragilities hit on 2026-09-29 — `routing_excluded_ips` keyed to a **DHCP
lease** (if the lease moves, the orchestrator is silently tunnelled, and whichever
device inherits the address is silently *not*), and the discovery that the
orchestrator's own TCP is terminated by sing-box, which invalidates timing
measurements taken there.

### At a remote site, the trade is genuinely different

`docs/04-runbook.md` prescribes three independent deployments, each "Beryl **+ a
small always-on box**". For a site you do not live at that second box is the whole
problem: you cannot maintain it, and **you cannot reach it** — a remote site is not
on your LAN, so the orchestrator here cannot `fleet sync` a router there. Pushing
config across the internet would mean exposing something, and the LAN panel is
deliberately never port-forwarded.

So at a remote site the options are only these:

- an always-on box there, running `fleet` locally — the current documented answer,
  and the one that needs a person;
- **`fleet` on that site's Beryl itself** — which makes a remote site a
  single-device deployment, and is the strongest argument for the merge;
- out-of-band subscription distribution the router *pulls* — unsolved, and listed
  as such.

The objections above still apply there, but the comparison is not "safer with a
separate box" — it is "single device" against "no deployment, or a drive across
town". Objection 2 is also the most mitigable remotely: a site's state can be
backed up centrally, and `fleet` already keeps per-site state side by side
(`state/site-b/`, `state/site-c/`) in one checkout.

**Current position:** separate at the fleet site, unresolved for remote sites, and
the remote case is the one worth revisiting. Anyone reasoning about it should start
from the numbers above rather than from an assumption about router capacity.

## Router-side or server-side split?

**Server-side, on the RU entry node.** Concretely:

| | RU node split (chosen) | Beryl/podkop split |
|---|---|---|
| RU services see | a **Russian IP** (RU datacenter) | your **home ISP IP** |
| Works for phones on LTE, away from home | **yes** | no — only behind the Beryl |
| Router CPU | none | TPROXY + FakeIP + sing-box |
| Rule updates | one file on one server, `fleet push` | flash/SSH each router |
| Breaks if | RU node dies | nothing (that's its advantage) |

The decisive one is row 2: your rules live where the tunnel terminates, so a phone on mobile data in another
city gets identical behaviour. The podkop path can't do that.

**But do both, in layers.** Use podkop on the Beryl as a *failure-domain* split, not a geography split: if the
tunnel is down, RU traffic still flows via the local ISP rather than blackholing. `router/` has that config.
It costs nothing and it means a dead entry node degrades to "foreign sites broken" instead of "internet
broken", which is the difference between an annoyance and a support call from your family.

## Common mistakes in hand-written VLESS/REALITY configs

Eight things worth checking in any config of this shape. Each is drawn from a real
working configuration; several are subtle enough to look correct and fail quietly.

The overall shape below is sound. Seven details are worth changing:

### 1. `dest: corp.ozon.ru:443` on the **foreign** node is a real problem — fix this one

On the RU entry node (`config.json`) this is excellent: a Russian client connects to a Russian IP presenting
SNI `corp.ozon.ru` on 443. Perfectly unremarkable.

On the Hetzner node (`config 2.json`) the same masking is actively harmful:

- The RU node opens a TLS connection to a **German IP** with **SNI `corp.ozon.ru`**. SNI/IP-geography mismatch
  is exactly the domain-fronting signature TSPU looks for. You've taken your most-scrutinised flow — the one
  that crosses the border — and given it a suspicious fingerprint.
- REALITY's active-probe defence works by **proxying unauthenticated handshakes to the genuine `dest`**. So
  the Hetzner box must be able to reach `corp.ozon.ru:443` and get a real response. `corp.*` hosts frequently
  geo-block foreign IPs. If the dest is unreachable from the server, probing yields a hang or a TLS error
  instead of Ozon's real certificate — which is *worse* than no REALITY at all, because it's a unique,
  stable, remotely-testable fingerprint.

**Fix:** each node's `dest` must be a site that is (a) reachable from *that* node, (b) hosted plausibly near
*that* node, (c) TLS 1.3 + HTTP/2, (d) not blocked in RU. So: RU node keeps a `.ru` dest; a German exit uses
something genuinely German/European. `deploy/templates/` picks per-node and `tests/t01_reality_probe.sh`
verifies the served certificate actually matches the claimed dest.

### 2. Missing split rules — the `direct` outbound is defined but never used

`config.json` declares `{"protocol":"freedom","tag":"direct"}` and then routes **everything** from `client-in`
to the relay. So today RU traffic goes RU → Germany → RU. That's the thing you wanted to avoid. Fixed in the
entry template with `geosite:category-ru` / `geoip:ru` / private ranges → `direct`.

### 3. `domainStrategy: "AsIs"` — keep it, but understand what it does and does not cover

The obvious-looking fix here is `IPIfNonMatch`, so that a request arriving as a domain gets resolved and then
tested against `geoip:ru`. **Don't.** `IPIfNonMatch` makes the entry node resolve every foreign domain locally,
using a resolver subject to Russian DNS poisoning. A poisoned answer for a blocked foreign domain returns a
Russian sinkhole address; that address matches `geoip:ru`; the request is routed **direct** instead of through
the tunnel. Exactly backwards, and it fails silently.

`AsIs` is correct, and it covers both cases:

- requests carrying a **hostname** match the `domain` rules → the RU list goes direct;
- requests to a **bare IP** match the `ip` rules → `geoip:ru` catches them.

The residual gap is a Russian domain that isn't on the list: it goes abroad and back. That's a performance and
geo-block problem, not a leak, and the fix is to add it. Two things close it in the generated config:
`sniffing` with `routeOnly` on the inbound, so TLS SNI is recovered and domain rules apply even when the client
connected to an IP; and a `dns` block whose Russian resolvers are scoped (`skipFallback`) to the direct list
only, so RU-direct traffic lands on the right domestic CDN edge while nothing foreign is ever resolved on
Russian soil.

The default list ships 75 domains — banks, gosuslugi, Yandex, VK, Ozon, WB, Avito, telecoms. They are plain
`domain:` entries rather than `geosite:` tags on purpose: a `geosite:` tag missing from the geosite.dat on the
node makes Xray refuse to start, and on a remote entry node that is a fleet-wide outage. `fleet entry deploy`
runs `xray -test` remotely before swapping configs, so if you do add `geosite:category-ru` a bad tag is caught
before it can take the node down.

### 4. Port 4273 / 8944 — move to 443

REALITY's entire premise is "this is an ordinary HTTPS server". TLS on port 4273 is an anomaly visible in
one pass of flow metadata, and it throws away most of the benefit. Put both nodes on **443**. Move SSH
somewhere else (the bootstrap does).

### 5. `fingerprint` on the **inbound** does nothing

`realitySettings.fingerprint` is a **client-side uTLS** setting; on an inbound it's ignored. Harmless, but
don't rely on it — the RU node's *outbound* is where `firefox` matters, and that one is set correctly.

Your friend's note that "фингерпринт всегда firefox, другие убили" is a useful field observation and I've
defaulted every outbound to `firefox`. Worth re-testing periodically: uTLS fingerprint viability rotates, and
the `chrome` profile being burned today doesn't mean `firefox` is safe in six months. `fleet health` records
which fingerprint each node last connected with so you can correlate failures.

### 6. XHTTP `mode` is unset — set it explicitly

Unset means `auto`. With REALITY, `stream-one` is what you want (single connection, lowest overhead, fixes a
class of disconnect bugs). Use `packet-up` only if you later front a node behind a CDN that can't handle
streaming uploads. Also: **keep Xray versions identical on client, entry and exit** — XHTTP is still moving
fast and cross-version mismatches produce silent breakage.

### 7. shortIds derived from UUIDs

Fine cryptographically — REALITY authenticates with the X25519 key, and shortId is just a client selector. But
it means one leaked UUID also gives the attacker a valid shortId, so a probe that guesses a UUID gets a
slightly better oracle. Generation is cheap; `fleet` uses independent random shortIds. Not urgent.

### 8. `"targetStrategy": "UseIP"` on the freedom outbound is not a thing

The exit config has `{"protocol":"freedom","tag":"direct","targetStrategy":"UseIP"}`. Xray's freedom outbound
has no `targetStrategy` — the field is called **`domainStrategy`**, and it belongs inside `settings`:

```json
{"protocol": "freedom", "tag": "direct", "settings": {"domainStrategy": "UseIP"}}
```

As written it is silently ignored, so that outbound is running with the default `AsIs`. Mostly harmless on an
exit, but on the RU node's `direct` outbound it is the difference between resolving Russian hostnames locally
(right CDN edge) and passing them through unresolved.

### Choices worth keeping

- **XHTTP over REALITY** rather than plain TCP — better against traffic-shape analysis, works through more
  middleboxes.
- **`"flow": ""`** — correct. XTLS-Vision is TCP-only and must be empty with XHTTP.
- **Per-device UUIDs** — lets you revoke one phone without re-keying everything. `fleet client revoke` does this.
- **RU-domiciled masking domain on the RU node** — the single best idea in the config.
- **Cascade with the RU node as the client-facing anchor** — this is the architecture; everything else is detail.

## Key and identity model

| Secret | Lives on | Rotates |
|---|---|---|
| Client UUIDs + shortIds | entry node inbound, + each client | only when you revoke a device |
| Entry REALITY keypair | entry node | rarely — changing it re-keys every client |
| Entry→exit UUID/shortId | entry outbound + exit inbound | **every rotation**, unique per exit |
| Exit REALITY keypair | exit node | generated fresh per exit, dies with it |
| SSH host keys | injected via cloud-init, pinned locally | per server |

SSH host keys are generated locally and injected through cloud-init, then pinned in
`state/known_hosts`. That removes the usual TOFU window on first connect to a brand-new box — which matters
when you're creating servers unattended on a cron.

## Rotation: make-before-break

`fleet rotate` never leaves you with zero exits:

1. Provision exit N+1, inject host key, bootstrap, install Xray, start.
2. Verify from your workstation **and from the entry node** (reachability from RU is the check that counts).
3. Add it as an outbound on the entry node and include it in the balancer.
4. Remove the old exit from the balancer; leave the outbound for a drain window (default 120 s).
5. Destroy the old server. With SporeStack, also just stop topping it up as a backstop.

The Xray `burstObservatory` + balancer means step 3–4 also gives you automatic failover between rotations: if
an exit is IP-blocked from Russia at 3am, traffic moves to the other exit within a few seconds and `fleet
health` files it for replacement on the next cron tick.
