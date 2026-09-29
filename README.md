# vpncascade

Obfuscated VPN with ephemeral foreign exits that rotate on a schedule, and split
tunnelling so Russian services keep a Russian source address.

Two topologies, one codebase — set `mode` in `inventory.toml`:

**`mode = "direct"`** — start here on a fixed-line home ISP.

```
 Beryl AX ──VLESS/REALITY/Vision─▶ EXIT #1 (rotating) ──▶ internet
 phones      client-side balancer ▶ EXIT #2 (rotating)
     └──direct──▶ RU banks, gosuslugi, Ozon…   (your own home ISP address)
```

No Russian node: nothing tied to your passport, nothing for RKN to order a host to
delete. Russian sites see your home IP, which is a *better* source than a datacenter
one — residential, stable, and bank anti-fraud likes it. The cost is that clients hold
exit addresses, so rotation is visible to them (`fleet subscription` fixes that), and
every exit holds all client credentials — and **rotating an exit does not revoke
them**: the same UUIDs are pushed to each replacement, so use `fleet client revoke`
when a credential rather than an address needs changing.

**`mode = "cascade"`** — add a Russian anchor when you need one.

```
 Beryl AX ──VLESS/REALITY/Vision─▶ ENTRY (RU, stable) ──REALITY──▶ EXIT (rotating)──▶ internet
 phones   ──AmneziaWG───────────▶        │
                                         └──freedom──▶ RU services  (Russian source IP)
```

Clients only ever know the entry node, so exits rotate invisibly. Worth the extra node
when phones on mobile data stall — RU DPI has been observed freezing *any* foreign TLS
connection past ~16 KB on mobile, which no amount of obfuscation helps with, and the
cascade sidesteps it because the client's leg never leaves the country.

**Resuming after a break, or handing this to someone else?** Read
[`HANDOFF.md`](HANDOFF.md) first — settled decisions, hardware status, what is blocked,
and the mistakes already made so they are not repeated.

**Setting it up?** [`docs/00-setup-plan.md`](docs/00-setup-plan.md) is the
step-by-step, and [`docs/08-software-overview.md`](docs/08-software-overview.md)
explains what each component does plus a `fleet` reference.

**Start here:** [`docs/01-research-findings.md`](docs/01-research-findings.md) answers
the "is there a unified multi-VPS API" and "what can the Beryl actually run" questions.
[`docs/02-architecture.md`](docs/02-architecture.md) explains the topology and reviews
the reference configs this was built from.
[`docs/05-detectability.md`](docs/05-detectability.md) is the threat model: what actually
gets nodes found, and why the cascade matters more than it looks.
[`docs/06-choosing-providers.md`](docs/06-choosing-providers.md) covers ASN targeting,
why "less well-known" is not the same as "independent", and where the orchestrator
should live. [`docs/07-dns.md`](docs/07-dns.md) covers DNS — since August 2026 TSPU
silently rewrites queries aimed at 8.8.8.8 and 1.1.1.1 to the state resolver, which
makes the obvious local-DoH setup actively harmful.

## What's here

| | |
|---|---|
| `bin/fleet` | the CLI — provision, rotate, tear down, generate client configs |
| `fleet/` | the library. `providers/` has one small driver per VPS provider |
| `deploy/` | cloud-init, the server bootstrap, the AmneziaWG installer |
| `router/` | GL.iNet / OpenWrt setup (podkop, or xray-core directly) |
| `tests/` | `t00` runs the whole cascade on loopback with no servers at all |
| `cron/` | scheduled rotation, with locking and failure notification |
| `orchestrator/` | the always-on box at home: SD-card flasher, one-shot bootstrap, split DNS (dnsmasq + stubby), and the LAN web panel |
| `docs/` | findings, architecture, router guide, runbook |

No third-party Python packages. Python 3.11+, `ssh`, `curl`. That is the whole
dependency list, because this runs from cron on whatever box is handy.

## Quick start

### The short path: hand it to a coding agent

These docs are written to be read by a machine as well as a person. If you use a
coding agent, the fastest route is to clone the repo, connect the hardware, and
point the agent at `docs/` with what you have:

> Read `docs/` in this repository, starting with `00-setup-plan.md` and
> `02-architecture.md`. I have **<your router>** and **<your always-on box>**, on
> **<your ISP>**. Set this up for me. Measure rather than assume, and stop and ask
> before anything that spends money or changes my router.

**The tested baseline is a GL.iNet Beryl AX (GL-MT3000) plus a Raspberry Pi 3.**
Buy that and everything here is known to work. Substituting is fine, but the router
is fussy in a way that is easy to get wrong: podkop needs **OpenWrt ≥ 24.10**, and
GL.iNet's MTK-SDK firmware builds — which often carry a *higher* version number than
the native-OpenWrt `-op` builds — cannot install it at all. Not every GL.iNet model
has an `-op` build. `docs/03-router-glinet-openwrt.md` has the matrix.

The always-on box is the relaxed one: `fleet` is stdlib-only Python 3.11+ plus `ssh`
and `curl`, so a Pi, a NAS or an old mini-PC all work, and it is never in the data
path. `docs/06-choosing-providers.md` covers picking an exit host and the provider
quirks that cost money.

Two things to insist on, because they are what this project learned the hard way:
**every reachability claim needs a known-good control in the same test**, and
**nothing that reconfigures your router should run without you seeing it first**.

### The manual path

```bash
cp .env.example .env && chmod 600 .env   # provider tokens go here
./bin/fleet init                          # writes inventory.toml
$EDITOR inventory.toml                    # set entry.host and pick providers
```

`inventory.toml` is every decision in one file: mode, transport, which providers and
regions, pool size, rotation age, and the RU domains that bypass the tunnel. `.env` is
just the provider API tokens, kept separate so the interesting file can be read and
diffed without exposing anything.

```bash
# direct mode — no Russian node needed
./bin/fleet client add beryl
./bin/fleet up                            # creates the exit pool
./bin/fleet status

# cascade mode — additionally:
# ./bin/fleet entry init
# ./bin/fleet entry deploy --first-run    # installs Xray on your RU VPS
```

```bash
./bin/fleet client uri beryl --qr         # scan into v2rayTun / Happ / NekoBox
./bin/fleet subscription                  # or: a URL clients poll, so rotation is invisible
./bin/fleet subscription site-b -o site-b.txt   # or: a file to carry to another household
./bin/fleet router push --from-file site-b.txt   # ...and apply it there (or use the panel)
```

Before any of that, and after any change to config generation:

```bash
./tests/t00_local_cascade.sh
```

That stands up three real Xray processes on loopback with genuine VLESS/REALITY
between them and verifies traffic flows and the split rule sends matching domains out
a different outbound. No servers, no money, no accounts.

## Running it unattended

Rotation only rotates if something is awake to run it. `orchestrator/` turns a spare
Raspberry Pi into that something — the box also serves split DNS, and is deliberately
**not** in the data path, so it can be off for days without anyone losing internet.

```bash
./orchestrator/flash-sd.sh --disk /dev/diskN --hostname orchestrator --user you \
    --wifi-ssid <ssid> --wifi-pass <pass>      # keys and wifi preloaded; boots headless
./orchestrator/bootstrap-orchestrator.sh you@orchestrator.local
```

The bootstrap installs fleet to `/opt/vpncascade`, moves the journal to RAM and turns
off swap (SD cards die of writes), sets up dnsmasq + stubby split DNS, installs a
systemd timer that ticks every 15 minutes with jitter, and starts a **LAN web panel on
:8088**. The panel exists so someone who will never open a terminal can paste a
provider token, see whether the exits are healthy, and press rotate. It prints a
generated password on first start:

```bash
ssh you@orchestrator "sudo journalctl -u fleet-web | grep -i 'panel password'"
```

Change it by running, **as the same user the service runs as — not under `sudo`**:

```bash
/opt/vpncascade/orchestrator/fleet-web.py -i /opt/vpncascade/inventory.toml --set-password
```

It prompts, and the new password takes effect on the next login with no restart. Under
`sudo` it writes a root-owned `state/web-password` that the service cannot read, which
locks you out. The password is scrypt-hashed, sessions are HMAC-signed, the action list is a fixed
allowlist invoked as argument vectors rather than through a shell, and secrets are
write-only — but it can still spend money, so **keep it on the LAN and never
port-forward 8088.**

A `cron` alternative for boxes without systemd is in
[`cron/crontab.example`](cron/crontab.example).

## Day-to-day

```bash
./bin/fleet status                  # what exists, how old, what's serving
./bin/fleet health                  # probes each exit FROM the entry node
./bin/fleet rotate                  # replace anything past max_age_hours
./bin/fleet rotate --node exit-het-hel1-a1b2c3 --force
./bin/fleet up                      # refill the pool
./bin/fleet reap                    # destroy orphans and expired servers
./bin/fleet check <ip>              # AS number, holder, blocklist presence
./bin/fleet dns                     # split-DNS config for the always-on box
./bin/fleet destroy --all           # tear everything down (exits only)
```

`fleet health` checks reachability **from the entry node**, not from your laptop. Large
parts of Hetzner, DigitalOcean, Vultr, OVH and AWS address space are blacklisted from
Russia; an exit that looks perfect from here can be completely unreachable from there,
and that is the only measurement that decides whether it works.

Scheduling: see [`cron/crontab.example`](cron/crontab.example). The 15-minute `cron`
tick is the important one — it replaces an exit the moment its range gets blocked,
rather than at its next scheduled rotation.

## AmneziaWG alongside VLESS

```bash
./bin/fleet awg init --generation 2.0     # GL.iNet 4.9+ speaks 2.0; use 1.0 only below 4.8
./bin/fleet awg peer beryl -o beryl.conf
./bin/fleet awg deploy --install
```

AmneziaWG traffic is tproxy'd into the same Xray instance, so it follows the same
split and the same cascade — one policy, two transports. VLESS survives networks that
suppress UDP; AmneziaWG is much cheaper on router CPU. Run both.

## Safety properties worth knowing

- **Fail closed.** With no serving exits, the entry node blocks client traffic rather
  than letting it egress from a Russian datacenter rented under your passport.
- **Make-before-break.** A rotation stands the replacement up, verifies it, adds it to
  the balancer, drains, and only then destroys the old one.
- **Never restart onto a broken config.** Every push runs `xray -test` remotely first,
  keeps the previous config, and rolls back if the service fails to come up.
- **No trust-on-first-use.** SSH host keys are generated locally and injected via
  cloud-init, then pinned — servers get created unattended, with nobody to eyeball a
  fingerprint.
- **Secrets are scrubbed.** The bootstrap shreds the cloud-init user-data, which holds
  the node's private keys and is readable by any local process.
- **The RU node cannot be destroyed by a script.** It uses the `manual` driver, which
  refuses.

## The legal situation, briefly

The Russian entry node cannot be anonymous: Russian hosts must be in the RKN registry,
must identify customers (Gosuslugi/ESIA or passport), and must interoperate with SORM.

The takedown risk is real but narrower than the headlines suggest — there is one
documented mass-notice event (Aeza, December 2025), against a host that was already
OFAC-sanctioned and publicly known as the cheap VPN host, and the nodes that got caught
had exposed management panels rather than broken obfuscation. A personal node with no
panel, no public footprint and nothing but REALITY on 443 routinely runs for months.
Keep a cold spare anyway; don't lose sleep. Full threat model in
[`docs/05-detectability.md`](docs/05-detectability.md).

Do not use Aeza — OFAC-sanctioned since 2025-07-01, and simultaneously the host RKN
made an example of.
