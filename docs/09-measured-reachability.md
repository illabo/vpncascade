# Measured reachability — what actually works from a Russian home ISP

**There is no reliable public list of this.** Published AS blocklists are wrong in
both directions: they marked Hetzner AS215859 100% blocked when it demonstrably
works, and they cannot tell you that AS24940 and AS215859 — the same company —
behave oppositely. The only authority is a probe from the line you will actually
use. This file records ours.

- **Measured:** 2026-09-27
- **From:** Rostelecom AS12389, fixed residential line, Russian Far East
- **Tool:** [`Runnin4ik/dpi-detector`](https://github.com/Runnin4ik/dpi-detector) v4.2.4, tests `1234`
- **Tunnel state:** none active (podkop installed but inert) — mandatory, or you measure your own circumvention
- **Raw report:** `state/probes/2026-09-27-rostelecom.txt` (gitignored)

Headline: **21 of 110 endpoints survived (19%)**. The failure mode is a read timeout
at **15–19 KB**, which is why "the 16 KB freeze" is really a 14–34 KB window.

## Passes — safe to build on

| ASN | Provider | endpoints OK | |
|---|---|---|---|
| `AS12222` | Akamai 3 | 2/2 | — |
| `AS33905` | Akamai 4 | 2/2 | — |
| `AS215859` | Hetzner Cloud 4 | 2/2 | — |
| `AS46652` | DigitalOcean 2 | 2/2 | — |
| `AS54113` | Fastly | 2/2 | — |
| `AS54113` | Fastly GitHub 108 | 1/1 | — |
| `AS54113` | Fastly GitHub 109 | 1/1 | — |
| `AS54113` | Fastly GitHub 110 | 1/1 | — |
| `AS54113` | Fastly GitHub 111 | 1/1 | — |
| `AS54113` | Fastly Gog | 1/1 | — |
| `AS8987` | AWS 3 | 2/2 | — |
| `AS396982` | Google Cloud | 2/2 | — |
| `AS48040` | Fornex | 1/1 | — |

## Partial — works sometimes, do not rely on it

| ASN | Provider | endpoints OK | failure |
|---|---|---|---|
| `AS14618` | AWS 2 | 1/2 | Read Timeout at 15KB, 18.0s |

## Fails — do not rent exits here

| ASN | Provider | endpoints OK | failure |
|---|---|---|---|
| `AS20940` | Akamai 1 | 0/3 | Read Timeout at 15KB | 13.2s |
| `AS20940` | Akamai 1 HTTP | 0/1 | Read Timeout at 23KB | 11.7s |
| `AS16625` | Akamai 2 | 0/2 | Read Timeout at 15KB | 17.1s |
| `AS63949` | Akamai Cloud | 0/2 | Read Timeout at 15KB | 8.3s |
| `AS31898` | Oracle Cloud | 0/4 | Read Timeout at 19KB | 15.4s |
| `AS54253` | Oracle 1 | 0/2 | Read Timeout at 15KB | 8.6s |
| `AS6142` | Oracle 3 | 0/2 | Read Timeout at 19KB | 8.3s |
| `AS14544` | Oracle 4 | 0/1 | Read Timeout at 15KB | 5.3s |
| `AS20054` | Oracle 5 | 0/1 | Read Timeout at 15KB | 7.0s |
| `AS24940` | Hetzner | 0/2 | Read Timeout at 19KB | 8.9s |
| `AS213230` | Hetzner Cloud 2 | 0/2 | Read Timeout at 19KB | 11.1s |
| `AS212317` | Hetzner Cloud 3 | 0/2 | Read Timeout at 19KB | 18.4s |
| `AS12876` | Scaleway 1 | 0/3 | Read Timeout at 15KB | 7.9s |
| `AS12876` | Scaleway 1 HTTP | 0/1 | Read Timeout at 23KB | 17.8s |
| `AS29447` | Scaleway 2 | 0/2 | Read Timeout at 19KB | 17.9s |
| `AS51167` | Contabo 1 | 0/4 | Read Timeout at 19KB | 14.8s |
| `AS141995` | Contabo 2 | 0/2 | Read Timeout at 19KB | 6.8s |
| `AS14061` | DigitalOcean 1 | 0/5 | Read Timeout at 15KB | 13.5s |
| `AS16509` | AWS 1 | 0/2 | Read Timeout at 19KB | 18.5s |
| `AS60068` | CDN77 #1 | 0/1 | Read Timeout at 19KB | 14.3s |
| `AS60068` | CDN77 #1 HTTP | 0/1 | Read Timeout at 23KB | 7.9s |
| `AS212238` | CDN77 #2 | 0/3 | Read Timeout at 19KB | 7.4s |
| `AS13335` | Cloudflare | 0/6 | Read Timeout at 15KB | 2.6s |
| `AS199524` | Gcore 1 | 0/4 | Read Timeout at 19KB | 9.3s |
| `AS202422` | Gcore 2 | 0/1 | Read Timeout at 19KB | 17.3s |
| `AS16276` | OVH | 0/3 | Read Timeout at 19KB | 5.3s |
| `AS16276` | OVH HTTP | 0/1 | Read Timeout at 23KB | 7.7s |
| `AS20473` | Vultr | 0/4 | Read Timeout at 15KB | 3.7s |
| `AS62240` | Clouvider | 0/2 | Read Timeout at 19KB | 9.4s |
| `AS53667` | FranTech | 0/1 | Read Timeout at 15KB | 6.1s |
| `AS20860` | IOMART 1 | 0/1 | Read Timeout at 15KB | 6.5s |
| `AS21130` | IOMART 2 | 0/1 | Read Timeout at 19KB | 14.7s |
| `AS8849` | Melbicom 1 | 0/1 | Read Timeout at 15KB | 4.5s |
| `AS56630` | Melbicom 2 | 0/1 | Read Timeout at 15KB | 3.4s |
| `AS51765` | CreaNova | 0/1 | Read Timeout at 19KB | 17.6s |
| `AS9009` | M247 Europe SRL | 0/1 | Read Timeout at 15KB | 9.1s |
| `AS58061` | Scalaxy | 0/1 | Read Timeout at 15KB | 16.4s |
| `AS21859` | Zenlayer | 0/1 | Read Timeout at 19KB | 18.1s |

## The rule this produced

**Never reason about a provider by brand — resolve the ASN of the specific
location.** Hetzner announces at least four ASNs and exactly one passes:

| Hetzner ASN | What | Result |
|---|---|---|
| AS24940 | German estate | fails at 19 KB |
| AS213230 | Cloud 2 | fails at 19 KB |
| AS212317 | Cloud 3 | fails at 15–19 KB |
| **AS215859** | **Cloud 4 / `CLOUD-SIN` / Singapore** | **passes** |

`regions = ["sin"]` lands in AS215859 deterministically, because Singapore is its
own Hetzner network zone. Verified on live exits 2026-09-27.

DigitalOcean is the inverse trap: AS46652 passes but is only 3,584 addresses of
legacy space, while **every** DO Singapore range is AS14061, which fails. You
cannot ask DO for a specific ASN, so that pass is not reproducible.

## Re-running this

> **Superseded 2026-09-29.** This said to run it on the orchestrator Pi "because the
> Pi sits on the bare ISP". That was true when written and is no longer. podkop has
> since been configured, and `routing_excluded_ips` is a source-IP rule *inside*
> sing-box rather than an nftables bypass — so the Pi's TCP connections are
> terminated on the router and re-originated by sing-box. Its egress IP is still the
> bare ISP, so IP-based checks stay valid, but every timing, TCP option and TLS
> record pattern is sing-box's. Measured: TCP connect from the Pi to `1.1.1.1` takes
> 0.0008 s against a 118 ms ICMP RTT.

Run it from a laptop joined **directly to the upstream router's WiFi** (`35`,
`192.168.88.x`), with the laptop's own VPN off. Verify the vantage point first —
both checks, every time:

```bash
netstat -rn -f inet | grep default        # must be the MikroTik, not a utun
curl --interface en0 https://api.ipify.org  # must be the ISP address, not 5.223.x
```

Then:

```bash
~/dpi/bin/python dpi_detector.py -t 1234 --batch -o ~/dpi-report.txt
```

`-d <domain>` checks specific hosts, which is how you point it at a candidate
provider's own infrastructure. The tool has no notion of an ASN — you have to give
it something that lives in the ASN you care about.

Re-measure after any ISP change, after a TSPU escalation, and before committing to a
new provider. Results go stale: this is a moving target by design.
