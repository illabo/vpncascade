# Research findings

Date: 2026-09-26. Everything below was checked against current sources; links at the bottom.

---

## 1. Is there a service unifying API access to multiple VPS providers?

Short answer: **there is no serious commercial "one API to rent VPS anywhere" SaaS worth subscribing to.** There
are four categories of thing that get marketed that way, and only two of them are useful to you.

### 1a. Crypto/no-KYC resellers with a real API — **useful**

**SporeStack** (`sporestack.com`) is the closest match to what you described, and it happens to be almost
purpose-built for your rotation requirement:

- Resells **DigitalOcean, Vultr and Slow Servers** behind one API.
- **No account, no email, no KYC.** Auth is a bearer "token" you generate yourself; it doubles as the wallet.
- Paid in **XMR / BTC / BCH / USDT**.
- Official **OpenAPI spec + Python library + CLI** (`pip install 'sporestack[cli]'`), plus a JS lib.
- **Servers carry an expiry timestamp.** You buy N "days to live". When the balance runs out the server is
  powered off and then deleted. You extend with `topup` (max 90 days per call).
- Can talk to the API **over Tor** (`SPORESTACK_USE_TOR_ENDPOINT=1`).

The expiry model is the interesting part: *self-destruct is the default behaviour rather than something you have
to remember to cron.* Your "tear down on schedule" requirement becomes "just don't top up". Cron becomes a safety
net rather than the mechanism. That is a meaningfully better failure mode — if your laptop dies, the fleet still
evaporates.

**Bithost** (`bithost.io`) is the same idea (DO / Linode / Vultr, BTC + ~300 coins, no KYC, hourly) but it is a
thinner wrapper with no first-class API/library, so it is a fallback, not a base.

Caveats for both: you are trusting a reseller with your infrastructure, they hold the real provider account, and
the underlying DO/Vultr IP ranges are the *most* blocked ranges in Russia (see §3). Fine for **exit** nodes,
useless for the RU **entry** node.

### 1b. Reseller/billing aggregators — **not what you want**

**Caasify** advertises exactly your phrasing ("Hetzner, DigitalOcean, Vultr, Linode through one module/API",
81+ datacenters, hourly PAYG, crypto). But it is fundamentally a **WHMCS module for people reselling hosting to
their own customers**. You fund a Caasify balance and provision on their account. It adds a billing middleman,
a margin, and a second party who can see and terminate your whole fleet at once. **Deploynix** and
**FlexiCloud Panel** are dashboards in the same family — nice UIs, but they are management panels, not an
infrastructure API you would want to build automation against.

### 1c. Library/IaC abstraction layers — **useful, and free**

This is the honest answer to "unified API", and it's what the industry actually uses:

| Tool | Shape | Providers | Verdict |
|---|---|---|---|
| **OpenTofu / Terraform** | Declarative IaC | Everything (hcloud, vultr, digitalocean, linode, scaleway…) | Best-in-class, but declarative state fights you when the whole point is churn |
| **Apache Libcloud** | Python lib, one object API | 30+ incl. DO, Vultr, Linode, Scaleway, EC2 | Still maintained (last release activity Feb 2026). **No first-class Hetzner Cloud driver** |
| **Pulumi** | IaC in real languages | Same as TF | Heavier, needs a backend |
| **Crossplane** | K8s CRDs | Same | Requires a cluster. Absurd overkill here |

The catch with OpenTofu for *this* job: Terraform-family tools model a desired end state. You want a fleet whose
members are deliberately short-lived, created and destroyed on a schedule, with a stable anchor node that must
never be touched. Expressing "three exits, each younger than 72h, replaced make-before-break" in HCL means
fighting the state file with `taint`/`replace_triggered_by`/`time_rotating` hacks. It's doable and people do it,
but it's the wrong grain.

### 1d. What this project does, and why

**Skip the subscription.** Write a thin driver interface (~60 lines per provider) over each provider's native
REST API, because "create server with cloud-init, get IP, destroy server" is genuinely all you need and every
provider exposes it in three endpoints. That gives you:

- No middleman who can nuke the fleet or correlate it.
- Providers are **hot-swappable per node**, so one provider's ban wave costs you one exit, not the fleet.
- Provider diversity is itself the security property here — a single aggregator account defeats the purpose of
  scattering nodes across countries.

That's what `fleet/providers/` is. Drivers included: **Hetzner Cloud, Vultr, DigitalOcean, SporeStack**, plus
`manual` for the hand-bought RU node. Adding one is a small class with `create`/`destroy`/`list`.

**Recommended split:** SporeStack (anonymous, self-expiring) for rotating exits; a directly-purchased Hetzner or
similar account for a couple of stable mid-nodes if you want them; hand-bought RU VPS for the entry node
(no API, no automation, never destroyed — see §4).

---

## 2. Protocols: what to run, and what the router can do

### 2a. Server-side protocol landscape (Russia, late 2026)

| Protocol | DPI resistance | Notes |
|---|---|---|
| OpenVPN, IKEv2, L2TP | **Dead** | Detected and blocked in seconds |
| Plain WireGuard | **Dead / throttled** | Distinctive handshake, fixed message sizes; throttled to unusable within minutes since ~mid-2024 |
| Shadowsocks-2022 | Weak-moderate | Random-looking, but no TLS cover; falls to active probing + traffic analysis |
| **VLESS + XHTTP + REALITY** | **Strong** | Borrows a real site's TLS identity; survives active probing by proxying unauthenticated handshakes to the genuine site |
| **AmneziaWG** (1.5 / 2.x) | **Strong** | Junk packets + randomised header types + protocol mimicry (QUIC/DNS) over UDP |
| **Hysteria2** | Strong *where UDP lives* | QUIC masquerading as HTTP/3, Salamander per-packet XOR obfuscation, Brutal congestion control. **Not adopted** — same UDP failure axis as AmneziaWG, needs a second server binary (xray-core cannot do it), and needs a certificate we would have to own. See 2a-i |

Two survive. They fail differently, which is the argument for running **both**:

- **VLESS/REALITY is TCP+TLS.** Survives networks that throttle or block UDP wholesale (common on RU mobile
  during "shutdown" events). Higher latency, more CPU.
- **AmneziaWG is UDP.** Much cheaper on CPU, better for a router doing line-rate, better for real-time traffic —
  but dies whenever UDP is being suppressed.

Run VLESS as primary, AmneziaWG as the fast path, fail over to VLESS when UDP dies.
A third protocol only helps if it fails under *different* conditions — which is
exactly why Hysteria2 did not make the cut. See 2a-i.

### 2a-i. Hysteria2 — researched 2026-09-29, **not adopted**

Asked directly: is Hysteria2 a good third option beside VLESS+REALITY and
AmneziaWG, and is it worth a server-side deployment plus another sing-box client?

**Verdict: no, not on the evidence we have — but for a reason worth understanding,
because it may change.** Hysteria2 is a genuinely strong protocol. It is a poor
*fit for this architecture*, and those are different claims.

#### What it would add

Hysteria2 is QUIC/UDP, masquerading as HTTP/3, with two features neither of our
current protocols has:

- **Salamander obfuscation** XORs every QUIC packet with a BLAKE2b-256 keystream,
  so the Initial packet is not recognisable as QUIC at all. That defeats TSPU's
  QUIC-pattern matching earlier in the pipeline than AmneziaWG's junk packets do,
  and reportedly gives the best results on most Russian ISPs in 2026.
- **Brutal congestion control**, which holds throughput on lossy or throttled
  links where WireGuard's standard CC collapses. This is its real distinguishing
  feature and the one thing neither VLESS-over-TCP nor AmneziaWG can replicate.

#### What it costs, and why that is decisive here

**1. It is on the same failure axis as AmneziaWG.** Hysteria2 is UDP. So is
AmneziaWG. Measurements put **~15% of Russian ISPs restricting UDP and ~5%
blocking it outright**, and where UDP dies, both die together. A third protocol
that fails in the same conditions as the second one is not diversity — it is a
second seat in the same lifeboat. Our actual diversity comes from VLESS being
TCP+TLS, and that is unchanged.

**2. `xray-core` does not implement Hysteria2.** Our exits run Xray and nothing
else. Adding Hysteria2 server-side means a **second server binary** on every exit
— another download and checksum in `bootstrap.sh`, another systemd unit, another
open port in nftables, another rendered config, another thing to get right during
every rotation, and more attack surface on a box whose entire value is being
boring. The *client* side is genuinely cheap, because podkop's sing-box already
speaks it; the server side is where the cost is, and rotation multiplies it.

**3. The certificate problem, which is the real objection.** REALITY presents a
**real, valid certificate belonging to a real third-party site** — our exits
currently answer with an Amazon-issued `*.lovelive-anime.jp` or `www.grab.com`
certificate. We own no domain and register nothing. Hysteria2 cannot do this. It
needs its own certificate, and both options are worse for us:

  - **ACME** — requires a domain you control. That is a registration trail, a
    name that can itself be blocked, and something tied to a person. The README's
    claim that there is *"nothing tied to your passport"* would stop being true.
  - **Self-signed for an IP** — works, but a self-signed certificate on 443 is
    itself the anomaly. Its masquerade serves a real site to unauthenticated
    visitors, but the certificate does not match that site.

REALITY solves the hardest problem in this space — looking like a real HTTPS
server without owning anything — and Hysteria2 does not. That is an architectural
regression, not a tuning difference.

#### Evidence, and how much to trust it

The strongest source is Habr community research on TSPU's June 2026 shift to
**behavioural detection**: foreign-datacentre ASN + JA3/JA4 fingerprint + **more
than three parallel TLS handshakes to one SNI** combined with AND logic, producing
a ~120 s freeze by blackholing (escalating to ~600 s if the fingerprint changes
during the freeze). On that analysis, `VLESS+REALITY+TCP` is *more* exposed than
`VLESS+gRPC+REALITY`, which multiplexes over one HTTP/2 connection and never shows
handshake parallelism. **If that holds, the cheaper answer for us is a transport
change within Xray, not a second protocol stack.**

Treat the Hysteria2-specific claims with more caution: most of the sources
asserting "Hysteria2 is the most resistant protocol in 2026" are **commercial VPN
vendors selling Hysteria2 access**, and they have an obvious incentive. The
consistent, less-interested findings across sources are narrower: it works on most
Russian ISPs, it is **2-3x slower on throttling ISPs**, and it does not work at
all where UDP is blocked. Also relevant: TSPU is **decentralised**, so MTS,
Rostelecom and Beeline behave differently — a result on one operator does not
transfer. Our line is fixed-line Rostelecom.

#### What would change the answer

Two measurements, neither requiring a deployment, both of which we can run:

1. **Does UDP survive on our line at all?** If QUIC/UDP to our own exits is
   blocked or throttled here, Hysteria2 is moot and so is some of AmneziaWG's
   value. Cheap and decisive.
2. **Does AmneziaWG's throughput collapse under loss where Brutal would not?**
   That is the one thing Hysteria2 uniquely fixes. If AWG holds up on this line,
   the case evaporates.

There is a third scenario where it wins regardless: **phones on mobile data**.
Mobile operators are where VLESS-over-TCP degrades worst, and where Hysteria2
reportedly does best — but also where UDP blocking is most common. Contradictory
enough that it needs its own measurement rather than a guess.

**Revisit if** UDP is measured healthy here *and* AmneziaWG underperforms on a
lossy path, *or* if the behavioural-freeze research above starts costing us real
outages on TCP. Until then the cheaper moves are already on the table: a gRPC or
XHTTP transport within Xray addresses the handshake-parallelism signal without a
second binary or a certificate we would have to own.

### 2b. AmneziaWG parameter generations — **this one bites you**

| Version | Adds | Beryl AX support |
|---|---|---|
| 1.0 | `Jc`, `Jmin`, `Jmax` (junk packets), `S1`/`S2` (init/response prefixes), `H1`–`H4` (custom message types) | 4.8.2+ |
| 1.5 | `I1`–`I5` — Custom Protocol Signature: carries a hex snapshot of a real protocol (e.g. a QUIC Initial) so the first packet *is* a plausible QUIC/DNS packet | **4.9+** |
| 2.0 | `S3`/`S4` (prefixes on Cookie and Data packets too), dynamic header ranges, `HeaderProtectionKey` | **4.9+** |
| 3.1 | Statistical-analysis resistance: `ContentPaddingAddition`, `RandomTrailers`, randomised rekey/keepalive timing ranges | not yet |

**Corrected 2026-09-26.** The 4.8.2 beta thread had a GL.iNet moderator saying "currently the 1.0 is supported",
and that was true then. It is not true now: GL.iNet's own FAQ states **"AmneziaWG 2.0 is available on GL.iNet
routers with firmware v4.9 or later"**, and **4.9.0 went stable for the Beryl AX on 2026-09-04**. So the full
2.0 parameter set is available today — `fleet awg init` defaults to `--generation 2.0`. Only pin `1.0` if you
stay on 4.8.x, because every peer on an interface must agree on these values. `I1`–`I5` are *not*
auto-generated by GL.iNet or by `fleet`: a signature only helps if it mimics a protocol plausible on your path.

Also note `amneziawg-tools` has had real bugs parsing `I1`–`I5` (`<b 0x...>` producing "Invalid argument", and
`awg-quick` crashing on empty `i1`–`i5`). Pin your tools version and test.

### 2c. What the GL-MT3000 Beryl AX can actually do

Hardware (verified against FCC ID 2AFIW-MT3000, board rev MT3000-V1.2): MediaTek **MT7981BA** (Filogic 820),
dual-core Cortex-A53 @ 1.3 GHz, **512 MB DDR4**, **256 MB SLC NAND**, 2.5 GbE WAN + 1 GbE LAN, USB 3.0.
Comfortable for everything below — flash and RAM are not constraints on this device; **CPU on crypto is the
only ceiling**. Full breakdown in `docs/03-router-glinet-openwrt.md`.

**Four firmware paths, in increasing order of capability and effort:**

1. **GL.iNet stock 4.8.x (OpenWrt 21.02 base)** — WireGuard + OpenVPN in the GUI. No obfuscation worth having.
2. **GL.iNet 4.9.0 stable** (2026-09-04) — native AmneziaWG **2.0**, client *and* server, in the GUI. Built on
   the MTK SDK with an older kernel, so podkop (OpenWrt ≥24.10) will not install. No VLESS.
3. **GL.iNet 4.9.0-op24_beta1** — the native-OpenWrt track (kernel 6.6, open driver). Vendor GUI *and* a
   userland new enough for podkop / xray-core / sing-box. Still labelled beta/testing.
4. **Vanilla OpenWrt 25.12.5** (kernel 6.12.94) — published `sysupgrade` and `initramfs-kernel` images for
   `glinet_gl-mt3000`; supported since 23.05. Maximum control and current packages; you lose the GL.iNet GUI
   and use `amneziawg-tools` directly (which gives finer parameter control anyway).

**Recommendation depends on what terminates on the router** — see `docs/03-router-glinet-openwrt.md`. For the
architecture in this repo (VLESS+REALITY+Vision on the router), vanilla **OpenWrt 25.12.5** is the defensible
choice, with **4.9.0-op24** as the option that keeps the vendor GUI.

**Protocols installable on OpenWrt 24.10:**

| Package | Gives you |
|---|---|
| `xray-core` | VLESS + REALITY + XHTTP, Trojan, VMess, Shadowsocks |
| `sing-box` (≥1.12) | VLESS + REALITY, Hysteria2, TUIC, ShadowTLS, AnyTLS, WireGuard |
| `amneziawg-tools` + `kmod-amneziawg` | AmneziaWG (full 1.5/2.x params, unlike the GL.iNet GUI) |
| **`podkop`** | Orchestration layer: sing-box + nftables TPROXY + dnsmasq FakeIP, with a LuCI page |
| `ProxyRules` / `VPNpool` | podkop alternatives adding priority failover chains and subscription auto-update |

**podkop is the right tool for your split tunnelling** if you do it on the router. It needs **OpenWrt ≥ 24.10**
and **≥25 MB free** — which is exactly why firmware path 3 or 4 matters. Its model is precisely yours: Russian
domains resolve through the real DNS and egress directly; everything else gets a FakeIP and is TPROXY'd into the
VLESS tunnel.

Throughput on MT7981B, using GL.iNet's own published figures as the anchor: **WireGuard ~300 Mbit/s**,
**OpenVPN ~150 Mbit/s**. VLESS+REALITY+XTLS-Vision should land between them (Vision splices after the
handshake rather than re-encrypting, and the A53 has ARMv8 AES/SHA extensions); VLESS+XHTTP is the slowest
because every packet is re-encrypted in userspace. Measure rather than assume — `tests/t06_throughput.sh`
does it on your line. If gigabit matters, terminate VLESS on a small box behind the router.

---

## 3. Where to put nodes

**Do not put exits on Hetzner, DigitalOcean, Vultr, OVH, AWS or Oracle if the RU entry node must reach them
reliably.** RKN has blacklisted large swathes of exactly these ranges; the Tor Project forum has reports of
Hetzner ranges where *TCP to all ports stops receiving SYN* from Russia. There are continuously-updated
aggregate blocklists of RU-blocked IP space covering Hetzner, DO, AWS, Oracle, OVH, Vultr and Contabo.

This matters much less in your cascade than in a normal setup, because **only the RU entry node ever talks to
the exits** — one datacenter-to-datacenter flow, not thousands of residential ones. But "blocked at the IP
level" still means blocked. Mitigations, in order of effectiveness:

1. Prefer providers *not* on the mass-blocked list for the first foreign hop, then chain onward to Hetzner etc.
   for the actual exit (which never needs RU reachability).
2. Health-check reachability **from the RU node**, not from your laptop. `fleet health` does this.
3. Keep ≥2 live exits and fail over automatically (the generated entry config uses an Xray balancer +
   observatory, so a dead exit is bypassed within seconds without touching clients).

**Avoid Aeza.** OFAC-sanctioned on 2025-07-01 (Aeza Group LLC, Aeza Logistic, Cloud Solutions, plus four named
individuals) as a bulletproof host. Separately, in December 2025 RKN handed Aeza a list demanding removal of VPN
servers within 24 hours under threat of blocking the whole host — so it's the worst of both worlds.

---

## 4. Legal / operational reality of the Russian entry node

Your friend's topology puts the anchor node inside Russia. That is technically excellent (see
`02-architecture.md`) but you should go in with eyes open:

- Russian hosting providers must be in the **RKN registry**, must **identify users** (Gosuslugi/ESIA, the
  biometric system, or passport at contract signing), must interoperate with **SORM**, and must connect to
  **GosSOPKA**. Operating outside the registry carries fines of ₽600k–1M; turnover-based fines for failing FSB
  requirements were legislated in Dec 2025.
- Providers are now required to **check clients against a blacklist and refuse service to clients who ignore the
  law**, and RKN has begun issuing **24-hour VPN-server takedown orders** to hosts (documented at Aeza,
  Dec 2025).
- Since Sept 2025 there are administrative fines for *advertising* circumvention tools, and for searching for
  designated "extremist" material.

Practical consequences for the design:

- **The RU node is identity-bound.** It cannot be anonymous and it cannot be API-churned. Buy it by hand, treat
  it as disposable-but-replaceable, and keep zero logs and nothing incriminating on it.
- **Make it look boring.** It should terminate TLS on 443 for a plausible domestic hostname and, ideally, also
  serve something real. Masking as something like `corp.ozon.ru` is a good instinct — a RU client
  connecting to a RU IP with a RU SNI on 443 is the single least interesting flow on the network.
- **Assume it dies without warning.** The architecture therefore keeps *all* client-facing identity (UUIDs,
  REALITY keys) on the RU node, and makes exits swappable behind it — but you should also keep a **cold spare
  entry** on a second RU provider with the same keys, so failover is a DNS/IP change on clients rather than a
  re-provision.
- If this risk profile is unacceptable, the fallback is: no RU node, entry node abroad, and split tunnelling
  moves to the Beryl via podkop. You lose RU-origin egress for RU services (banking geo-blocks etc.), which is
  usually the reason people want the RU node in the first place.

---

## Sources

- [SporeStack](https://sporestack.com/) · [sporestack-python](https://git.sporestack.com/SporeStack/sporestack-python) · [PyPI](https://pypi.org/project/sporestack/)
- [Bithost](https://bithost.io/) · [Caasify all-in-one WHMCS module](https://caasify.com/blog/all-in-one-hetzner-digitalocean-vultr-linode-whmcs-module) · [Deploynix](https://dev.to/deploynix/managing-servers-across-6-cloud-providers-from-one-dashboard-e2p)
- [Apache Libcloud](https://libcloud.apache.org/) · [apache/libcloud](https://github.com/apache/libcloud) · [OpenTofu hcloud provider](https://search.opentofu.org/provider/opentofu/hcloud/latest)
- [GL.iNet 4.8.2 WireGuard-obfuscation beta thread](https://forum.gl-inet.com/t/beta-release-beryl-ax-gl-mt3000-v4-8-2-with-new-wireguard-obfuscation-support/64284) · [GL-MT3000 4.8.x op24 thread](https://forum.gl-inet.com/t/gl-mt3000-beryl-ax-4-8-x-op24-openwrt-24/63916) · [GL.iNet VPN obfuscation docs](https://docs.gl-inet.com/router/en/4/tutorials/vpn_obfuscation/) · [GL-MT3000 downloads](https://dl.gl-inet.com/router/mt3000)
- Hysteria2 (researched 2026-09-29): [protocol spec](https://v2.hysteria.network/docs/developers/Protocol/) ·
  [server config](https://v2.hysteria.network/docs/advanced/Full-Server-Config/) ·
  [sing-box hysteria2](https://sing-box.sagernet.org/manual/proxy-protocol/hysteria2/) ·
  [self-signed / no domain](https://github.com/apernet/hysteria/discussions/1052) ·
  TSPU behavioural freezing, June 2026 ([Habr](https://habr.com/ru/articles/1047442/)) ·
  Hysteria2 as HTTP/3 ([Habr](https://habr.com/ru/articles/1008554/)).
  **Vendor-blog claims that Hysteria2 is "the most resistant protocol of 2026" come
  from companies selling Hysteria2 access — discount accordingly.**
- [AmneziaWG docs](https://docs.amnezia.org/documentation/amnezia-wg/) · [1.0→1.5 config conversion](https://docs.amnezia.org/documentation/instructions/upgrade-awg-config/) · [amneziawg-tools I1–I5 issue](https://github.com/amnezia-vpn/amneziawg-tools/issues/31) · [awg-quick i1–i5 crash](https://github.com/amnezia-vpn/amneziawg-tools/issues/40)
- [podkop](https://github.com/itdoginfo/podkop) · [podkop DeepWiki](https://deepwiki.com/itdoginfo/podkop) · [ProxyRules](https://github.com/m0n5ter/ProxyRules) · [VPNpool](https://github.com/roman-png/VPNpool)
- [XHTTP: Beyond REALITY](https://github.com/XTLS/Xray-core/discussions/4113) · [XHTTP guide & insights](https://github.com/net4people/bbs/issues/440) · [XHTTP 5-in-1 config discussion](https://github.com/XTLS/Xray-core/discussions/4118)
- [russia-blocked-ips aggregate list](https://github.com/eduard256/russia-blocked-ips) · [Tor forum: Tor and Hetzner block in Russia](https://forum.torproject.org/t/tor-and-hetzner-block-in-russia/16134) · [Zona: Russia's internet censorship in 2026](https://en.zona.media/article/2026/04/07/russian_internet_censorship_2026)
- [US Treasury sanctions Aeza Group](https://home.treasury.gov/news/press-releases/sb0185) · [BleepingComputer coverage](https://www.bleepingcomputer.com/news/security/aeza-group-sanctioned-for-hosting-ransomware-infostealer-servers/)
- [Teplitsa: how hosts are made to police VPNs](https://te-st.org/2026/05/12/hostingrules/) · [RKN registry requirement](https://old.rkn.gov.ru/news/rsoc/news74803.htm) · [Kommersant: VPN services and hosts](https://www.kommersant.ru/doc/8590872)
