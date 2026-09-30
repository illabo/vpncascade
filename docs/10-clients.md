# Clients: credentials, export, import

How a device gets credentials, what those credentials are, what invalidates them, and
which of the three export formats to reach for.

Run every command here **on the box that owns the state file** — the orchestrator, if
you have one. State is deliberately excluded from the deploy rsync, so a second
checkout has its own empty copy and will tell you there is no such client.

---

## 1. What a client *is*

`fleet client add <name>` mints two secrets and stores them in state:

| field | what it is |
|---|---|
| `uuid` | the VLESS user id — the actual credential |
| `short_id` | the REALITY `sid`, matched by the server's `shortIds` list |

Both are per-client. Two devices given separate clients share no secret, and the
server can tell them apart in its logs by the `email` field, which carries the name.

A client is not live until it reaches the servers:

```bash
./bin/fleet client add phone --sync
```

Without `--sync` the client exists only in state and the device will fail to connect.
`fleet sync` later does the same job.

### In direct mode, every exit holds every client credential

There is no entry node to keep clients behind, so each ephemeral foreign box gets the
full list of UUIDs and shortIds. They are bearer tokens for your own proxy and nothing
else, but a seized exit yields working credentials for every device. This is the real
cost of dropping the cascade, and it is why `revoke` matters more here than it would
in cascade mode.

### Rotation does not revoke anything

Exits rotate at `max_age_hours` (72 by default). Client credentials live in state and
are re-pushed to every new exit, so a UUID taken from a retired exit still works on all
future ones. Verified against a live fleet: one client UUID outlived several rotations.

- **Rotate exits** to change your *addresses*.
- **Revoke clients** to change your *credentials*.

They are separate operations and neither implies the other.

### Giving a device a fresh UUID

There is no re-key command. `fleet client revoke <name>` **deletes** the client and
re-syncs the pool without it; it does not issue a new secret. To rotate one device's
credential:

```bash
./bin/fleet client revoke phone
./bin/fleet client add phone --sync
./bin/fleet client uri phone --qr        # re-issue: the old link is dead
```

Revoking the last client leaves the pool with no one to sync for, and fleet says so
rather than pushing a config that locks everyone out.

---

## 2. The three export formats

| format | command | for |
|---|---|---|
| share link | `fleet client uri <name>` | one device, one-off; every mobile app takes it |
| subscription | `fleet subscription <name>` | devices that should follow rotation by themselves |
| Xray JSON | `fleet client config <name>` | a desktop/OpenWrt local proxy, and Amnezia |

They all carry the same credential. Choose by how the device consumes it, not by
security — a subscription is not more or less secret than a link.

### Share link

```bash
./bin/fleet client uri phone          # prints one vless:// per serving exit
./bin/fleet client uri phone --qr     # same, each as a scannable QR
```

Import **all** of them. Each is one exit; a client holding only one loses service the
moment that exit rotates or fails, whereas a client holding both fails over.

`--qr` shells out to `qrencode`. Without it the flag degrades to printing the URI and
says so. Install it with `apt install qrencode` or `brew install qrencode`.

### Subscription

A base64 blob containing every current link, written to `state/sub-<name>.txt` at
mode 0600:

```bash
./bin/fleet subscription phone           # → state/sub-phone.txt
./bin/fleet subscription phone -o -      # → stdout, for piping
```

`-o -` exists so credentials can go straight into a pipe rather than resting on disk:

```bash
./bin/fleet subscription phone -o - | qrencode -t ANSIUTF8
```

A subscription is only useful if the client can *fetch* it. Reading a local file is
not a subscription — see §4.

### Xray JSON

```bash
./bin/fleet client config laptop -o laptop.json
```

A complete Xray client config: a SOCKS inbound on 10808, an HTTP inbound on 10809, one
`vless` outbound per serving exit, plus `direct` and `block`, and routing rules that
send RU destinations straight out. This is a local proxy, not a system VPN — point
applications at 127.0.0.1:10808.

`--no-local-split` removes those routing rules. In direct mode they *are* the
geography split and the only one there is, so turning them off sends Russian traffic
out of a foreign datacenter: your bank sees a German IP instead of a residential
Russian one. Leave it on unless you know why you want it off.

---

## 3. Mobile

Everything here speaks VLESS + REALITY with `flow=xtls-rprx-vision` over TCP. Any
client that supports that combination will work; support for REALITY *without* Vision
is not enough.

**Take a share link or a QR** (`fleet client uri <name> --qr`) and import it. Clients
known to handle this combination include v2rayTun, Streisand and Shadowrocket on iOS,
v2rayNG and NekoBox on Android, and Hiddify on both.

### AmneziaVPN specifically

Amnezia can import this, but not as a link. Per its own documentation the client
accepts `.vpn`, `.ovpn`, `.conf` and `.json` (X-Ray Reality), and states: *"You can
also import configurations from XRay VMESS and VLESS, but it is not possible to create
a VPN with VMESS and VLESS protocol support using AmneziaVPN."* That restriction is
about **deploying a server** from the app — importing a client config for a server you
already run is the supported path. `vless://` links are not mentioned anywhere in the
supported formats.

So for Amnezia, use the JSON:

```bash
./bin/fleet client config phone -o phone.json
```

Two cautions before choosing Amnezia over a plain Xray client:

- Amnezia's XRay support has an open defect ([amnezia-client#2958]) in which touching
  the XRay settings UI regenerates the config, flipping `security` between `"reality"`
  and `""` and rotating keys, while the UI continues to display Reality as active. The
  result is `flow: xtls-rprx-vision` with no REALITY — a combination that fails
  silently. Reported against 4.8.15.4 and 5.0.0.5. Import the config and do not open
  the protocol settings.
- Amnezia's strength is AmneziaWG, whose obfuscation is a different tool for a
  different job. If you want AmneziaWG rather than VLESS, use `fleet awg`, which
  configures it on the entry node; that path does not involve these exports at all.

[amnezia-client#2958]: https://github.com/amnezia-vpn/amnezia-client/issues/2958

---

## 4. On the go: the part that is unsolved

A phone off the LAN has the same problem the router has, without the router's fix.

Exits rotate at `max_age_hours`, so **any static export expires within that window** —
72 hours by default, and less if you export mid-cycle. A QR scanned today may be dead
on Thursday. Nothing about the export is wrong when this happens; the address it names
no longer exists.

The router avoids this because fleet pushes to it over SSH from inside the LAN. A phone
on a mobile network cannot be pushed to, so it has to pull, and there is currently
nowhere to pull from. Three honest options:

**a. Re-issue by hand.** Run `fleet client uri phone --qr` after each rotation and
re-scan. No new attack surface, no hosting, but a chore on a fixed schedule.

**b. Host the subscription.** Put `state/sub-<name>.txt` somewhere fetchable over
HTTPS and point the client's subscription setting at the URL; it then re-reads on its
own schedule and rotation becomes invisible. The cost is real: that URL hands working
credentials to anyone who has it, so it needs an unguessable path and HTTPS, and it
creates an internet-reachable artifact that this design otherwise does not have. Do
not host it on the orchestrator — the LAN panel must not be exposed.

**c. Slow the rotation for mobile.** `max_age_hours = 336` on a monthly-billed
provider turns the re-issue chore from twice-weekly into monthly. The inventory already
notes this trade: fast rotation and cheap hourly providers are one axis, slow rotation
and less-blocked ASNs are another. Picking slow rotation weakens the address-churn
property that rotation exists for.

None of these is free. (a) is the default because it is the only one that adds no
attack surface.

---

## 5. Import: what to expect

The LAN panel takes a config *from elsewhere* — another provider, a friend's server —
and pushes it to the router. It does not create clients and it does not touch the
fleet.

Upload a `fleet subscription` blob or a plain list of `vless://` lines. The panel
parses it, shows what it found, and waits:

- Hosts, ports, SNI and flow per entry, with the UUID **truncated to 8 characters** —
  enough to tell two configs apart, not enough to be a credential in a screenshot.
- A confirm token that works exactly once. Reloading or replaying the confirmation
  fails with "expired or was already used" rather than pushing twice.
- A warning if the orchestrator has serving exits of its own, because it does: the
  next rotation or `fleet cron` pass will push state's exits over the top of your
  import. An import is a temporary override, not a new configuration.

XHTTP configs are rejected with an explanation — podkop's sing-box cannot speak that
transport, and accepting them would produce a router that looks configured and carries
no traffic.

The same path exists on the command line:

```bash
./bin/fleet router push --from-file sub.txt --dry-run   # parse and show, touch nothing
./bin/fleet router push --from-file sub.txt             # actually push
```

`--dry-run` is honoured on both forms of `router push`, with and without
`--from-file`.
