# currawong_knob

Haptic dial firmware for the Seeed XIAO ESP32-S3. Drives a DFRobot 2804 gimbal motor through a TMC6300 with voltage-mode FOC (SimpleFOC 2.3.3), reads an AS5600 encoder, and takes its feel from the Pi over UART. It also runs standalone, cycling through test modes from a button.

Haptic model after [SmartKnob](https://github.com/scottbez1/smartknob).

## Build and flash

Requires [PlatformIO](https://platformio.org).

```sh
pio run -t upload        # first flash, over the XIAO's USB-C
pio device monitor       # USB-CDC log at 115200; type ? for commands
```

After the first flash, updates go over the Pi link: `sudo bash currawong_setup.sh --update-dial firmware.bin` (see [docs/setup.md](../../docs/setup.md)). The image is written to the spare OTA partition and MD5-checked before it becomes the boot image.

SimpleFOC is pinned to 2.3.3, the last release that builds on PlatformIO's Arduino-ESP32 2.x core.

## Layout

| Path | Contents |
| --- | --- |
| `src/main.cpp` | Motor setup, FOC loop, haptic modes, UART protocol, OTA receiver, power management |
| `src/feel.h` | Detent, wall, coast, landmark and magnet torque maths, hardware-independent |
| `test/test_feel.cpp` | Desktop tests for `feel.h` |

```sh
g++ -std=c++17 -Isrc test/test_feel.cpp && ./a.out
```

## Behaviour

- **Detents and walls:** spacing and strength per mode, end stops that hold full strength for 0.5 s then taper to a third.
- **Coast:** on endless ranges and lists longer than 16, clicks fade out above about one turn per second so a flick glides, and return as it slows.
- **Landmarks:** selected positions get a click twice as strong, still felt while coasting.
- **Magnet:** pulls the knob into a target position from one detent away, with neighbouring detents weakened.
- **Spin and roll:** a full revolution on track skip, and continuous rotation during auto-scrub. Grabbing the knob cancels a roll.
- **Pocket lock:** motor off and positions suppressed until unlocked.
- **Calibration:** run once (tries 1.0, 1.5 then 2.0 V), stored in flash with the motor direction, skipped on later boots.
- **Power:** driver disabled 3 s after the knob stops. While idle the CPU drops to 80 MHz and the sensor is polled every 10 ms.
- **Thermal:** running I²R estimate of motor power, all effects derated above 1 W average.

The Pi-side commands are documented in [docs/dial-protocol.md](../../docs/dial-protocol.md).

## Standalone controls

| Input | Action |
| --- | --- |
| BOOT (or D4 to GND), short press | Next mode |
| BOOT, hold 3 s | Re-run calibration (keeps the previous one if all attempts fail) |
| Monitor: `s <volts>` | Detent strength for the current mode |
| Monitor: `w <deg>` | Detent spacing for the current mode |
| Monitor: `r` | Reverse direction (saved) |

Tuning changes last until the mode changes. Copy values worth keeping into `PRESETS` in `main.cpp`. Other tuning constants: `COAST_FROM`, `COAST_TO`, `MARK_GAIN`, `MAGNET_VOLTS`, `LOW_POWER_IDLE`.
