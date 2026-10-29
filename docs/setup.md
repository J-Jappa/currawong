# Setup

## 1. Dial firmware (once, over USB)

See [firmware/currawong_knob](../firmware/currawong_knob). After the first USB flash, later updates go through the Pi (step 4).

## 2. Raspberry Pi OS

1. Flash **Raspberry Pi OS Lite (32-bit)** with Raspberry Pi Imager. In the customisation step set the hostname, username, Wi-Fi credentials and country, and enable SSH.
2. Plug the Dawn Pro 2 into the Pi's USB data port.
3. Copy the `pi/` folder across:

   ```sh
   scp -r pi/* <user>@currawong.local:~/
   ```

## 3. Install

```sh
sudo bash currawong_setup.sh
```

The script installs and configures everything on the Pi side: packages, SPI, I2C and serial, go-librespot, MPD, Bluetooth audio (BlueALSA), the UI, the add-music page and their systemd services. It is idempotent: it only changes what differs and keeps a `.bak` of any file it replaces.

| Option | Does |
| --- | --- |
| `--dry-run` | Show what would change, change nothing |
| `--check` | Report the state of an existing install |
| `--boot` | Break down boot time (run after a fresh reboot) |
| `--bluetooth` | Check the Bluetooth stack layer by layer, then scan |
| `--fast-boot` | Disable boot-time services the player doesn't need |
| `--power` | Show what is drawing power and the current battery drain |
| `--dac-sleep` / `--dac-awake` | Allow or prevent USB autosuspend of the DAC while idle |
| `--cpu [secs]` | CPU use of MPD, the UI and arecord over a window (default 30 s) |
| `--update-dial [file.bin]` | Send new dial firmware over the UART link |

Environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DAC_CARD` | `PRO2` | ALSA card ID of the DAC (`cat /proc/asound/cards`) |
| `DEVICE_NAME` | `Currawong` | Name shown in Spotify |
| `GLR_VERSION` | `v0.10.3` | go-librespot release |
| `NEW_HOSTNAME` | `currawong` | Hostname to set (empty leaves it alone) |
| `TARGET_USER` | the sudo user | User that owns and runs everything |

## 4. Updating the dial from the Pi

Build the firmware (`pio run` in `firmware/currawong_knob`), copy `.pio/build/xiao_esp32s3/firmware.bin` to the Pi, then:

```sh
sudo bash currawong_setup.sh --update-dial firmware.bin
```

The image is written to the inactive OTA partition and verified against its MD5 before it becomes the boot image. On any failure the dial keeps running its current firmware. The XIAO's USB-C port remains a fallback.

## 5. Adding music

Open `http://currawong.local` from any device on the same network and upload files or a folder. The page accepts audio, cover art, `.cue` and `.lrc` only, from local-network addresses only, and triggers an MPD rescan when the upload finishes.

## Pinned Spotify playlists

Optional. `~/.config/currawong/spotify_pins.json` on the Pi:

```json
[
  {"name": "Late drives", "uri": "https://open.spotify.com/playlist/..."},
  {"name": "Running",     "uri": "spotify:playlist:..."}
]
```

Share links from the Spotify app work as-is.

## Development without hardware

```sh
python3 pi/currawong_ui.py --preview          # renders each screen to preview/*.png
python3 pi/bird_preview.py --scene idle       # renders the character animation to PNG frames and a GIF
python3 pi/test_battery.py                    # battery monitor and low-battery shutdown tests
```

These need Python 3 and Pillow.
