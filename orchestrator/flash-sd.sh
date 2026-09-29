#!/usr/bin/env bash
# Write a headless Raspberry Pi OS card for a fleet orchestrator.
#
#   ./orchestrator/flash-sd.sh --disk /dev/disk6 --hostname orchestrator \
#       --user pi --wifi-ssid <your-ssid> --wifi-pass '<your-wifi-password>'
#
# Downloads and checksums the current Raspberry Pi OS Lite arm64, writes it, then
# drops a `custom.toml` on the boot partition so the Pi comes up headless with SSH
# keys, a hostname and WiFi already set. Ethernet needs no configuration and takes
# priority whenever a cable is present.
#
# DESTRUCTIVE. It shows you the target and makes you type the disk identifier back.
set -euo pipefail

DISK=""; HOSTNAME_="orchestrator"; USER_="$(id -un)"
# Default the Pi's clock to whatever this Mac is set to, so a local run needs no
# flag and the published source carries no location. Override with --timezone.
TZ_="$(readlink /etc/localtime 2>/dev/null | sed -n 's|.*/zoneinfo/||p')"
[ -n "$TZ_" ] || TZ_="Etc/UTC"
SSID=""; WPASS=""; COUNTRY="RU"; KEYS=(); YES=0
CACHE="${TMPDIR:-/tmp}/fleet-rpios"

while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --user) USER_="$2"; shift 2 ;;
    --wifi-ssid) SSID="$2"; shift 2 ;;
    --wifi-pass) WPASS="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --timezone) TZ_="$2"; shift 2 ;;
    --key) KEYS+=("$2"); shift 2 ;;
    --yes) YES=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$DISK" ] || { echo "need --disk (see: diskutil list external physical)" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || { echo "this script is macOS-only (diskutil/rdisk)" >&2; exit 1; }

if [ "${#KEYS[@]}" -eq 0 ]; then
  for k in ~/.ssh/id_ed25519_vpncascade.pub ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub; do
    [ -f "$k" ] && KEYS+=("$k")
  done
fi
[ "${#KEYS[@]}" -gt 0 ] || { echo "no SSH public key found; pass --key" >&2; exit 1; }

echo "── image ──"
mkdir -p "$CACHE"
BASE=https://downloads.raspberrypi.com/raspios_lite_arm64/images
DIR=$(curl -fsSL "$BASE/" | grep -oE 'raspios_lite_arm64-[0-9-]+/' | sort -u | tail -1)
IMG=$(curl -fsSL "$BASE/$DIR" | grep -oE '[0-9-]+-raspios-[a-z]+-arm64-lite\.img\.xz' | sort -u | tail -1)
echo "  ${IMG}"
[ -f "$CACHE/$IMG" ] || curl -fL --retry 5 -o "$CACHE/$IMG" "$BASE/$DIR$IMG"
# Cache the checksum too, and tolerate a failed refresh when we already hold a copy.
# Re-fetching it every run means one transient "Connection reset by peer" aborts a
# flash that had everything it needed on disk.
if [ ! -s "$CACHE/$IMG.sha256" ]; then
  curl -fsSL --retry 3 -o "$CACHE/$IMG.sha256" "$BASE/$DIR$IMG.sha256" \
    || { echo "✗ could not fetch the checksum, and none is cached" >&2; exit 1; }
else
  curl -fsSL --retry 2 -o "$CACHE/$IMG.sha256.new" "$BASE/$DIR$IMG.sha256" 2>/dev/null \
    && mv -f "$CACHE/$IMG.sha256.new" "$CACHE/$IMG.sha256" \
    || { rm -f "$CACHE/$IMG.sha256.new"; echo "  (using the cached checksum; refresh failed)"; }
fi
WANT=$(awk '{print $1}' "$CACHE/$IMG.sha256"); GOT=$(shasum -a 256 "$CACHE/$IMG" | awk '{print $1}')
[ "$WANT" = "$GOT" ] || { echo "✗ checksum mismatch — refusing to flash" >&2; exit 1; }
echo "  ✓ checksum verified"

echo; echo "── target ──"
# Card readers can take several seconds to enumerate after insertion, and failing
# instantly on a disk that is about to appear just makes you run the whole thing
# again. Wait a little before giving up.
for _ in $(seq 1 12); do
  diskutil list "$DISK" >/dev/null 2>&1 && break
  sleep 2
done
if ! diskutil list "$DISK" >/dev/null 2>&1; then
  echo "✗ $DISK did not appear after 24s." >&2
  echo "  External disks currently visible:" >&2
  diskutil list external physical 2>/dev/null | sed 's/^/    /' >&2 || echo "    (none)" >&2
  exit 1
fi
diskutil list "$DISK" | sed 's/^/  /'
if diskutil info "$DISK" 2>/dev/null | grep -q "Internal:.*Yes"; then
  echo "✗ $DISK is an INTERNAL disk. Refusing." >&2; exit 1
fi
RO=$(diskutil info "$DISK" 2>/dev/null | awk -F': *' '/Media Read-Only/{print $2; exit}')
if [ "$RO" = "Yes" ]; then
  echo "✗ $DISK reports Media Read-Only: Yes — the card or reader is write-protected." >&2
  echo "  Nothing was written. Re-seat it, or the card may have latched read-only." >&2
  exit 1
fi
if [ "$YES" != 1 ]; then
  echo
  printf 'This ERASES %s. Type the identifier (%s) to confirm: ' "$DISK" "$(basename "$DISK")"
  read -r reply
  [ "$reply" = "$(basename "$DISK")" ] || { echo "aborted"; exit 1; }
fi

echo; echo "── writing (a few minutes, Ctrl-T for progress) ──"
diskutil unmountDisk "$DISK"
RAW="/dev/r$(basename "$DISK")"
xz -dc "$CACHE/$IMG" | sudo dd of="$RAW" bs=4m
sync

echo; echo "── configuring ──"
BOOT=""
for _ in $(seq 1 30); do
  for c in /Volumes/bootfs /Volumes/boot /Volumes/BOOT; do [ -d "$c" ] && { BOOT="$c"; break 2; }; done
  diskutil mountDisk "$DISK" >/dev/null 2>&1 || true
  sleep 2
done
[ -n "$BOOT" ] || { echo "✗ boot partition never mounted" >&2; exit 1; }
[ -f "$BOOT/config.txt" ] || { echo "✗ $BOOT is not a Pi boot partition" >&2; exit 1; }

# Which provisioning mechanism does this image use? Raspberry Pi OS switched from
# the Bookworm-era custom.toml to cloud-init in Trixie (2026). The boot partition
# tells you: cloud-init images ship user-data / meta-data / network-config.
if [ -f "$BOOT/user-data" ] && [ -f "$BOOT/meta-data" ]; then
  echo "  mechanism: cloud-init (Trixie and later)"
  KEYBLOCK=""
  for k in "${KEYS[@]}"; do KEYBLOCK="${KEYBLOCK}      - \"$(cat "$k")\"
"; done
  {
    echo "#cloud-config"
    echo "# Written by flash-sd.sh. Nothing here needs the internet, so first boot"
    echo "# cannot stall waiting for a package mirror."
    echo "hostname: ${HOSTNAME_}"
    echo "manage_etc_hosts: true"
    echo "ssh_pwauth: false"
    echo "users:"
    echo "  - name: ${USER_}"
    echo "    shell: /bin/bash"
    echo "    lock_passwd: true"
    echo "    sudo: \"ALL=(ALL) NOPASSWD:ALL\""
    # Only groups Debian certainly has. A longer list looks harmless but useradd
    # aborts wholesale on an unknown group, and on Lite gpio/i2c/spi/netdev may not
    # exist — which produces no account at all, silently. That cost a whole card.
    echo "    groups: sudo"
    echo "    ssh_authorized_keys:"
    printf '%s' "$KEYBLOCK"
    echo "timezone: ${TZ_}"
    echo ""
    # Belt and braces: create the account from plain shell too. The declarative
    # block above has failed in the field; these two paths share no failure mode.
    echo "runcmd:"
    echo "  - [ sh, -c, \"id ${USER_} >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo ${USER_}\" ]"
    echo "  - [ sh, -c, \"install -d -m 0700 -o ${USER_} -g ${USER_} /home/${USER_}/.ssh\" ]"
    echo "  - [ sh, -c, \"install -m 0600 -o ${USER_} -g ${USER_} /boot/firmware/authorized_keys /home/${USER_}/.ssh/authorized_keys\" ]"
    echo "  - [ sh, -c, \"printf '${USER_} ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/010_${USER_} && chmod 0440 /etc/sudoers.d/010_${USER_}\" ]"
    echo "  - [ sh, -c, \"passwd -l ${USER_} || true\" ]"
    # macOS cannot read ext4, so leave the evidence somewhere FAT can hold it.
    echo "  - [ sh, -c, \"id ${USER_} > /boot/firmware/ci-result.txt 2>&1; cloud-init status --long >> /boot/firmware/ci-result.txt 2>&1; sync\" ]"
  } > "$BOOT/user-data"

  # The runcmd path reads the keys from here.
  : > "$BOOT/authorized_keys"
  for k in "${KEYS[@]}"; do cat "$k" >> "$BOOT/authorized_keys"; done

  {
    echo "network:"; echo "  version: 2"
    echo "  ethernets:"; echo "    eth0:"; echo "      dhcp4: true"; echo "      optional: true"
    if [ -n "$SSID" ]; then
      echo "  wifis:"; echo "    wlan0:"; echo "      dhcp4: true"; echo "      optional: true"
      echo "      regulatory-domain: ${COUNTRY}"
      echo "      access-points:"; echo "        \"${SSID}\":"; echo "          password: \"${WPASS}\""
    fi
  } > "$BOOT/network-config"

  { echo "dsmode: local"
    echo "instance_id: fleet-${HOSTNAME_}-$(date +%Y%m%d%H%M%S)"; } > "$BOOT/meta-data"
  rm -f "$BOOT/custom.toml"
else
  echo "  mechanism: custom.toml (Bookworm era)"
  LOCKED=$(openssl passwd -6 "$(openssl rand -base64 32)")
  {
    echo "config_version = 1"; echo
    echo "[system]"; echo "hostname = \"${HOSTNAME_}\""; echo
    echo "[user]"; echo "name = \"${USER_}\""
    echo "password = \"${LOCKED}\""; echo "password_encrypted = true"; echo
    echo "[ssh]"; echo "enabled = true"; echo "password_authentication = false"
    echo "authorized_keys = ["
    for k in "${KEYS[@]}"; do echo "  \"$(cat "$k")\","; done
    echo "]"
    if [ -n "$SSID" ]; then
      echo; echo "[wlan]"; echo "ssid = \"${SSID}\""; echo "password = \"${WPASS}\""
      echo "password_encrypted = false"; echo "hidden = false"; echo "country = \"${COUNTRY}\""
    fi
    echo; echo "[locale]"; echo "keymap = \"us\""; echo "timezone = \"${TZ_}\""
  } > "$BOOT/custom.toml"
fi
# Either way: if provisioning is ever ignored, this still gets you sshd rather than
# an unreachable board. It is what saved the first attempt.
touch "$BOOT/ssh"
sync
echo "  ✓ provisioning written"

diskutil eject "$DISK"
echo "✓ card ready — ethernet preferred, ${SSID:-no WiFi} as fallback"
