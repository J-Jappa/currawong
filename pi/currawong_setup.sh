#!/usr/bin/env bash
#
# Currawong setup
# ---------------
# Rebuilds the Pi side of the Currawong player on a fresh Raspberry Pi OS Lite
# (32-bit, Pi Zero W): packages, SPI/I2C/serial, go-librespot, MPD, Bluetooth
# audio, the UI and its services. Safe to run again: it only changes what
# differs, and keeps a .bak copy of any file it replaces.
#
# Before you run it
#   1. Flash Raspberry Pi OS Lite (32-bit) with Raspberry Pi Imager. In the
#      customisation step set the hostname, username, Wi-Fi name, password and
#      Wi-Fi country, and enable SSH.
#   2. Copy this script and the other Currawong files to the Pi, into the same folder:
#        scp -r currawong_setup.sh currawong_ui.py currawong_upload.py currawong_boot.anim currawong_bird.py assets \
#            curra@currawong.local:~/
#   3. Plug the Dawn Pro 2 into the Pi's USB data port (the middle one).
#
# Run it
#   sudo bash currawong_setup.sh             install everything
#   sudo bash currawong_setup.sh --dry-run   show what it would do, change nothing
#   sudo bash currawong_setup.sh --check     report the state of an existing install
#   sudo bash currawong_setup.sh --boot      where the boot time goes (after a fresh reboot)
#   sudo bash currawong_setup.sh --bluetooth check the Bluetooth radio layer by layer, then scan
#   sudo bash currawong_setup.sh --fast-boot switch off boot-time services the player doesn't need
#   sudo bash currawong_setup.sh --power     what's drawing power, and the battery drain right now
#   sudo bash currawong_setup.sh --dac-sleep let the USB DAC sleep while nothing plays (--dac-awake undoes it)
#   sudo bash currawong_setup.sh --cpu [secs]     CPU of MPD, the UI and arecord over secs (default 30),
#                                            and whether MPD's Visualiser output is on
#   sudo bash currawong_setup.sh --update-dial [currawong_knob.bin]
#                                            new firmware for the dial over its normal cable (needs v0.5+)
#
# Settings you can override, for example:  sudo DAC_CARD=PRO2 bash currawong_setup.sh
#   DAC_CARD       ALSA card ID of the DAC (see: cat /proc/asound/cards)   default PRO2
#   DEVICE_NAME    name shown in Spotify                                   default Currawong
#   GLR_VERSION    go-librespot release to install                         default v0.10.3
#   NEW_HOSTNAME   hostname to set (empty string leaves it alone)          default currawong
#   TARGET_USER    user that owns and runs everything                      default the sudo user
#
set -Eeuo pipefail

GLR_VERSION="${GLR_VERSION:-v0.10.3}"
DEVICE_NAME="${DEVICE_NAME:-Currawong}"
DAC_CARD="${DAC_CARD:-PRO2}"
BATTERY_MAH="${BATTERY_MAH:-}"   # empty: the UI's setting (Settings > Advanced > Battery), else 2000
NEW_HOSTNAME="${NEW_HOSTNAME-currawong}"
TARGET_USER="${TARGET_USER:-}"
DRY_OUT="${DRY_OUT:-/tmp/currawong-dry}"

MODE=install
DRY_RUN=0
DIAL_BIN=""
want_bin=0
for arg in "$@"; do
  if [ "$want_bin" = 1 ]; then
    want_bin=0
    case "$arg" in
      --*) ;;
      *) DIAL_BIN="$arg"; continue ;;
    esac
  fi
  case "$arg" in
    --update-dial) MODE="update-dial"; want_bin=1 ;;
    --cpu) MODE="cpu"; want_bin=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --check) MODE=check ;;
    --boot) MODE=boot ;;
    --bluetooth) MODE=bluetooth ;;
    --power) MODE=power ;;
    --fast-boot) MODE="fast-boot" ;;
    --dac-sleep) MODE="dac-sleep" ;;
    --dac-awake) MODE="dac-awake" ;;
    -h | --help)
      sed -n '2,/^set -Eeuo/p' "$0" | sed '$d'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

WARNINGS=()
HOME_DIR=""
TARGET_GROUP=""
GLR_ASSET=""
UI_SRC=""

say() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
good() { printf '    ok: %s\n' "$*"; }
warn() {
  printf '    WARNING: %s\n' "$*" >&2
  WARNINGS+=("$*")
}
die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}
trap 'printf "\nStopped on line %s. Fix the problem above, then run the script again.\n" "$LINENO" >&2' ERR

# run: do it (or just print it in a dry run). try: same, but a failure is only a warning.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}
try() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  "$@" || {
    warn "Command failed, carrying on: $*"
    return 0
  }
}

# put PATH MODE OWNER:GROUP, with the file content on stdin. Backs up what it replaces.
put() {
  local path="$1" mode="${2:-644}" owner="${3:-root:root}" tmp
  if [ "$DRY_RUN" = 1 ]; then
    mkdir -p "$DRY_OUT$(dirname "$path")"
    cat >"$DRY_OUT$path"
    info "[dry-run] would write $path"
    return 0
  fi
  tmp="$(mktemp)"
  cat >"$tmp"
  if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
    rm -f "$tmp"
    info "unchanged: $path"
    return 0
  fi
  if [ -f "$path" ]; then
    cp -a "$path" "$path.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  mkdir -p "$(dirname "$path")"  # e.g. a new drop-in folder (the dry run already did this)
  install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$path"
  rm -f "$tmp"
  info "wrote: $path"
}

# own_dir DIR...: create directories owned by the target user, parents first.
own_dir() {
  local d
  for d in "$@"; do
    if [ "$DRY_RUN" = 1 ]; then
      printf '    [dry-run] mkdir %s\n' "$d"
    else
      install -d -o "$TARGET_USER" -g "$TARGET_GROUP" "$d"
    fi
  done
}

boot_dir() {
  if [ -d /boot/firmware ]; then echo /boot/firmware; else echo /boot; fi
}

detect_user() {
  if [ -z "$TARGET_USER" ]; then TARGET_USER="${SUDO_USER:-}"; fi
  if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
    TARGET_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
  fi
  if [ -z "$TARGET_USER" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      TARGET_USER=curra
    else
      die "Couldn't work out which user to set up. Run it with sudo from your normal login, or set TARGET_USER=name."
    fi
  fi
  HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6 || true)"
  if [ -z "$HOME_DIR" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      HOME_DIR="/home/$TARGET_USER"
    else
      die "User '$TARGET_USER' doesn't exist."
    fi
  fi
  TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"
}

pick_asset() {
  case "$(uname -m)" in
    armv6l | armv7l) GLR_ASSET=armv6_rpi ;;
    aarch64) GLR_ASSET=arm64 ;;
    *)
      if [ "$DRY_RUN" = 1 ]; then
        GLR_ASSET=armv6_rpi
      else
        die "Unsupported CPU '$(uname -m)'. This script is for a Raspberry Pi."
      fi
      ;;
  esac
}

find_ui_source() {
  local here c
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for c in "$here/currawong_ui.py" "$HOME_DIR/currawong_ui.py" "$HOME_DIR/currawong/currawong_ui.py"; do
    if [ -f "$c" ]; then
      UI_SRC="$c"
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------- steps

preflight() {
  say "Checking the system"
  if [ "$DRY_RUN" != 1 ] && [ "$(id -u)" -ne 0 ]; then
    die "Run this with sudo:  sudo bash $0"
  fi
  detect_user
  pick_asset
  info "User: $TARGET_USER   Home: $HOME_DIR   CPU: $(uname -m)   go-librespot build: $GLR_ASSET"

  local osid
  # shellcheck source=/dev/null
  osid="$( (. /etc/os-release 2>/dev/null && echo "${ID:-} ${ID_LIKE:-}") || true)"
  case "$osid" in
    *debian* | *raspbian*) ;;
    *) warn "This doesn't look like Raspberry Pi OS or Debian ($osid). Carrying on, but expect surprises." ;;
  esac

  if ! find_ui_source; then
    if [ "$DRY_RUN" = 1 ]; then
      warn "currawong_ui.py not found next to the script (the real run needs it)."
    else
      die "currawong_ui.py not found. Put it in the same folder as this script and run again."
    fi
  else
    info "UI file: $UI_SRC"
  fi

  if [ "$DRY_RUN" != 1 ]; then
    curl -fsS -m 15 -o /dev/null https://github.com || die "No internet. Check the Wi-Fi first (nmcli dev wifi list)."
  fi
}

install_packages() {
  say "Installing packages (the first run takes a few minutes on a Zero W)"
  export DEBIAN_FRONTEND=noninteractive
  local required=(alsa-utils avahi-daemon bluez ca-certificates curl fonts-dejavu-core mpd mpc
    python3 python3-gpiozero python3-lgpio python3-numpy python3-pil python3-spidev)
  local optional=(libasound2-plugin-equal caps python3-qrcode python3-serial i2c-tools rfkill bluez-alsa-utils libasound2-plugin-bluez)
  local p
  run apt-get update
  run apt-get install -y --no-install-recommends "${required[@]}"
  for p in "${optional[@]}"; do
    try apt-get install -y --no-install-recommends "$p"
  done
  # battery gauge access: either name works for the UI
  if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] apt-get install python3-smbus2 (or python3-smbus)"
  elif ! apt-get install -y --no-install-recommends python3-smbus2; then
    try apt-get install -y --no-install-recommends python3-smbus
  fi
}

enable_interfaces() {
  say "Enabling SPI, I2C and the serial port (and turning the serial login console off)"
  try raspi-config nonint do_spi 0
  try raspi-config nonint do_i2c 0
  try raspi-config nonint do_serial_cons 1
  try raspi-config nonint do_serial_hw 0
  if [ "$DRY_RUN" = 1 ]; then return 0; fi

  local boot cfg cmd
  boot="$(boot_dir)"
  cfg="$boot/config.txt"
  cmd="$boot/cmdline.txt"
  grep -Eq '^dtparam=spi=on' "$cfg" || warn "SPI doesn't look enabled in $cfg (display won't work)."
  grep -Eq '^dtparam=i2c_arm=on' "$cfg" || warn "I2C doesn't look enabled in $cfg (battery gauge won't work)."
  grep -Eq '^enable_uart=1' "$cfg" || warn "The serial port doesn't look enabled in $cfg (the haptic dial link won't work)."
  if grep -Eq 'console=(serial0|ttyAMA0|ttyS0)' "$cmd"; then
    warn "The serial login console is still switched on in $cmd. Remove the console=serial0,115200 part."
  fi
  if grep -Eq 'g_ether|g_serial' "$cmd" || grep -Eq '^dtoverlay=dwc2' "$cfg"; then
    warn "USB gadget mode is still on (dwc2/g_ether). It stops the DAC working. Remove 'dtoverlay=dwc2' from $cfg and 'modules-load=dwc2,g_ether' from $cmd once you can reach the Pi over Wi-Fi."
  fi
}

set_hostname() {
  if [ -z "$NEW_HOSTNAME" ]; then return 0; fi
  say "Hostname"
  if [ "$(hostname)" = "$NEW_HOSTNAME" ]; then
    good "hostname is already $NEW_HOSTNAME"
    return 0
  fi
  try raspi-config nonint do_hostname "$NEW_HOSTNAME"
  info "Hostname set to $NEW_HOSTNAME (takes full effect after the reboot)."
}

add_groups() {
  say "Giving $TARGET_USER access to the hardware"
  local g wanted=()
  for g in audio bluetooth dialout gpio i2c netdev spi; do
    if getent group "$g" >/dev/null; then wanted+=("$g"); fi
  done
  try usermod -aG "$(
    IFS=,
    echo "${wanted[*]}"
  )" "$TARGET_USER"
}

make_dirs() {
  say "Creating folders"
  own_dir "$HOME_DIR/music" "$HOME_DIR/currawong" "$HOME_DIR/glr" \
    "$HOME_DIR/.config" "$HOME_DIR/.config/go-librespot" "$HOME_DIR/.config/currawong" \
    "$HOME_DIR/.mpd" "$HOME_DIR/.mpd/playlists"
}

install_librespot() {
  say "go-librespot $GLR_VERSION"
  local dir="$HOME_DIR/glr" marker url tmp
  marker="$dir/VERSION"
  if [ -x "$dir/go-librespot" ] && [ -f "$marker" ] && [ "$(cat "$marker")" = "${GLR_VERSION}_${GLR_ASSET}" ]; then
    good "already installed"
  else
    url="https://github.com/devgianlu/go-librespot/releases/download/${GLR_VERSION}/go-librespot_linux_${GLR_ASSET}.tar.gz"
    info "downloading $url"
    if [ "$DRY_RUN" = 1 ]; then
      info "[dry-run] download, unpack, install to $dir/go-librespot"
    else
      tmp="$(mktemp -d)"
      curl -fL --retry 3 -o "$tmp/glr.tgz" "$url"
      tar -xzf "$tmp/glr.tgz" -C "$tmp" go-librespot
      systemctl stop go-librespot 2>/dev/null || true
      install -m 755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$tmp/go-librespot" "$dir/go-librespot"
      echo "${GLR_VERSION}_${GLR_ASSET}" >"$marker"
      chown "$TARGET_USER:$TARGET_GROUP" "$marker"
      rm -rf "$tmp"
      good "installed $dir/go-librespot"
    fi
  fi

  put "$HOME_DIR/.config/go-librespot/config.yml" 644 "$TARGET_USER:$TARGET_GROUP" <<EOF
# Written by currawong_setup.sh. Edit freely, the script keeps a .bak if it replaces this.
log_level: info
device_name: $DEVICE_NAME
device_type: speaker
audio_backend: alsa
# The UI rewrites this line when you switch between wired and Bluetooth output.
audio_device: currawong_dac
bitrate: 320
# Volume is set on the DAC, so the signal reaches it at full resolution.
external_volume: true
zeroconf_enabled: true
zeroconf_backend: avahi
credentials:
  type: zeroconf
  zeroconf:
    # Keeps the Spotify login across reboots, so the player can resume Spotify after a
    # power-up without a phone (Settings > On power up). Connect once from Spotify to set it.
    persist_credentials: true
# The UI reads and controls playback through this local API.
server:
  enabled: true
  address: localhost
  port: 3678
EOF

  put /etc/systemd/system/go-librespot.service 644 root:root <<EOF
[Unit]
Description=go-librespot (Spotify Connect)
# No waiting for network-online: on a Zero W that can hold up the whole boot for up to a minute.
# Instead go-librespot itself waits (below) until it can look up Spotify's servers.
After=sound.target avahi-daemon.service currawong-ui.service
Wants=avahi-daemon.service
# Keep retrying forever rather than giving up after a few quick failures at boot.
StartLimitIntervalSec=0

[Service]
User=$TARGET_USER
# Two dashes matter: with one, the flag is read as the short option -c and the daemon exits.
# It exits at once if it can't reach Spotify, and at boot Wi-Fi isn't up yet: without this wait
# it crash-looped every 2 s, taking CPU from everything else that was starting.
ExecStartPre=/bin/sh -c 'until getent hosts apresolve.spotify.com >/dev/null 2>&1; do sleep 3; done'
TimeoutStartSec=infinity
ExecStart=$HOME_DIR/glr/go-librespot --config_dir $HOME_DIR/.config/go-librespot
Restart=always
RestartSec=2
# Audio wins over the UI
Nice=-10

[Install]
WantedBy=multi-user.target
EOF
}

install_mpd() {
  say "MPD (local music, playlists and the visualiser feed)"
  # The loopback sound card the Visualiser output plays into (one substream is plenty).
  put /etc/modules-load.d/currawong-loopback.conf 644 root:root <<'EOF'
# Written by currawong_setup.sh: the visualiser's copy of the audio (see /etc/mpd.conf)
snd-aloop
EOF
  put /etc/modprobe.d/currawong-loopback.conf 644 root:root <<'EOF'
options snd-aloop id=Loopback pcm_substreams=1
EOF
  try modprobe snd-aloop
  put /etc/mpd.conf 644 root:root <<EOF
# Written by currawong_setup.sh
music_directory     "$HOME_DIR/music"
playlist_directory  "$HOME_DIR/.mpd/playlists"
db_file             "$HOME_DIR/.mpd/database"
state_file          "$HOME_DIR/.mpd/state"
sticker_file        "$HOME_DIR/.mpd/sticker.sql"
log_file            "syslog"
user                "$TARGET_USER"
bind_to_address     "localhost"
port                "6600"
auto_update         "no"
restore_paused      "yes"
zeroconf_enabled    "no"

# Wired output: the DAC, with volume left to the DAC itself.
audio_output {
    type            "alsa"
    name            "DAC"
    device          "currawong_dac"
    mixer_type      "none"
}

# Bluetooth output: off until you choose it in the UI (Menu > Output).
audio_output {
    type            "alsa"
    name            "Bluetooth"
    device          "bt"
    mixer_type      "none"
    enabled         "no"
}

# A copy of the audio for the spectrum and Scope page, played into an ALSA loopback card
# (snd-aloop) that the UI records from. The UI expects exactly this format. The loopback is
# clocked like a real sound card, so MPD keeps it a second ahead just like the DAC. (A fifo
# output is paced to real time, and MPD never runs more than 64 chunks past its slowest
# output, so with 48 kHz/24-bit files the DAC ran dry a few seconds into each track.)
audio_output {
    type            "alsa"
    name            "Visualiser"
    device          "hw:Loopback,0,0"
    format          "22050:16:2"
    mixer_type      "none"
}

# Only the visualiser copy is resampled, and "quick" keeps the Zero W's CPU low.
resampler {
    plugin          "soxr"
    quality         "quick"
}
EOF
}

# bluealsa only offers the profiles it's started with. Without A2DP source, headphones connect but
# get no audio. If the packaged service doesn't ask for it, add it with a drop-in.
ensure_a2dp_source() {
  local unit="" u cmd
  for u in bluealsa.service bluealsad.service; do
    if systemctl cat "$u" >/dev/null 2>&1; then
      unit="$u"
      break
    fi
  done
  [ -n "$unit" ] || return 0
  cmd="$(systemctl show -p ExecStart --value "$unit" 2>/dev/null | sed -n 's/.*argv\[\]=\([^;]*\) ;.*/\1/p' | head -1)"
  case "$cmd" in
    "") return 0 ;;
    *a2dp-source*) good "$unit offers A2DP source (audio to headphones)"; return 0 ;;
  esac
  put "/etc/systemd/system/$unit.d/currawong.conf" 644 root:root <<EOF
# Written by currawong_setup.sh: send audio to Bluetooth headphones (A2DP source)
[Service]
ExecStart=
ExecStart=$cmd -p a2dp-source
EOF
  run systemctl daemon-reload
  try systemctl restart "$unit"
}

EQ_FILE=/var/lib/currawong/eq.bin

# The ALSA devices MPD and go-librespot play into. With the equaliser plugin installed, the DAC
# goes through it (bands kept in $EQ_FILE, set by the UI); Bluetooth always goes straight to
# bluealsa (see below).
write_asound_conf() {
  local eq=0
  if ls /usr/lib/*/alsa-lib/libasound_module_pcm_equal.so >/dev/null 2>&1; then eq=1; fi
  if [ "$eq" = 1 ]; then
    put /etc/asound.conf 644 root:root <<EOF
# Written by currawong_setup.sh
# "currawong_dac" (wired) and "bt" (Bluetooth) are what MPD and go-librespot play into. The wired
# one goes through the equaliser (alsaequal + CAPS Eq10); the UI sets its bands, kept in $EQ_FILE.
ctl.equal {
    type equal
    controls "$EQ_FILE"
}
pcm.currawong_eq {
    type equal
    slave.pcm "plughw:CARD=$DAC_CARD,DEV=0"
    controls "$EQ_FILE"
}
pcm.currawong_dac {
    type plug
    slave.pcm "currawong_eq"
}
# Bluetooth goes straight to bluealsa, without the equaliser: alsaequal and bluealsa's ALSA
# plugin can't agree on buffer sizes, and alsa-lib aborts the player (MPD crashed on every
# attempt). 00:00:00:00:00:00 means whichever headphones are connected.
pcm.bt {
    type plug
    slave.pcm "bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp"
}
EOF
  else
    warn "The equaliser plugin (libasound2-plugin-equal) isn't installed, so the EQ screen will say so."
    put /etc/asound.conf 644 root:root <<EOF
# Written by currawong_setup.sh: "currawong_dac" (wired) and "bt" (Bluetooth), what MPD and
# go-librespot play into. (No equaliser: libasound2-plugin-equal isn't installed.)
pcm.currawong_dac {
    type plug
    slave.pcm "plughw:CARD=$DAC_CARD,DEV=0"
}
# 00:00:00:00:00:00 means whichever headphones are connected.
pcm.bt {
    type plug
    slave.pcm "bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp"
}
EOF
    return 0
  fi
  # The bands' file: written by the UI (as $TARGET_USER), read by MPD (as mpd, in the audio group).
  run install -d -m 2775 -o root -g audio /var/lib/currawong
  # Measured flat (the plugin's own 66 everywhere is about +3 dB, which clipped loud tracks).
  # Written every run: the UI puts your own EQ back when it starts.
  if [ "$DRY_RUN" != 1 ]; then
    local b=1 v
    for v in 61 60 60 60 60 60 60 59 60 62; do
      sudo -u "$TARGET_USER" amixer -q -D equal cset "numid=$b" "$v,$v" >/dev/null 2>&1 || true
      b=$((b + 1))
    done
  fi
  if [ -f "$EQ_FILE" ]; then
    run chgrp audio "$EQ_FILE"
    run chmod 664 "$EQ_FILE"
  fi
  good "equaliser in place (flat until you set it on the player)"
}

install_bluetooth_audio() {
  say "Bluetooth audio"
  # A blocked radio is the other common reason pairing fails without saying why.
  try rfkill unblock bluetooth
  write_asound_conf
  ensure_a2dp_source
}

install_ui() {
  say "The UI"
  local dest="$HOME_DIR/currawong/currawong_ui.py"
  if [ -n "$UI_SRC" ] && [ "$DRY_RUN" != 1 ]; then
    if [ "$(readlink -f "$UI_SRC")" = "$(readlink -f "$dest")" ]; then
      good "UI is already in place"
    else
      install -m 755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$UI_SRC" "$dest"
      good "installed $dest"
    fi
    # the boot animation, the add-music page, the currawong (its module and assets/), and an
    # optional logo, from next to the script
    local here f
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for f in currawong_boot.anim currawong_upload.py currawong_bird.py logo.png \
             assets/currawong_sheet.png assets/branch_sway.png assets/animation_spec.json; do
      if [ -f "$here/$f" ] && [ "$(readlink -f "$here/$f")" != "$(readlink -f "$HOME_DIR/currawong/$f")" ]; then
        install -d -m 755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$(dirname "$HOME_DIR/currawong/$f")"
        install -m 644 -o "$TARGET_USER" -g "$TARGET_GROUP" "$here/$f" "$HOME_DIR/currawong/$f"
        good "installed $HOME_DIR/currawong/$f"
      fi
    done
    [ -f "$HOME_DIR/currawong/currawong_bird.py" ] && [ -f "$HOME_DIR/currawong/assets/currawong_sheet.png" ] \
      || info "The currawong (currawong_bird.py and assets/) isn't installed: the UI runs without it."
    for f in currawong_boot.anim currawong_upload.py; do
      [ -f "$HOME_DIR/currawong/$f" ] || warn "$f wasn't next to the script, so that part is skipped."
    done
  else
    info "[dry-run] copy currawong_ui.py to $dest"
  fi

  # The add-music page: http://currawong.local on any phone or computer on the same Wi-Fi.
  # Port 80 so there's no :8080 to type; it starts after the UI's first screen.
  put /etc/systemd/system/currawong-upload.service 644 root:root <<EOF
[Unit]
Description=Currawong add-music page
After=network.target currawong-ui.service

[Service]
User=$TARGET_USER
ExecStart=/usr/bin/python3 $HOME_DIR/currawong/currawong_upload.py --music $HOME_DIR/music --port 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
Nice=10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  put "$HOME_DIR/.config/currawong/spotify_pins.example.json" 644 "$TARGET_USER:$TARGET_GROUP" <<'EOF'
[
  {"name": "Late drives", "uri": "https://open.spotify.com/playlist/REPLACE_WITH_YOUR_PLAYLIST_ID"},
  {"name": "Running", "uri": "spotify:playlist:REPLACE_WITH_YOUR_PLAYLIST_ID"}
]
EOF

  put /etc/systemd/system/currawong-ui.service 644 root:root <<EOF
[Unit]
Description=Currawong UI
# Start straight away: the UI shows its logo and finds the players whenever they come up.
# (It used to wait for go-librespot, which waited for the network.)
After=sound.target
StartLimitIntervalSec=0

[Service]
# The UI tells systemd when its first screen is up (READY=1); MPD and go-librespot are ordered
# after that, so the screen gets the single CPU core to itself while it starts.
Type=notify
NotifyAccess=main
TimeoutStartSec=120
# Show the logo saved from the last run before loading anything slow.
Environment=CURRAWONG_EARLY_SPLASH=1
User=$TARGET_USER
WorkingDirectory=$HOME_DIR/currawong
# Run as a module so Python keeps the compiled code (__pycache__) instead of recompiling all of
# it at every boot, which takes seconds on a Zero W.
ExecStart=/usr/bin/python3 -m currawong_ui
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3
# Normal priority while the system boots, so the logo isn't starved of CPU. Once its first screen
# is up, the UI drops itself to Nice 10: audio always wins after that.
Nice=0

[Install]
WantedBy=multi-user.target
EOF
  # MPD takes about 25 s to start on a Zero W: let the UI's first screen go first.
  put /etc/systemd/system/mpd.service.d/currawong.conf 644 root:root <<'EOF'
# Written by currawong_setup.sh: start after the UI has shown its first screen
[Unit]
After=currawong-ui.service
EOF
}

install_sudoers() {
  say "Letting the UI run its few root commands without a password"
  local f=/etc/sudoers.d/currawong po nm sc line tmp
  po="$(command -v poweroff || echo /usr/sbin/poweroff)"
  nm="$(command -v nmcli || echo /usr/bin/nmcli)"
  sc="$(command -v systemctl || echo /usr/bin/systemctl)"
  line="$TARGET_USER ALL=(root) NOPASSWD: $po, $nm, $sc restart go-librespot"
  if [ "$DRY_RUN" = 1 ]; then
    put "$f" 440 root:root <<<"$line"
    return 0
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$line" >"$tmp"
  if visudo -cf "$tmp" >/dev/null; then
    install -m 440 -o root -g root "$tmp" "$f"
    good "wrote $f"
  else
    warn "The sudoers line failed its syntax check, so it was NOT installed. Menu > Power off and Wi-Fi will not work."
  fi
  rm -f "$tmp"
  # an older, narrower file from the first version of the notes
  if [ -f /etc/sudoers.d/currawong-poweroff ]; then
    rm -f /etc/sudoers.d/currawong-poweroff
    info "removed the old /etc/sudoers.d/currawong-poweroff"
  fi
}

unit_exists() {
  systemctl list-unit-files --no-legend "$1.service" 2>/dev/null | grep -q "^$1.service"
}

start_services() {
  say "Starting the services"
  run systemctl daemon-reload
  try systemctl enable --now avahi-daemon
  try systemctl enable --now bluetooth
  if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] enable bluealsa (or bluealsad), go-librespot, mpd and currawong-ui"
    return 0
  fi
  local ba
  for ba in bluealsa bluealsad; do
    if unit_exists "$ba"; then
      try systemctl enable --now "$ba"
      break
    fi
  done
  # Debian's mpd.socket would listen on every network address and ignore bind_to_address
  try systemctl disable --now mpd.socket
  try systemctl enable mpd
  try systemctl restart mpd
  try systemctl enable go-librespot
  try systemctl restart go-librespot
  if [ -f "$HOME_DIR/currawong/currawong_upload.py" ]; then
    try systemctl enable currawong-upload
    try systemctl restart currawong-upload
  fi
  try systemctl enable currawong-ui
  if [ -e /dev/spidev0.0 ]; then
    try systemctl restart currawong-ui
  else
    info "SPI isn't active yet, so the UI will start after the reboot."
  fi
  # build the library database (harmless if ~/music is empty)
  try mpc update
}

verify_librespot() {
  local tries=0
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  say "Checking go-librespot actually came up"
  while [ "$tries" -lt 12 ]; do
    tries=$((tries + 1))
    if curl -fsS -m 2 http://localhost:3678/status >/dev/null 2>&1; then
      good "go-librespot is running and its API is answering"
      return 0
    fi
    sleep 1
  done
  warn "go-librespot isn't answering. The last log lines are below. The full log: journalctl -u go-librespot -n 50 --no-pager"
  journalctl -u go-librespot -n 12 --no-pager 2>&1 | sed 's/^/        /' || true
}

check_all() {
  say "Status"
  local s d
  for s in avahi-daemon bluetooth bluealsa mpd go-librespot currawong-ui; do
    printf '    %-16s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || true)"
  done
  if [ "$(systemctl is-active go-librespot 2>/dev/null || true)" != active ]; then
    info "go-librespot isn't running properly. See why with: journalctl -u go-librespot -n 30 --no-pager"
  fi
  for d in /dev/spidev0.0 /dev/i2c-1 /dev/serial0; do
    if [ -e "$d" ]; then
      printf '    %-16s present\n' "$d"
    else
      printf '    %-16s MISSING (a reboot is needed after enabling the interfaces)\n' "$d"
    fi
  done
  printf '    %-16s %s\n' "Wi-Fi radio" "$(nmcli radio wifi 2>/dev/null || echo unknown) (the UI turns it back on at startup)"
  local con auto
  while IFS=: read -r con auto; do
    [ -n "$con" ] || continue
    if [ "$auto" = "no" ]; then
      printf '    %-16s %s: WON'"'"'T JOIN BY ITSELF (sudo nmcli con mod "%s" connection.autoconnect yes)\n' "saved Wi-Fi" "$con" "$con"
    else
      printf '    %-16s %s: joins by itself\n' "saved Wi-Fi" "$con"
    fi
  done < <(nmcli -t -f NAME,TYPE,AUTOCONNECT con show 2>/dev/null | awk -F: '$2 == "802-11-wireless" {print $1 ":" $3}' || true)
  if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
    printf '    %-16s BLOCKED (sudo rfkill unblock bluetooth)\n' "Bluetooth radio"
  elif bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
    printf '    %-16s on\n' "Bluetooth radio"
  else
    printf '    %-16s off or missing (the UI powers it on when you scan)\n' "Bluetooth radio"
  fi
  if grep -q "type equal" /etc/asound.conf 2>/dev/null && [ -f "$EQ_FILE" ]; then
    printf '    %-16s on (set it on the player: Menu > Equaliser)\n' "equaliser"
  else
    printf '    %-16s not installed (sudo apt install libasound2-plugin-equal caps, then run this again)\n' "equaliser"
  fi
  printf '    %-16s %s\n' "add-music page" "$(systemctl is-active currawong-upload 2>/dev/null || true): http://$(hostname).local"
  if [ -e /proc/asound/Loopback ]; then
    printf '    %-16s present\n' "loopback card"
  else
    printf '    %-16s MISSING, so the visualisers get nothing (sudo modprobe snd-aloop)\n' "loopback card"
  fi
  if aplay -l 2>/dev/null | grep -q "$DAC_CARD"; then
    printf '    %-16s found\n' "DAC ($DAC_CARD)"
  else
    printf '    %-16s NOT FOUND (plug it into the USB data port, then: cat /proc/asound/cards)\n' "DAC ($DAC_CARD)"
  fi
  printf '    %-16s %s\n' "MPD" "$(mpc stats 2>/dev/null | grep -m1 -i songs || echo 'not answering')"
  if curl -fsS -m 3 http://localhost:3678/status >/dev/null 2>&1; then
    printf '    %-16s answering\n' "go-librespot API"
  else
    printf '    %-16s not answering\n' "go-librespot API"
  fi
  if command -v i2cdetect >/dev/null 2>&1 && i2cdetect -y 1 2>/dev/null | grep '^30:' | grep -q ' 36'; then
    printf '    %-16s found at 0x36\n' "battery gauge"
  else
    printf '    %-16s not seen (fine until the battery is connected)\n' "battery gauge"
  fi
  if command -v vcgencmd >/dev/null 2>&1; then
    printf '    %-16s %s (0x0 is good)\n' "power flags" "$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
  fi
  if [ -f /etc/sudoers.d/currawong ] && visudo -cf /etc/sudoers.d/currawong >/dev/null 2>&1; then
    printf '    %-16s ok\n' "sudoers"
  else
    printf '    %-16s missing or invalid\n' "sudoers"
  fi
  if [ -f /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf ]; then
    printf '    %-16s ok\n' "UI fonts"
  else
    printf '    %-16s missing (sudo apt install fonts-dejavu-core)\n' "UI fonts"
  fi
}

finish() {
  say "Done"
  if [ "${#WARNINGS[@]}" -gt 0 ]; then
    info "Things to look at:"
    local w
    for w in "${WARNINGS[@]}"; do info " - $w"; done
  fi
  cat <<EOF

    Next steps
      1. Reboot:                  sudo reboot
      2. Music:                   scp -r "Artist" $TARGET_USER@${NEW_HOSTNAME:-$(hostname)}.local:~/music/
                                  then on the Pi:  mpc update --wait
      3. Spotify:                 open the Spotify app and choose "$DEVICE_NAME" as the device
      4. Check everything:        sudo bash $0 --check
      5. Change the password:     passwd        (before you hand it over)

    Not covered here: flashing the ESP32 dial, Bluetooth pairing (Menu > Bluetooth) and
    Wi-Fi changes (Menu > Wi-Fi), which you do from the player itself.
EOF
}

# ---------------------------------------------------------------- main

SLOW_UNITS="cloud-init-local.service cloud-init-main.service cloud-init.service cloud-config.service cloud-final.service NetworkManager-wait-online.service systemd-networkd-wait-online.service apt-daily.timer apt-daily-upgrade.timer man-db.timer"

unit_state() {
  local st
  st="$(systemctl is-enabled "$1" 2>/dev/null || true)"
  printf '%s' "${st:-not-found}"
}

boot_report() {
  local unit st
  say "Boot time (the firmware's few seconds before the kernel aren't included)"
  systemd-analyze 2>/dev/null || true
  say "When the screen came to life (seconds after the kernel started)"
  journalctl -b -u currawong-ui --no-pager 2>/dev/null | grep "Boot timing" | sed 's/^.*Boot timing: /    /' || true
  say "Known slow starters on a Zero W"
  for unit in $SLOW_UNITS; do
    st="$(unit_state "$unit")"
    if [ "$st" != "not-found" ]; then info "$(printf '%-36s %s' "$unit" "$st")"; fi
  done
  if [ -e /etc/cloud/cloud-init.disabled ]; then info "cloud-init is switched off (/etc/cloud/cloud-init.disabled)"; fi
  if [ -f /etc/rpi/swap.conf ]; then
    info "$(printf '%-36s %s' "swap" "$(cat /etc/rpi/swap.conf /etc/rpi/swap.conf.d/*.conf 2>/dev/null | sed -n 's/^[[:space:]]*Mechanism=//p' | tail -1 | sed 's/^$/auto/')")"
    info "                                     (auto = zram+file: a swap file resized at every boot)"
  fi
  info "Anything 'enabled' above: sudo bash currawong_setup.sh --fast-boot switches it off."
  say "Slowest units"
  systemd-analyze blame 2>/dev/null | head -15 || true
  say "What the UI waited for"
  systemd-analyze critical-chain currawong-ui.service 2>/dev/null || true
  say "What go-librespot waited for"
  systemd-analyze critical-chain go-librespot.service 2>/dev/null || true
  say "go-librespot since this boot"
  journalctl -b -u go-librespot -n 15 --no-pager 2>/dev/null || true
}

# Each layer that can stop a scan finding anything, in order, then a scan that doesn't use the UI.
bt_report() {
  local cfg out
  cfg="$(boot_dir)/config.txt"
  say "Bluetooth radio, layer by layer"
  if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
    warn "The radio is blocked. Fix: sudo rfkill unblock bluetooth"
  elif rfkill list bluetooth 2>/dev/null | grep -q "Hard blocked: yes"; then
    warn "The radio is hard-blocked (unusual on a Pi)."
  elif rfkill list bluetooth 2>/dev/null | grep -q "Bluetooth"; then
    good "not blocked (rfkill)"
  else
    warn "rfkill lists no Bluetooth radio, so the kernel hasn't attached one."
  fi
  for svc in hciuart bluetooth bluealsa; do
    info "$(printf '%-12s %s' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)")"
  done
  if grep -Eq '^[[:space:]]*dtoverlay=(pi3-)?(disable-bt|miniuart-bt)' "$cfg" 2>/dev/null; then
    warn "$cfg has a Bluetooth overlay: $(grep -E '^[[:space:]]*dtoverlay=(pi3-)?(disable-bt|miniuart-bt)' "$cfg" | head -1)"
  else
    good "no overlay disabling Bluetooth in $cfg"
  fi
  if id -nG "$TARGET_USER" 2>/dev/null | grep -qw bluetooth; then
    good "$TARGET_USER is in the bluetooth group"
  else
    warn "$TARGET_USER isn't in the bluetooth group. Re-run this script without options."
  fi
  out="$(sudo -u "$TARGET_USER" timeout 8 bluetoothctl show 2>&1 | sed 's/\x1b\[[0-9;]*m//g' || true)"
  if printf '%s' "$out" | grep -q "^Controller"; then
    info "$(printf '%s\n' "$out" | grep -m1 '^Controller' || true)"
    info "$(printf '%s\n' "$out" | grep -m1 'Powered:' | sed 's/^[[:space:]]*//' || true)"
  elif printf '%s' "$out" | grep -q "No default controller"; then
    warn "No Bluetooth controller. The radio's serial link didn't come up."
    info "Look at: systemctl status hciuart, and: dmesg | grep -i -E 'hci|bluetooth'"
  else
    warn "Can't reach bluetoothd as $TARGET_USER. Look at: systemctl status bluetooth"
    info "bluetoothctl said: $(printf '%s\n' "$out" | head -2 | tr '\n' ' ')"
  fi

  local tool
  tool="$(command -v bluealsa-cli || command -v bluealsactl || true)"
  if [ -n "$tool" ]; then
    if sudo -u "$TARGET_USER" "$tool" status 2>/dev/null | grep -qi "a2dp-source"; then
      good "bluealsa offers A2DP source (audio to headphones)"
    else
      warn "bluealsa isn't offering A2DP source, so headphones get no audio. Re-run this script without options."
    fi
    info "Audio links to headphones right now: $(sudo -u "$TARGET_USER" "$tool" list-pcms 2>/dev/null | grep -c a2dp || true)"
  fi

  say "Scanning for 15 s. Put the headphones in pairing mode now."
  sudo -u "$TARGET_USER" timeout 8 bluetoothctl power on >/dev/null 2>&1 || true
  sudo -u "$TARGET_USER" timeout 25 bluetoothctl --timeout 15 scan on 2>&1 |
    sed 's/\x1b\[[0-9;]*m//g' | grep -E "NEW\] Device|Failed|No default|Waiting" | sed 's/^/    /' || true
  say "Devices BlueZ knows now"
  out="$(sudo -u "$TARGET_USER" timeout 8 bluetoothctl devices 2>&1 | sed 's/\x1b\[[0-9;]*m//g' || true)"
  if printf '%s' "$out" | grep -q "^Device"; then
    printf '%s\n' "$out" | grep "^Device" | sed 's/^/    /'
    info "If the headphones are listed here, the radio works: send ~/currawong/bt.log from a UI attempt."
  else
    warn "Nothing found. With the layers above all fine, check the headphones are in pairing mode and close by."
  fi
}

# The DAC's USB device directory in sysfs (holds power/control), or nothing.
dac_usb_dir() {
  local idfile n dev
  for idfile in /proc/asound/card*/id; do
    [ -r "$idfile" ] || continue
    if [ "$(cat "$idfile")" = "$DAC_CARD" ]; then
      n="${idfile#/proc/asound/card}"
      n="${n%/id}"
      dev="$(readlink -f "/sys/class/sound/card$n/device" 2>/dev/null || true)"
      dev="$(dirname "$dev")"  # the USB interface's parent is the device
      if [ -e "$dev/power/control" ]; then
        printf '%s\n' "$dev"
        return 0
      fi
    fi
  done
  return 0
}

power_report() {
  local dac user cfg
  user="${SUDO_USER:-}"
  if [ -z "$user" ] || [ "$user" = root ]; then user="$(getent passwd 1000 | cut -d: -f1 || true)"; fi
  cfg="$(getent passwd "$user" 2>/dev/null | cut -d: -f6)/.config/currawong/settings.json"
  say "Battery (the UI's ABOUT page shows the same estimate)"
  python3 - "$cfg" "$BATTERY_MAH" 2>/dev/null <<'PY' || info "Couldn't read the fuel gauge (no battery connected?)"
import json, sys
try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus
try:
    settings = json.load(open(sys.argv[1]))
except Exception:
    settings = {}
mah = float(sys.argv[2] or settings.get("battery_mah") or 2000)   # the same capacity the UI uses
cutoff = float(settings.get("cutoff_volts") or 3.30)
bus = SMBus(1)
def word(reg):
    w = bus.read_word_data(0x36, reg)
    return ((w & 0xFF) << 8) | (w >> 8)  # the chip is big-endian
volts = word(0x02) * 78.125e-6
soc = word(0x04) / 256
crate = word(0x16)
rate = (crate - 0x10000 if crate & 0x8000 else crate) * 0.208
if not 2.5 <= volts <= 4.6 or soc > 110:
    print(f"    the gauge gave a reading that makes no sense ({volts:.2f} V, {soc:.0f}%): try again")
    sys.exit(0)
print(f"    {soc:.0f}%  {volts:.2f} V  rate {rate:+.1f} %/h", end="")
if rate < -0.3:
    print(f"  ->  roughly {-rate * mah / 100:.0f} mA, {soc / -rate:.1f} h left")
    print(f"    (an estimate: the gauge's %/h times a {mah:.0f} mAh cell. The MAX17048 doesn't measure current.)")
elif rate > 3.0:
    print("  (charging)")
else:
    print("  (still measuring: the rate takes a few minutes to settle)")
print(f"    The UI switches off at {5}% or below for 60 s (paused only while surely charging),")
print(f"    or with the cell under {cutoff:.2f} V for 30 s (under 3.10 V: 5 s).")
PY
  say "What's switched on"
  info "$(printf '%-16s %s' "Wi-Fi radio" "$(nmcli radio wifi 2>/dev/null || echo unknown)")"
  if command -v iw >/dev/null 2>&1; then
    info "$(printf '%-16s %s' "Wi-Fi power save" "$(iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}' || echo unknown)")"
  fi
  info "$(printf '%-16s %s' "Bluetooth" "$(bluetoothctl show 2>/dev/null | awk '/Powered:/ {print $2}' || echo unknown)")"
  dac="$(dac_usb_dir)"
  if [ -n "$dac" ]; then
    info "$(printf '%-16s control=%s, now %s' "DAC (USB)" "$(cat "$dac/power/control")" "$(cat "$dac/power/runtime_status" 2>/dev/null || echo unknown)")"
    info "                 (control=on: never sleeps. --dac-sleep lets it sleep while nothing plays.)"
  else
    info "$(printf '%-16s %s' "DAC (USB)" "not found")"
  fi
  info "$(printf '%-16s %s' "Throttled" "$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo unknown)")"
  say "Busiest processes (% of the CPU)"
  ps -eo pcpu=,comm= --sort=-pcpu 2>/dev/null | head -6 | sed 's/^/    /' || true
  say "Rough share of an idle draw (at the 5 V rail; the cell side is about 1.4 times this)"
  info "Pi Zero W with Wi-Fi on        100-130 mA   (Wi-Fi off saves about 20-40)"
  info "Moondrop DAC, awake            50-100 mA    (asleep: a few mA, if it supports it)"
  info "ESP32 dial, idle               15-20 mA     (v0.2 firmware ran flat out: 50-60)"
  info "Display, gauge, sensor, boost  15-20 mA"
}

dac_sleep() {
  local dac vendor product rule=/etc/udev/rules.d/90-currawong-dac-sleep.rules
  dac="$(dac_usb_dir)"
  [ -n "$dac" ] || die "The DAC ($DAC_CARD) isn't plugged in, so there's nothing to set up."
  vendor="$(cat "$dac/idVendor")"
  product="$(cat "$dac/idProduct")"
  say "Letting the DAC sleep while nothing plays ($vendor:$product)"
  printf 'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="%s", ATTR{idProduct}=="%s", TEST=="power/control", ATTR{power/control}="auto", ATTR{power/autosuspend_delay_ms}="3000"\n' \
    "$vendor" "$product" | put "$rule" 644
  run sh -c "echo 3000 > '$dac/power/autosuspend_delay_ms'; echo auto > '$dac/power/control'"
  info "Now: stop the music, wait 10 s, then run --power. The DAC line should say 'suspended'."
  info "If it stays 'active', the Pi's USB doesn't let it sleep and nothing changes."
  info "If playback starts late or not at all, undo it: sudo bash currawong_setup.sh --dac-awake"
}

# Set key=value in config.txt: change the line if it's there, otherwise add it under [all].
set_boot_option() {
  local cfg="$1" key="$2" value="$3"
  if grep -Eq "^[[:space:]]*${key}=${value}[[:space:]]*\$" "$cfg" 2>/dev/null; then
    info "unchanged: $key=$value"
  elif grep -Eq "^[[:space:]]*$key=" "$cfg" 2>/dev/null; then
    run sed -i "s/^[[:space:]]*$key=.*/$key=$value/" "$cfg"
    good "$key=$value"
  else
    run sh -c "printf '\n[all]\n%s=%s\n' '$key' '$value' >> '$cfg'"
    good "$key=$value"
  fi
}

fast_boot() {
  local unit cfg
  cfg="$(boot_dir)/config.txt"
  say "Faster boot"
  # Nothing on the player waits for the network: go-librespot and the UI cope when it arrives later.
  for unit in NetworkManager-wait-online.service systemd-networkd-wait-online.service; do
    if [ "$(unit_state "$unit")" = enabled ]; then
      run systemctl disable "$unit"
      good "boot no longer waits for Wi-Fi ($unit)"
    fi
  done
  # cloud-init applies the Imager's settings on the very first boot, then just costs time.
  if [ -d /etc/cloud ] && [ ! -e /etc/cloud/cloud-init.disabled ]; then
    if [ -e /var/lib/cloud/instance/boot-finished ]; then
      run touch /etc/cloud/cloud-init.disabled
      good "cloud-init off (its first-boot job is done; delete /etc/cloud/cloud-init.disabled to undo)"
    else
      warn "cloud-init hasn't finished its first boot yet, so it's left on."
    fi
  fi
  # Background package-list downloads and manual-page indexing: minutes of one busy core after boot.
  for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
    if [ "$(unit_state "$unit")" = enabled ]; then
      run systemctl disable --now "$unit"
      good "off: $unit"
    fi
  done
  # Swap in compressed RAM only. Trixie's default also keeps a swap file on the SD card, and
  # resizing it holds up everything else at every boot (about 20 s on a Zero W).
  if [ -f /etc/rpi/swap.conf ] || [ "$DRY_RUN" = 1 ]; then
    put /etc/rpi/swap.conf.d/currawong.conf 644 root:root <<'EOF'
# Written by currawong_setup.sh --fast-boot: swap in compressed RAM (zram) only, no swap file
# on the SD card. Delete this file and reboot to go back to the default (zram+file).
[Main]
Mechanism=zram
EOF
    good "swap: compressed RAM only (takes effect at the next boot)"
  fi
  # Firmware: no rainbow screen, no pause before the kernel.
  if [ -f "$cfg" ] || [ "$DRY_RUN" = 1 ]; then
    set_boot_option "$cfg" disable_splash 1
    set_boot_option "$cfg" boot_delay 0
  fi
  info "Reboot, wait for the player, then run --boot again to compare."
  info "Package updates no longer run in the background: sudo apt update && sudo apt full-upgrade"
}

# New firmware for the dial over the cable it already shares with the Pi (dial firmware v0.5 and
# later): no need to reach the XIAO's USB port. The UI holds the serial port, so it's stopped
# for the update and started again afterwards.
update_dial() {
  local here bin ui was=0
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  bin="${DIAL_BIN:-$here/currawong_knob.bin}"
  [ -f "$bin" ] || die "No firmware file at $bin (copy currawong_knob.bin next to this script)."
  ui="$here/currawong_ui.py"
  [ -f "$ui" ] || ui="$HOME_DIR/currawong/currawong_ui.py"
  say "Updating the dial's firmware over the cable"
  if systemctl is-active --quiet currawong-ui; then
    was=1
    run systemctl stop currawong-ui
  fi
  if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] would send $bin to the dial"
  elif sudo -u "$TARGET_USER" python3 "$ui" --update-dial "$(readlink -f "$bin")"; then
    good "dial updated"
  else
    warn "The dial wasn't updated. It keeps the firmware it had."
  fi
  if [ "$was" = 1 ]; then try systemctl start currawong-ui; fi
}

# CPU over a while, per process, as a share of the Pi's (single) core, for comparing visual modes
# and builds: run it with the same track playing in each state you want to compare.
cpu_report() {
  local secs="${1:-30}"
  case "$secs" in *[!0-9]*|"") secs=30 ;; esac
  say "CPU over ${secs} s (% of one core; the Zero W has one)"
  python3 - "$secs" <<'PY'
import os, sys, time
secs = int(sys.argv[1])
tick, ncpu = os.sysconf("SC_CLK_TCK"), os.cpu_count() or 1
def procs():
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            argv = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
            st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        a = " ".join(x.decode(errors="replace") for x in argv if x)
        name = ("mpd" if os.path.basename(argv[0].decode(errors="replace")) == "mpd" else
                "UI (currawong_ui)" if "currawong_ui" in a else
                "arecord (visualiser tap)" if os.path.basename(argv[0].decode(errors="replace")) == "arecord" else
                "go-librespot" if "go-librespot" in a else None)
        if name:
            out[int(d)] = (name, int(st[11]) + int(st[12]))
    return out
def total():
    f = open("/proc/stat").readline().split()[1:]
    return sum(int(x) for x in f), int(f[3]) + int(f[4])
a, (t0, i0) = procs(), total()
time.sleep(secs)
b, (t1, i1) = procs(), total()
use = {}
for pid, (name, ticks) in b.items():
    use[name] = use.get(name, 0) + ticks - (a[pid][1] if pid in a else 0)
for name in ("mpd", "UI (currawong_ui)", "arecord (visualiser tap)", "go-librespot"):
    print(f"    {name:26s} {100 * use.get(name, 0) / tick / secs:5.1f}%")
busy = 100 * (1 - (i1 - i0) / max(1, t1 - t0)) * ncpu
print(f"    {'whole system':26s} {busy:5.1f}%")
PY
  info "$(printf '%-28s %s' "Visualiser output" "$(mpc outputs 2>/dev/null | grep -i visualiser | sed 's/.*) is //' || echo unknown)")"
  info "Compare like with like: the same track, the same screen and mode, a minute or two in."
}

dac_awake() {
  local dac rule=/etc/udev/rules.d/90-currawong-dac-sleep.rules
  say "Keeping the DAC awake"
  run rm -f "$rule"
  dac="$(dac_usb_dir)"
  if [ -n "$dac" ]; then run sh -c "echo on > '$dac/power/control'"; fi
  good "done"
}

main() {
  case "$MODE" in
    power) power_report; return 0 ;;
    dac-sleep) dac_sleep; return 0 ;;
    dac-awake) dac_awake; return 0 ;;
    fast-boot) fast_boot; return 0 ;;
    update-dial) detect_user; update_dial; return 0 ;;
    cpu) cpu_report "${DIAL_BIN:-30}"; return 0 ;;
  esac
  if [ "$MODE" = bluetooth ]; then
    detect_user
    bt_report
    return 0
  fi
  if [ "$MODE" = boot ]; then
    boot_report
    return 0
  fi
  if [ "$MODE" = check ]; then
    detect_user
    check_all
    return 0
  fi

  if [ "$DRY_RUN" = 1 ]; then
    say "DRY RUN: nothing will be changed. Config files are written under $DRY_OUT for inspection."
  fi
  preflight
  install_packages
  enable_interfaces
  set_hostname
  add_groups
  make_dirs
  install_librespot
  install_mpd
  install_bluetooth_audio
  install_ui
  install_sudoers
  start_services
  verify_librespot
  if [ "$DRY_RUN" != 1 ]; then check_all; fi
  finish
}

# Only run when executed, so the functions can be tested by sourcing the file.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main
fi
