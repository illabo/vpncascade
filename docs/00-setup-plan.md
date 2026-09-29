# The plan

Every step runs from your Mac. Nothing needs a monitor or keyboard attached to
anything. Times are roughly how long each step takes, not how long it takes to read.

**The shape you are building:**

```
ISP ──▶ GL.iNet Beryl AX   router + tunnel. Radios off if APs cover the space.
           │
           ├──▶ access point(s)   optional: any dumb AP, only if the router's
           │                      own radios do not cover the space
           └──▶ Raspberry Pi 3    fleet on a timer + split DNS.
                                  NOT in the data path.

                 rotating exits abroad ◀── created and destroyed by fleet
```

**This was built and measured on a Beryl AX (GL-MT3000) and a Pi 3.** Both are
swappable, but not for anything — read the constraints before substituting.

**The router is the fussy one, and the constraint is the firmware track, not the
brand.** podkop needs **OpenWrt ≥ 24.10**, and GL.iNet ships two tracks: builds with
`-op` in the filename are native OpenWrt and work, while the MTK SDK builds carry an
older kernel and userland where **podkop will not install at all** — no VLESS, on a
router that otherwise looks identical and has a *higher* version number. Several
GL.iNet models have no `-op` build. Beyond that the role needs enough flash and RAM
for podkop plus sing-box (the Beryl's 256 MB NAND / 512 MB RAM is comfortable;
16 MB-flash devices are not), and a CPU that can do userspace TLS at your line rate.
`docs/03-router-glinet-openwrt.md` has the full matrix, including which hardware
fails outright.

**The always-on box is genuinely flexible.** `fleet` is stdlib-only Python 3.11+
plus `ssh` and `curl`, deliberately, so a Pi, a NAS or an old mini-PC all qualify. A
Pi 3 is the tested recommendation rather than a requirement.
`orchestrator/audit-candidate.sh` scores a candidate on the three things that
actually decide it: hardware AES, a real wired NIC, and idle power.

Access points are a property of your walls, not of this design.

---

## 0 · Before you start  · 15 min

- [ ] A provider account and API token. SporeStack needs no account at all:
      `curl -s https://api.sporestack.com/token`, then fund it with XMR/BTC.
- [ ] `git`, `python3` (3.11+) and `ssh` on the Mac. `brew install qrencode` if you
      want QR codes for phones.
- [ ] An SSH keypair you will use everywhere: `ssh-keygen -t ed25519`.

## 1 · Put an OS on the Pi  · 10 min

```bash
diskutil list external physical          # find the card — check the size!
./orchestrator/flash-sd.sh --disk /dev/diskN \
    --hostname orchestrator --user <you> \
    --wifi-ssid '<your-router-ssid>' --wifi-pass '<your-wifi-password>'
```

It fetches the current Raspberry Pi OS Lite arm64, verifies the published SHA256,
refuses to touch an internal disk, makes you type the disk identifier back, writes
the image, and drops a `custom.toml` on the boot partition so the Pi comes up
headless with your SSH keys, a hostname and WiFi already set.

- **Ethernet needs no configuration** and takes priority whenever a cable is present.
  WiFi is only the fallback — prefer the cable if you have one, because a headless Pi
  that fails to associate gives you no way to find out why.
- SSH is **key-only**; the account has a hash of a random string as its password, so
  nobody can log in at a console either.
- A legacy empty `ssh` file goes on too, so even if `custom.toml` is ever rejected you
  still get sshd rather than an unreachable board.

Put the card in, connect **ethernet** and a real **5.1 V 2.5 A micro-USB** supply.
Note **micro-USB**, not USB-C — the Pi 3 B predates USB-C, and the Beryl's USB-C
brick will not fit it. The 5.1 V is deliberate: it compensates for cable drop, and a
phone charger that sags is the classic cause of "my Pi died after six months",
because undervoltage corrupts SD cards.

**No PoE on any of this kit** — see `orchestrator/README.md` for why a splitter is a
false economy here. Put the Pi where there is a socket. Then:

```bash
ssh <you>@orchestrator.local
```

*Using the Q1800G2-P instead?* Debian 13 netinst from a USB stick, select only
"SSH server" and "standard system utilities". Everything after this point is identical.

## 2 · Configure the fleet  · 15 min

On the Mac, in this repo:

```bash
cp .env.example .env && chmod 600 .env     # put your provider token in it
cp inventory.example.toml inventory.toml
$EDITOR inventory.toml
```

Minimum edits: pick one `[[exits.providers]]` you have credentials for. Leave
`mode = "direct"` and `transport = "tcp"` alone — both are already what you want.

```bash
./bin/fleet client add beryl
./tests/t00_local_cascade.sh     # 12/12, and needs no servers at all
```

## 3 · Bootstrap the Pi  · 10 min

```bash
./orchestrator/bootstrap-orchestrator.sh <user>@orchestrator.local
```

That installs fleet to `/opt/vpncascade`, moves the journal to RAM and disables swap
(SD longevity), installs **stubby + dnsmasq** for split DNS, enables a systemd timer
that runs `fleet cron` every 15 minutes, and starts the **LAN control panel** on
port 8088 — printing its generated password, which is what you text to whoever is
standing next to the always-on box at a remote site.

## 4 · Create the exits  · 10 min

```bash
ssh <user>@orchestrator.local
cd /opt/vpncascade
./bin/fleet up            # provisions the pool, re-rolling any blocked address
./bin/fleet status        # check the ASN column shows two DIFFERENT AS numbers
./bin/fleet health
```

If both exits land on one ASN, add a second provider and `./bin/fleet rotate --force`.
A sweep is per-AS, so ASN diversity is the whole defence.

## 5 · Flash the Beryl  · 20 min

You need OpenWrt **24.10 or newer** for podkop. Two routes:

| | Risk | Notes |
|---|---|---|
| **GL.iNet 4.9.0-op24** *(recommended first)* | low | GL.iNet's own native-OpenWrt build (kernel 6.6). Flashes through the normal GL.iNet upgrade page, U-Boot recovery stays intact. Labelled beta/testing |
| Vanilla OpenWrt 25.12.5 | **higher** | Cleanest software, but there are community reports of the sysupgrade image refusing to flash via LuCI, the GL.iNet UI *and* U-Boot, and at least one recovery that needed a CH341 hardware programmer |

Start with **op24**. If you have a spare unit, prove vanilla on that later if you want it.

1. Download the `-op24` image from `dl.gl-inet.com/router/mt3000` → **OPENWRT 24** tab.
2. GL.iNet UI → **System → Upgrade** → upload → **do not keep settings**.
3. Wait for reboot, then set a root password and enable SSH.

If it ever refuses to boot: hold **Reset** while powering on, set your Mac to a static
`192.168.1.2/24`, and upload a factory image at `http://192.168.1.1`.

## 6 · Provision the Beryl  · 10 min

```bash
DISABLE_WIFI=1 ./router/beryl-setup.sh root@192.168.8.1 beryl
```

It checks the OpenWrt version and free space, refuses if your URIs are XHTTP (sing-box
cannot speak it), optionally turns the radios off, installs and configures podkop, and
tells you what to verify. Add `DRY_RUN=1` first if you want to see it without changes.

## 7 · Move WiFi and DNS  · 20 min

**Any secondary access points, as dumb APs** — skip this entirely if the router's
own radios cover the space. On each AP: bridge all ethernet ports *and* the wlan
interfaces, delete the DHCP **server** and the masquerade rule, and give the bridge
a static IP with the router as gateway. Use the same SSID and password on all of
them, on non-overlapping channels (2.4 GHz: 1 and 11). The steps are the same
whatever the AP runs; only the menu names differ.

**DNS** — point the Beryl's DHCP at the Pi's address, then:

```bash
ssh <user>@orchestrator.local '/opt/vpncascade/tests/t11_dns_integrity.sh'
```

## 8 · Verify  · 15 min

From a LAN client:

```bash
curl -s https://api.ipify.org                   # an EXIT address
curl -s https://yandex.ru/internet/api/v0/ip    # your HOME ISP address
```

Then the two that actually matter:

```bash
./tests/t06_throughput.sh          # what the Beryl really does on your line
./tests/t08_exposure_audit.sh <exit-ip>
```

**And the fail-open check — do not skip it:**

```bash
ssh root@192.168.8.1 '/etc/init.d/podkop stop'
#  from a LAN client: Russian sites must still work
ssh root@192.168.8.1 '/etc/init.d/podkop start'
```

**The mobile test**, which decides whether you ever need the cascade: put a client
config on a phone, leave WiFi, and pull ~50 MB over LTE. If it stalls near 16 KB you
have hit the foreign-TLS freeze and want `mode = "cascade"`. If it completes, direct
mode is all you will ever need.

## 9 · Additional sites  · 45 min each

Independent deployments, not one fleet serving every site. On each site's own box:

```bash
cp inventory.toml site-b.toml     # its own exits, its own state
$EDITOR site-b.toml
./bin/fleet -i site-b.toml client add site-b
./orchestrator/bootstrap-orchestrator.sh <user>@<that-box> site-b.toml
```

State separates automatically (`state/site-b/`). Set `max_age_hours = 336` at remote
sites — somewhere you are not standing does not need 72-hour churn.

**Before you leave, run the fail-open check there.** It is the difference between
fixing it on your next visit and driving across town.
