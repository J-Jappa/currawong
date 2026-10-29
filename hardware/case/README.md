# Case

Modelled in SolidWorks. `stl/` holds the print-ready parts, `solidworks/` the sources and the full assembly.

| Part | STL | Source |
| --- | --- | --- |
| Front case | `Front Case v3.STL` | `Front Case v3.SLDPRT` |
| Back case | `Back Case v4.STL` | `Back Case v2.SLDPRT` |
| Dial | `Dial v2.STL` | `Dial v2.SLDPRT` |
| Buttons (Back, Centre, Next, Play, Previous) | `Button *.STL` | `Button *.SLDPRT` |
| Power slide | `Slide.STL` | `Slide.SLDPRT` |
| Slide fix | `Bad Slide Fix.STL` | `Bad Slide Fix.SLDPRT` |

`Display`, `Dac`, `Battery` and `Slide Switch` are simplified models of bought parts, used for fit in the assembly.

The assembly (`Currawong assembly.SLDASM`) also uses third-party models that are not redistributed here: Raspberry Pi Zero, DFRobot 2804 motor and AS5600, Adafruit PowerBoost 1000C and MAX17048, Seeed XIAO ESP32-S3, SparkFun TMC6300, a USB-C breakout and McMaster-Carr screws. Those components show as missing unless the vendor models are supplied. The PCB is available as a STEP export in [../pcb](../pcb).

## Fasteners

| Qty | Part |
| --- | --- |
| 6 | M3 threaded inserts |
| 6 | M3 × 12 mm countersunk screws |
| 2 | M3 × 8 mm socket head screws |
| 2 | M3 nuts |
| 6 | M2.5 × 8 mm countersunk screws |

## Printing

Everything is PETG for its temperature resistance: a clear front, a black back case and white buttons. All parts were printed with a 0.4 mm nozzle.

| Part | Material | Layer height | Orientation |
| --- | --- | --- | --- |
| Front case | Clear PETG | 0.16 mm | Face down |
| Back case | Black PETG | 0.12 mm | On its side, with a PLA support interface |
| Buttons | White PETG | 0.12 mm | |

- The front uses 10% rectilinear infill, which gives it more of a frosted glass-brick look.
- Extrusion settings were tuned for transparency. Getting these right takes a bit of research and some trial and error.
- Post-processing was some light sanding and clean-up.
