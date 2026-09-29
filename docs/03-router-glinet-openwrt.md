# The router: GL-MT3000 Beryl AX

## If your router is a MikroTik

Worth stating plainly because it is a hard constraint, not a tuning problem:
**RouterOS cannot terminate VLESS/REALITY or AmneziaWG.** It speaks IPsec, OpenVPN,
SSTP, L2TP, PPTP, WireGuard and ZeroTier — and plain WireGuard is exactly the protocol
that is detected and throttled in Russia. There is no package, no plugin, no setting.

The only escape is the **container** feature, and on hAP-class hardware it mostly is
not available:

| Model | CPU | RAM / flash | Containers? |
|---|---|---|---|
| **hAP ac** (RB962UiGS) | QCA9558, **single-core MIPS** 720 MHz | 128 MB / 16 MB | **No — MIPS is not supported at all** |
| **hAP ac²** (RBD52G) | IPQ-4018, quad Cortex-A7 | 128 MB / 16 MB | ARM, but 16 MB flash and **no USB port** — not practical |
| **hAP ac³** (RBD53iG) | IPQ-4019, quad 716 MHz | 256 MB / **128 MB NAND + USB** | Feasible — but a 716 MHz A7 doing userspace TLS is slow |

Containers need arm/arm64/x86; MIPS, MMIPS, SMIPS, TILE and PPC are excluded outright.

Approximate VPN throughput, for scale — MikroTik deliberately publish no WireGuard
figures "because of its heavy dependence on CPU", so treat these as community numbers:

| | IPsec | WireGuard |
|---|---|---|
| hAP ac | no hardware acceleration, ~tens of Mbit/s | ~50-100 Mbit/s |
| hAP ac² / ac³ | **hardware accelerated**, ~400 Mbit/s | ~100-200 Mbit/s (ChaCha20 gets no help from the AES engine) |

**So a MikroTik is a fine router and a non-starter as the tunnel endpoint.** What it is
genuinely good at is the other half of the job: policy routing. Address lists, routing
marks, and `check-gateway=ping` on a route so that if the tunnel box disappears the
route deactivates and traffic falls back to the plain default route — graceful
degradation, natively, without podkop.

### Beryl as the router — the MikroTik is not faster

An earlier draft of this document argued for keeping a MikroTik hAP ac² as the router
with the Beryl as a tunnel appliance behind it. That was wrong, on the facts:

- **IPQ-4018 has no NAT hardware acceleration.** MikroTik's FastTrack is a *software*
  fast path — it skips conntrack and firewall for established flows. RouterOS L3
  hardware offloading applies to switch-chip devices like the CRS series, not to a
  hAP ac². So the hAP ac² routes in software on 4× Cortex-A7 at 716 MHz.
- **MT7981 does have a hardware packet engine (PPE)** and OpenWrt supports flow
  offload on it.
- Software-to-software, 2× Cortex-A53 at 1.3 GHz is the newer and faster arrangement.

So the Beryl is not a downgrade as a router. And a second box in front is a second
power supply, a second config and a second thing that can fail, bought for nothing.

**The one honest caveat:** flow offloading and transparent proxying conflict. Offloaded
flows bypass the netfilter path that TPROXY depends on, so running podkop generally
means turning `flow_offloading` and `flow_offloading_hw` off — which gives back the
MT7981's hardware advantage and leaves both devices routing in software, in the same
ballpark. That still argues for one box rather than two; it just means you should not
count on the PPE while podkop is doing the split.

If you later find software routing on the Beryl is the bottleneck, the fix is a faster
single box, not a MikroTik in front of it.

### Several sites, identical hardware

If more than one site runs the same router, that is a better position than it looks:

- **Identical hardware is a cold spare.** A dead unit is a swap and a config restore,
  not a shopping trip — better availability than a heterogeneous stack.
- **Each site is just another client.** `fleet client add site-a`,
  `fleet client add site-b` — one credential each, revocable independently, all
  riding the same exit pool.
- **Each site does its own RU split locally**, so domestic traffic leaves from that
  building's own Russian residential address. That is exactly the property the
  direct-mode design was built around, and it holds however many sites there are.

One thing multi-site does complicate: **how remote sites learn about rotated exits.**
The orchestrator and its subscription file live on one LAN, which the other sites
cannot reach. Options, roughly in order of how much work they are:

1. **Do nothing clever.** With `pool_size = 2` and staggered ages the pool fully turns
   over in about a week. Reissuing two configs weekly by copy-paste is tolerable.
2. **Publish the subscription somewhere reachable from Russia** — domestic object
   storage keeps it fast and unblocked, at the cost of the provider holding a file
   that is working credentials to anyone who has the URL.
3. **Slow the rotation right down** (`max_age_hours = 336`) so the question comes up
   fortnightly instead of daily. Given how low the detection base rate actually is
   (`docs/05-detectability.md`), this is a reasonable trade rather than a cop-out.

## Two questions worth settling first

**"Does the newer GL.iNet firmware support obfuscated VPN out of the box?"**
Yes, and better than early reports suggested. **Firmware 4.9 supports AmneziaWG 2.0**, client
*and* server, in the GUI — GL.iNet's own FAQ says so explicitly: *"AmneziaWG 2.0 is
available on GL.iNet routers with firmware v4.9 or later."* That means the full
parameter set: junk packets (`Jc`/`Jmin`/`Jmax`), random prefixes on **all four**
packet types (`S1`–`S4`, where 1.0 masked only Init and Response), dynamic header
ranges (`H1`–`H4`), and optional Custom Protocol Signature packets (`I1`–`I5`) for
mimicking QUIC or DNS. GL.iNet notes `I1`–`I5` are **not auto-generated** and must be
set by hand.

**4.9.0 went stable for the Beryl AX on 2026-09-04**, so this is available to you now,
not pending. This supersedes the 4.8.2 beta thread, where a
moderator said "currently the 1.0 is supported" — that was true of 4.8.2 and is no
longer true of 4.9. `fleet awg init` now defaults to `--generation 2.0`; use
`--generation 1.0` only if you stay on 4.8.x.

There is still **no VLESS/REALITY in the GL.iNet GUI**, and no sign of it coming.

**"Which protocols can be set up on OpenWrt?"** All of them, if the userland is new
enough:

| Package | Protocols | Notes |
|---|---|---|
| `xray-core` | VLESS + REALITY + XHTTP, VMess, Trojan, Shadowsocks | The reference implementation for REALITY, and the only one that speaks XHTTP |
| `sing-box` (≥1.12) | VLESS + REALITY, Hysteria2, TUIC, ShadowTLS, AnyTLS, WireGuard | Simpler config, better UDP — **but no XHTTP, ever** |
| `amneziawg-tools` + `kmod-amneziawg` | AmneziaWG, all generations, full parameter control | More configurable than the GUI |
| `podkop` | Orchestration: sing-box + nftables TPROXY + dnsmasq FakeIP + LuCI | Needs **OpenWrt ≥ 24.10** |
| `ProxyRules`, `VPNpool` | podkop alternatives with failover chains and subscription auto-update | |

## The hardware (verified against the FCC filing, board rev MT3000-V1.2)

FCC ID **2AFIW-MT3000** / IC **23019-MT3000**, GL Technologies (Hong Kong) Ltd.

| | |
|---|---|
| SoC | MediaTek **MT7981BA** (Filogic 820), dual-core Cortex-A53 @ 1.3 GHz |
| RAM | **512 MB DDR4** (Nanya NT5AD256M16E4-JR) |
| Flash | **256 MB SLC NAND** (Macronix MX35LF2GE4AD-Z41) |
| WiFi | MT7981BA + MT7976CN — 2×2 2.4 GHz + 3×3 (2×2 + zero-wait DFS) 5 GHz |
| Ethernet | 1× **2.5 GbE** WAN (MaxLinear GPY211), 1× 1 GbE LAN |
| Other | USB 3.0; **UART: 4-pin unpopulated header, 3.3 V TTL, 115200 8N1** |
| Board | MT3000-V1.2, 2023-03-08 |

Two things follow, and they are more generous than the pocket form factor suggests:

- **256 MB of flash makes podkop's ~25 MB requirement a non-issue**, with room left for
  xray-core, sing-box and `amneziawg-tools` side by side. Flash is not a constraint
  here; on a 16 MB device it would be the whole story.
- **512 MB of RAM** comfortably holds sing-box or xray with full geosite/geoip rule
  sets, FakeIP tables and a large connection table. The only real constraint on this
  box is **CPU on crypto**.

Also worth knowing before you flash anything: there is an **unpopulated 4-pin UART
header** inside (3.3 V TTL, 115200 8N1). That is your recovery path if a firmware
swap goes wrong, alongside GL.iNet's U-Boot web recovery.

## Firmware decision

Checked against GL.iNet's download centre on 2026-09-27. **The two tracks number
independently, and the higher number is not the more capable build** — this catches
people:

| Build | Track | Base | podkop / xray-core | Status |
|---|---|---|---|---|
| **4.11.0_beta1** | MTK SDK, closed WiFi driver | older kernel | **no** — userland predates podkop's OpenWrt 24.10 floor | testing |
| **4.9.0** (2026-09-04) | MTK SDK | older kernel | no | **stable** |
| **4.9.1-op25_beta3** | **native OpenWrt** | **OpenWrt 25** | **yes** | testing |
| 4.9.0-op24_beta1 | native OpenWrt | OpenWrt 24.10 | yes | superseded by op25 |
| **Vanilla OpenWrt 25.12.5** | — | kernel 6.12.94 | yes | **stable**, but see the flashing warning above |

`4.11.0` looks like the newest thing on the page and is *numerically* ahead of
`4.9.1-op25`, but it is on the **MTK SDK track**. Upgrading to it moves you away from
the only GL.iNet line that can run podkop. The filename is the tell: builds on the
native track carry `-op` (`mt3000-op-4.9.1-op25_beta3`), the SDK ones do not
(`mt3000-4.11.0_beta1`).

**OpenWrt 25 replaced `opkg` with `apk`**, and the GL.iNet op25 build ships no opkg
shim at all — so any guide or script that hardcodes `opkg install` fails silently.
podkop's own installer detects this correctly; `router/xray-openwrt.sh` and
`beryl-setup.sh` now do too.

Confirm what a given unit is actually running:

```bash
ssh root@192.168.8.1 'cat /etc/openwrt_release; uname -r'
```

`DISTRIB_RELEASE` of `24.10`/`25.x` means you are on the native track and podkop will
install. Anything older means you are on the SDK track.

GL.iNet's own note on the split: the MTK SDK line exists because *"of certain
performance and compatibility issues with the open-source drivers"*, and the
`-opXX` builds are the native-OpenWrt track they maintain in parallel.

**Which to pick depends on what you are terminating on the router:**

- **AmneziaWG only, and you want the GUI** → **4.9.0 stable**. Simplest path, full 2.0
  obfuscation, nothing to compile. You give up VLESS on the router.
- **VLESS on the router** (our `transport = "tcp"` default, via podkop or xray-core)
  → you need a 24.10+ userland, so either **4.9.1-op25_beta3** (keeps the GL.iNet GUI
  and its AmneziaWG 2.0, but it is a testing build) or **vanilla OpenWrt 25.12.5**
  (stable, current packages, kernel 6.12 — but you lose the GL.iNet GUI entirely,
  which matters if other people in the house use it).

Given the architecture we settled on — VLESS+REALITY+Vision from the router, split at
the router — **vanilla OpenWrt 25.12.5** is the defensible choice, with 4.9.0-op24
as the option that keeps the vendor GUI. Whichever you flash, the unpopulated UART
header is your way back.

## Throughput, and where to terminate VLESS

The WAN is 2.5 GbE and RAM is ample, so the ceiling is the two A53 cores doing crypto.
GL.iNet's own published figures are the anchor:

| Path | Expect |
|---|---|
| WireGuard (GL.iNet's published figure) | **~300 Mbit/s** |
| OpenVPN (GL.iNet's published figure) | **~150 Mbit/s** |
| AmneziaWG (same kernel datapath + junk overhead) | somewhat under WireGuard |
| VLESS + REALITY + **XTLS-Vision** (TCP) | between the two — Vision splices after the handshake instead of re-encrypting, and the A53 has ARMv8 AES/SHA extensions |
| VLESS + REALITY + **XHTTP** | **lowest** — full userspace re-encryption on every packet |

The Vision figure is the one not to quote without measuring: it depends on the
traffic mix, and it is the main reason `transport = "tcp"` is the default rather than
a preference. `tests/t06_throughput.sh` measures it on your actual line — run it
before deciding you need more hardware.

If you have gigabit and the number disappoints, don't terminate VLESS on the Beryl —
put the tunnel on a small x86 box or a Pi 5 behind it and let the router route.

**The cheap win: use AmneziaWG from the router and VLESS from the phones.** The router
gets the fast kernel path; phones on mobile data get the TCP path that survives UDP
suppression. Both land on the same entry node and follow the same split.

## The router does not create servers — so who does?

Worth stating plainly, because it is the part that surprises people: **the Beryl is a
client. It never talks to a VPS API, never holds a provider token, never creates or
destroys anything.** `fleet` does that, from somewhere else. The router's only job is
to notice that the server list changed and reconnect.

That leaves two questions: where `fleet` runs, and how the router learns about changes.

### Where the orchestrator lives

| Option | Verdict |
|---|---|
| **Your Mac, via cron/launchd** | Fine to start, **dangerous as a steady state** — see below |
| **A small always-on box at home** (Pi, NAS, mini-PC) | **Best.** Always on, on your LAN, can serve the subscription locally |
| **The Beryl itself** | Feasible — capacity is not the issue (+17.9 MiB for Python against 119.9 MB free) — but it loses the repair path and puts every key on the device most likely to be reset. Best answer at a site nobody maintains; see below |
| **A cheap always-on VPS** | Works, and it is not a VPN node so it is uninteresting — but it is a permanent server again |

**On running it on the Beryl:** capacity is not the objection and has not been for a
while. Measured on a GL-MT3000 running OpenWrt 25.12.5: `python3` costs **+17.9 MiB**
across 25 packages against **119.9 MB free** on `/overlay`, with 228 MB of 492 MB RAM
available; `fleet` itself is 2.3 MB of stdlib-only Python. It would also be always-on
and in exactly the right place on the network. Three real objections remain, in
increasing order of weight.

**1. A bad push severs the network of the box that has to repair it.** `fleet sync`
rewrites podkop's config on the router and restarts it. With the orchestrator on a
separate box, a broken config is an inconvenience: the other machine still has
working networking, still holds state, and can push a correction. Merge them and
that escape route disappears — the tool that fixes the router is behind the router
it just broke. This is not hypothetical; a podkop config with four simultaneous
schema errors, a factory reset, and a stale host fingerprint needing
`router push --force` have each happened here, and each time the separate box was
what made recovery a one-liner.

**2. A factory reset or firmware upgrade wipes `/overlay`, and `state/fleet.json`
with it** — every REALITY private key and every client UUID. Recovering means
destroying every exit and reissuing every client. GL.iNet firmware also rewrites
config on its own initiative (it has been observed overwriting `wireless.sta` from
the repeater layer). The device most likely to be reset is the wrong place for the
only copy of the fleet's identity.

**3. It is a *travel* router.** If it leaves the house it leaves with every provider
API token, the SSH key for every exit, and that same `state/fleet.json`.

Flash wear is deliberately **not** on that list: UBI wear-levelling on NAND handles
`fleet`'s write pattern comfortably, and it is a much better medium for this than an
SD card.

**The case in favour is real, though**, and worth stating rather than dismissing.
One box instead of two means less to power, less to maintain, and one less thing to
fail. It also removes two genuine fragilities that exist only because the
orchestrator is separate: podkop's `routing_excluded_ips` has to name the
orchestrator by a **DHCP-assigned address** — if that lease moves, the orchestrator
is silently tunnelled and whichever device inherits the address is silently *not* —
and the orchestrator's own TCP connections are terminated and re-originated by
sing-box, which quietly invalidates any timing measurement taken from it.

**And at a site nobody maintains, merging may be the only workable answer.** A
remote site is not on your LAN, so an orchestrator elsewhere cannot `fleet sync` its
router at all; there the comparison is not "safer apart" but "one device" against
"no deployment". Full analysis, including what changes for remote sites, is in
`docs/02-architecture.md`, "Could the orchestrator just run on the Beryl?".

**The footgun with the laptop option:** SporeStack servers *self-destruct*. That is
normally a feature — the fleet evaporates if you vanish — but in direct mode the router
depends on those exits, so an orchestrator that is asleep when they expire takes your
household's internet with it.

`exits.expiry_buffer_hours` is the dial for this, and it is the only number here that
is really an outage budget: `fleet` buys `max_age_hours + expiry_buffer_hours` of life
up front and tops it back up on every cron tick. The default of 120h means the fleet
keeps working for five days with the orchestrator switched off. `fleet health` fails
loudly under 24h of runway and warns under half the buffer.

If the orchestrator is a laptop, raise it:

```toml
[exits]
expiry_buffer_hours = 336   # two weeks of runway
```

### How the router follows a rotation

In **cascade** mode it doesn't have to — the entry node's address never changes.

In **direct** mode, three options, best first:

1. **Subscription URL.** `fleet subscription` writes a base64 list of every current
   server. Host it over HTTPS; the router polls it and switches itself. Two useful
   properties: the payload is ~1 KB, so it slips well under the ~16 KB foreign-TLS
   freeze and works even on affected mobile networks; and it must be fetched *outside*
   the tunnel, which is right, because you need it most when every exit is dead.
   Anyone with the URL gets working credentials — treat it as a password, use a long
   random path, and prefer serving it from the always-on box on your own LAN.

   Base podkop has no subscription support yet ([PR #325](https://github.com/itdoginfo/podkop/pull/325)
   is open). Today you want one of:
   - **[VPNpool](https://github.com/roman-png/VPNpool)** — built for exactly this:
     auto-updating subscription, ping-based failover, LuCI dashboard.
   - **[podkop-subscriptions](https://github.com/makxis/podkop-subscriptions)** — an
     add-on that fetches, filters and validates links into podkop's UCI config, with
     `proxy_type = 'urltest'` for failover.
   - **[netshift](https://github.com/yandexru45/netshift)** — a podkop fork with
     subscription support built in.

2. **Static multi-node config.** `fleet client config beryl` already emits every
   serving exit with a `leastPing` balancer, so the router survives losing one. You
   re-import when the pool fully turns over — with `pool_size = 2` and staggered ages
   that is roughly weekly. Simple, no hosting, no secret URL.

3. **DNS / DDNS per server.** Don't. See `docs/05-detectability.md` — WireGuard
   resolves its endpoint once at bring-up and never again, and a hostname is a new
   blockable, loggable identifier.

### Transport: this one will bite you

**Upstream sing-box does not implement XHTTP, and has said it will not.** podkop,
VPNpool and most OpenWrt and mobile tooling are built on sing-box. So a
VLESS+XHTTP+REALITY config simply cannot be consumed by them — only `xray-core` and a
couple of forks (`sing-box-lx`, `sing-box-extended`, and those have version-skew
breakage against newer Xray releases).

Hence `transport = "tcp"` is the default in `inventory.toml`: VLESS + REALITY +
**XTLS-Vision** over TCP. Universally supported, and materially faster — Vision splices
rather than re-encrypting, which on the MT7981B is the difference between ~80–150
Mbit/s and something close to line rate. Keep `xhttp` for the cascade's internal
entry→exit hop (both ends are xray-core, so nothing else has to understand it) or when
you specifically want CDN fronting.

## Recipe A — AmneziaWG from the GL.iNet GUI (simplest, fastest)

```bash
# on your workstation
fleet awg init                         # defaults to generation 2.0 (needs fw >= 4.9)
fleet awg peer beryl -o beryl-awg.conf
fleet awg deploy --install             # first time; compiles the kernel module
```

On firmware 4.8.x use `fleet awg init --generation 1.0` instead — a 2.0 config with
`S3`/`S4` will not connect, and every peer on an interface must agree on these values.

Then in the GL.iNet admin UI: **VPN → WireGuard Client → Add → Manual**, paste
`beryl-awg.conf`, and enable the obfuscation toggle. The `Jc/Jmin/Jmax/S1/S2/H1..H4`
lines in the file are what the toggle consumes.

No split-tunnel configuration is needed on the router: the entry node already sends
Russian destinations out of its own Russian IP. See `docs/02-architecture.md`.

## Recipe B — VLESS on the router with podkop (most capable)

Podkop's model is exactly the one you want: Russian domains resolve normally and go
direct, everything else gets a FakeIP and is TPROXY'd into the tunnel.

```bash
# on the router, over SSH
opkg update
sh <(wget -O - https://raw.githubusercontent.com/itdoginfo/podkop/refs/heads/main/install.sh)
```

Then feed it the client URI:

```bash
fleet client uri beryl          # vless://... (one line per serving exit)
```

Paste into **LuCI → Services → Podkop → Proxy configuration → URL**.
`router/podkop-setup.sh` automates it including the RU direct list, and accepts
several URIs so podkop's `urltest` can fail over between them.

Requires `transport = "tcp"` in `inventory.toml` — podkop runs sing-box, which cannot
speak XHTTP.

## Recipe C — xray-core directly, no podkop

For a minimal setup, or on firmware where podkop won't install:

```bash
fleet client config beryl -o beryl-xray.json
scp beryl-xray.json root@192.168.8.1:/etc/xray/config.json
```

`router/xray-openwrt.sh` installs xray-core, the init script and the nftables TPROXY
rules. This is more moving parts than podkop and you maintain the rules yourself.

## The router-side split: a failure domain, not a geography

Worth being precise about, because it is easy to configure the wrong thing.

The **geography** split (Russian sites get a Russian IP) lives on the entry node. Doing
it on the router instead would mean Russian sites see your **home ISP address**, and it
would not apply at all to a phone on mobile data.

The split that belongs on the router is a **failure domain** split: if the tunnel is
down, Russian destinations should still work over the local ISP rather than
blackholing. That turns a dead entry node from "the internet is broken" into "foreign
sites are broken" — the difference between an annoyance and a phone call from your
family while you're away.

`router/podkop-setup.sh` configures podkop that way: the RU list is `direct`, the
default route is the tunnel, and podkop's own health check falls back to direct when
the tunnel dies.

## Things that will bite you

- **MTU.** Two REALITY+XHTTP hops plus AmneziaWG headers eat a lot. If large pages hang
  while small ones load, that is PMTU blackholing. Start at `MTU = 1320` on the AWG
  interface (the default here) and drop to 1280 if needed. `tests/t02_chain_egress.sh`
  includes a 1 MB transfer that catches this.
- **GL.iNet firmware upgrades wipe opkg packages.** Anything installed with `opkg` —
  podkop, xray-core — is gone after a firmware update. Keep `router/podkop-setup.sh`
  handy; it is idempotent.
- **DNS.** If the router resolves locally while tunnelling the data, your ISP sees every
  domain you visit. Podkop's FakeIP handles this. With plain xray, make sure clients
  send hostnames to the proxy rather than pre-resolving. `tests/t04_dns_and_leaks.sh`
  checks it.
- **Don't run both recipes at once** without thinking. An AmneziaWG default route plus
  podkop's TPROXY will fight over the same packets.
