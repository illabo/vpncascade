# Choosing where exits live

You named the two-fold problem exactly, and it is worth separating because the two
halves want different mitigations.

## Problem 1 — the provider kills you

Big hosts police their own networks. The wording is narrower than people assume:
DigitalOcean's AUP prohibits *"operating open proxies, open mail relays, open
recursive domain name servers, Tor exit nodes, or other similar network services"* —
aimed at open relays, not a key-authenticated personal proxy. Vultr's reservation is
broader and vaguer: they may suspend *"if Vultr determines, in our sole discretion,
that doing so is necessary or in our best interests."*

In practice a single-user REALITY node draws no abuse reports and survives. What
actually gets accounts killed is outbound noise — scanning, spam, torrents from the
exit — which is why the generated exit config blocks `geoip:private`, `169.254/16`
and bittorrent by default. Keep it that way.

## Problem 2 — the censor targets the range

This is the bigger one and it is not about your server at all. RKN works at **AS
granularity**. A February 2026 action put **391 autonomous systems, >225 million
addresses** behind throttling, explicitly including:

| ASN | Holder |
|---|---|
| AS14061 | DigitalOcean |
| AS24940 | Hetzner |
| AS16509 | AWS |
| AS16276 | OVH |
| AS20473 | Vultr / Choopa |
| AS31898 | Oracle Cloud |
| AS13335 | Cloudflare (partial) |
| AS16625, AS63949 | Akamai / Linode |

The mechanism is the one from `docs/05-detectability.md`: a foreign IP in a flagged
range, TLS, and the connection freezes past ~16 KB. Your obfuscation is irrelevant —
the trigger never looks at your protocol. And it is **regional**: measurements were
taken on Rostelecom in the north-west, and different ISPs carry different lists.

So: *"change the port"* does not help, *"use better obfuscation"* does not help. Only
a different IP in a different AS helps.

## So — a less well-known provider abroad?

Yes, that is the right instinct. But "less well known" has a failure mode of its own,
and it bit hard this year.

In May 2026 Dutch FIOD raided **MIRHosting** and seized 800+ servers. MIRHosting is a
*wholesaler*, so the outage cascaded into THE.Hosting, PQ.Hosting, Stark Industries,
Geo.Hosting, Eurobyte, Makhost, VDSina, Alexhost and others — all at once, in June,
when nLighten then powered down MIRHosting's remaining racks without notice. Every one
of those looked like an independent "less well-known provider" until the day they
turned out to be the same rack.

**What you actually want is not obscurity, it is independence:**

- **Its own ASN and its own hardware**, not resold capacity. This is checkable —
  `fleet check <ip>` prints the AS holder. If you rent from "SmallHost" and the holder
  comes back as someone else's name, you have learned that SmallHost is a reseller and
  that you share fate with everyone else on that upstream.
- **A boring customer base.** A host marketed at the circumvention or "offshore" crowd
  concentrates exactly the traffic that attracts sweeps — and sanctions (Aeza).
- **A jurisdiction that is not a single point of failure.** Half of the June cascade
  was one Dutch datacenter.
- **An API and short billing increments**, or rotation is manual.

That last requirement is what makes this hard: small independent hosts usually have no
API and bill monthly. The honest position is that you will trade rotation speed for
ASN quality. With the detection base rate as low as it actually is (see
`docs/05-detectability.md`), **slow rotation on a clean ASN beats fast rotation on a
swept one.** Set `max_age_hours = 336` and stop churning.

## Do not pick a provider — build a method

Any list of "good hosts" is stale within weeks; that is why this repo does not ship
one. What it ships instead:

```bash
fleet check <ip>          # AS number, holder, prefix, blocklist presence
fleet status              # ASN column, and a warning if all exits share one AS
fleet health              # live reachability — the only ground truth
```

Three rules that survive the lists going stale:

1. **Never run two exits on one ASN.** `fleet status` warns when you do. A sweep is
   per-AS, so ASN diversity is the entire defence. `choose_provider` already biases
   towards a provider you are not already using.
2. **Probe before you trust.** `fleet` now treats "provider says running, nothing
   answers from here" as the signature of a swept range rather than a broken server,
   and **destroys and re-rolls automatically** (`provision_exit_with_reroll`, 3
   attempts). Providers allocate from large pools, so a second roll often lands in a
   different prefix — and if it does not, the next attempt moves provider.
3. **Measure from where the clients are.** In cascade mode `fleet health` probes each
   exit over SSH *from the Russian entry node*. In direct mode it probes from wherever
   `fleet` runs — which is the point of the next section.

### A note on the aggregate blocklist

`fleet check` consults [russia-blocked-ips](https://github.com/eduard256/russia-blocked-ips)
(~40k merged ranges from ~146 sources, refreshed every 6h, cached in `state/`). It is
**advisory and deliberately not a gate**: it merges whole cloud and CDN allocations
that are *candidates* for sweeping, so it flags 1.1.1.1, 8.8.8.8 and most of Hetzner.
Gating on it would reject nearly every address you can rent. Use it as context; use
the live probe as the decision.

## Provider quirks that cost money, or a night

Everything below was measured against a live account, not read off a pricing page.
None of it appears in the marketing copy, and each item either bills you for
nothing or leaves you with a server you cannot reach.

### The failure that looks like censorship but isn't

Worth internalising before the specifics, because it will happen to you: **a
provider-side firewall is indistinguishable from a swept IP range** unless you
check. `fleet` deliberately reports "provider says running, nothing answers from
here" as a probable sweep, because from inside Russia that is usually right. The
first time it was wrong, the real cause was UpCloud's platform firewall blocking
port 2222 — and the "swept range" message was completely convincing.

Two cheap habits settle it in seconds:

- **Probe from a second vantage point.** If you already have one working exit, your
  own tunnel *is* a foreign vantage point. Reaching the new node from abroad but not
  from home means censorship; failing from both means the provider or the node.
- **Read 401 vs 403.** On any sane API, 401 is "who are you?" and 403 is "I know
  who you are, and no". They separate a broken client from a missing account grant
  in one request. Most API debugging that starts with "is the token right?" ends
  there.

### Hetzner — the ASN you get depends on the location

Hetzner Cloud is not one ASN. The German estate is **AS24940**, which is
comprehensively blocked from Russia. Hetzner **Singapore is AS215859**
(`5.223.0.0/17`), a separate allocation that passes. Ordering "Hetzner" tells you
nothing; ordering `sin` does. If an exit ever comes up in AS24940, treat it as a bad
roll and re-roll — `fleet check <ip>` tells you which you got.

Their Debian images ship root with an **already-expired password**. PAM then refuses
every session, including key-authenticated non-interactive ones, with *"password
change required but no TTY available"* — you are locked out of a server that booted
perfectly. `deploy/bootstrap.sh` clears it via `chpasswd: expire: false` plus a
`bootcmd` `chage`, on every provider, because it costs nothing and the failure is
opaque.

### UpCloud (AS202053) — the trial firewall is a wall

A promising ASN: absent from both the 391-AS full block and the 47-AS dynamic list,
with a Singapore zone. **Measured from a Russian home line: passes the 14–34 KB DPI
freeze in both directions**, 1 MB uploaded in 4.32 s against 4.27 s for
known-good Hetzner Singapore. `DEV-1xCPU-1GB-10GB` is EUR 3.75/mo in `sg-sin1`
(1 TB transfer — the same price as `STARTER-1xCPU-1GB` with half the transfer).

Two things will stop you before any of that:

- **Trial accounts get a mandatory platform firewall.** It is enabled by default
  with a 50-rule starter set, and `PUT /server/{uuid} {"firewall":"off"}` returns
  `403 TRIAL_FIREWALL — "Trial mode firewall cannot be disabled."` Inbound allows
  only 22/80/443/3389/8443/8880 — **not 2222**, where the bootstrap moves sshd, so
  provisioning locks itself out at the moment it succeeds. Outbound is worse and is
  the real killer: **only tcp 80/443/8080/11550-11570/6443 and udp 53/123**, then
  `drop any`. An exit must reach arbitrary ports for its clients; mail, QUIC and
  non-standard HTTPS all die silently. A minimum **10-unit deposit** leaves trial
  and unlocks it, and **existing servers are retained**, so you do not rebuild.
- **`"tier": "maxiops"` is rejected by every cheap plan.** All DEV/STARTER/
  CLOUDNATIVE plans answer `409 TIER_INVALID`. Omit the tier and let UpCloud pick.

### Fornex (AS48040) — cheap, fast, and the most awkward API here

The most interesting ASN in this document and the hardest to use. 22 prefixes, small
enough that sweeps have not bothered with it, and **167 ms from the Russian Far East
against 293 ms to Singapore** — European exits are *faster* from the Russian
Far East, because the traffic transits Moscow either way. Do not reason about latency from a
map; measure.

Four quirks, in the order they will bite:

1. **VPS creation is disabled by default.** `POST /orders/vps/` returns 403 until
   their support enables it per account. The grant covers a **cluster** of
   endpoints, not just create — `templates`, `apps`, `snapshots` and `services` are
   all gated too, while `locations`, `disks`, `plans`, `ssh_keys` and `account`
   work. So an image lookup fails with the same 403 and looks like a different bug.
2. **Billing is whole months with no early cancel.** `term` is in months (1/3/6/12).
   `POST /orders/{order_id}/cancel/` defaults to `when=expired`, which only blocks
   autorenewal, and `when=now` is documented as *"not yet supported"*. A node
   destroyed after three days still costs a full month, so at `max_age_hours = 72`
   Fornex bills roughly **ten fresh months per month** — about EUR 55 against
   Hetzner Singapore's EUR 19.61. **Never put it in a fast-rotating pool.** It is a
   slow diversity anchor beside providers that bill hourly, or it is nothing.
3. **No `user_data` / cloud-init at all.** The only lever at order time is
   `ssh_keys`, so provisioning must be two-phase: order with a key, then push the
   build over SSH. `fleet` handles this with `needs_ssh_bootstrap` on the driver and
   `deploy.bootstrap_script()`, which renders the *same* inputs as the cloud-config
   so the two paths cannot drift.
4. **`ssh_keys` must be passed explicitly**, and `save_backup` defaults to **true**.
   Their docs: *"By default, an empty list is passed, even if there are keys marked
   as 'add by default' in your dashboard."* With no cloud-init, forgetting it means
   a running server with no way in — a paid brick. And every teardown would
   otherwise snapshot the disk, which is chargeable.

Capacity is genuinely scarce and moves by the minute: `disabled_by_slots` on the
locations endpoint flipped between all-full, DE-only and SE-only within one
evening, and once disagreed with their own control panel. Treat it as advisory,
pick a region at call time rather than from a static list, and let the re-roll
handle a create that fails anyway.

### The general shape of it

Cheap providers are cheap because something is manual, gated, rationed, or billed in
units that do not suit rotation. Before committing to one, answer four questions —
all four have bitten somebody here:

1. **Can the API create a server at all**, or is that a support ticket?
2. **What is the minimum billing unit, and is cancellation prorated?** This decides
   whether the provider can rotate at all. A cheap monthly host is expensive if you
   replace it every 72 hours.
3. **Is there a platform firewall**, and can you turn it off? Check the *outbound*
   rules especially — an exit needs arbitrary destination ports.
4. **Does it accept `user_data`?** Without it, provisioning is two-phase and you
   must have a key on the box from the first second.

## Where the orchestrator lives — and why it need not be a Russian VPS

Running the fleet manager on a Russian server is a tempting option. It solves three
real problems at once: always-on (no self-expiry footgun), a Russian vantage point for
health checks, and somewhere domestic to serve the subscription from.

**But it is the wrong machine to hold the keys.** The orchestrator carries provider API
tokens, the SSH key for every exit, and `state/fleet.json` — every REALITY private key
and every client UUID. Putting that on a Russian VPS means putting it on hardware that
is identity-bound, SORM-attached, and the single most seizable asset in the design. An
adversary with it can enumerate and destroy your entire fleet and impersonate every
client.

**An always-on box at home gets you all three benefits and none of the exposure:**

- It is physically in Russia, on the same ISP as your clients, so `fleet health` and
  the provisioning probe measure *exactly* the path that matters. This is a better
  vantage point than a Russian datacenter, which sits on a different network with
  different TSPU behaviour.
- It is under your physical control. No registry, no SORM, no takedown order.
- It is always on, so `expiry_buffer_hours` stops being load-bearing.
- It can serve the subscription file on the LAN, where no URL leaks at all.

A Raspberry Pi, a NAS, an old mini-PC — `fleet` needs Python 3.11+, `ssh` and `curl`
and nothing else, precisely so it can live on something like that. The Beryl itself
has the capacity (512 MB RAM, 256 MB flash) and OpenWrt 24.10 ships Python 3.11+, so
it qualifies too — but only if it never leaves the house, because it would be carrying
every key in the fleet. Custody is not the main objection though: the stronger ones are
the circular dependency (a bad `fleet sync` severs the network of the box that would
have to repair it) and the fact that a firmware upgrade or factory reset wipes
`/overlay` and with it every REALITY key. Measured capacity figures and the full
argument — including why the answer differs for a **remote** site — are in
`docs/02-architecture.md`, "Could the orchestrator just run on the Beryl?".

**The resulting shape, on a fixed-line home ISP:**

```
 always-on box at home ── fleet cron ──▶ provider APIs
        │                                      │
        │ serves subscription on LAN           ▼
        ▼                             rotating exits, ≥2 distinct ASNs,
 Beryl AX ──split: RU direct out home ISP──────┘  less-known independent hosts
          └─foreign──▶ exit (VLESS+REALITY+Vision)
```

No Russian VPS anywhere. Add the cascade later **only if** the mobile test in
`docs/05-detectability.md` shows phones stalling at ~16 KB — that is the one symptom a
domestic first hop fixes and nothing else does.


---

## Measuring instead of guessing: `dpi-detector`

Published lists answer "has anyone ever listed this range", which is not the question.
The question is whether *your* line can hold a connection to *this* host, and the
community has a tool for exactly that:

**<https://github.com/Runnin4ik/dpi-detector>** — discussed on ntc.party thread 22237
(111 posts, active through 2026). It tests:

- **TCP 14–34 KB drops** against 110 probe targets across **43 hosting/CDN ASNs**
  (`tcp16.json`) — note the real freeze window is 14–34 KB, wider than the 16 KB
  figure quoted elsewhere in these docs
- **"white SNI" discovery** — finds a working hostname *within the same AS*, which
  separates "this domain is blocked" from "this whole network is blocked"
- **DNS interception** — UDP/53 hijack, stub-IP substitution, DoH/DoT blocking
- TLS 1.2 / 1.3 / HTTP reachability across 42 domains

```bash
docker run --rm -it --pull=always ghcr.io/runnin4ik/dpi-detector:latest
```

Native builds exist for macOS, Linux (incl. ARM64, so it runs on the orchestrator Pi),
Windows, Termux and a-Shell.

**Run it with the tunnel off.** Any bypass tool active (podkop, zapret, GoodbyeDPI)
distorts the result — you end up measuring your own circumvention, not the ISP.

Two things worth knowing about the ASN list it ships: it covers the usual suspects
(AS24940 Hetzner, AS14061 DigitalOcean, AS20473 Vultr, AS63949 Akamai, AS51167
Contabo, AS53667 FranTech, AS16276 OVH, AS16509 AWS, AS31898 Oracle, AS13335
Cloudflare), and **UpCloud AS202053 is absent from it** — obscure enough that nobody
is routinely testing it, which is a weak positive signal rather than a guarantee.

### Also useful, but answering a different question

ntc.party thread 4867, "List of VPS/Dedicated server network censorship in Russia",
tabulates **Russian** providers by whether their *uplinks* are censored. That is the
list for choosing a cascade entry node — it says nothing about foreign exits seen from
a home ISP. Bulk data is from 2023 with a refresh in April 2026.

**ntc.party is IPv6-only** (`2a02:e00:ffec:4b8::1`, no A record). On an IPv4-only line
it is unreachable and looks like DNS failure; read it through
`web.archive.org/web/2026/https://ntc.party/...`.
