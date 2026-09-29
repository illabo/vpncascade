# What every piece does

A map of the stack, then a `fleet` reference.

## The stack

| | What it is | Where it runs | Why it is there |
|---|---|---|---|
| **fleet** | this repo's CLI | the Pi | Rents, rotates and destroys exit servers; generates every config so the two ends can never drift apart |
| **Xray** | the proxy engine (Go) | each exit; optionally a router | Speaks VLESS/REALITY. The reference implementation |
| **VLESS** | the proxy protocol | — | Thin transport inside TLS. No crypto of its own — REALITY provides that |
| **REALITY** | TLS camouflage | both ends | Borrows a real site's certificate. A prober connecting without credentials is proxied to the genuine site and sees nothing unusual |
| **XTLS-Vision** | the transport, `transport = "tcp"` | both ends | After the handshake it *splices* instead of re-encrypting — much cheaper on a router. Works with everything, including sing-box |
| **XHTTP** | alternative transport | both ends | Better against flow-shape analysis and CDN-frontable. **Xray only** — upstream sing-box does not implement it |
| **podkop** | orchestration on OpenWrt | the Beryl | Wires sing-box + nftables TPROXY + dnsmasq FakeIP together, with a LuCI page. This is what makes the split transparent to every LAN device |
| **sing-box** | proxy engine podkop drives | the Beryl | Simpler config, good UDP. Cannot do XHTTP — hence `transport = "tcp"` |
| **AmneziaWG** | obfuscated WireGuard | optional | UDP, kernel-speed. Fails where UDP is suppressed, so it is a fast *second* path, never the only one |
| **stubby** | DNS-over-TLS client | the Pi | Encrypts lookups for Russian names to a domestic resolver |
| **dnsmasq** | DNS forwarder + cache | the Pi | Sends Russian names to stubby and deliberately resolves nothing else — foreign names travel inside the tunnel |
| **SporeStack** *(or Hetzner/Vultr/DO)* | where exits are rented | — | SporeStack needs no account and expires servers by itself |

### The two ideas the whole thing rests on

**REALITY is camouflage, not encryption.** Your node presents `corp.ozon.ru`'s real
certificate. Anyone who connects without the right key gets silently proxied to the
genuine site. That is why the masking host must be reachable *from that node* — an
unreachable one turns probe-resistance into a unique, scannable fingerprint.

**The split is what makes it liveable.** Russian traffic never enters the tunnel: it
leaves via your own ISP, from a Russian residential address that banks like better
than any datacenter IP. Only foreign traffic pays the tunnel's latency and CPU.

---

## fleet — TL;DR

A single-file-per-concern Python CLI, **no third-party packages**, so it runs on a Pi
with nothing but `python3`, `ssh` and `curl`.

**What it is for:** treating exit servers as cattle. It rents them, configures them,
checks them, replaces them and destroys them, and generates matching configs for both
ends so they cannot drift.

**What it is not:** it is never in the data path. Your traffic never passes through
the machine running fleet. If the Pi is off for five days, nothing breaks — that is
what `exits.expiry_buffer_hours` buys.

### Three files you touch

| | |
|---|---|
| `inventory.toml` | everything you decide: mode, providers, the RU split list, rotation policy |
| `.env` | provider API tokens. `chmod 600`, never committed |
| `state/fleet.json` | what exists right now, and every key. Written by fleet, **back it up** |

### Daily

```bash
fleet status                 # what exists, how old, which ASNs, what is serving
fleet health                 # probe every exit; warns on low paid runway
fleet up                     # bring the pool to pool_size
fleet rotate                 # replace anything past max_age_hours
fleet cron                   # all of the above — what the systemd timer runs
```

### Clients

```bash
fleet client add beryl       # a credential per device or site
fleet client uri beryl --qr  # share links; QR for phones
fleet client config beryl -o beryl.json    # full Xray config, desktop or OpenWrt
fleet client revoke oldphone # revoke one device without re-keying the rest
fleet subscription           # a polled URL so rotation is invisible to clients
fleet subscription site-b -o site-b-sub.txt      # ...or a file, to carry to another site
```

**Give every site its own client.** Exporting `beryl`'s subscription and using it at
a second house means one UUID in two places: you cannot revoke one without killing
the other, and the exits see a single identity connecting from two ISPs.

```bash
fleet client add site-b --sync                # --sync is REQUIRED, not cosmetic:
                                           # it pushes the credential to every
                                           # serving exit. Without it the UUID
                                           # exists only locally and every exit
                                           # rejects it.
fleet subscription site-b -o site-b-sub.txt      # use -o; plain stdout mixes in log lines
```

### Moving a config to a site you cannot reach

A remote household is not on this LAN, so `fleet sync` cannot reach its router (see
`docs/02-architecture.md`). Until that is solved properly, carry the file:

```bash
# on the orchestrator
fleet subscription site-b -o site-b-sub.txt      # ~1 KB of base64

# at the other site, either upload it on that Beryl's panel (import page), or:
fleet router push --from-file site-b-sub.txt --dry-run   # list what it would do
fleet router push --from-file site-b-sub.txt             # apply
```

`--from-file` accepts the base64 blob *or* a plain list of `vless://` lines, and is
the same code path the panel's import button uses — there is one implementation of
the podkop schema, deliberately (HANDOFF #32).

Three things about that file:

- **It is a credential.** Anyone holding it has that client's access until
  `fleet client revoke site-b`. Treat it like a password, not a config.
- **It goes stale.** Exit addresses rotate every `max_age_hours` (72 by default), so
  a file exported today is wrong by the weekend. This staleness is the whole
  argument for a pull-based channel rather than hand-carried files.
- **Any normal client reads it** — v2rayTun, NekoBox, Happ — so the same export
  serves a phone, not only a router.

### Occasional

```bash
fleet check <ip>             # AS number, holder, blocklist presence
fleet dns                    # regenerate the Pi's split-DNS config
fleet render entry|exit      # print a generated config without deploying it
fleet sync                   # push current state to the servers
fleet destroy --all          # tear down every exit (never the entry node)
fleet providers --catalogue  # live SporeStack slugs
```

Add `-i site-b.toml` to work on another site; its state separates automatically.

### The safety properties, so you know what you can lean on

- **Fails closed.** With no serving exits, clients are blocked rather than leaking.
- **Make-before-break.** A rotation stands the replacement up, verifies it, adds it to
  the balancer, drains, and only then destroys the old one.
- **Never restarts onto a broken config.** Every push runs `xray -test` remotely first
  and rolls back if the service does not come up.
- **No trust-on-first-use.** SSH host keys are generated locally and injected via
  cloud-init, then pinned — servers are created unattended, with nobody to eyeball a
  fingerprint.
- **Re-rolls blocked addresses.** "Provider says running, nothing answers from here" is
  treated as a swept IP range, and the server is destroyed and replaced automatically.
- **Secrets are scrubbed** from cloud-init user-data on first boot.

### The LAN control panel

`orchestrator/fleet-web.py`, installed and enabled by the bootstrap script on port
**8088**. It exists for the case SSH cannot cover: an orchestrator at a site you do
not live at, behind CGNAT, where someone in the room has to paste in a credential
you sent them.

- **status** — live `fleet status`, plus the eight actions as buttons
- **credentials** — set provider tokens into `.env`. Shows only *set* / *not set*; a
  saved value is never rendered back, and a blank field leaves the existing one alone
- **import** — upload a subscription and push it to the router on this LAN. Two
  steps on purpose: the file is parsed and shown for confirmation (address, SNI,
  transport, flow, **truncated** client id) before anything is sent. Confirmations
  are single-use and expire in 10 minutes, and the page warns if this orchestrator
  has serving exits of its own, because a rotation will overwrite the import

Deliberately narrow, because a panel that holds provider tokens and can spend money is
worth keeping boring: stdlib only, one scrypt-hashed password, rate-limited login,
HMAC-signed session cookies, and a fixed action allowlist invoked as argument lists —
never a shell. **LAN only. Do not port-forward it.**

```bash
orchestrator/fleet-web.py --set-password     # change it
```

### Tests

`tests/t00_local_cascade.sh` and `t09_direct_failover.sh` need **no servers** — they
run real Xray processes on loopback. Run t00 after any config change. The rest
(`t01`–`t08`, `t11`) need a live fleet; `tests/run-all.sh` runs what applies.
