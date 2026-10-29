# Dial protocol

The Pi (`/dev/serial0`) and the XIAO ESP32-S3 (`Serial1`, RX GPIO6, TX GPIO43) exchange ASCII lines at 115200 8N1.

## Framing

```
$<body>*<checksum>\n
```

`<checksum>` is the XOR of every byte of `<body>`, as two uppercase hex digits (NMEA style). Example: `$POS,3,1*4A`. Lines that fail the checksum are dropped, which also discards the ESP32 ROM boot log that appears on the same pin at every reset. Fields are comma-separated.

## Dial to Pi

| Message | Meaning |
| --- | --- |
| `HELLO,currawong-knob,<version>` | Reply to `PING`. Current firmware reports version 6 |
| `POS,<position>,<delta>` | Detent position changed |
| `JOG,<-100..100>` | Deflection in elastic (shuttle) mode |
| `MODE,<index>,<name>` | Active mode changed |
| `GRAB` | A hand stopped a spin or roll |
| `OTA,…` | Firmware update replies (below) |

## Pi to dial

| Command | Effect |
| --- | --- |
| `PING` | Request `HELLO` |
| `MODE,<index>` | Select a preset mode |
| `CFG,<kind>,<positions>,<position>` | Configure a feel. `kind` is `list` (menu detents with end stops) or a preset mode name |
| `RANGE,<n>,<pos>` | Add walls after `n` positions (0 = endless) and set the position, without moving the detent grid |
| `MARK[,<p>,…]` | Landmark positions (up to 64) with stronger clicks. No arguments clears them |
| `MAG,<pos>` | Magnetic pull into `pos` from one position away. `-1` disables |
| `SPIN,<1\|-1>` | One full revolution forward or back |
| `ROLL,<mrev/s>` | Continuous rotation, e.g. `500` = half a turn per second. `0` stops |
| `LOCK,<1\|0>` | Pocket lock: motor off and no `POS` messages while locked |
| `TICK[,<n>[,<pct>]]` | One short click, or `n` stronger alert ticks (felt while locked). `pct` scales strength |
| `DIR,<1\|-1>` | Direction relative to the stored calibration |
| `STR,<0\|1\|2>` | Strength preset: 0.7×, 1.0×, 1.3× |
| `FEEL,<pct>` | Strength as a percentage of normal, clamped to 20–200 |

## Firmware update

The Pi cannot enter the ESP32 ROM bootloader (EN and BOOT are not wired to it), so firmware is sent over this link and written with the Arduino `Update` API.

| Step | Pi sends | Dial replies |
| --- | --- | --- |
| Start | `OTA,BEGIN,<size>,<md5>` | `OTA,READY` or `OTA,ERR,<reason>` |
| Data | `OTA,DATA,<seq>,<base64>` | `OTA,ACK,<seq>`, or `OTA,NAK,<expected seq>` to resend from an earlier chunk |
| Finish | `OTA,END` | `OTA,DONE`, then reboots into the new image, or `OTA,ERR,<reason>` |
| Cancel | `OTA,ABORT` | `OTA,ERR,aborted` |

- Chunks are 240 bytes (320 base64 characters), each acknowledged before the next. The Pi retries a chunk up to 5 times with a 3 s timeout.
- All other commands are ignored and the motor stays off during an update.
- The dial abandons the update after 20 s without traffic.
- The image goes to the inactive app partition and only becomes the boot image after the MD5 matches. Any failure leaves the running firmware in place.
