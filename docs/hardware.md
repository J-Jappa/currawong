# Hardware reference

Currawong v1.0. Design values from the PCB and datasheets. Where a value was set as a starting point before bring-up, it is marked as such.

## System

A Raspberry Pi Zero W runs audio, the UI, the display and the buttons. A Seeed XIAO ESP32-S3 runs the haptic dial and reports to the Pi over a 3.3 V UART. Everything runs from one 1S Li-ion cell through an Adafruit PowerBoost 1000C, which supplies a single 5 V rail rated at about 1 A. Every signal line on the board is 3.3 V.

| Part | Ref | Role | Connects to |
| --- | --- | --- | --- |
| Raspberry Pi Zero W | A1 | Audio, UI, display, buttons | Everything except the motor |
| Seeed XIAO ESP32-S3 | U3 | Haptic dial: FOC loop and encoder | Pi over UART |
| Adafruit Sharp 2.7" memory LCD | J1 | 400 × 240, 1-bit, mounted portrait | Pi SPI0 |
| SparkFun TMC6300 breakout | U4 | 3-phase motor driver | XIAO, six PWM lines |
| DFRobot 2804 motor with AS5600 | J2, J3 | Dial motor and angle sensor | TMC6300 (phases), XIAO I2C (sensor) |
| Adafruit MAX17048 breakout | U2 | Battery fuel gauge | Pi I2C1 |
| Adafruit PowerBoost 1000C | U1 | Charger and 5 V boost | Battery, USB-C (J5), switch (SW5) |
| Moondrop Dawn Pro 2 | off-board | USB DAC and headphone amp | Pi micro-USB OTG port |
| Five buttons, EC11 backup | SW1–SW6, J4 | Input | Pi GPIO |

## Raspberry Pi pin map

All button and encoder inputs are active-low with internal pull-ups.

| Pin | GPIO | Net | Use |
| --- | --- | --- | --- |
| 1, 17 | 3V3 | +3V3 | Display VIN and DISP, gauge VIN, EC11 VCC |
| 2, 4 | 5V | +5V | Power in from the PowerBoost |
| 3 | GPIO2 (SDA1) | M_SDA | MAX17048 |
| 5 | GPIO3 (SCL1) | M_SCL | MAX17048 |
| 8 | GPIO14 (TXD0) | RX | To XIAO D5 |
| 10 | GPIO15 (RXD0) | TX | From XIAO D6 |
| 11 | GPIO17 | RE_A | EC11 A (backup encoder, J4) |
| 13 | GPIO27 | RE_B | EC11 B (backup encoder, J4) |
| 15 | GPIO22 | Center | Centre button SW6, shared with the EC11 push switch |
| 19 | GPIO10 (MOSI) | D_DI | Display data |
| 23 | GPIO11 (SCLK) | D_CLK | Display clock |
| 29 | GPIO5 | BACK | Back button SW1 |
| 31 | GPIO6 | D_CS | Display chip select, **active-high**, software-driven |
| 33 | GPIO13 | PLAY/PAUSE | Play/Pause button SW3 |
| 35 | GPIO19 | NEXT | Next button SW2 |
| 37 | GPIO26 | PREVIOUS | Previous button SW4 |

- **SPI0:** `/dev/spidev0.0`, mode 0, 2 MHz. The kernel still toggles GPIO8 (CE0), so GPIO8 is reserved even though it is unconnected.
- **I2C1:** MAX17048 at 0x36.
- **UART0:** `/dev/serial0`, serial console disabled. The ESP32's ROM boot log arrives on this line at every XIAO reset, so the link protocol is framed and checksummed (see [dial-protocol.md](dial-protocol.md)).
- **Orientation:** the Pi mounts face-up with its header on the underside. Face-down mirrors the pinout and puts 5 V on the 3.3 V pin.

## XIAO ESP32-S3 pin map

| XIAO pin | GPIO | Net | Use |
| --- | --- | --- | --- |
| D0 | GPIO1 | UH | TMC6300 U high side |
| D1 | GPIO2 | UL | TMC6300 U low side |
| D2 | GPIO3 | VH | TMC6300 V high side (strapping pin, unaffected on a stock XIAO) |
| D3 | GPIO4 | VL | TMC6300 V low side |
| D4 | GPIO5 | – | Spare. Mode button input in standalone use (to GND) |
| D5 | GPIO6 | RX | UART receive, from Pi pin 8 |
| D6 | GPIO43 | TX | UART transmit, to Pi pin 10 (also the ROM boot-log pin) |
| D7 | GPIO44 | WL | TMC6300 W low side |
| D8 | GPIO7 | WH | TMC6300 W high side |
| D9 | GPIO8 | SDA | AS5600 |
| D10 | GPIO9 | SCL | AS5600 |
| 3V3 | – | – | AS5600 VCC and TMC6300 VIO |
| 5V | – | – | +5 V through a 1N5819 Schottky (about 4.6 to 4.7 V) |

- **Motor lines float at boot.** The first lines of `setup()` drive all six low before anything else.
- **UART0 is not used.** GPIO44 carries WL, so the default Arduino `Serial` would fight the motor driver. Debug logging goes over USB-CDC, and the Pi link is `Serial1` on RX = GPIO6, TX = GPIO43.
- **No reflash path from the Pi's pins.** Download mode needs EN, BOOT and GPIO44, none of which reach the Pi. Firmware updates therefore go over the application-level UART link into the ESP32's OTA partition.

## Power

1. The cell connects to one JST port on the MAX17048 breakout. A cable from its other port feeds the PowerBoost battery input.
2. USB-C (J5) feeds the PowerBoost VBUS for charging at up to about 1 A. The device runs while charging.
3. The slide switch (SW5) grounds the PowerBoost EN pin to cut the 5 V. Charging still works with the switch off.
4. The 5 V rail feeds Pi pins 2 and 4, the TMC6300 VIN, and the XIAO through the Schottky. C1 (470 µF) sits by the TMC6300 and C2 (100 µF) near the Pi.
5. The Pi's regulator supplies +3V3 for the display, gauge logic and EC11. The XIAO's regulator supplies a separate 3.3 V for the AS5600 and TMC6300 VIO, so the driver is in standby whenever the XIAO is unpowered.

**Estimated load at 5 V** (design estimates)

| Load | Typical | Peak |
| --- | --- | --- |
| Pi Zero W, streaming | 150–250 mA | ~300 mA |
| Dawn Pro 2 DAC | 50–100 mA | ~150 mA |
| XIAO running the FOC loop | 40–80 mA | ~100 mA |
| Display, gauge, AS5600 | < 15 mA | < 15 mA |
| Motor | 0 | 250–600 mA, set by the firmware voltage limit |

Feeding the Pi on pins 2 and 4 bypasses its input fuse. The PowerBoost LBO pin is unconnected, so charge state is inferred from the fuel gauge. Motor transients share the Pi's rail, so under-voltage can be checked with `vcgencmd get_throttled`.

<!-- WRITE: bring-up result. Did the 5 V rail hold with the motor on it, or did you do the planned rework
     (cut +5V at U4 pin 1 and feed the motor from the battery)? Measured currents if you have them. -->
> **[Jasper to add]** Measured bring-up results.

## Motor and haptics

DFRobot 2804 gimbal motor, 7 pole pairs, with a 12-bit AS5600 encoder, driven by a TMC6300 from the 5 V rail. The driver's SEN, DIAG and VCP pins are unconnected, so there is no current sensing or fault reporting. The firmware runs voltage-mode FOC and enforces its own limits:

| Limit | Value |
| --- | --- |
| Driver | `BLDCDriver6PWM`, non-zero dead zone |
| PWM frequency | 25 kHz |
| Supply voltage assumed | 5.0 V |
| Voltage limit | 2.0 V (2.5 V maximum without re-measuring the rail) |
| End stops | Full strength for 0.5 s, then taper to a third |
| Thermal | Running I²R estimate, derate above 1 W averaged over 10 s |
| Idle | Driver disabled 3 s after the dial stops, re-enabled on movement |
| Alignment | Run once at low voltage, zero angle and direction stored in flash |

Phase resistance measured 6.6 Ω lead to lead (3.3 Ω per phase, `PHASE_RESISTANCE` in the firmware). Stall current is roughly the voltage limit divided by phase resistance, about 0.6 A at 2.0 V. Spins, rolls and end stops all count towards the thermal estimate.

## Display

Adafruit 2.7" Sharp memory LCD, 400 × 240, 1-bit, mounted portrait. The UI draws 240 × 400 and rotates 90° or 270° (a setting) before sending. Only DI, CLK, CS, VIN, GND and DISP are wired.

- **Write protocol (spidev, MSB-first):** command byte `0x80` (plus `0x40` when VCOM is high), then per line: bit-reversed line address (1 to 240), 50 data bytes, `0x00`. A final `0x00` ends the frame. A 1 bit is white.
- **VCOM** must toggle at least once a second while powered. EMD and EIN are unwired, so this is done in software, either inside each write or with a 2-byte static command when nothing changed. The UI toggles it every 0.5 s.
- **DISP is tied high,** so blanking means writing a white frame.
- **Partial updates:** each panel line is one portrait column, so only changed columns are sent. A full frame takes about 50 ms at 2 MHz.

## Audio

Audio leaves through the Pi's micro-USB OTG port to the Dawn Pro 2 (ALSA card `PRO2`), which does volume in hardware. Software volume stays at 100%.

**Measured on the Pi Zero W**

| Process | CPU | Condition |
| --- | --- | --- |
| go-librespot | ~35% | Spotify at 320 kbps |
| MPD | < 10% | FLAC, visualiser output off |
| MPD | peaks ~20% | FLAC, visualiser FIFO on, soxr "quick" |

Consequences for the software:

- go-librespot runs at `Nice=-10`, the UI at `Nice=10`.
- Visualisers run only for local (MPD) playback and only while the screen is active.
- The DAC is exclusive, so when one source starts the other is paused. Both release the device on pause.
- MPD's FIFO output writes `22050:16:2` to `/tmp/currawong.fifo` for the analyser.
- Bluetooth shares the antenna with Wi-Fi. A Zero W gives SBC only.

## Battery and fuel gauge

The MAX17048 at 0x36 is the only source of battery information. Its INT and QSTART pins are unconnected, so the UI polls.

- The gauge is powered from the cell. A missing gauge means "unknown", never "empty", so a bench supply doesn't trigger shutdown.
- Registers: `0x02` VCELL (78.125 µV/bit), `0x04` SOC (high byte whole percent, big-endian), `0x16` CRATE (signed, ~0.208%/h per bit, positive when charging, used for the charging icon).
- Low battery: haptic alerts at 10% and 5%. Clean shutdown when charge stays at or below 5% for 60 s (unless charging), or the cell drops below the voltage cutoff (3.30 V default, adjustable 3.0–3.6 V).
