# DNS

Your instinct is right — running a local resolver on the always-on box is worth doing.
But the obvious version of it now **backfires**, and the reason is recent enough that
most guides are wrong about it.

## What changed in 2026: interception, not just blocking

| When | What |
|---|---|
| **3 July 2026** | `8.8.8.8` stops answering over TCP at several Russian ISPs — DoH (443/tcp), DoT (853/tcp) and DNS-over-TCP/53 all time out |
| **August 2026** | Plain UDP queries to `8.8.8.8`, `8.8.4.4`, `1.1.1.1`, `1.0.0.1` stop reaching Google and Cloudflare at all: **TSPU recognises DNS in the packet and rewrites the destination to NSDI**, the state's National Domain Name System |
| **1 September 2026** | RKN regulation approved mandating **DoH blocking via TSPU** |

Affected ISPs reported so far include Rostelecom, Dom.ru, Tattelekom, SkyNet and
Beeline — and like everything else in this system, it varies by region.

The second row is the dangerous one. It is not a block, it is a **silent redirect**.
You send a query to Cloudflare, the state answers, and nothing in your client tells
you. That is also why the aggregate blocklist flags `1.1.1.1` and `8.8.8.8`.

**So the naive build is worse than doing nothing:** stand up dnscrypt-proxy at home,
point it at Cloudflare DoH, and you either get timeouts or you have built a tidy
encrypted pipe to a hijacked endpoint — with *less* visibility than before, because
everything now looks encrypted and healthy.

## The shape that works: DNS split mirrors the routing split

The rule is the same one that governs traffic, applied to names:

| Name | Resolved where | Why |
|---|---|---|
| Russian domains | **domestic resolver, from home** | You need domestic CDN answers, and a Russian home IP querying a Russian resolver is the most ordinary flow on the network |
| Everything else | **at the exit, through the tunnel** | Never resolved on Russian soil, so there is nothing to intercept, poison, or log |
| LAN names | the local resolver | obvious |

**Foreign names are not resolved at home at all.** That is not a compromise, it is the
strongest available option, and the configs in this repo already do it:

- The client sends a **hostname**, not an address, to the proxy (`--socks5-hostname`,
  or any proxy-aware app), and `domainStrategy: "AsIs"` passes it along untouched.
- `sniffing` with `destOverride` recovers the name from TLS SNI even when an app
  resolved locally first, so the routing decision still uses the name.
- On the router, podkop's **FakeIP** does the same for non-proxy-aware clients: a
  synthetic address locally, the real name travelling inside the tunnel.
- The **exit** node resolves, using DoH from a country where that works.

This is also why the tunnel has no DNS bootstrap problem: clients reach exits by **IP**
and REALITY's SNI is a literal string, so the tunnel comes up with no resolution at all.
DNS can be completely broken and you can still connect and fix it.

## So what does the local resolver actually do?

Three jobs, none of them "resolve foreign domains":

1. **Russian domains**, over DoT to a domestic resolver.
2. **Blocking and caching** for the traffic that stays local.
3. **LAN names.**

### Yandex DNS endpoints (verified 2026-09-26)

| Mode | IPv4 | DoT / DoH hostname |
|---|---|---|
| Basic | 77.88.8.8, 77.88.8.1 | `common.dot.dns.yandex.net` (853 / 443) |
| Safe | 77.88.8.88, 77.88.8.2 | `safe.dot.dns.yandex.net` |
| Family | 77.88.8.7, 77.88.8.3 | `family.dot.dns.yandex.net` |

Use **Basic**. Safe and Family apply their own filtering, which is a second censor.

`inventory.toml` defaults `entry.split.ru_dns` to plain `77.88.8.8` deliberately: the
interception described above targets *foreign* public resolvers, a domestic query is
not the thing being redirected, and plain UDP has no failure mode that takes the split
down with it. `orchestrator/dnscrypt-proxy.toml` uses DoT for the same lookups on the
home box, where a failure is visible and recoverable.

## Pi-hole, dnscrypt-proxy, Unbound — which does what

- **dnscrypt-proxy** is the piece that matters. It speaks DoT/DoH/DNSCrypt/ODoH and,
  critically, has `forwarding_rules` for **per-domain upstream selection** — which is
  exactly the split above.
- **Pi-hole** is blocking, caching and a UI. It is a forwarder, not a resolver, so it
  sits *in front of* dnscrypt-proxy. Worth it if you want the dashboard; not load-bearing.
- **Unbound** does full recursion from the root. Tempting — no third party sees your
  queries — but recursion **from Russia** means cleartext DNS from your home address to
  authoritative servers all over the world, in the clear, past TSPU. It is the wrong
  tool here for foreign names. Fine for a local zone.
- **ODoH** hides your address from the resolver by interposing a proxy. If foreign
  lookups already egress from your own exit, the resolver already cannot see your
  address, so ODoH buys little and adds a dependency. Skip it.

Chain: `clients → Pi-hole (optional) → dnscrypt-proxy → {RU: Yandex DoT | rest: nothing}`.

### On ad-blocking

If foreign names never reach Pi-hole, Pi-hole cannot block foreign ad domains. Do that
blocking **at the proxy layer instead** — `entry.split.block_domains` in
`inventory.toml` sends matching domains to a blackhole outbound, and that applies to
every device including phones on mobile data, which Pi-hole never could.

```toml
[entry.split]
block_domains = ["geosite:category-ads-all"]
```

## Detecting interception, because it is silent

This is the part that needs tooling rather than prose. `tests/t11_dns_integrity.sh`
runs the checks that distinguish "my DNS is fine" from "the state is answering":

- Ask `1.1.1.1` for `id.server` in class CHAOS. Real Cloudflare returns a datacenter
  code; NSDI does not.
- Ask `8.8.8.8` for `o-o.myaddr.l.google.com TXT`. Real Google echoes the resolver's
  address; a redirect does not.
- Check whether DoT (853) and DNS-over-TCP (53) to those addresses complete at all.
- Compare answers for the same name from the system resolver, the domestic resolver,
  and **through the tunnel** — divergence on a name you expect to be filtered is the
  signature.

Run it from the home box, not from your laptop on some other network.
