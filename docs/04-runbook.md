# Runbook

## First deployment, start to finish

**1. Buy the RU entry node by hand.** Any RKN-registered host; you will need Gosuslugi
or a passport. Debian 12, 1 vCPU / 1 GB is plenty. Put your SSH key on it at build time
if the panel lets you. Note the IP.

**2. Configure.**

```bash
cp .env.example .env && chmod 600 .env    # provider tokens
./bin/fleet init && $EDITOR inventory.toml
```

Minimum edits: `entry.host`, and at least one `[[exits.providers]]` whose credential
you actually have. Also set `FLEET_SSH_ALLOW_V4` in `.env` if you have a static
address — it is the cheapest hardening on offer.

**3. Choose the masking domain for the entry node.** It must be reachable *from the
entry node*, TLS 1.3, HTTP/2, not blocked in Russia, and plausible for a Russian client
to visit on 443. `corp.ozon.ru` is a good pattern. Verify before deploying:

```bash
openssl s_client -connect corp.ozon.ru:443 -servername corp.ozon.ru -tls1_3 -brief </dev/null
```

**4. Deploy.**

```bash
./bin/fleet entry init
./bin/fleet client add beryl && ./bin/fleet client add phone
./bin/fleet entry deploy --first-run
./bin/fleet up
```

`--first-run` connects on port 22 and accepts the existing host key, because the node
predates us and its key cannot be pre-pinned. The bootstrap then moves sshd to 2222 and
the key gets pinned; every later command uses the pinned key.

**5. Verify.**

```bash
./tests/run-all.sh
```

**6. Hand out configs.**

```bash
./bin/fleet client uri phone --qr
./bin/fleet client config beryl -o beryl-xray.json
```

**7. Schedule.** See `cron/crontab.example`.

---

## Several sites

Several households, each with a router + a small always-on box, are simplest run as
**independent deployments** rather than one fleet serving every site.

> **The "+ a small always-on box" is the hard part at someone else's house**, and it
> is load-bearing: a remote site is not on your LAN, so an orchestrator here cannot
> `fleet sync` a router there, and the LAN panel is deliberately never exposed. The
> realistic options are a box at that site, `fleet` on that site's Beryl itself, or
> subscription distribution the router pulls — which is unsolved. See
> `docs/02-architecture.md`, "Could the orchestrator just run on the Beryl?".
>
> In the meantime the manual path works and is documented under **Moving a
> config to a site you cannot reach** in `docs/08-software-overview.md`:
> `fleet client add <site> --sync`, `fleet subscription <site> -o f.txt`, carry
> the file, then `fleet router push --from-file f.txt` or the panel's import
> page. Give each site its **own** client — a shared UUID cannot be revoked
> for one household without cutting off the others.

Each site gets its own checkout, its own `inventory.toml`, its own exits and its own
state. Nothing is shared, so nothing can break sideways: no subscription to host, no
cross-site secrets, and rotating an exit at one site cannot take another site's
internet down.

The cost scales with the number of sites: two exits each at, say, $7.50/month is
about $15/month per household. Trim it by slowing rotation at remote sites
(`max_age_hours = 336`) — a site that is not yours does not need 72-hour churn, and
the detection base rate does not justify it (`docs/05-detectability.md`).

If you also want copies of every site's inventory in **one** checkout on your own
machine, name them per site — `site-b.toml`, `site-c.toml` — and state separates
automatically (`state/site-b/`, `state/site-c/`, and plain `state/` for `inventory.toml`).
Two inventories in one directory used to share one state file and silently destroy
each other; they no longer do.

### What actually makes a remote site survivable

**Fail-open.** `router/podkop-setup.sh` sets `detour_mode='direct'`, so a dead tunnel
degrades to "foreign sites do not load" rather than "the internet is down". A parent
can still bank, read the news and phone you. That single setting is the difference
between fixing it on your next visit and driving across town.

Verify it deliberately at each remote site before you leave: stop the tunnel and
confirm Russian sites still work.

### Two things that bite at remote sites

- **One provider token across three Pis.** Convenient, and it means three machines in
  three houses hold a credential that can destroy every exit and drain the balance.
  Fund it modestly and top up rather than parking a year of credit on it. Separate
  tokens per site is the stricter option if the provider allows it cheaply.
- **The orchestrator has to reach the provider API from inside Russia.** API calls are
  small and slip under the ~16 KB freeze, and SSH is reported to pass unmolested — but
  the ~700 KB blocklist download for `fleet check` may not. That failure is handled
  (it warns and uses the cached copy, or skips the check), so it degrades rather than
  breaking, but do not be surprised by it.

---

## Symptoms → causes

### Everything stopped, all at once

```bash
./bin/fleet health
```

- **entry says DOWN** — the RU node is gone or blocked. Most likely an RKN takedown
  order to the host (24-hour notice is the documented pattern) or the host acting
  pre-emptively. Check the provider panel before assuming a technical fault.
- **entry fine, every exit `BLOCKED FROM RU`** — a provider-wide range block. Add a
  different provider to `[[exits.providers]]` and `fleet rotate --force`.
- **entry fine, exits reachable, clients still broken** — most likely a client/server
  Xray version skew. XHTTP changes between releases. Pin `xray_version` to a concrete
  version in `inventory.toml` and redeploy everything.

### One exit died

Nothing to do — the balancer routes around it within ~30 s and the 15-minute cron tick
replaces it. If you are impatient: `./bin/fleet rotate --node <name> --force`.

### Connects, then dies after a few seconds or minutes

Classic DPI behaviour: the handshake passes, then statistical analysis catches up.

1. Move the masking domain. A popular dest gets probed more.
2. Check the uTLS fingerprint. `firefox` is the current survivor; `chrome` is
   reportedly burned. `inventory.toml → entry.fingerprint`.
3. Confirm you are on 443. TLS on an odd port is a standalone signal.
4. Try `xhttp_mode = "packet-up"` — slower, but it looks more like ordinary request/
   response traffic than a long-lived stream.

### Small pages load, large ones hang

PMTU blackhole. Drop the AmneziaWG `MTU` to 1280. For VLESS, check the exit's MTU.
`tests/t02_chain_egress.sh` includes a 1 MB transfer that reproduces it.

### A Russian site is slow, or geo-blocks you

It is going abroad and back — it is not in the split list.

```bash
grep -n "domain:" inventory.toml
$EDITOR inventory.toml       # add it under [entry.split] direct_domains
./bin/fleet entry sync
./tests/t03_split_routing.sh
```

Remember that listing `direct_domains` in `inventory.toml` *replaces* the 75-entry
built-in list rather than adding to it. Copy the default from `fleet/config.py` first.

### `xray failed to start … rolled back: NO — node is down`

The rollback itself failed, so the node is serving nothing. Get on it directly:

```bash
ssh -p 2222 -o UserKnownHostsFile=state/known_hosts root@<ip>
journalctl -u xray -n 50
cp /usr/local/etc/xray/config.json.prev /usr/local/etc/xray/config.json
systemctl restart xray
```

### `fleet` says a server exists but the provider disagrees

```bash
./bin/fleet reap
```

`reap` drops state entries whose servers are gone, and destroys servers carrying our
label that state does not know about — the leak you get from a crash between "create"
and "save", which quietly costs money.

---

## Routine operations

### Add a device

```bash
./bin/fleet client add laptop --sync
./bin/fleet client uri laptop --qr
```

### Revoke a device

```bash
./bin/fleet client revoke laptop     # syncs automatically
```

Per-device UUIDs are the reason this is one command instead of re-keying everyone.

### Replace the RU entry node

The painful one, because clients are pinned to its address.

1. Buy the replacement. Put its IP in `inventory.toml`.
2. `./bin/fleet entry deploy --first-run`

The REALITY keypair and all client UUIDs live in `state/fleet.json`, not on the server,
so they survive. Clients only need the **new IP** — same UUID, same key, same shortId.

This is the argument for pointing clients at a **hostname** rather than a bare IP: then
replacing the entry node is a DNS change and clients need nothing. The cost is that the
hostname is a thing RKN can block, and it leaks in DNS. For a handful of devices, an IP
plus a five-minute reconfiguration is usually the better trade.

**Keep a cold spare** on a second RU provider with the same keys, deployed but not
advertised. Then a takedown is a five-minute change rather than a shopping trip.

### Change the masking domain

```bash
$EDITOR inventory.toml        # entry.reality_dest and entry.reality_sni
./bin/fleet entry sync
./bin/fleet client uri beryl  # the URI changed — hand out the new one
./tests/t01_reality_probe.sh --entry
```

Clients must be updated: `sni` is part of the share link.

### Full teardown

```bash
./bin/fleet destroy --all     # exits only; asks for confirmation
```

The RU node is `manual` and is never touched. Delete it in the provider panel.

---

## What to check monthly

- `./tests/t01_reality_probe.sh --entry` — does your node still serve the genuine
  certificate for its masking domain? Sites change CDNs and this silently breaks.
- uTLS fingerprint viability. `firefox` works now; that will not last forever.
- Whether your exit providers' ranges are still reachable from Russia
  (`./bin/fleet health`).
- Xray releases. Pin, upgrade deliberately, upgrade all nodes together.
- Whether GL.iNet has shipped 4.9 with official AmneziaWG support, and whether it has
  moved past generation 1.0.
