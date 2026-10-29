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

<!-- WRITE: print settings. Prompts: material per part (e.g. clear PETG front, black back, white dial and buttons),
     layer height, orientation (front printed face-down without supports?), screws used, any post-processing. -->
> **[Jasper to add]** Materials and print settings.
