# How detectable are we, really?

Short version: **months of uninterrupted uptime is the normal outcome, not luck.**
"Assume it dies without warning" is a sound design principle — it should shape where
spare capacity is kept — but stated as a probability it is far too pessimistic. Here
is the calibrated version.

## The base rate

There is exactly **one** documented mass-notice event against personal servers on
Russian hosting: **Aeza, 3 December 2025**. RKN handed Aeza a list of IPs from Aeza's
own `138.124/16`, demanded removal within 24 hours, and threatened to block the whole
host. Users had to send a screenshot proving deletion.

Aeza is close to a worst case, and picking it as the exemplar skews the picture:

- **OFAC-sanctioned** (1 July 2025) as a bulletproof host for ransomware and infostealer
  operators.
- Publicly known as *the* cheap VPN host — in November 2025 Aeza itself said publicly
  that using their hosting for VPN was "not the best option".
- The sweep was **range-targeted**: a whole /16 belonging to one provider RKN had
  decided to make an example of, not a nationwide scan of all Russian hosting.

Everything else in 2026 has been aimed at **commercial services**, not personal nodes:
469 VPN services blocked by end of February 2026, a further wave on 4 August 2026
taking out 20+ (Amnezia, Paper VPN and similar). Those are things with public
subscription links, marketing, and thousands of users. A single-user node with no
public footprint is a different object.

The tightest proposal — Mintsifry's weekly monitoring, a 24-hour explanation window,
and a 30-minute suspension for low-verification clients — was still **a proposal under
discussion** as of August 2026, not a regulation in force.

## What actually got people caught

This is the useful part. From users' own accounts in the Aeza thread, ranked by how
much it matters:

1. **An exposed management panel.** The clearest first-hand report: x-ui on a port in
   the 50000s, SSH on 22, REALITY on 443 — notice within 24 hours. As one commenter put
   it, 90% of people never change the default panel port, which makes a mass scan
   trivial. **REALITY did not fail these people. Their control plane did.**
2. **Being on a host that is itself the target.** Aeza. Nothing you configure helps.
3. **Weak protocols on the same address.** Plain WireGuard, OpenVPN, SOCKS5 and L2TP
   have all been added to active blocking since December 2025. One badly-chosen listener
   taints the whole IP.
4. **A public footprint.** Listed in a subscription, shared in a Telegram channel,
   resold. That is how you become a "service" rather than a person.
5. **Client count.** Many distinct residential clients on one IP reads as commercial.
6. **TLS fingerprint.** TSPU started leaning on **JA4** fingerprinting in early 2026,
   and the summer 2026 wave hit **chrome, safari and ios** uTLS profiles. **firefox
   held.** That is independent corroboration of the operator maxim "фингерпринт всегда
   firefox, другие убили" — treat it as a live variable, not a settled fact.
7. **Traffic shape.** "Fat TCP sessions without multiplexing" came up repeatedly.

Note what is *not* on this list: REALITY being cryptographically broken. Community
measurement put VLESS+REALITY bypass around **99.5%** after the February 2026 wave. The
protocol is holding; the operational hygiene around it is what fails.

The design in this repo already avoids #1 entirely — there is **no panel**, configs are
files pushed over SSH, nftables defaults to drop, and SSH moves off 22. Run
`tests/t08_exposure_audit.sh <host>` to confirm from outside.

## Choosing a Russian host

There is no public dataset ranking hosts by leniency, and anyone claiming otherwise is
guessing. What can be said from evidence:

**The terms-of-service wording is the signal.** Several hosts wrote VPN clauses before
they were legally required to, and the wording is narrow:

| Host | Prohibited |
|---|---|
| Beget | "provision of **mass public** VPN services and proxy services" |
| RUVDS | "**open** VPN/proxy servers" |
| NTX.ru | "**open** proxy, **open** VPN" |

"Mass public" and "open" are doing the work. A single-user, key-authenticated node is
neither an open relay nor a mass public service. That tells you how they would read a
complaint absent a direct RKN order.

**Counterintuitive but important: prefer a compliant host over a defiant one.** A host
that ignores RKN gets its whole subnet blocked, which kills your node just as dead — and
takes your neighbours with it. A large, boring, compliant host has no incentive to scan
its own customers unless handed a list, and you are one of tens of thousands of
unremarkable VPSes. The registry now has 566 hosting providers; be a rounding error in a
big one.

**Avoid:** Aeza (proven, sanctioned). Anything marketed as bulletproof, offshore, or
"no-abuse" — that marketing attracts exactly the sweeps you are trying to avoid, and
increasingly attracts sanctions too.

**Reasonable:** ordinary mid-to-large domestic hosts — Timeweb, Selectel, RUVDS,
FirstVDS, VDSina, Beget, Firstbyte. None is *verified* safe; the point is that none is a
known VPN haven. Your friend's working node is better evidence than any of this — ask
which host, and use that one.

## The June 2026 shift: behaviour, not protocol — and it is aimed at us

Found while researching Hysteria2 (see `01-research-findings.md` 2a-i), and more
consequential than that question was. Habr community research describes TSPU moving
from protocol signatures to **behavioural detection**, combining several weak
signals with AND logic rather than matching any one of them:

| Signal | What it looks at |
|---|---|
| 1. Server location | Foreign datacentres — **Hetzner and DigitalOcean named explicitly** |
| 2. TLS fingerprint | JA3/JA4 of the ClientHello; suspicious mainly *in combination* with a flagged ASN |
| 3. Connection parallelism | **more than 3 parallel TLS handshakes to one SNI**, with small gaps between them |

When all of them line up, the result is a **~120 s freeze by blackholing** — no RST,
just silence — escalating to **~600 s if the client changes fingerprint during the
freeze**, which is precisely what a retrying client looks like.

**Why this matters to this project specifically.** Our exits are on Hetzner
(signal 1), and our transport is `VLESS + REALITY over plain TCP` (`transport =
"tcp"` in `inventory.toml`, because sing-box has no XHTTP). Every browser tab that
opens several connections at once produces exactly the pattern signal 3 looks for.
On this analysis `VLESS + gRPC + REALITY` is materially safer, because it
multiplexes streams over a **single** HTTP/2 connection and never shows handshake
parallelism at all.

**Do not act on this yet.** It is community research, not a controlled study, and
TSPU is decentralised — MTS, Rostelecom and Beeline differ, so a result on one
operator does not transfer. Our own fixed-line Rostelecom measurements on
2026-09-29 were clean in both directions at 1 MB, with no sign of a freeze. But it
is the most plausible next failure mode we know of, and it has a cheap mitigation
*inside the stack we already run* — a transport change in Xray, not a second
protocol.

The diagnostic signature to watch for, since it is unlike anything else here: a
connection that **establishes and then goes silent for about two minutes**, with no
RST, and gets *worse* rather than better when the client retries.

## Do you even need the cascade? (Yes, and more than before)

This is the finding that changes things most, and it runs the opposite way from the
legal risk.

Researchers on net4people/bbs have documented a Russian DPI behaviour that does not
send RST but **freezes the TCP connection** once these conditions hold together:

- TLS 1.2/1.3 over TCP,
- the **server IP is outside Russia** (Hetzner, DigitalOcean and friends named
  explicitly),
- and roughly **25 packets / ~16 KB** have crossed in either direction.

Reported scope is **mobile networks**, varying by region and ISP. There is also a move
to **CIDR whitelisting of destination subnets**, where the summary is blunt:
circumvention is "extremely difficult — as a rule, an intermediate node with an IP from
the whitelist is required."

The consequences for your question:

- **A single-hop foreign node is fine on many fixed-line ISPs and increasingly broken on
  mobile.** It is not that REALITY is detected; it is that *any* foreign TLS flow past
  ~16 KB is throttled where this is deployed. Your obfuscation is irrelevant to it.
- **The cascade is the direct countermeasure.** The client's leg is RU→RU: a domestic
  destination IP, so the foreign-IP trigger never fires. Only the entry node talks
  abroad, from a datacenter uplink rather than a mobile network.
- So the RU node is no longer merely a convenience for split tunnelling. On mobile it
  may be the only thing that works.

Practical test rather than argument: hand a phone a direct-to-exit config, pull ~50 MB
over mobile data, and see whether it stalls around 16 KB or completes. `fleet client
config` can generate both shapes. If direct works on every network you care about, you
can simplify; if it stalls on LTE, you have your answer.

## Endpoint addressing: hostname per server?

You asked whether binding a hostname to each new server would let the GL.iNet router
follow rotations. Mechanically yes, but it is the wrong lever here and it costs you
something.

**It solves a problem you do not have.** In this topology clients point at the *entry*
node, which is stable by design. Exits rotate behind it and no client ever learns their
addresses. DNS would only matter if you dropped the cascade and pointed clients straight
at rotating exits.

**What it costs.** A hostname is a new, blockable, loggable identifier: it appears in
plaintext DNS from the client, it can be poisoned or blacklisted by name, and a domain
you control resolving to a foreign VPS is a cleaner signal than a bare IP. You would be
adding a correlation handle in exchange for convenience.

**And the re-resolution is genuinely awkward on the router.** WireGuard resolves its
`Endpoint` **once, at interface bring-up**, and never again — an OpenWrt peer whose
endpoint IP changes just stops working, typically noticed a day later. Fixing it needs
an external poller (`openwrt-wg-ddns` re-resolves via `dig` every minute and rewrites
the endpoint; `wgtrack` re-resolves on tunnel failure). GL.iNet's GUI gives you no
control over this. Xray and sing-box are better behaved — they accept a hostname in the
outbound `address` and resolve per connection — but then you are back to leaking the
name.

**If you want clients to follow rotation, use a subscription URL, not DNS.** That is
what the ecosystem actually does: the client periodically fetches a signed list of
servers and switches itself. podkop, VPNpool and the mobile clients (v2rayTun, Happ,
NekoBox) all support it, with health-check failover between entries. The URL can live
behind a CDN and be fetched rarely, which is a much smaller signal than per-connection
DNS.

**Recommendation:** keep clients pinned to the entry node's IP, keep rotation invisible
behind it. Add a subscription URL later if you ever want multiple independent entry
points. Reserve DNS for the one case it genuinely helps — replacing the *entry* node
without touching every device — and accept the exposure knowingly if you do.

## Sources

- [Хакер: Aeza asked users to delete VPN servers](https://xakep.ru/2025/12/05/aeza-vpn/) · [Habr: Aeza начала блокировать хостинг](https://habr.com/ru/news/973644/) · [Habr comments — first-hand detection reports](https://habr.com/ru/news/973644/comments/) · [SecurityLab](https://www.securitylab.ru/news/566895.php)
- [net4people/bbs #490 — Russia's new blocking method (connection freezing, CIDR whitelist)](https://github.com/net4people/bbs/issues/490)
- [Teplitsa: how hosts are made to police VPNs](https://te-st.org/2026/05/12/hostingrules/) — ToS wording, provider compliance patterns
- [Habr: 469 VPN services restricted by Feb 2026](https://habr.com/ru/news/1003760/) · [Novaya Gazeta Europe: August 2026 blocking wave](https://novayagazeta.eu/articles/2026/08/05/rkn-provel-novuiu-masshtabnuiu-volnu-blokirovok-vpn-servisov-news) · [iStories](https://istories.media/news/2026/08/04/vlasti-ustroili-krupneishuyu-blokirovku-vpn-servisov/)
- [SecurityLab: Mintsifry 30-minute takedown proposal](https://www.securitylab.ru/news/575627.php) · [Meduza: ban on supplying capacity to VPN operators](https://meduza.io/en/news/2026/04/17/kommersant-reports-russia-seeks-to-ban-hosting-providers-from-supplying-computing-capacity-to-vpn-operators)
- [openwrt-wg-ddns](https://github.com/7Ji/openwrt-wg-ddns) · [OpenWrt forum: auto re-resolve DNS in WireGuard](https://forum.openwrt.org/t/automatically-re-resolve-dns-in-wireguard/150366) · [wgtrack](https://pypi.org/project/wgtrack/)
- [US Treasury sanctions Aeza Group](https://home.treasury.gov/news/press-releases/sb0185)
