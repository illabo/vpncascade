# The always-on box at home

This is where `fleet` runs. Not a Russian VPS — see `docs/06-choosing-providers.md`
for why: the orchestrator holds every provider API token, the SSH key for every exit,
and `state/fleet.json` (every REALITY private key and client UUID), and a box under
your own roof is both a better vantage point and a far worse target.

A Pi, a NAS, an old mini-PC. Requirements: Python 3.11+, `ssh`, `curl`. Nothing else.

## Picking the machine

```bash
./audit-candidate.sh        # run on each candidate; nothing is installed or changed
```

It reports CPU, hardware-AES presence, RAM, wired NIC and speed, storage type, and
benchmarks AES-GCM and ChaCha20 — then gives a verdict for each of the two roles. The
benchmark, not the CPU flag, drives the verdict: flag detection has false negatives
(Apple Silicon reports crypto support somewhere else entirely) and the measurement
never lies.

**Missing AES-NI is less damning than it sounds, for this workload specifically.**
Xray is written in Go, and since Go 1.16 `crypto/tls` reorders its cipher preference
to put **ChaCha20-Poly1305 ahead of AES-GCM whenever it detects no AES hardware** —
precisely because AES-GCM is hard to do quickly and safely in software. ChaCha20 was
designed to be fast without special instructions. So a CPU with no AES-NI does not
fall off a cliff; it just runs ChaCha20 instead, and the number that predicts tunnel
throughput is **the faster of the two benchmarks**, which is what the verdict now
uses. An earlier version of this file judged on the AES figure alone and under-rated
every AES-NI-less CPU.

**The question that decides everything: does this box also terminate the tunnel, or is
it only the orchestrator and resolver?** If the tunnel stays on the Beryl, the workload
here is a cron job making a few HTTPS calls every 15 minutes plus a DNS cache. Almost
anything does. Optimise for idle power, silence and no moving parts, because the box
you resent is the box you switch off.

Actual hardware to hand, assessed:

| Box | Crypto | Wired NIC | Role |
|---|---|---|---|
| **Raspberry Pi 3 B v1.2** | no ARMv8 crypto (BCM2837) → ChaCha20 in software | **100 Mbit, on the USB 2.0 bus** | **Orchestrator + split DNS.** The NIC is irrelevant to cron and DNS and disqualifying for anything else |
| **Q1800G2-P v2.0** (Celeron J1800) | **no AES-NI** (Bay Trail) → ChaCha20 in software, but on a 2.4 GHz x86 core | **2× gigabit** | Shelf spare / fallback tunnel box if the Beryl disappoints |

| Candidate | Hardware AES | Wired NIC | Idle | Verdict |
|---|---|---|---|---|
| **Raspberry Pi 3** | **no** — BCM2837 omits ARMv8 crypto, as do all Pis before the Pi 5 | 100 Mbit, shares the USB bus | ~3 W | **Best, for orchestrator + DNS.** Silent, no fan, no disk to fail. Software AES caps it around tens of Mbit/s, so never the tunnel |
| Raspberry Pi 2 | no | 100 Mbit | ~2 W | Same, but 32-bit ARMv7 and slower. Works; the Pi 3 is strictly better |
| **Laptop, 2010-11 (Vaio / MBA)** | **depends** — i5/i7 yes, Core 2 Duo / i3 / Pentium no | Vaio: gigabit onboard. MBA 2011: **none** | 15-25 W | Vaio with an i5/i7 and an SSD is the pick if you want the option of moving the tunnel here. Battery = built-in UPS |
| MacBook Air 2011 | yes (Sandy Bridge i5/i7) | none onboard, and only a **USB 2.0** bus — a dongle caps ~200-300 Mbit | ~10 W | Right CPU, genuinely limited I/O |
| **12" MacBook, Core M (2015)** | yes (Broadwell-Y) | none onboard, but **USB-C at 5 Gbps** — a gigabit dongle runs at line rate | ~5 W | Viable. The limits are the single port, sustained throttling, and a 10-year-old battery — see below |
| Celeron board, ~2012 | almost certainly **no** — Intel reserved AES-NI for i5/i7 then | real NIC | 20-40 W | Fine as orchestrator + DNS. An oversized ATX PSU at 20 W load is very inefficient |

### On USB ethernet dongles

Not disqualifying by itself — it depends entirely on the **host bus**, which is why
`audit-candidate.sh` now walks sysfs to find it and prints it:

- **USB 2.0 (480 Mbit/s bus)** — a "gigabit" adapter delivers ~200-300 Mbit/s. This is
  the MacBook Air 2011 and most 2010-era laptops.
- **USB 3.x (5000 Mbit/s bus)** — the same adapter does ~940 Mbit/s. Not a bottleneck.
  This is the 12" MacBook's USB-C port.

Either way the dongle is one more thing to fail in a box meant to run unattended for
years, so buy a decent one rather than the cheapest.

### If you use the 12" MacBook (Core M, 2015)

It works, with three caveats in descending order of how much they should worry you:

1. **Check the battery before anything else.** That model glued in terraced battery
   cells, and ten-year-old packs in it are known to swell — which deforms the case and
   is a genuine fire risk, not a theoretical one. A machine running plugged in 24/7 is
   the worst case for a tired battery. If it is swollen, do not use it until the pack
   is replaced or removed.
2. **One port, shared with charging.** You need a USB-C hub with Power Delivery
   passthrough *and* gigabit ethernet, and that hub becomes the single point of
   failure. Cheap ones drop the ethernet link on power events.
3. **It throttles under sustained load.** 4.5 W fanless Broadwell-Y matches a
   Core i5-4200U in short bursts and degrades under continuous full load. Irrelevant
   for cron and DNS, which are idle workloads. It does mean this is not the box to
   carry the household's tunnelled traffic, despite having AES-NI.

Linux on a MacBook8,1 needs wired ethernet to install — the Broadcom BCM4350 WiFi is
the classic pain point — which the dongle gives you anyway. Set
`HandleLidSwitch=ignore` in `logind.conf` for lid-closed operation.

### If you are *buying* rather than reviving

The calculus flips. Reviving something you own is free, so "good enough" wins. Spending
money means you should buy the right thing, and the right thing is not old Apple
hardware.

**Mac mini A1283 (Early/Late 2009) — don't buy one for this.** It would *work*: gigabit
ethernet onboard, quiet, ~13-14 W idle, 2.5" SATA you can put an SSD in, and Linux
installs on it without drama. But it is a Core 2 Duo, and **AES-NI did not exist until
Westmere in 2010** — so it permanently closes the door on moving the tunnel here, which
is the only reason to prefer an x86 box over a Pi in the first place. Add 17-year-old
electrolytics, fan bearings and a PSU brick, DDR3 capped at 8 GB, SATA II, and a case
you open with a putty knife.

**Mac mini A1347 (2010-2014) — depends entirely on the year, and they look identical.**
Sixteen SKUs share that case number across four generations. The **EMC number on the
underside** identifies the generation from a photo, so ask the seller for one:

| EMC | Year | Model ID | CPU | AES-NI | RAM | Bays |
|---|---|---|---|---|---|---|
| 2364 | Mid 2010 | Macmini4,1 | Core 2 Duo | **no** | socketed | 1 (Server: 2) |
| 2442 | Mid 2011 | Macmini5,1/5,2/**5,3** | Sandy Bridge i5/i7 dual; **5,3 Server = quad i7** | yes | socketed | 1 (Server: 2) |
| **2570** | **Late 2012** | Macmini6,1 / **6,2** | Ivy Bridge i5 dual; **6,2 = quad i7-3615QM/3720QM** | yes | socketed, 16 GB | 1 (Server: 2) |
| 2840 | Late 2014 | Macmini7,1 | Haswell, **dual-core only** | yes | **soldered** | 1 |

- **EMC 2364 — avoid.** Core 2 Duo, same no-AES-NI problem as the A1283.
- **EMC 2570 (Late 2012) is the one to want**, specifically a Macmini6,2: quad-core
  Ivy Bridge i7 with AES-NI, socketed DDR3 to 16 GB, all-Intel graphics (no AMD dGPU
  to fight on Linux, unlike some 2011 configs), gigabit onboard, 4× USB 3.0,
  Thunderbolt, internal PSU. The Server variants have **two** 2.5" bays.
- **EMC 2840 — skip despite being newest.** Dual-core only, RAM soldered, one bay.

Caveats even on a good 2570: it is still 14 years old (fan, thermal paste, and replace
any spinning disk with an SSD), a quad in that chassis runs warm under sustained load,
and it idles ~11-15 W against a Pi's 3 W — roughly 100 kWh a year more.

**Buy a used office micro-PC instead.** Lenovo ThinkCentre M-series Tiny (M700 / M710q /
M720q), HP EliteDesk 800 G2-G4 Mini, Dell OptiPlex Micro 3050 / 5050 / 7050. An
i5-6500T or i5-7500T class part gives you:

- **AES-NI** — several GB/s, so the tunnel option stays open
- gigabit onboard, sometimes two
- M.2 NVMe *and* a 2.5" SATA bay
- 8-32 GB DDR4
- ~8-15 W idle, and they were designed to run in offices 24/7
- standard, plentiful, boring parts

They are the default homelab recommendation for exactly these reasons, and they are
cheap used because corporates retire them in fleets. Against a Late 2012 mini they are
four to five years newer, DDR4 not DDR3, NVMe not SATA-only, and usually cheaper —
Macs hold their used value, which works against you here. For a *new* purchase, a Pi 5 is
the one Pi with ARMv8 crypto (BCM2712) and would also be a reasonable answer.

Three things worth knowing before you spend a weekend on this:

- **Reliability matters less than it feels.** `exits.expiry_buffer_hours` (default 120)
  is exactly how long the fleet survives with this box switched off. Days of downtime
  cost nothing.
- **A 15-year-old spinning disk is the likeliest thing to fail.** Fit an SSD, or use a
  Pi. Old laptop batteries also swell — remove or replace rather than trusting one.
- **SD cards wear out** under logging. On a Pi, put the journal in RAM
  (`Storage=volatile` in `journald.conf`) and consider `log2ram`.

If a Pi 3 turns up, stop looking — it fits this job exactly. Reach for the Vaio only
if the audit shows an i5/i7 *and* you want the option of moving the tunnel off the
Beryl later.

## Powering it — there is no PoE path worth taking

| Device | Power | PoE in | PoE out |
|---|---|---|---|
| **Pi 3 Model B v1.2** | **micro-USB**, 5.1 V 2.5 A | no — the 4-pin PoE header arrived on the **B+**, so the official PoE HAT does not fit this board | no |
| **Beryl AX (GL-MT3000)** | USB-C, 5 V 2 A | no | no |
| **hAP ac (RB962)** | 11–57 V passive | **yes** | **yes, port 5** (passive, passes the input voltage) |
| **hAP ac² (RBD52G)** | 18–28 V passive | yes | **no** |

So a PoE path does technically exist: the older hAP ac can feed passive PoE out of
port 5 into a 24 V → 5 V micro-USB splitter. **Don't.** Two reasons:

1. Port 5's output is current-limited, and a splitter delivering 5 V at 2.5 A sits
   right at the edge of that budget.
2. The Pi 3 B is unusually intolerant of undervoltage, and a sagging splitter
   corrupts SD cards — the exact failure this whole section exists to avoid. Saving a
   power brick is not worth introducing the fault you are trying to design out.

Put the Pi where there is a mains socket. If a single cable genuinely matters later, a
Pi 3 **B+** or Pi 4 has the PoE header and does it properly.

Also worth stating because it catches people: the Pi 3 is **micro-USB**, the Beryl is
**USB-C**. Different cables, and they are not interchangeable.

## Getting the writes off the SD card

The failure that bit us: a 32 GB card wrote fine, booted twice, then latched itself
permanently read-only mid-session. That is SD end-of-life, and it is *the* failure
mode of an always-on Pi. Two ways to stop caring about it.

### Boot partition on SD, root filesystem on USB — no OTP, works today

The bootloader only needs the FAT partition. Point `root=` at a USB disk and every
write — journal, apt, `state/fleet.json`, the blocklist cache — lands there instead.
The SD becomes ~537 MB of read-mostly boot files, which even a cheap card survives
for years.

1. Write the image to the SD as usual, and separately to the USB disk.
2. Find the USB root partition's PARTUUID (`lsblk -o NAME,PARTUUID`).
3. On the **SD's** `cmdline.txt`, change `root=PARTUUID=…` to the USB one and append
   `rootdelay=5` so the kernel waits for USB enumeration.
4. On the **USB** root filesystem, edit `/etc/fstab` so `/boot/firmware` still points
   at the SD partition.

PARTUUID rather than `/dev/sda2`: device names move around depending on what else is
plugged in, and a wrong one is an unbootable board.

### Full USB boot, no SD at all — Pi 3 B needs a one-time SD boot first

The Pi 3 **B** ships with USB mass-storage boot disabled. Enabling it means adding
`program_usb_boot_mode=1` to `config.txt` and booting **from an SD card once** to burn
an OTP bit. That bit is permanent and cannot be undone — after which the SD is no
longer needed at all.

So it does not rescue you from a dead card: you need one working SD to get there. (The
Pi 3 **B+** and Pi 4 have the bit set at the factory and boot from USB out of the box.)

### Which USB device

| | Verdict |
|---|---|
| **USB SSD in an enclosure** | Best. Real wear levelling, and the workload is trivial |
| **USB stick** | Better than SD, but it is still cheap flash and can die the same way |
| **2.5" spinning HDD** | Excellent write endurance, but the Pi 3's USB ports share a ~1.2 A budget and a bus-powered drive can brown the board out — which is the undervoltage fault we are trying to design out. Use a self-powered enclosure or a powered hub |

The Pi 3's USB is 2.0 and shares its bus with the 100 Mbit ethernet, so throughput is
poor — and completely irrelevant to a cron job and a DNS cache.

## What it is NOT

It is **not** in the data path. The tunnel terminates on the Beryl; this box does slow,
out-of-band work only. That separation is deliberate — see
`docs/02-architecture.md`. It is why this machine can be switched off for days without
anyone noticing, and why "anything more powerful than a Pi 3 is overkill" is the right
instinct.

## What it does

1. **Runs the fleet** — `cron/fleet-cron.sh` on a timer. Being always-on means
   `exits.expiry_buffer_hours` stops being load-bearing.
2. **Measures reachability from the right place** — it sits on the same ISP as your
   clients, so `fleet health` and the provisioning probe test the path that matters.
   A Russian datacenter would be a different network with different TSPU behaviour.
3. **Serves DNS** — split, so Russian names resolve domestically and foreign names are
   not resolved on Russian soil at all. See `docs/07-dns.md`.
4. **Optionally serves the subscription** on the LAN, where no URL leaks.

## DNS setup

```bash
fleet dns                      # writes orchestrator/dns/
```

```
dns/dnsmasq-split.conf  ->  /etc/dnsmasq.d/99-fleet.conf   (Pi-hole: same path)
dns/stubby.yml          ->  /etc/stubby/stubby.yml
```

```bash
apt install stubby dnsmasq     # or add stubby alongside Pi-hole
systemctl enable --now stubby
systemctl restart dnsmasq      # or: pihole restartdns
```

Both generated files are validated against their real parsers before release
(`dnsmasq --test`, `stubby -i -C`), and regenerated from the same `direct_domains`
list the routing uses, so the two can't drift apart.

Then point the router's DHCP at this box and verify:

```bash
tests/t11_dns_integrity.sh 127.0.0.1:10808
```

That test exists because DNS interception is **silent** — you query Cloudflare, the
state answers, and nothing in any client tells you. It is the one failure in this
system you cannot notice by using the internet normally.
