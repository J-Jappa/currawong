#!/usr/bin/env python3
"""Currawong UI v10.

Two sources, chosen automatically by whichever is playing:

  SPOTIFY  (go-librespot)  simple screen: big cover, track info, progress.
  LOCAL    (MPD)           Now Playing with spectrum strips, plus the SCOPE page.

Menus: Library (artists, albums, playlists, up next), Spotify pinned playlists,
track options (shuffle, repeat, add to playlist), Bluetooth pairing, Wi-Fi
joining with an on-screen keyboard, audio output (wired or Bluetooth),
settings, about and power off.

Input: buttons and the EC11 on GPIO, plus the ESP32 haptic dial over UART
when it's connected (it then gets a feel for each screen: list detents with
end stops in menus, light scrub clicks on Now Playing). The dial spins a turn on
a track skip, rolls while you auto-scrub (hold next or previous), and can be
pocket-locked until the centre button is pressed.

Settings > Visuals: FULL (strips + Scope), REGULAR (strips, no Scope) or FAST
(no visuals, the lightest on a single-core Pi Zero W).

Rendering is event-driven: a screen is only redrawn when something on it
changes. Animation only runs for local playback in Active mode.

On the Pi:        python3 currawong_ui.py
Preview anywhere: python3 currawong_ui.py --preview   (writes PNGs, no hardware)
Profile on the Pi: python3 currawong_ui.py --profile 60   (writes ~/currawong/profile.txt)
"""
import atexit
import http.client
import json
import math
import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from io import BytesIO

import struct

CACHE_DIR = os.path.expanduser("~/.cache/currawong")
EARLY_SETTINGS_FILE = os.path.expanduser("~/.config/currawong/settings.json")
BOOT_WINDOW_SECS = 180   # the animation plays at boot; a restart later just shows the last frame


def splash_file(rot):
    return os.path.join(CACHE_DIR, f"splash_{rot}.bin")


def boot_anim_file(rot):
    return os.path.join(CACHE_DIR, f"boot_anim_{rot}.bin")


def _saved_rotation():
    """The screen's rotation from settings.json, read with nothing but the standard library."""
    try:
        with open(EARLY_SETTINGS_FILE) as f:
            saved = json.load(f)
        if saved.get("settings_v", 1) < 2:  # from before 270 became the default (see load_settings)
            return 270
        return 90 if saved.get("rotate") == 90 else 270
    except Exception:  # noqa: BLE001
        return 270


def _system_uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 1e9


def _read_boot_anim(path):
    """(fps, [packet]) from the panel-ready cache that build_boot_anim() writes."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"CRWBOOT1":
        raise ValueError("not a boot animation")
    fps, count = struct.unpack_from("<HI", data, 8)
    off, packets = 14, []
    for _ in range(count):
        (n,) = struct.unpack_from("<I", data, off)
        off += 4
        packets.append(bytearray(data[off:off + n]))
        off += n
    return fps, packets


class _BootAnimation(threading.Thread):
    """Plays the boot animation once, straight to the panel, then holds its last frame (keeping
    VCOM toggling, as the panel needs) until main() takes the display over. Each packet is one
    frame's changed panel lines, built by build_boot_anim(), so this needs no PIL."""

    def __init__(self, fps, packets, spi, lg, h):
        super().__init__(name="boot-anim", daemon=True)
        self.fps, self.packets, self.spi, self.lg, self.h = fps, packets, spi, lg, h
        self.vcom, self.flip_at = False, 0.0
        self.played = threading.Event()   # set once the last frame is up
        self.release = threading.Event()  # set by main() to take over the display

    def _vbit(self, now, force=False):
        if force or now >= self.flip_at:  # VCOM flips twice a second
            self.vcom, self.flip_at = not self.vcom, now + 0.5
        return 0x40 if self.vcom else 0x00

    def _send(self, buf):
        self.lg.gpio_write(self.h, 6, 1)
        self.spi.writebytes2(buf)
        self.lg.gpio_write(self.h, 6, 0)

    def run(self):
        try:
            start, dt = time.monotonic(), 1.0 / self.fps
            for i, pkt in enumerate(self.packets):
                now = time.monotonic()
                if pkt:
                    pkt[0] = 0x80 | self._vbit(now)
                    self._send(pkt)
                elif now >= self.flip_at:
                    self._send(bytes([self._vbit(now), 0]))
                wait = start + (i + 1) * dt - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
        except Exception:  # noqa: BLE001 - a hiccup must never stop the UI starting
            pass
        finally:
            self.played.set()
        while not self.release.wait(0.5):
            try:
                self._send(bytes([self._vbit(time.monotonic(), force=True), 0]))
            except Exception:  # noqa: BLE001
                break


_BOOT = None  # the boot animation while it plays; main() takes the display over from it


def _early_display():
    """The moment the service starts, before the slow imports below (PIL and the rest of this
    file): at boot, play the animation; on a later restart, show its last frame. Both replay
    bytes saved by an earlier run. Any problem: skip quietly, main() draws the splash itself."""
    global _BOOT
    rot = _saved_rotation()
    anim = frame = None
    if _system_uptime() < BOOT_WINDOW_SECS:
        try:
            anim = _read_boot_anim(boot_anim_file(rot))
        except Exception:  # noqa: BLE001 - not built yet (first run): the still splash instead
            anim = None
    try:
        if anim is None:
            with open(splash_file(rot), "rb") as f:
                frame = f.read()
        import lgpio
        import spidev
        h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(h, 6, 0)  # CS_PIN, active-high
        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz, spi.mode = 2_000_000, 0
        if anim is not None:
            _BOOT = _BootAnimation(anim[0], anim[1], spi, lgpio, h)
            _BOOT.start()
            return
        try:
            lgpio.gpio_write(h, 6, 1)
            spi.writebytes2(frame)
            lgpio.gpio_write(h, 6, 0)
            spi.close()
        finally:
            lgpio.gpio_free(h, 6)
            lgpio.gpiochip_close(h)
    except Exception:  # noqa: BLE001 - a missing logo must never stop the UI starting
        pass


if __name__ == "__main__" and os.environ.get("CURRAWONG_EARLY_SPLASH") == "1":
    _early_display()


class _LazyNumpy:
    """numpy takes seconds to load on a Zero W and the first screen doesn't need it, so it loads
    on first use (main() starts that in the background once the first screen is up)."""

    def __getattr__(self, name):
        global np
        try:
            import numpy
        except ImportError:
            sys.exit("numpy is missing: sudo apt install -y python3-numpy")
        np = numpy
        return getattr(numpy, name)


np = _LazyNumpy()

from PIL import Image, ImageDraw, ImageFont, ImageOps

# ======================================================================
# Settings
# ======================================================================
API_HOST, API_PORT = "127.0.0.1", 3678   # go-librespot (it listens on 127.0.0.1 only)
API = f"http://{API_HOST}:{API_PORT}"
MPD_HOST, MPD_PORT = "localhost", 6600
MPD_FIFO = "/tmp/currawong.fifo"         # MPD fifo output, format "22050:16:2" (the old way)
LOOPBACK_CAPTURE = "hw:Loopback,1,0"     # the Visualiser output's copy, via the ALSA loopback (snd-aloop)
RATE = 22050
MPD_ANY_SONG = '(file != "")'          # an MPD filter matching every song (with "window 0:1": just one)
MPD_OUTPUT_VIS = "Visualiser"            # MPD's output feeding the loopback (switched on only when needed)
VIS_OFF_DELAY = 10.0      # the analyser idle this long: switch MPD's Visualiser output off
TAP_POLL = 0.1            # capture reads check every this long whether they're still wanted
TAP_STALL_SECS = 4.0      # no samples this long while wanted: restart the capture

ROTATE = 270              # 90 or 270, depending on which way the panel sits
FPS_ACTIVE = 5            # local Now Playing frame rate while Active
FPS_SCOPE = 1             # Scope page frame rate while Active (it redraws everything)
VISUALS = ["FULL", "REGULAR", "FAST"]  # Settings > Visuals: strips + Scope / strips only / none
SCRUB_SPEEDS = [4, 8, 16, 32]   # auto-scrub: times normal speed; each extra press goes up one
ROLL_RATES = [0.25, 0.45, 0.7, 1.0]   # dial turns per second at each of those speeds
SCRUB_TICK = {"local": 0.25, "spotify": 0.6}   # seconds between jumps (Spotify rebuffers on each)
SHUTTLE_GEARS = [(12, 0), (30, 2), (50, 4), (70, 8), (88, 16)]  # shuttle: twist (of 100) below -> speed; beyond: 32x
RANGE_SYNC_SECS = 2.0     # how often the dial's song walls follow playback while it's left alone
BATTERY_MAH = 2000        # default capacity, for the drain estimate (Settings > Advanced > Battery)
WIFI_SAVER_SECS = 300     # Wi-Fi saver: radio off after this long without Spotify playing
ENC_REVERSE = False       # flip if the encoder turns the wrong way
SPOTIFY_FORMAT = "OGG 320k"
UI_BUILD = "2026-10-24l"   # shown in ABOUT, logged at start, and by --version: which file is running
SERIAL_PORT, SERIAL_BAUD = "/dev/serial0", 115200   # ESP32 haptic dial link
LIBRESPOT_CONFIG = os.path.expanduser("~/.config/go-librespot/config.yml")
WIRED_DEVICE = "currawong_dac"    # /etc/asound.conf: the DAC, through the equaliser when installed
BT_DEVICE = "bt"                 # ALSA PCM for bluez-alsa, defined in /etc/asound.conf
DAC_CARD = "PRO2"                # the DAC's ALSA card ID, for its hardware volume (if it has one)
MPD_OUTPUT_WIRED, MPD_OUTPUT_BT = "DAC", "Bluetooth"
PINS_FILE = os.path.expanduser("~/.config/currawong/spotify_pins.json")

PINS = dict(enc_a=17, enc_b=27, enc_sw=22, back=5, play=13, next=19, prev=26)
CS_PIN = 6
LBO_PIN = None            # set to 16 once PowerBoost LBO is wired (10k series resistor)
LOW_BATT_SHUTDOWN_SECS = 60   # charge at or below BATT_SOC_LOW this long: switch off (unless surely charging)
BATT_SOC_LOW = 5               # %
BATT_SOC_TRUST_VOLTS = 3.80    # a charge that low while the cell is above this isn't believed (gauge settling)
BATT_CUTOFF_VOLTS = 3.30       # default voltage cutoff (setting "cutoff_volts", 3.0-3.6)
BATT_VOLT_HOLD_SECS = 30       # the cell's median voltage below the cutoff this long: switch off
BATT_CRITICAL_VOLTS = 3.10     # below this the boost is near its limit: switch off sooner
BATT_CRITICAL_HOLD_SECS = 5
BATT_VOLT_WINDOW_SECS = 12     # the median's window (a motor kick lasts a fraction of a second)
BATT_VOLT_HYST = 0.05          # the voltage timer clears only this far above the cutoff
BATT_STALE_SECS = 60           # a gauge silent this long: level unknown
BATT_VOLTS_SANE = (2.5, 4.6)   # readings outside this are glitches

ANALYSIS_FPS = 5
NFFT = 2048
HISTORY = 50              # analysis frames kept (10 s at 5 fps)

LOGO_FILE = os.path.expanduser("~/currawong/logo.png")  # optional: replaces the placeholder logo
SETTINGS_FILE = os.path.expanduser("~/.config/currawong/settings.json")
DEFAULT_SETTINGS = {"active_secs": 30, "sleep_secs": 180, "output": "wired",
                    "haptics": 1, "feel": 100, "rotate": ROTATE, "auto_off_mins": 30,
                    "visuals": 1, "scrub_step": 2, "skip_spin": 1, "pocket_secs": 0, "dial_dir": 1,
                    "scrub_style": "clicks", "wifi_saver": 0,
                    "eq": {"preset": "FLAT", "gains": [0, 0, 0, 0, 0]}, "resume": "ready", "click": 30,
                    "battery_mah": BATTERY_MAH, "cutoff_volts": BATT_CUTOFF_VOLTS}
POLL_EVERY = {"active": 1.0, "glance": 1.0, "sleep": 5.0}

STATUS = {"wifi": None, "radio": True, "output": "wired", "locked": False, "scrub": ""}   # shared with every screen

W, H = 240, 400           # portrait canvas
PANEL_W, PANEL_H = 400, 240
BLACK, WHITE = 0, 1

_T = getattr(Image, "Transpose", Image)
_R = getattr(Image, "Resampling", Image)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def _font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        return ImageFont.load_default()


F_TINY = _font("DejaVuSansMono.ttf", 10)
F_SMALL = _font("DejaVuSansMono.ttf", 11)
F_BODY = _font("DejaVuSansMono.ttf", 13)
F_BOLD = _font("DejaVuSansMono-Bold.ttf", 13)
F_SBOLD = _font("DejaVuSansMono-Bold.ttf", 11)
F_BIG = _font("DejaVuSansMono-Bold.ttf", 16)
F_HUGE = _font("DejaVuSansMono-Bold.ttf", 20)


# ======================================================================
# Text and drawing helpers
# ======================================================================
def tlen(font, text):
    return font.getlength(text)


def fit(text, font, max_w):
    """Trim text with an ellipsis so it fits in max_w pixels."""
    if tlen(font, text) <= max_w:
        return text
    while text and tlen(font, text + "…") > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(text, font, max_w, max_lines):
    """Greedy word wrap into at most max_lines, ellipsis on overflow."""
    words = text.split()
    lines, i = [], 0
    while i < len(words) and len(lines) < max_lines:
        line = words[i]
        i += 1
        while i < len(words) and tlen(font, line + " " + words[i]) <= max_w:
            line += " " + words[i]
            i += 1
        lines.append(line)
    if i < len(words) and lines:
        lines[-1] = fit(lines[-1] + " " + " ".join(words[i:]), font, max_w)
    return [fit(line, font, max_w) for line in lines]


_TEXT_CACHE = {}


def text_mask(text, font):
    """1-bit mask of rendered text, cached. Rendering glyphs is the slow part on a Zero W."""
    key = (text, id(font))
    m = _TEXT_CACHE.get(key)
    if m is None:
        if len(_TEXT_CACHE) > 3000:
            _TEXT_CACHE.clear()
        x0, y0, x1, y1 = font.getbbox(text) if text else (0, 0, 0, 0)
        m = Image.new("1", (max(1, int(x1) + 1), max(1, int(y1) + 1)), 0)
        if text:
            ImageDraw.Draw(m).text((0, 0), text, font=font, fill=1)
        _TEXT_CACHE[key] = m
    return m


class CDraw:
    """ImageDraw wrapper whose text() pastes cached glyph masks (identical output, much faster)."""

    def __init__(self, img):
        self.img = img
        self.d = ImageDraw.Draw(img)

    def text(self, xy, text, font=None, fill=BLACK, **kw):
        if self.img.mode != "1" or kw:
            return self.d.text(xy, text, font=font, fill=fill, **kw)
        m = text_mask(text, font)
        self.img.paste(fill, (int(round(xy[0])), int(round(xy[1]))), m)

    def __getattr__(self, name):
        return getattr(self.d, name)


def Draw(img):
    return CDraw(img)


def label(d, x, y, text, font=None):
    """Small label on a white plate, drawn over busy content."""
    font = font or F_TINY
    tw = tlen(font, text)
    d.rectangle((x - 1, y - 1, x + tw + 1, y + 11), fill=WHITE)
    d.text((x, y), text, font=font, fill=BLACK)


def centred(d, cx, y, text, font):
    d.text((cx - tlen(font, text) / 2, y), text, font=font, fill=BLACK)


def hatch(w, h, step=5):
    im = Image.new("1", (w, h), WHITE)
    d = ImageDraw.Draw(im)
    for x in range(-h, w + h, step):
        d.line((x, h - 1, x + h, 0), fill=BLACK, width=2)
    d.rectangle((0, 0, w - 1, h - 1), outline=BLACK)
    return im


def dots(w, h):
    im = Image.new("1", (w, h), WHITE)
    px = im.load()
    for y in range(1, h, 4):
        for x in range(1 + ((y // 4) % 2) * 2, w, 4):
            px[x, y] = BLACK
    return im


def titled(title):
    img = Image.new("1", (W, H), WHITE)
    d = Draw(img)
    d.rectangle((0, 0, W - 1, 27), fill=BLACK)
    d.text((8, 6), title, font=F_BIG, fill=WHITE)
    return img, d


_TWIG = []


def twig(img, frac):
    """The progress bar as the bird's twig (currawong_bird.draw_twig); the plain bar if the bird's
    module isn't there. Returns the playhead's x."""
    if not _TWIG:
        try:
            from currawong_bird import draw_twig
            _TWIG.append(draw_twig)
        except ImportError:
            _TWIG.append(None)
    if _TWIG[0]:
        try:
            return _TWIG[0](img, frac)
        except Exception:  # noqa: BLE001
            _TWIG[0] = None    # the plain bar from now on
    d = Draw(img)
    d.rectangle((8, 338, W - 9, 344), outline=BLACK)
    end = 9 + int((W - 19) * max(0.0, min(1.0, frac)))
    d.rectangle((9, 339, end, 343), fill=BLACK)
    return end


BIRD_TICK = 0.125   # the currawong's tick: 8 fps (currawong_bird.TICK)
PAUSED_IDLE_SECS = 60   # paused this long with nothing touched: Home shows the bird's branch


def bird_columns(boxes):
    """Merge boxes (x0, y0, x1, y1) into the column ranges the display refreshes."""
    spans = sorted((max(0, int(b[0])), min(W, int(b[2]))) for b in boxes if b)
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        elif a < b:
            out.append((a, b))
    return out


def mmss(ms):
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def dither(raw, size):
    """Album art -> 1-bit, Floyd-Steinberg dithered, with an auto-contrast pass."""
    im = Image.open(BytesIO(raw))
    im.draft("L", (size * 2, size * 2))  # fast JPEG decode at reduced scale
    im = im.convert("L")
    im = ImageOps.fit(im, (size, size), _R.LANCZOS)
    im = ImageOps.autocontrast(im, cutoff=2)
    return im.convert("1")


def make_covers(raw):
    return {"small": dither(raw, 104), "large": dither(raw, 240)}


def stipple_mask(h, w):
    """Diamond dot lattice used for stippled fills."""
    y, x = np.mgrid[0:h, 0:w]
    return ((y % 4 == 0) & (x % 4 == 0)) | ((y % 4 == 2) & (x % 4 == 2))


# ======================================================================
# Logo and power screens
# ======================================================================
def placeholder_logo(size):
    """Simple bird mark, drawn large in greyscale then reduced for clean 1-bit edges."""
    S = size * 4
    im = Image.new("L", (S, S), 255)
    d = ImageDraw.Draw(im)
    u, c = S / 100, S / 2

    def p(x, y):
        return (c + x * u, c + y * u)

    w2, w3 = max(1, int(2 * u)), max(1, int(3 * u))
    d.ellipse([p(-46, -46), p(46, 46)], outline=0, width=w3)
    d.polygon([p(-20, 4), p(-44, 24), p(-38, 30), p(-16, 14)], fill=0)        # tail
    d.ellipse([p(-26, -8), p(20, 18)], fill=0)                                  # body
    d.ellipse([p(10, -22), p(32, 0)], fill=0)                                   # head
    d.polygon([p(29, -15), p(45, -8), p(29, -6)], fill=0)                       # beak
    d.ellipse([p(-6, 0), p(8, 8)], fill=255)                                    # wing patch
    d.polygon([p(-41, 25), p(-44, 24), p(-38, 30), p(-35, 28)], fill=255)       # tail tip
    d.ellipse([p(20, -15), p(26, -9)], fill=255)                                # eye
    d.line([p(-2, 17), p(-4, 30)], fill=0, width=w2)                            # legs
    d.line([p(6, 17), p(6, 30)], fill=0, width=w2)
    d.line([p(-30, 30), p(34, 30)], fill=0, width=w2)                           # perch
    im = im.resize((size, size), _R.LANCZOS)
    return im.point(lambda v: 255 if v > 128 else 0).convert("1")


def logo_image(size):
    """Your own logo from LOGO_FILE if it exists, otherwise the placeholder."""
    try:
        im = Image.open(LOGO_FILE).convert("L")
        im = ImageOps.contain(im, (size, size), _R.LANCZOS)
        return im.convert("1")
    except Exception:
        return placeholder_logo(size)


ANIM_FILE = os.path.expanduser("~/currawong/currawong_boot.anim")
_INVERT = bytes(255 - i for i in range(256))   # the animation's 1 = ink; PIL's 1 = white
_HOLD = None


def read_anim_asset(path=None, frames=True):
    """(meta, hold frame bytes, all frames bytes or None) from currawong_boot.anim."""
    import zlib
    with open(path or ANIM_FILE, "rb") as f:
        data = f.read()
    if data[:8] != b"CRWANIM1":
        raise ValueError("not a Currawong animation")
    (n,) = struct.unpack_from("<I", data, 8)
    meta = json.loads(data[12:12 + n])
    off = 12 + n
    hold = data[off:off + meta["bpf"]]
    return meta, hold, (zlib.decompress(data[off + meta["bpf"]:]) if frames else None)


def anim_image(raw, meta):
    return Image.frombytes("1", (meta["w"], meta["h"]), raw.translate(_INVERT))


def hold_image():
    """The animation's last frame: the currawong on the wordmark, the personal mark below. None
    without the animation file."""
    global _HOLD
    if _HOLD is None:
        try:
            meta, hold, _ = read_anim_asset(frames=False)
            _HOLD = anim_image(hold, meta)
        except Exception:  # noqa: BLE001
            _HOLD = False
    return _HOLD or None


def brand_parts():
    """(lockup, mark) cut from the last frame: the bird on the wordmark, and the personal mark."""
    hold = hold_image()
    if hold is None:
        return None, None
    ink = ImageOps.invert(hold.convert("L"))
    top = ink.crop((0, 0, W, H * 3 // 5)).getbbox()
    low = ink.crop((0, H * 3 // 5, W, H)).getbbox()
    lockup = hold.crop(top) if top else None
    mark = hold.crop((low[0], low[1] + H * 3 // 5, low[2], low[3] + H * 3 // 5)) if low else None
    return lockup, mark


def splash_screen():
    """What shows while the UI starts: the animation's last frame, or the drawn logo without it."""
    hold = hold_image()
    return hold.copy() if hold is not None else logo_screen()


def logo_screen(text=None, sub=None, note=()):
    img = Image.new("1", (W, H), WHITE)
    d = Draw(img)
    logo = logo_image(140)
    img.paste(logo, ((W - logo.width) // 2, 100))
    if text:
        centred(d, W / 2, 262, text, F_BIG)
    if sub:
        centred(d, W / 2, 286, sub, F_SMALL)
    for n, line in enumerate(note):
        centred(d, W / 2, 330 + n * 16, line, F_SMALL)
    return img


def powered_off_screen(reason=None):
    # The Pi has halted, but the boost converter still powers it until the switch goes off.
    notes = ("Slide the switch off.", "Switch on again to start.")
    hold = hold_image()
    if hold is None:
        return logo_screen("POWERED OFF", reason, notes)
    img = hold.copy()   # the lockup sits above, the mark below; the words go in the gap and under
    d = Draw(img)
    centred(d, W / 2, 230, "POWERED OFF", F_BIG)
    if reason:
        centred(d, W / 2, 252, reason, F_SMALL)
    for n, line in enumerate(notes):
        centred(d, W / 2, 346 + n * 16, line, F_SMALL)
    return img


def system_stopping():
    """True while the whole system is shutting down (not just this service restarting)."""
    try:
        out = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        return out == "stopping"
    except Exception:
        return False


# ======================================================================
# Settings persistence
# ======================================================================
SETTINGS_V = 2   # 2: the panel now sits the other way, so 270 is the default rotation


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        if saved.get("settings_v", 1) < 2:
            saved["rotate"] = 270  # saved before the panel was turned round
        if "feel" not in saved:   # from when the dial's strength was LIGHT/MEDIUM/STRONG
            saved["feel"] = {0: 70, 1: 100, 2: 130}.get(saved.get("haptics", 1), 100)
        s.update(saved)
    except Exception:
        pass
    s["settings_v"] = SETTINGS_V
    return s


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass


# ======================================================================
# Display driver (Sharp memory LCD over spidev)
# ======================================================================
def _rev(b):
    return int(f"{b:08b}"[::-1], 2)


class _LgpioPin:
    """An output pin straight through lgpio: much quicker to set up than gpiozero on a Zero W,
    which matters because it's the first thing the UI does."""

    def __init__(self, pin):
        import lgpio
        self.lg, self.pin = lgpio, pin
        self.h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self.h, pin, 0)

    @classmethod
    def adopt(cls, lg, h, pin):
        """Take over a pin the boot animation already claimed."""
        p = cls.__new__(cls)
        p.lg, p.h, p.pin = lg, h, pin
        return p

    def on(self):
        self.lg.gpio_write(self.h, self.pin, 1)

    def off(self):
        self.lg.gpio_write(self.h, self.pin, 0)


class SharpDisplay:
    def __init__(self, spi=None, cs=None):
        if spi is None:
            import spidev

            spi = spidev.SpiDev()
            spi.open(0, 0)
            spi.max_speed_hz = 2_000_000
            spi.mode = 0
            try:
                cs = _LgpioPin(CS_PIN)
            except Exception:  # noqa: BLE001 - no lgpio: gpiozero does the same job, just slower
                from gpiozero import DigitalOutputDevice
                cs = DigitalOutputDevice(CS_PIN, active_high=True, initial_value=False)
        self.spi, self.cs = spi, cs
        self.addr = [_rev(n) for n in range(1, PANEL_H + 1)]  # LSB-first line addresses
        self.stride = PANEL_W // 8
        self.vcom = False
        self.last = None
        self.set_rotation(ROTATE)

    def set_rotation(self, deg):
        rot = 270 if deg == 270 else 90
        if getattr(self, "rot", None) == rot:
            return
        self.rot = rot
        self.transpose = _T.ROTATE_90 if self.rot == 90 else _T.ROTATE_270
        self.last = None  # next frame is sent whole

    def _send(self, buf):
        self.cs.on()
        self.spi.writebytes2(buf)
        self.cs.off()

    def _vbit(self):
        self.vcom = not self.vcom
        return 0x40 if self.vcom else 0x00

    def frame_bytes(self, img):
        """Every line of `img` as one write command (VCOM low): what the early splash replays."""
        data = img.transpose(self.transpose).tobytes()
        s = self.stride
        buf = bytearray([0x80])
        for y in range(PANEL_H):
            buf.append(self.addr[y])
            buf += data[y * s:(y + 1) * s]
            buf.append(0)
        buf.append(0)
        return bytes(buf)

    def vcom_only(self):
        """Nothing changed: just toggle VCOM (2 bytes)."""
        self._send(bytearray([self._vbit(), 0x00]))

    def show(self, img):
        """Full frame: send only changed panel lines, or just toggle VCOM."""
        vbit = self._vbit()
        data = img.transpose(self.transpose).tobytes()
        s = self.stride
        if self.last is None:
            lines = range(PANEL_H)
        else:
            last = self.last
            lines = [y for y in range(PANEL_H)
                     if data[y * s:(y + 1) * s] != last[y * s:(y + 1) * s]]
        self.last = bytearray(data)
        if lines:
            buf = bytearray([0x80 | vbit])
            for y in lines:
                buf.append(self.addr[y])
                buf += data[y * s:(y + 1) * s]
                buf.append(0)
            buf.append(0)
        else:
            buf = bytearray([vbit, 0x00])
        self._send(buf)

    def show_columns(self, img, ranges):
        """Fast path: send only the given portrait column ranges [(x0, x1), ...]."""
        if self.last is None:
            self.show(img)
            return
        s = self.stride
        buf = bytearray([0x80 | self._vbit()])
        for x0, x1 in ranges:
            part = img.crop((x0, 0, x1, H)).transpose(self.transpose).tobytes()
            base = (W - x1) if self.rot == 90 else x0
            for r in range(x1 - x0):
                row = base + r
                line = part[r * s:(r + 1) * s]
                self.last[row * s:(row + 1) * s] = line
                buf.append(self.addr[row])
                buf += line
                buf.append(0)
        buf.append(0)
        self._send(buf)

    def show_changed(self, img, ranges):
        """The bird's fast path: look only at the given portrait column ranges [(x0, x1), ...]
        and send just the panel lines in them that changed (each panel line is one portrait
        column). Nothing changed: just toggle VCOM."""
        if self.last is None:
            self.show(img)
            return
        s = self.stride
        vbit = self._vbit()
        buf = bytearray([0x80 | vbit])
        sent = 0
        for x0, x1 in ranges:
            part = img.crop((x0, 0, x1, H)).transpose(self.transpose).tobytes()
            base = (W - x1) if self.rot == 90 else x0
            for r in range(x1 - x0):
                row = base + r
                line = part[r * s:(r + 1) * s]
                if self.last[row * s:(row + 1) * s] != line:
                    self.last[row * s:(row + 1) * s] = line
                    buf.append(self.addr[row])
                    buf += line
                    buf.append(0)
                    sent += 1
        if sent:
            buf.append(0)
            self._send(buf)
        else:
            self._send(bytearray([vbit, 0x00]))

    def clear(self):
        self.last = None
        self.show(Image.new("1", (W, H), WHITE))


# ======================================================================
# Battery: MAX17048 fuel gauge if fitted, else the PowerBoost LBO pin
# ======================================================================
class ChargeDetector:
    """Notices the charger going in or out well before the gauge's own charge rate does (that's
    worked out from slow changes in its state-of-charge estimate, and lags by minutes).

    Plugging in lifts the cell voltage at once, and unplugging drops it. But the voltage also
    steps up when the player's own load eases off (music stopping, the screen sleeping, the
    boot rush settling), so a step is a guess until it's checked: charging keeps lifting the
    voltage at a steady pace for minutes, while a cell recovering from a load slows right down.
      - a big step (SHOW_V) shows at once, and is checked;
      - a smaller one (GUESS_V) shows only once the check confirms it;
      - nothing counts in the first couple of minutes after the UI starts (the boot settling);
      - the gauge's rate counts as charging only above RATE_ON: a resting cell drifts the
        estimate up by a percent or two an hour, which used to read as charging after boot.
    Every decision, and steps too small to count, go to the journal:
        journalctl -u currawong-ui | grep Charging"""
    SHOW_V = 0.12
    GUESS_V = 0.06
    NOTE_V = 0.03          # smaller than GUESS_V: only logged, so the thresholds can be tuned
    BIG_STEP_V = 0.18      # this big counts even in the quiet spell after a false alarm
    SETTLE_S = 120
    RATE_ON = 3.0          # %/h; real charging is 10-50 %/h
    RECENT_S = 8
    BEFORE_S = (20, 40)
    CHECK_AFTER_S = 150    # checked this long after the step: the climb 30-90 s after it against
    CHECK_FROM_S = 30      # 90-150 s. Charging keeps its pace (about 10 mV/min seen)
    RISE_MV_MIN = 4.0
    KEEPS_PACE = 0.7
    RATE_VETO_S = 240      # by then the gauge's rate has moved: if it clearly disagrees, it wins
    COOLDOWN_S = 300
    TRUST_S = 600
    KEEP_S = 200

    def __init__(self, log=None):
        self.samples = deque()   # (t, volts)
        self.event = None        # (t, charging, shown at once): the current guess
        self.checked = False
        self.started = None
        self.quiet_until = -1e9
        self.noted_at = -1e9
        self.log = log or (lambda msg: None)

    @staticmethod
    def _median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None

    def _rise(self, t0, t1):
        """Least-squares slope of the voltage over [t0, t1], in mV per minute (None: too few)."""
        pts = [(t, v) for t, v in self.samples if t0 <= t <= t1]
        if len(pts) < 8:
            return None
        mt = sum(t for t, _ in pts) / len(pts)
        mv = sum(v for _, v in pts) / len(pts)
        den = sum((t - mt) ** 2 for t, _ in pts)
        return sum((t - mt) * (v - mv) for t, v in pts) / den * 60_000 if den else None

    def add(self, now, volts):
        if self.started is None:
            self.started = now
        self.samples.append((now, volts))
        while self.samples and now - self.samples[0][0] > self.KEEP_S:
            self.samples.popleft()
        if self.event is not None and not self.checked and now - self.event[0] >= self.CHECK_AFTER_S:
            t, plugged, _ = self.event
            early = self._rise(t + self.CHECK_FROM_S, t + 90)
            rise = self._rise(t + 90, now)
            if plugged:  # still climbing, and at much the same pace
                ok = (rise is not None and rise >= self.RISE_MV_MIN
                      and (early is None or rise >= self.KEEPS_PACE * early))
            else:        # not climbing like a charger does
                ok = rise is not None and rise < self.RISE_MV_MIN
            what = "in" if plugged else "out"
            if ok:
                self.checked = True
                self.log(f"charger {what}: confirmed (then {rise:+.1f} mV/min)")
            else:
                self.event = None
                self.quiet_until = now + self.COOLDOWN_S
                self.log(f"charger {what}? no, a load change (then {rise if rise is not None else 0:+.1f} mV/min)")
        recent = [v for t, v in self.samples if now - t <= self.RECENT_S]
        before = [v for t, v in self.samples if self.BEFORE_S[0] <= now - t <= self.BEFORE_S[1]]
        if len(recent) < 3 or len(before) < 3 or now - self.started < self.SETTLE_S:
            return
        base = self._median(before)
        step = self._median(recent) - base
        if self.NOTE_V <= abs(step) < self.GUESS_V and now - self.noted_at > 60:
            self.noted_at = now
            self.log(f"voltage {step * 1000:+.0f} mV ({volts:.3f} V): too small to count")
        if now < self.quiet_until and abs(step) < self.BIG_STEP_V:
            return
        last = self.event[1] if self.event else None
        if step >= self.GUESS_V and min(recent) >= base + self.GUESS_V * 0.6 and last is not True:
            show = step >= self.SHOW_V
            self.event, self.checked = (now, True, show), False
            self.log(f"voltage up {step * 1000:+.0f} mV to {volts:.3f} V: charger in? "
                     + ("(showing it)" if show else "(checking first)"))
        elif step <= -self.GUESS_V and max(recent) <= base - self.GUESS_V * 0.6 and last is not False:
            show = step <= -self.SHOW_V
            self.event, self.checked = (now, False, show), False
            self.log(f"voltage down {step * 1000:+.0f} mV to {volts:.3f} V: charger out? "
                     + ("(showing it)" if show else "(checking first)"))

    def charging(self, now, rate):
        """The verdict: the current guess once it's shown (or confirmed), otherwise the rate."""
        by_rate = rate is not None and rate > self.RATE_ON
        if self.event is not None:
            t, plugged, shown = self.event
            agrees = (rate is not None and rate > self.RATE_ON) if plugged else (rate is None or rate < 0)
            vetoed = (rate is not None and now - t > self.RATE_VETO_S
                      and (rate < -2.0 if plugged else rate > 2.0 * self.RATE_ON))
            if vetoed:
                self.log(f"the gauge's rate ({rate:+.1f} %/h) says otherwise: going with it")
            if agrees or vetoed or now - t > self.TRUST_S:
                self.event = None
            elif shown or self.checked:
                return plugged
        return by_rate


CHARGE_PIN = None   # a GPIO wired to the PowerBoost's USB pin through a divider (e.g. 4, header pin 7):
                    # with it, charging is known for certain and at once (see the hardware notes)


class Battery:
    """The battery, from the MAX17048 fuel gauge (or, without it, the PowerBoost's LBO pin).

    Two protections, each with its own timer, both ending in power_off("BATTERY LOW"):
      - charge: the gauge's state of charge at or below BATT_SOC_LOW for LOW_BATT_SHUTDOWN_SECS.
        Paused only while the battery is *surely* charging (charge_confidence() == "yes": the
        wired charger pin, or the gauge's own rate clearly rising). A guess from the voltage
        alone doesn't pause it: if the charger isn't really there, the cell is still emptying.
      - voltage: the cell's voltage, as a median over BATT_VOLT_WINDOW_SECS (motor kicks last a
        fraction of a second and don't move the median), below the cutoff for
        BATT_VOLT_HOLD_SECS, or below BATT_CRITICAL_VOLTS for BATT_CRITICAL_HOLD_SECS. It clears
        only BATT_VOLT_HYST above the cutoff. Only the wired charger pin pauses this one.
    Readings that fail, or make no sense (0 V, 5 V, 300 %/h), are dropped. If the gauge stops
    answering, its last values stand for BATT_STALE_SECS; then the level reads as unknown (never
    "empty"), but a protection already counting down still finishes, since it can't be checked
    any more. A missing gauge is looked for again every REPROBE_EVERY seconds. The I2C load is
    as before: the voltage every 2 s, charge and rate every 15 s.
    """
    VOLT_EVERY = 2.0      # seconds between quick voltage reads
    FULL_EVERY = 15.0     # seconds between charge and rate reads
    REPROBE_EVERY = 30.0  # seconds between looks for a gauge that isn't answering

    def __init__(self, bus="auto", usb="auto", lbo="auto", log=None):
        self.log = log or (lambda m: print(f"Battery: {m}", flush=True))
        self.source = None      # "gauge", "lbo" or None
        self.pct = None
        self.low = False        # at or below BATT_SOC_LOW (or LBO): the icon's LOW, and the alerts
        self.charging = False   # the indicator's view: wired pin, else the voltage detector's guess
        self.rate = None        # %/hour from the gauge: negative while discharging
        self.volts = None
        self.low_since = None        # the charge protection's timer
        self.volt_low_since = None   # the voltage protection's timer
        self.volt_critical = False
        self.capacity_mah = BATTERY_MAH
        self.cutoff_volts = BATT_CUTOFF_VOLTS
        self.detector = ChargeDetector(log=lambda m: print(f"Charging: {m}", flush=True))
        self.gauge_ok = False
        self.fails = 0               # gauge reads failed in a row
        self.last_ok = None          # last time the gauge answered sensibly
        self.volts_ok_at = None      # last good voltage reading (a charge reading must have one beside it)
        self.full_at = None          # last good charge/rate reading
        self.rate_up_reads = 0       # charge/rate readings in a row with the rate clearly rising
        self.vhist = deque()         # (t, volts), for the median
        self.distrust_logged = False
        self._read_at = self._volts_at = -999.0
        self._reprobe_at = 0.0
        self._usb = self._pin(CHARGE_PIN, False) if usb == "auto" else usb
        self._lbo = self._pin(LBO_PIN, True) if lbo == "auto" else lbo   # with pull_up, 1 = LBO low
        if bus == "auto":
            try:
                try:
                    from smbus2 import SMBus
                except ImportError:
                    from smbus import SMBus
                bus = SMBus(1)
            except Exception:
                bus = None
        self._bus = bus
        if self._bus is not None:
            self._probe(0.0, quiet=True)
        if not self.gauge_ok and self._lbo is not None:
            self.source = "lbo"

    @staticmethod
    def _pin(pin, pull_up):
        if pin is None:
            return None
        try:
            from gpiozero import DigitalInputDevice
            return DigitalInputDevice(pin, pull_up=pull_up)
        except Exception:
            return None

    def configure(self, capacity_mah=None, cutoff_volts=None):
        """From the settings: the cell's capacity (for the drain estimate) and the voltage cutoff."""
        try:
            c = int(capacity_mah)
            self.capacity_mah = c if 100 <= c <= 20000 else BATTERY_MAH
        except (TypeError, ValueError):
            self.capacity_mah = BATTERY_MAH
        try:
            v = float(cutoff_volts)
            self.cutoff_volts = v if 3.0 <= v <= 3.6 else BATT_CUTOFF_VOLTS
        except (TypeError, ValueError):
            self.cutoff_volts = BATT_CUTOFF_VOLTS

    # -------------------------------------------------------------- reading the gauge
    def _word(self, reg):
        w = self._bus.read_word_data(0x36, reg)
        return ((w & 0xFF) << 8) | (w >> 8)  # the chip is big-endian

    def _probe(self, now, quiet=False):
        self._reprobe_at = now + self.REPROBE_EVERY
        try:
            self._word(0x04)
        except Exception:
            return False
        self.gauge_ok, self.source, self.fails, self.last_ok = True, "gauge", 0, now
        try:  # never hibernate: it samples every 250 ms instead of every 45 s (about 20 uA more)
            self._bus.write_word_data(0x36, 0x0A, 0x0000)
        except Exception:
            pass
        if not quiet:
            self.log("fuel gauge answering")
        return True

    def _fail(self, now, what):
        self.fails += 1
        if self.fails in (1, 10) or self.fails % 100 == 0:
            self.log(f"gauge read failed or made no sense ({what}), {self.fails} in a row")

    def _read_volts(self, now):
        try:
            v = self._word(0x02) * 78.125e-6
        except Exception:
            self._fail(now, "voltage")
            return
        if not BATT_VOLTS_SANE[0] <= v <= BATT_VOLTS_SANE[1]:
            self._fail(now, f"{v:.2f} V")
            return
        self.fails, self.last_ok, self.volts, self.volts_ok_at = 0, now, v, now
        self.vhist.append((now, v))
        while self.vhist and now - self.vhist[0][0] > 2 * BATT_VOLT_WINDOW_SECS:
            self.vhist.popleft()
        if self._usb is None:
            self.detector.add(now, v)
            self.charging = self.detector.charging(now, self.rate)

    def _read_full(self, now):
        if self.volts_ok_at is None or self.volts_ok_at < self._volts_at:
            # the latest voltage read failed or made no sense: a glitch that zeroes the bus zeroes
            # the voltage too, so a charge read beside it (0%, 0 %/h) isn't trusted
            self._fail(now, "charge, with no good voltage to check it against")
            return
        try:
            soc = self._word(0x04) / 256
            crate = self._word(0x16)
        except Exception:
            self._fail(now, "charge")
            return
        rate = (crate - 0x10000 if crate & 0x8000 else crate) * 0.208  # signed, 0.208 %/h per bit
        if soc > 110 or abs(rate) > 300:
            self._fail(now, f"{soc:.0f}%, {rate:+.0f} %/h")
            return
        self.fails, self.last_ok, self.full_at = 0, now, now
        self.pct = max(0, min(100, int(soc)))
        self.low = self.pct <= BATT_SOC_LOW
        self.rate = rate
        self.rate_up_reads = self.rate_up_reads + 1 if rate > ChargeDetector.RATE_ON else 0
        if self._usb is None:
            self.charging = self.detector.charging(now, self.rate)

    def _lost(self, now):
        """The gauge has stopped answering: unknown, never empty. Timers already running stay."""
        self.gauge_ok = False
        self._reprobe_at = now + self.REPROBE_EVERY
        self.pct = self.rate = self.volts = None
        self.vhist.clear()
        self.rate_up_reads = 0
        if self._usb is None:
            self.charging = False
        self.source = "lbo" if self._lbo is not None else "gauge"
        self.log(f"fuel gauge not answering for {BATT_STALE_SECS} s: level unknown"
                 + (" (a shutdown already counting down still happens)" if self.armed() else ""))

    def update(self, now):
        if self._usb is not None:  # wired: no guessing
            try:
                self.charging = bool(self._usb.value)
            except Exception:
                pass
        if self._bus is not None and not self.gauge_ok and now >= self._reprobe_at:
            self._probe(now)
        if self.gauge_ok:
            if now - self._volts_at >= self.VOLT_EVERY:
                self._volts_at = now
                self._read_volts(now)
            if now - self._read_at >= self.FULL_EVERY:
                self._read_at = now
                self._read_full(now)
            if now - (self.last_ok if self.last_ok is not None else now) > BATT_STALE_SECS:
                self._lost(now)
        if not self.gauge_ok and self._lbo is not None:
            try:
                self.low = bool(self._lbo.value)
            except Exception:
                pass
        self._protect(now)

    # -------------------------------------------------------------- what it all means
    def vmedian(self, now):
        """The cell voltage as a median over the last BATT_VOLT_WINDOW_SECS (None: too few)."""
        vs = sorted(v for t, v in self.vhist if now - t <= BATT_VOLT_WINDOW_SECS)
        return vs[len(vs) // 2] if len(vs) >= 3 else None

    def charge_confidence(self):
        """"yes" only when charging is sure: the wired pin, or the gauge's own rate clearly rising
        (two readings in a row), or the voltage detector's charger confirmed by its check with the
        rate not falling. "unsure" when it's only a guess; "no" otherwise."""
        if self._usb is not None:
            return "yes" if self.charging else "no"
        if self.gauge_ok:
            if self.rate_up_reads >= 2:
                return "yes"
            d = self.detector
            if d.event is not None and d.event[1] and d.checked and (self.rate is None or self.rate >= 0):
                return "yes"
        return "unsure" if self.charging else "no"

    def armed(self):
        return self.low_since is not None or self.volt_low_since is not None

    def _protect(self, now):
        # the charge protection: only on a fresh, believable reading (missing data leaves it alone)
        if self.gauge_ok and self.full_at is not None and now - self.full_at <= 2 * self.FULL_EVERY + 1:
            vm = self.vmedian(now)
            believable = vm is None or vm < BATT_SOC_TRUST_VOLTS
            if self.low and not believable and not self.distrust_logged:
                self.distrust_logged = True
                self.log(f"gauge says {self.pct}% but the cell is at {vm:.2f} V: not believed (still settling?)")
            sure = self.charge_confidence() == "yes"
            if self.low and believable and not sure:
                if self.low_since is None:
                    self.low_since = now
                    self.log(f"charge at {self.pct}%: switching off in {LOW_BATT_SHUTDOWN_SECS} s unless it "
                             f"charges ({self.charge_confidence()} charging)")
            elif self.low_since is not None:
                self.low_since = None
                self.log("charge shutdown cancelled: " + ("charging" if sure else f"back to {self.pct}%"))
        elif not self.gauge_ok and self._lbo is not None:      # no gauge: LBO, as before
            if self.low:
                self.low_since = self.low_since or now
            else:
                self.low_since = None
        # the voltage protection: only the wired pin pauses it
        vm = self.vmedian(now)
        if self._usb is not None and self.charging:
            if self.volt_low_since is not None:
                self.volt_low_since = None
                self.log("voltage shutdown cancelled: on USB power (charger pin)")
        elif vm is not None:
            if vm < self.cutoff_volts:
                self.volt_critical = vm < BATT_CRITICAL_VOLTS
                if self.volt_low_since is None:
                    self.volt_low_since = now
                    hold = BATT_CRITICAL_HOLD_SECS if self.volt_critical else BATT_VOLT_HOLD_SECS
                    self.log(f"cell at {vm:.2f} V (cutoff {self.cutoff_volts:.2f} V): switching off in {hold} s "
                             "unless it recovers")
            elif vm >= self.cutoff_volts + BATT_VOLT_HYST and self.volt_low_since is not None:
                self.volt_low_since = None
                self.volt_critical = False
                self.log(f"voltage shutdown cancelled: back to {vm:.2f} V")

    def shutdown_due(self, now):
        """None, or which protection has run out ("charge" or "voltage")."""
        if self.low_since is not None and now - self.low_since > LOW_BATT_SHUTDOWN_SECS:
            return "charge"
        if self.volt_low_since is not None:
            hold = BATT_CRITICAL_HOLD_SECS if self.volt_critical else BATT_VOLT_HOLD_SECS
            if now - self.volt_low_since > hold:
                return "voltage"
        return None

    def state(self):
        return (self.source, self.pct, self.low, self.charging)

    def drain_text(self):
        """An estimate of what the whole player draws from the cell: the gauge's %/hour times the
        capacity set in Settings. The MAX17048 doesn't measure current; this is worked out from
        how fast its state-of-charge estimate falls, averaged over minutes, so let it settle
        after a change before reading it. e.g. "~380 mA, 5.2 h"."""
        if self.source != "gauge" or self.rate is None:
            return "--"
        if self.charging:
            return "charging"
        if self.rate > -0.3:
            return "measuring"
        hours = f", {self.pct / -self.rate:.1f} h" if self.pct else ""
        return f"~{-self.rate * self.capacity_mah / 100:.0f} mA{hours}"

    def text(self):
        if self.source == "gauge" and self.pct is not None:
            return f"{self.pct}%"
        if self.source == "lbo":
            return "LOW" if self.low else "OK"
        return "N/A"


def battery_icon(d, x, y, batt):
    """22 x 11 battery at (x, y). Segments for the gauge, full/empty for LBO."""
    d.rectangle((x, y, x + 19, y + 10), outline=BLACK)
    d.rectangle((x + 20, y + 3, x + 21, y + 7), fill=BLACK)
    if batt.source == "gauge" and batt.pct is not None:
        segs = min(4, math.ceil(batt.pct / 25))
    elif batt.source == "lbo":
        segs = 0 if batt.low else 4
    else:
        d.text((x + 7, y), "?", font=F_TINY, fill=BLACK)
        return
    if getattr(batt, "charging", False):
        # a lightning bolt inside the outline instead of the level
        d.polygon([(x + 11, y + 1), (x + 6, y + 6), (x + 9, y + 6), (x + 8, y + 10), (x + 14, y + 4), (x + 11, y + 4)], fill=BLACK)
        return
    for i in range(segs):
        d.rectangle((x + 2 + i * 4, y + 2, x + 4 + i * 4, y + 8), fill=BLACK)
    if batt.low:
        d.text((x - 22, y), "LOW", font=F_TINY, fill=BLACK)


# ======================================================================
# MPD client (plain text protocol, no extra packages)
# ======================================================================
def mpd_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


class MPDError(Exception):
    pass


class MPDClient:
    def __init__(self, host=MPD_HOST, port=MPD_PORT, timeout=3):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.rfile = None
        self.lock = threading.RLock()

    def _connect(self):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock, self.rfile = s, s.makefile("rb")
        if not self.rfile.readline().startswith(b"OK MPD"):
            self.close()
            raise MPDError("not an MPD server")
        try:  # cover art in big pieces: MPD's default 8 KB meant hundreds of round trips per cover
            self.sock.sendall(b"binarylimit 524288\n")
            self._read_pairs()
        except MPDError:
            pass  # an older MPD: the small pieces still work

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = self.rfile = None

    def _read_pairs(self):
        pairs = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                raise ConnectionError("MPD closed the connection")
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line == "OK":
                return pairs
            if line.startswith("ACK"):
                raise MPDError(line)
            if line == "list_OK":
                continue
            k, _, v = line.partition(": ")
            pairs.append((k, v))

    def _run(self, payload):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(payload)
                    return self._read_pairs()
                except (OSError, ConnectionError):
                    self.close()
                    if attempt:
                        raise MPDError("MPD unreachable")

    def command(self, line):
        """Run one command, return [(key, value), ...]."""
        return self._run(line.encode("utf-8") + b"\n")

    def command_list(self, lines):
        body = "command_list_begin\n" + "".join(l + "\n" for l in lines) + "command_list_end\n"
        return self._run(body.encode("utf-8"))

    def as_dict(self, line):
        return dict(self.command(line))

    def values(self, line, key):
        return [v for k, v in self.command(line) if k == key]

    def records(self, line, first="file"):
        out, cur = [], None
        for k, v in self.command(line):
            if k == first:
                cur = {}
                out.append(cur)
            if cur is not None:
                cur[k] = v
        return out

    def _binary(self, cmd, uri):
        with self.lock:
            if self.sock is None:
                self._connect()
            buf, offset = bytearray(), 0
            while True:
                self.sock.sendall(f"{cmd} {mpd_quote(uri)} {offset}\n".encode("utf-8"))
                size, chunk = None, b""
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        raise ConnectionError("MPD closed the connection")
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line == "OK":
                        break
                    if line.startswith("ACK"):
                        raise MPDError(line)
                    k, _, v = line.partition(": ")
                    if k == "size":
                        size = int(v)
                    elif k == "binary":
                        chunk = self.rfile.read(int(v))
                        self.rfile.read(1)  # newline after the binary block
                if not chunk:
                    return bytes(buf) or None
                buf += chunk
                offset += len(chunk)
                if size is None or offset >= size:
                    return bytes(buf)

    def picture(self, uri):
        """Embedded cover (readpicture), else a cover file in the album folder (albumart).
        None means the song has no cover. A dropped or slow connection raises OSError instead,
        so the caller can try again rather than decide there's no cover."""
        for cmd in ("readpicture", "albumart"):
            try:
                data = self._binary(cmd, uri)
                if data:
                    return data
            except MPDError:
                continue
            except (OSError, ConnectionError) as e:
                self.close()
                raise OSError(f"cover fetch interrupted: {e}") from e
        return None


# ======================================================================
# Player: both sources, polled in the background
# ======================================================================
class Snap:
    reachable = False        # at least one player is reachable
    source = None            # "spotify", "local" or None
    has_track = playing = paused = False
    uri = title = artists = album = ""
    pos = dur = 0
    fmt = ""
    cover = None
    shuffle = repeat = False


def local_format(audio, path):
    """MPD 'audio' (e.g. 48000:24:2) + file extension -> 'FLAC 48k/24'."""
    ext = os.path.splitext(path)[1].lstrip(".").upper() or "FILE"
    try:
        rate, bits, _ = audio.split(":")
        r = int(rate) / 1000
        rate_s = f"{r:g}k"
        return f"{ext} {rate_s}/{bits}" if bits.isdigit() else f"{ext} {rate_s}"
    except Exception:
        return ext


class LibrespotAPI:
    """Keep-alive HTTP to go-librespot, one connection per thread. Much cheaper on a Zero W than
    opening a new urllib connection (and resolving "localhost") every second."""

    def __init__(self, host=API_HOST, port=API_PORT):
        self.host, self.port = host, port
        self.local = threading.local()

    def request(self, method, path, body=None, timeout=3):
        """Returns (status, body bytes). Raises OSError when go-librespot can't be reached."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        for attempt in (0, 1):
            conn = getattr(self.local, "conn", None)
            if conn is None:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
                self.local.conn = conn
            try:
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.read()
            except (OSError, http.client.HTTPException):
                conn.close()
                self.local.conn = None
                if attempt:
                    raise OSError("go-librespot unreachable")
        return 0, b""


class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.interval = 1.0
        self.kick = threading.Event()
        self.sp, self.sp_at = None, 0.0        # go-librespot status
        self.mp, self.mp_at = None, 0.0        # MPD status + current song
        self.active = None
        self.hold = None   # a source to show while it's being resumed (Resume sets and clears it)
        self.was_sp_playing = self.was_mp_playing = False
        self.mpd_poll = MPDClient()
        self.mpd = MPDClient()                 # commands from the UI
        self.mpd_art = MPDClient()             # cover art fetches
        self.covers = {}
        self.cover_pending = set()
        self.cover_retry = {}   # key -> (failures so far, when to try again): fetches that failed
                                # (a timeout, a dropped connection), as opposed to "no cover"
        self.seek_override = None
        self.seek_target = None
        self.seek_event = threading.Event()
        self.api = LibrespotAPI()
        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()
        threading.Thread(target=self._seek_loop, name="seek", daemon=True).start()

    # --- polling --------------------------------------------------
    def _poll_spotify(self):
        try:
            status, body = self.api.request("GET", "/status", timeout=2)
        except OSError:
            return None
        if status == 200 and body:
            try:
                return json.loads(body)
            except ValueError:
                return {}
        return {}

    def _poll_mpd(self):
        try:
            return dict(self.mpd_poll.command_list(["status", "currentsong"]))
        except MPDError:
            return None

    def _poll_loop(self):
        while True:
            sp, mp = self._poll_spotify(), self._poll_mpd()
            now = time.monotonic()
            sp_playing = bool(sp and sp.get("track") and not sp.get("stopped") and not sp.get("paused"))
            mp_playing = bool(mp and mp.get("state") == "play")

            # Only one source plays at a time: the one that started most recently wins
            if sp_playing and mp_playing:
                if not self.was_sp_playing:
                    self._mpd_quiet("pause 1")
                    mp_playing = False
                else:
                    self._spotify_post("/player/pause")
                    sp_playing = False
            self.was_sp_playing, self.was_mp_playing = sp_playing, mp_playing

            with self.lock:
                self.sp, self.sp_at = sp, now
                self.mp, self.mp_at = mp, now
                if mp_playing:
                    self.active = "local"
                elif sp_playing:
                    self.active = "spotify"
                elif self.active == "local" and not (mp and mp.get("file")):
                    self.active = "spotify" if sp and sp.get("track") else None
                elif self.active == "spotify" and not (sp and sp.get("track")):
                    self.active = "local" if mp and mp.get("file") else None
                elif self.active is None:
                    if mp and mp.get("file"):
                        self.active = "local"
                    elif sp and sp.get("track"):
                        self.active = "spotify"
                if self.hold and not (mp_playing or sp_playing):
                    self.active = self.hold  # being resumed: not MPD's leftover queue meanwhile
            self._want_cover()
            self.kick.wait(self.interval)
            self.kick.clear()

    # --- covers ---------------------------------------------------
    def _want_cover(self):
        with self.lock:
            if self.active == "spotify" and self.sp:
                key = ((self.sp.get("track") or {}).get("album_cover_url"))
                job = ("url", key)
            elif self.active == "local" and self.mp:
                key = self.mp.get("file")
                job = ("mpd", key)
            else:
                return
            if not key or key in self.covers or key in self.cover_pending:
                return
            retry = self.cover_retry.get(key)
            if retry and time.monotonic() < retry[1]:
                return
            self.cover_pending.add(key)
        threading.Thread(target=self._fetch_cover, args=job, name="cover", daemon=True).start()

    COVER_RETRY_SECS = (3, 10, 30)   # after a failed fetch; then it's taken as having none

    def _fetch_cover(self, kind, key):
        covers, failed = None, False
        try:
            if kind == "url":
                with urllib.request.urlopen(key, timeout=8) as r:
                    raw = r.read()
            else:
                raw = self.mpd_art.picture(key)
            if raw:
                covers = make_covers(raw)
        except urllib.error.HTTPError as e:
            failed = e.code >= 500 or e.code == 429  # the server's trouble: try again; 404 etc.: none
        except OSError:
            failed = True        # timeout, no network, MPD busy: try again
        except Exception:        # noqa: BLE001 - an image PIL can't read: there's no cover to show
            covers = None
        with self.lock:
            self.cover_pending.discard(key)
            if failed:
                n = self.cover_retry.get(key, (0, 0))[0]
                if n < len(self.COVER_RETRY_SECS):
                    self.cover_retry[key] = (n + 1, time.monotonic() + self.COVER_RETRY_SECS[n])
                    return
            self.cover_retry.pop(key, None)
            while len(self.covers) > 8:
                self.covers.pop(next(iter(self.covers)))
            self.covers[key] = covers

    # --- commands -------------------------------------------------
    def _spotify_post_now(self, path, body=None):
        try:
            self.api.request("POST", path, body or {})
        except OSError:
            pass
        self.kick.set()

    def _spotify_post(self, path, body=None):
        threading.Thread(target=self._spotify_post_now, args=(path, body), name="post", daemon=True).start()

    def _mpd_quiet(self, *lines):
        try:
            if len(lines) == 1:
                self.mpd.command(lines[0])
            else:
                self.mpd.command_list(list(lines))
        except MPDError:
            pass
        self.kick.set()

    def play_pause(self, snap):
        if snap.source == "local":
            self._mpd_quiet("pause 1" if snap.playing else "play")
        elif snap.source == "spotify" and snap.has_track:
            self._spotify_post("/player/resume" if snap.paused else "/player/pause")

    def next(self, snap):
        if snap.source == "local":
            self._mpd_quiet("next")
        elif snap.source == "spotify":
            self._spotify_post("/player/next")

    def prev(self, snap):
        if snap.source == "local":
            self._mpd_quiet("previous")
        elif snap.source == "spotify":
            self._spotify_post("/player/prev")

    def seek_by(self, snap, delta_ms):
        """Dial scrubbing: the screen shows the new position at once, and one seek is sent when
        the dial pauses (so a fast spin is one seek, not dozens)."""
        if not snap.has_track or not snap.dur:
            return
        now = time.monotonic()
        base = (self.seek_override[0] if self.seek_override and now < self.seek_override[1]
                else snap.pos)
        target = max(0, min(snap.dur - 1000, base + delta_ms))
        self.seek_override = (target, now + 1.5)
        self.seek_target = (snap.source, int(target))
        self.seek_event.set()

    def seek_to(self, snap, pos_ms):
        """Immediate seek, for auto-scrub."""
        pos_ms = int(max(0, min(snap.dur - 1000, pos_ms) if snap.dur else pos_ms))
        self.seek_override = (pos_ms, time.monotonic() + 1.0)
        if snap.source == "local":
            self._mpd_quiet(f"seekcur {pos_ms / 1000:.1f}")
        elif snap.source == "spotify":
            self._spotify_post("/player/seek", {"position": pos_ms})

    def _seek_loop(self):
        """Waits for the dial to pause: 0.12 s for local files, 0.35 s for Spotify (which rebuffers)."""
        while True:
            self.seek_event.wait()
            self.seek_event.clear()
            while self.seek_event.wait(0.12 if self.seek_target[0] == "local" else 0.35):
                self.seek_event.clear()
            source, pos = self.seek_target
            if source == "local":
                self._mpd_quiet(f"seekcur {pos / 1000:.1f}")
            else:
                self._spotify_post_now("/player/seek", {"position": pos})

    def play_files(self, files, index=0, shuffle=False):
        """Replace the MPD queue and start playing (pauses Spotify first)."""
        if self.was_sp_playing:
            self._spotify_post_now("/player/pause")
        lines = ["clear", f"random {1 if shuffle else 0}"]
        lines += [f"add {mpd_quote(f)}" for f in files]
        lines.append(f"play {index}")
        self._mpd_quiet(*lines)

    # --- library --------------------------------------------------
    def lib_artists(self):
        names = [v for v in self.mpd.values("list albumartist", "AlbumArtist") if v]
        if not names:
            names = [v for v in self.mpd.values("list artist", "Artist") if v]
        return sorted(set(names), key=str.casefold)

    def lib_albums(self, artist=None):
        if artist is None:
            names = self.mpd.values("list album", "Album")
        else:
            names = self.mpd.values(f"list album albumartist {mpd_quote(artist)}", "Album")
            if not names:
                names = self.mpd.values(f"list album artist {mpd_quote(artist)}", "Album")
        return sorted({v for v in names if v}, key=str.casefold)

    def lib_tracks(self, artist=None, album=None):
        cmd = "find"
        if album is not None:
            cmd += f" album {mpd_quote(album)}"
        if artist is not None:
            cmd += f" albumartist {mpd_quote(artist)}"
        recs = self.mpd.records(cmd)
        if not recs and artist is not None:
            recs = self.mpd.records(cmd.replace(" albumartist ", " artist "))

        def num(v):
            try:
                return int(str(v).split("/")[0])
            except ValueError:
                return 0
        return sorted(recs, key=lambda r: (num(r.get("Disc", 0)), num(r.get("Track", 0)), r.get("file", "")))

    def lib_playlists(self):
        return sorted((v for v in self.mpd.values("listplaylists", "playlist") if v), key=str.casefold)

    def play_playlist(self, name, shuffle=False):
        if self.was_sp_playing:
            self._spotify_post_now("/player/pause")
        self._mpd_quiet("clear", f"random {1 if shuffle else 0}", f"load {mpd_quote(name)}", "play")

    def save_queue(self, name):
        try:
            self.mpd.command(f"save {mpd_quote(name)}")
            return True, ""
        except MPDError as e:
            return False, "A playlist with that name exists." if "exist" in str(e).lower() else str(e)

    def playlist_add(self, name, uri):
        try:
            self.mpd.command(f"playlistadd {mpd_quote(name)} {mpd_quote(uri)}")
            return True
        except MPDError:
            return False

    def playlist_delete(self, name):
        self._mpd_quiet(f"rm {mpd_quote(name)}")

    def playlist_songs(self, name):
        """A stored playlist's songs as records (file, Title, duration...), in order."""
        return self.mpd.records(f"listplaylistinfo {mpd_quote(name)}")

    def playlist_files(self, name):
        """A stored playlist's files; [] if there's no such playlist yet."""
        try:
            return self.mpd.values(f"listplaylist {mpd_quote(name)}", "file")
        except MPDError as e:
            if "no such playlist" in str(e).lower():
                return []
            raise

    def playlist_add_many(self, name, uris):
        """Add songs to a playlist (making it if it's new), skipping ones already in it.
        Returns (added, already there), or (None, 0) if MPD refused."""
        try:
            have = set(self.playlist_files(name))
            new = []
            for u in uris:
                if u not in have:
                    have.add(u)
                    new.append(u)
            if new:
                self.mpd.command_list([f"playlistadd {mpd_quote(name)} {mpd_quote(u)}" for u in new])
            return len(new), len(uris) - len(new)
        except MPDError:
            return None, 0

    def playlist_create(self, name):
        """A new, empty playlist; Up Next and what's playing are left alone. MPD has no command
        for an empty one, so a song is added (which makes the playlist) and taken straight out
        again, in one go. Refuses a name that's taken, so it can't empty an existing playlist."""
        try:
            if name in self.lib_playlists():
                return False, "A playlist with that name exists."
            one = self.mpd.values(f"find {mpd_quote(MPD_ANY_SONG)} window 0:1", "file")
            if not one:
                return False, "Add some music first."
            q = mpd_quote(name)
            self.mpd.command_list([f"playlistadd {q} {mpd_quote(one[0])}", f"playlistdelete {q} 0"])
            return True, ""
        except MPDError as e:
            return False, str(e)

    def playlist_remove(self, name, pos):
        self._mpd_quiet(f"playlistdelete {mpd_quote(name)} {int(pos)}")

    def playlist_move(self, name, src, dst):
        self._mpd_quiet(f"playlistmove {mpd_quote(name)} {int(src)} {int(dst)}")

    def playlist_rename(self, old, new):
        try:
            self.mpd.command(f"rename {mpd_quote(old)} {mpd_quote(new)}")
            return True, ""
        except MPDError as e:
            return False, "A playlist with that name exists." if "exist" in str(e).lower() else str(e)

    def play_playlist_from(self, name, pos):
        if self.was_sp_playing:
            self._spotify_post_now("/player/pause")
        self._mpd_quiet("clear", "random 0", f"load {mpd_quote(name)}", f"play {int(pos)}")

    def playlist_to_queue(self, name):
        """The playlist's songs onto the end of Up Next (nothing else changes)."""
        self._mpd_quiet(f"load {mpd_quote(name)}")

    def _current_pos(self):
        cur = self.mpd.as_dict("status").get("song")
        return int(cur) if cur and cur.isdigit() else -1

    def queue_add(self, uris):
        self._mpd_quiet(*[f"add {mpd_quote(u)}" for u in uris])

    def queue_add_next(self, uris):
        """Songs straight after the one playing (or at the end, if nothing is)."""
        try:
            cur = self._current_pos()
        except MPDError:
            return
        if cur < 0:
            self.queue_add(uris)
        else:
            self._mpd_quiet(*[f"addid {mpd_quote(u)} {cur + 1 + i}" for i, u in enumerate(uris)])

    def queue_remove(self, pos):
        self._mpd_quiet(f"delete {int(pos)}")

    def queue_move_next(self, pos):
        """Move a song in Up Next to straight after the one playing."""
        try:
            cur = self._current_pos()
        except MPDError:
            return
        if cur < 0 or pos == cur:
            return
        self._mpd_quiet(f"move {int(pos)} {cur + 1 if pos > cur else cur}")

    def queue(self):
        """MPD's queue as records, plus the position of the current song."""
        recs = self.mpd.records("playlistinfo")
        cur = self.mpd.as_dict("status").get("song")
        return recs, int(cur) if cur and cur.isdigit() else -1

    def play_pos(self, pos):
        if self.was_sp_playing:
            self._spotify_post_now("/player/pause")
        self._mpd_quiet(f"play {pos}")

    def toggle_shuffle(self, snap):
        if snap.source == "local":
            self._mpd_quiet(f"random {0 if snap.shuffle else 1}")
        elif snap.source == "spotify":
            # go-librespot API (see its api-spec.yml); ignored quietly if the endpoint differs
            self._spotify_post("/player/shuffle_context", {"shuffle_context": not snap.shuffle})

    def toggle_repeat(self, snap):
        if snap.source == "local":
            self._mpd_quiet(f"repeat {0 if snap.repeat else 1}")
        elif snap.source == "spotify":
            self._spotify_post("/player/repeat_context", {"repeat_context": not snap.repeat})

    def spotify_play(self, uri):
        """Start a Spotify context (playlist, album) on go-librespot. Returns True on success."""
        if self.was_mp_playing:
            self._mpd_quiet("pause 1")
        try:
            status, _ = self.api.request("POST", "/player/play", {"uri": uri}, timeout=8)
            ok = status == 200
        except OSError:
            ok = False
        self.kick.set()
        return ok

    def shuffle_all(self):
        if self.was_sp_playing:
            self._spotify_post_now("/player/pause")
        self._mpd_quiet("clear", 'add "/"', "random 1", "play")

    def rescan(self):
        self._mpd_quiet("update")

    # --- snapshot -------------------------------------------------
    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            sp, sp_at, mp, mp_at = self.sp, self.sp_at, self.mp, self.mp_at
            active = self.active
            covers = self.covers
            s = Snap()
            s.reachable = sp is not None or mp is not None
            s.source = active
            if active == "spotify" and sp and sp.get("track") and not sp.get("stopped"):
                t = sp["track"]
                s.cover = covers.get(t.get("album_cover_url"))
            elif active == "local" and mp and mp.get("file"):
                s.cover = covers.get(mp.get("file"))
            else:
                return s

        if active == "spotify":
            t = sp["track"]
            s.has_track = True
            s.uri = t.get("uri") or t.get("name", "")
            s.title = t.get("name") or ""
            s.artists = ", ".join(t.get("artist_names") or [])
            s.album = t.get("album_name") or ""
            s.paused = bool(sp.get("paused"))
            s.playing = not s.paused
            s.dur = t.get("duration") or 0
            pos = t.get("position", sp.get("position"))
            if pos is None:
                pos = 0
            elif s.playing and not sp.get("buffering"):
                pos += (now - sp_at) * 1000
            if self.seek_override and now < self.seek_override[1]:
                pos = self.seek_override[0]
            s.fmt = SPOTIFY_FORMAT
            s.shuffle = bool(sp.get("shuffle_context"))
            s.repeat = bool(sp.get("repeat_context") or sp.get("repeat_track"))
        else:
            s.has_track = True
            s.uri = mp.get("file", "")
            s.title = mp.get("Title") or os.path.splitext(os.path.basename(s.uri))[0]
            s.artists = mp.get("Artist") or mp.get("AlbumArtist") or ""
            s.album = mp.get("Album") or ""
            s.playing = mp.get("state") == "play"
            s.paused = not s.playing
            try:
                s.dur = float(mp.get("duration") or mp.get("Time") or 0) * 1000
                pos = float(mp.get("elapsed") or 0) * 1000
            except ValueError:
                s.dur, pos = 0, 0
            if s.playing:
                pos += (now - mp_at) * 1000
            if self.seek_override and now < self.seek_override[1]:
                pos = self.seek_override[0]
            s.fmt = local_format(mp.get("audio", ""), s.uri)
            s.shuffle = mp.get("random") == "1"
            s.repeat = mp.get("repeat") == "1"
        s.pos = max(0, min(pos, s.dur) if s.dur else pos)
        return s


# ======================================================================
# Resume on power up
# ======================================================================
RESUME_FILE = os.path.expanduser("~/.config/currawong/resume.json")
RESUME_SAVE_SECS = 10     # while playing, the place is saved this often: a power cut loses at most this
RESUME_WINDOW_SECS = 300  # how long after a start to keep trying (Spotify waits for Wi-Fi and its login)
RESUME_RETRY_SECS = 2


class Resume:
    """Remembers what was playing and where, and puts it back after a restart: the same source,
    track and second, and for Spotify the same playlist or album, ready to play or playing
    (Settings > On power up). MPD's own state file is only written a while after a change, so
    sliding the switch off could lose the place; this is written every few seconds while
    playing, and straight away on any change or power-off."""

    def __init__(self, player, settings, path=None):
        self.player, self.settings = player, settings
        self.path = path or RESUME_FILE
        self.saved = self._load()
        self.pending = self.saved if (self.saved and settings.get("resume", "ready") != "off") else None
        self.started = time.monotonic()
        self.next_try = 0.0
        self.last_save = -1e9
        self.last_key = None
        if self.pending:
            self.player.hold = self.pending["source"]
            STATUS["resuming"] = self.pending.get("title") or "where you left off"

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if data.get("source") in ("local", "spotify") and data.get("uri"):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _sp(self):
        sp = getattr(self.player, "sp", None)
        return sp if isinstance(sp, dict) else None

    def point(self, snap):
        """What to remember now, or None when there's nothing worth remembering."""
        if not snap.has_track or snap.source not in ("local", "spotify") or not snap.uri:
            return None
        p = {"source": snap.source, "uri": snap.uri, "pos": int(snap.pos), "title": snap.title,
             "saved": int(time.time())}
        sp = self._sp() or {}
        if snap.source == "spotify" and sp.get("context_uri"):
            p["context"] = sp["context_uri"]
        return p

    def save(self, snap, now=None):
        p = self.point(snap)
        if p is None:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(p, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            return
        self.last_save = time.monotonic() if now is None else now

    def _finish(self, source=None):
        self.pending = None
        self.player.hold = None
        STATUS.pop("resuming", None)
        if source:
            with self.player.lock:
                self.player.active = source

    def tick(self, now, snap):
        """Every step: put things back once after a start, then keep the place saved."""
        if self.pending is not None:
            if snap.playing or now - self.started > RESUME_WINDOW_SECS:
                self._finish()  # something's already playing (by hand, or from a phone), or too late
            elif now >= self.next_try:
                self.next_try = now + RESUME_RETRY_SECS
                r = self.pending
                play = self.settings.get("resume", "ready") == "play"
                done = self._local(r, play) if r["source"] == "local" else self._spotify(r, play)
                if done is None:
                    self._finish()             # nothing left to put back
                elif done:
                    self._finish(r["source"])  # restored: show it
            return
        if not snap.has_track or snap.source not in ("local", "spotify"):
            return
        key = (snap.source, snap.uri, snap.playing)
        if key != self.last_key or (snap.playing and now - self.last_save >= RESUME_SAVE_SECS):
            self.last_key = key
            self.save(snap, now)

    def _local(self, r, play):
        """True: put back. False: not yet, try again. None: nothing to put back."""
        mp = getattr(self.player, "mp", None)
        if not isinstance(mp, dict):
            return False  # MPD isn't up yet (it starts after the first screen)
        secs = f"{max(0, r.get('pos', 0)) / 1000:.1f}"
        mpd = self.player.mpd
        try:
            if mp.get("file") == r["uri"] and mp.get("state") == "pause":
                cmds = [f"seekcur {secs}"]  # MPD put the song back itself: just the second
            else:
                found = mpd.values(f"playlistfind file {mpd_quote(r['uri'])}", "Pos")
                if found:
                    cmds = [f"seek {found[0]} {secs}", "pause 1"]
                else:
                    added = mpd.values(f"addid {mpd_quote(r['uri'])}", "Id")
                    if not added:
                        return None  # not in the library any more: nothing to put back
                    cmds = [f"seekid {added[0]} {secs}", "pause 1"]
            if play:
                cmds.append("pause 0")
            mpd.command_list(cmds)
        except MPDError as e:
            if "No such" in str(e) or "Not found" in str(e):
                return None  # gone from the library: give up
            return False     # MPD busy or restarting: try again
        self.player.kick.set()
        return True

    def _spotify(self, r, play):
        sp = self._sp()
        if not sp or not sp.get("username"):
            return False  # not signed in yet: Wi-Fi, or go-librespot hasn't logged back in
        body = {"uri": r.get("context") or r["uri"], "paused": not play, "position": max(0, r.get("pos", 0))}
        if r.get("context") and r["context"] != r["uri"]:
            body["skip_to_uri"] = r["uri"]
        try:
            status, _ = self.player.api.request("POST", "/player/play", body, timeout=10)
        except OSError:
            return False
        if status == 200:
            self.player.kick.set()
            return True
        if r.get("context"):  # some lists can't be restarted: the track on its own, next try
            self.pending = {k: v for k, v in r.items() if k != "context"}
        return False


# ======================================================================
# Audio analyser: FFT spectra, BS.1770 loudness, peaks (from MPD's FIFO)
# ======================================================================
F_LO, F_HI = 50, 10000
STRIP_BINS = 40           # Now Playing strip resolution (1 px per bin)
SPEC_ROWS = 88            # Scope spectrogram rows
HOP = RATE // ANALYSIS_FPS


def log_bands(freqs, n):
    """Index ranges (lo, hi) into the FFT for n log-spaced bands from F_LO to F_HI."""
    edges = F_LO * (F_HI / F_LO) ** (np.arange(n + 1) / n)
    last = len(freqs) - 1
    lo = np.minimum(np.searchsorted(freqs, edges[:-1]), last)
    hi = np.minimum(np.maximum(lo + 1, np.searchsorted(freqs, edges[1:])), last + 1)
    return lo, hi


def k_weight_power(freqs):
    """|H(f)|^2 of the BS.1770 K-weighting filter (48 kHz coefficients)."""
    w = 2 * np.pi * np.minimum(freqs, 23999) / 48000
    z1 = np.exp(-1j * w)
    z2 = z1 * z1

    def biquad(b, a):
        return (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)

    h1 = biquad((1.53512485958697, -2.69169618940638, 1.19839281085285),
                (1.0, -1.69065929318241, 0.73248077421585))
    h2 = biquad((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621))
    return np.abs(h1 * h2) ** 2


class Frame:
    __slots__ = ("spec", "bins", "mom", "short", "peak", "level")


class FifoSource:
    """MPD's old fifo output (22050:16:2): only a fallback now, when the loopback isn't set up.
    Opened without blocking (a plain open() waits until MPD starts writing). Reads wait at most
    `timeout` and check `cancelled` every TAP_POLL, so the analyser can always stop; a partial
    block is kept for the next call, so samples never come out misaligned."""
    eof_is_idle = True   # a fifo with no writer reads as EOF: that only means "nothing yet"

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.raw = self.out = None
        self.have = 0

    def fileno(self):
        return self.fd

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def read_block(self, n, timeout=None, cancelled=None):
        """n stereo frames as float32 (n, 2), scaled to +-1. The array is reused by the next call.
        None if cancelled; TimeoutError if a whole block didn't arrive in time."""
        need = n * 4
        if self.raw is None or len(self.raw) != need:
            self.raw, self.out, self.have = bytearray(need), np.empty((n, 2), np.float32), 0
        view = memoryview(self.raw)
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.have < need:
            if cancelled is not None and cancelled():
                return None
            wait = TAP_POLL
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
                if wait <= 0:
                    raise TimeoutError
            if not select.select([self.fd], [], [], wait)[0]:
                continue
            try:
                got = os.readv(self.fd, [view[self.have:need]])
            except BlockingIOError:
                continue
            if got == 0:
                if not self.eof_is_idle:
                    raise EOFError
                time.sleep(min(0.05, wait))
                continue
            self.have += got
        self.have = 0
        # int16 -> float32 is exact, and x * 2**-15 == x / 32768 exactly: the same numbers as before
        np.multiply(np.frombuffer(self.raw, "<i2").reshape(n, 2), np.float32(1 / 32768), out=self.out)
        return self.out


class LoopbackSource(FifoSource):
    """Records MPD's Visualiser output from the far end of the ALSA loopback card (snd-aloop).
    The loopback is clocked like a real sound card, so MPD keeps it a second ahead just like the
    DAC. MPD's fifo output is paced to real time instead, and MPD never queues more than 64
    chunks past its slowest output: at 48 kHz/24-bit that held the DAC to 0.67 s of buffer and
    it ran out a few seconds into a track ("Decoder is too slow; playing silence").

    arecord's pipe is read without blocking; close() always ends and reaps arecord (terminate,
    then kill), so none is left behind, whatever went wrong."""
    eof_is_idle = False  # arecord's pipe closing means arecord has gone

    def __init__(self, device=LOOPBACK_CAPTURE):
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "2",
             "--buffer-time=200000", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.fd = self.proc.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.raw = self.out = None
        self.have = 0

    def close(self):
        p = self.proc
        if p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        try:
            p.stdout.close()
        except OSError:
            pass


def open_audio_tap():
    """The loopback when it's set up (currawong_setup.sh loads snd-aloop), otherwise the old fifo."""
    if os.path.exists("/proc/asound/Loopback"):
        return LoopbackSource()
    return FifoSource(MPD_FIFO)


class SynthSource:
    """Simulated music, used only by --preview."""

    def __init__(self):
        self.n = 0
        self.rng = np.random.default_rng(1)

    def read_block(self, n):
        t = (self.n + np.arange(n)) / RATE
        self.n += n
        beat = 0.5
        ph = t % beat
        kick = np.sin(2 * np.pi * (45 + 90 * np.exp(-ph * 25)) * ph) * np.exp(-ph * 9)
        bar = (t // (beat * 4)).astype(int) % 4
        bass_f = np.array([55.0, 65.4, 49.0, 73.4])[bar]
        bass = np.sin(2 * np.pi * bass_f * t) * (0.6 + 0.4 * np.exp(-((t + beat / 2) % beat) * 6))
        noise = self.rng.standard_normal(n)
        hat = np.diff(noise, prepend=0.0) * np.exp(-((t + beat / 2) % beat) * 45)
        swell = 0.5 + 0.5 * np.sin(2 * np.pi * t / 8)
        pad = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.2, 329.6, 440.0)) * 0.25 * swell
        lead = np.sin(2 * np.pi * (880 + 220 * np.sin(2 * np.pi * t / 3)) * t) * 0.08 * (1 - swell)
        spec = np.fft.rfft(self.rng.standard_normal(n))
        spec /= np.sqrt(np.maximum(np.arange(spec.size), 1))       # 1/f: pink noise
        pink = np.fft.irfft(spec, n)
        pink *= 0.07 / (np.sqrt(np.mean(pink ** 2)) + 1e-9) * (0.6 + 0.4 * swell)
        left = 0.42 * kick + 0.22 * bass + 0.06 * hat + 0.12 * pad + lead + pink
        right = 0.42 * kick + 0.22 * bass + 0.09 * hat + 0.10 * pad + 0.6 * lead + pink
        return np.stack([left, right], axis=1).astype(np.float32) * 1.6


class VisualiserOutput:
    """MPD's "Visualiser" output (its copy of the music for the analyser, played into the ALSA
    loopback) switched on only while the analyser is recording it. The rest of the time (FAST
    visuals, Glance, Sleep, menus, Spotify) MPD needn't resample and write a second copy of every
    local track. Only that output is ever touched: the DAC's carries on as it was.

    Called from the analyser's own thread (never the UI's), with its own MPD connection. The
    state is cached: a command goes to MPD only on a real change. Off waits VIS_OFF_DELAY after
    the analyser stops, so a quick trip between Active and Glance, or into a menu, doesn't flip it.
    If MPD has no output by that name, or refuses the command, switching stops and the output is
    left on (the old always-on arrangement) until the next start. MPD being unreachable (it starts
    after the UI) only means trying again a little later."""

    def __init__(self, switching=True, name=MPD_OUTPUT_VIS, client=None, log=None):
        self.switching = switching
        self.name = name
        self.mpd = client
        self.log = log or (lambda m: print(f"Visualiser output: {m}", flush=True))
        self.state = None        # True/False as last set (or read); None = unknown
        self.oid = None
        self.off_since = None
        self.retry_at = 0.0
        self.unreachable = 0
        self.commands = 0        # how many enable/disable commands went to MPD

    def _client(self):
        if self.mpd is None:
            self.mpd = MPDClient(timeout=1.5)
        return self.mpd

    def invalidate(self):
        """Something may have changed it behind our back (no samples while it should be on)."""
        self.state = None

    def want(self, on, now):
        """The analyser wants the samples (on) or not. Returns whether the output should be on."""
        if not self.switching:
            on = True                                   # always on: just make sure it is, once
        elif on:
            self.off_since = None
        else:
            self.off_since = self.off_since if self.off_since is not None else now
            on = now - self.off_since < VIS_OFF_DELAY   # keep it on for a while yet
        if on != self.state and now >= self.retry_at:
            self._set(on, now)
        return on

    def _set(self, on, now):
        try:
            mpd = self._client()
            if self.oid is None:
                for o in mpd.records("outputs", first="outputid"):
                    if o.get("outputname") == self.name:
                        self.oid = o["outputid"]
                        self.state = o.get("outputenabled") == "1"
                if self.oid is None:
                    raise LookupError(f"MPD has no output called {self.name!r}")
                if self.state == on:
                    self.unreachable = 0
                    return
            mpd.command(f"{'enableoutput' if on else 'disableoutput'} {self.oid}")
            self.commands += 1
            self.state, self.unreachable = on, 0
        except MPDError as e:
            if "unreachable" in str(e):
                self.unreachable += 1          # MPD not up yet, or restarting: try again soon
                self.retry_at = now + min(30.0, 2.0 * self.unreachable)
                self.state = None
                return
            self._give_up(f"MPD refused it ({e})", now)
        except LookupError as e:
            self._give_up(str(e), now)

    def _give_up(self, why, now):
        """Switching isn't reliable here: leave the output on, as it always used to be."""
        was = self.switching
        self.switching = False
        self.state = None
        self.retry_at = now + 2.0
        if was:
            self.log(f"switching off, leaving it on: {why}")


class Analyser:
    """Turns MPD's Visualiser copy of the music into spectrum strips, loudness and (for Scope)
    spectra, five times a second, in its own thread, only while `enabled` (the local Now Playing
    screen or Scope, animating). The thread wakes when `enabled` changes, never waits on audio
    longer than TAP_POLL without checking it, restarts a tap that goes quiet, and can be stopped
    (stop(), also run at exit) and started again. It closes the tap (ending arecord) as soon as
    it isn't wanted, and tells `vis` (VisualiserOutput) so MPD's output follows."""

    def __init__(self, threaded=True, vis=None):
        self.lock = threading.Lock()
        self.frames = deque(maxlen=HISTORY)
        self._enabled = False
        self.need_spec = False                       # only the Scope page needs full spectra
        self.win = None                              # the numpy parts: built with the first audio (_prepare)
        self.mom_n = max(1, round(0.4 * ANALYSIS_FPS))
        self.z = deque(maxlen=max(1, round(3 * ANALYSIS_FPS)))
        self.vis = vis
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.stats = {"blocks": 0, "taps": 0, "stalls": 0, "failures": 0}
        if threaded:
            self.start()
            atexit.register(self.stop)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, on):
        on = bool(on)
        if on != self._enabled:
            self._enabled = on
            self.wake.set()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._run, name="analyser", daemon=True)
        self.thread.start()

    def stop(self, timeout=2.0):
        """Stop the worker (it closes its tap, ending arecord). It can be start()ed again."""
        self.stopping.set()
        self.wake.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        return t is None or not t.is_alive()

    def _prepare(self):
        """Window, weights and band edges. Not in __init__: that runs before the first screen, and
        numpy isn't loaded until after it."""
        freqs = np.fft.rfftfreq(NFFT, 1 / RATE)
        self.kw = k_weight_power(freqs)
        self.kw2 = self.kw[:, None]
        self.spec_norm = (NFFT / 4) ** 2             # full-scale sine peak bin (Hann)
        win = np.hanning(NFFT).astype(np.float32)
        self.ms_norm = NFFT * float(np.sum(win ** 2))
        self.rows = log_bands(freqs, SPEC_ROWS)
        self.bands = log_bands(freqs, STRIP_BINS)
        self.buf = np.zeros((NFFT, 2), np.float32)
        self.work = np.empty((NFFT, 2), np.float32)  # the windowed copy the FFT reads
        self.win2 = win[:, None]
        self.absbuf = None                           # |block|, for the peaks
        self.layout = None
        self.P = None                                # power, its running sum, K-weighted: made to
        self.win = win                               # match the FFT's precision on the first block

    def history(self):
        with self.lock:
            return list(self.frames)

    def latest(self):
        with self.lock:
            return self.frames[-1] if self.frames else None

    def _cancelled(self):
        return not self._enabled or self.stopping.is_set()

    def _rest(self, secs):
        """Sleep, but wake at once for a change of `enabled` or stop()."""
        self.wake.wait(secs)
        self.wake.clear()

    def _run(self):
        src = None
        quiet_since = 0.0
        failures = 0
        try:
            while not self.stopping.is_set():
                want = self._enabled
                if self.vis is not None:
                    try:
                        self.vis.want(want, time.monotonic())
                    except Exception as e:  # noqa: BLE001 (never let MPD trouble stop the analyser)
                        print(f"Visualiser output: {e}", flush=True)
                if not want:
                    if src is not None:
                        src.close()
                        src = None
                    self._rest(0.25)
                    continue
                if src is None:
                    try:
                        src = open_audio_tap()
                        self.stats["taps"] += 1
                        quiet_since = time.monotonic()
                    except OSError:
                        failures += 1
                        self.stats["failures"] += 1
                        self._rest(min(5.0, 0.5 * failures))
                        continue
                try:
                    block = src.read_block(HOP, timeout=0.5, cancelled=self._cancelled)
                except TimeoutError:
                    block = None
                except Exception:  # noqa: BLE001 (arecord gone, device error: start again shortly)
                    src.close()
                    src = None
                    failures += 1
                    self.stats["failures"] += 1
                    self._rest(min(5.0, 0.5 * failures))
                    continue
                if block is None:
                    if self._enabled and time.monotonic() - quiet_since > TAP_STALL_SECS:
                        # no samples for a while: start the tap again, and have the output re-checked
                        self.stats["stalls"] += 1
                        src.close()
                        src = None
                        if self.vis is not None:
                            self.vis.invalidate()
                        self._rest(0.5)
                    continue
                quiet_since = time.monotonic()
                failures = 0
                if self._enabled:
                    self.process(block)
        finally:
            if src is not None:
                src.close()

    def process(self, block):
        if self.win is None:
            self._prepare()
        n = len(block)
        if n >= NFFT:                     # a block (HOP) is longer than the FFT: its last NFFT samples
            self.buf[:] = block[-NFFT:]
        else:
            self.buf[:-n] = self.buf[n:]
            self.buf[-n:] = block
        if self.absbuf is None or self.absbuf.shape != block.shape:
            self.absbuf = np.empty_like(block)
        np.abs(block, out=self.absbuf)
        # each channel's peak from its own column: a reduction down axis 0 of an (n, 2) array is
        # ten times slower, and was most of this function's time. (Exact: max ignores order.)
        peaks = (self.absbuf[:, 0].max(), self.absbuf[:, 1].max())
        np.multiply(self.buf, self.win2, out=self.work)
        X = np.fft.rfft(self.work, axis=0)
        layout = (X.real.dtype, X.shape, X.strides[0] < X.strides[1])
        if self.P is None or layout != self.layout:
            # The same precision and memory order as numpy gives the FFT (numpy 1.x: float64 and
            # column-major; 2.x: float32): the sums below then round exactly as they always did.
            self.layout = layout
            self.P = np.empty_like(X.real)
            self.P_im = np.empty_like(X.real)
            self.run_sum = np.empty_like(X.real)
            self.cs = np.zeros((X.shape[0] + 1, 2))  # float64, as vstack([zeros, cumsum]) gave
            self.pk = np.empty_like(X.real, dtype=np.result_type(X.real.dtype, self.kw.dtype))
        P = self.P
        np.square(X.real, out=P)
        np.square(X.imag, out=self.P_im)
        P += self.P_im
        self.cs[1:] = np.cumsum(P, axis=0, out=self.run_sum)
        cs = self.cs

        def band_db(lo_hi):
            lo, hi = lo_hi
            band = cs[hi] - cs[lo]   # total power in each log band (pink-flat for music)
            return 10 * np.log10(band / self.spec_norm + 1e-12)

        spec = band_db(self.rows) if self.need_spec else None
        strip = band_db(self.bands)
        pk = np.multiply(P, self.kw2, out=self.pk)
        total = pk[0] + 2 * pk[1:-1].sum(axis=0) + pk[-1]
        self.z.append(total / self.ms_norm)
        zs = np.array(self.z)
        mom = -0.691 + 10 * np.log10(zs[-self.mom_n:].mean(axis=0) + 1e-12)
        short = -0.691 + 10 * np.log10(zs.mean(axis=0) + 1e-12)

        f = Frame()
        f.spec = None if spec is None else (spec[:, 0].copy(), spec[:, 1].copy())
        v = np.clip((strip + 50) / 45, 0, 1) ** 1.5
        f.bins = (v[:, 0], v[:, 1])
        f.mom = (float(mom[0]), float(mom[1]))
        f.short = (float(short[0]), float(short[1]))
        f.peak = tuple(float(20 * math.log10(p + 1e-9)) for p in peaks)
        f.level = tuple(float(min(1.0, max(0.0, (m + 40) / 34))) for m in f.mom)
        self.stats["blocks"] += 1
        with self.lock:
            self.frames.append(f)


# ======================================================================
# Screen base: event-driven rendering
# ======================================================================
class Screen:
    version = 0               # bump when internal state changes

    def handle(self, kind, arg):
        return None

    def animates(self, snap, mode):
        return False

    def render_key(self, snap, now):
        """Anything that changes what's drawn. Same key -> no redraw."""
        return (snap.source, snap.has_track, snap.uri, snap.paused, id(snap.cover),
                snap.title, self.version, STATUS.get("wifi"), STATUS.get("output"),
                STATUS.get("locked"), STATUS.get("scrub"))

    def haptic(self):
        """(kind, positions, position, sync) for the ESP32 dial on this screen."""
        return ("scrub", 0, 0, 0)

    def haptic_extras(self):
        """{"marks": [...], "magnet": n} for this screen's dial, or None."""
        return None

    def dial_range(self, snap):
        """(positions, position) for walls that follow playback, or None."""
        return None


class NowPlaying:
    """The dial on the Now Playing screens. CLICKS (Settings > Scrub style): clicks through the song
    with walls at 0:00 and the end, and a fast spin coasts. SHUTTLE: a spring-back wheel, where a
    bigger twist scrubs faster and letting go stops."""
    now_playing = True

    def haptic(self):
        if self.app.settings.get("scrub_style") == "shuttle":
            return ("elastic", 0, 0, 0)
        return ("scrub", 0, 0, 0)

    def dial_range(self, snap):
        if self.app.settings.get("scrub_style") == "shuttle" or not snap.has_track or not snap.dur:
            return None
        step = max(1, self.app.settings.get("scrub_step", 2)) * 1000
        return int(snap.dur // step) + 1, int(min(snap.pos, snap.dur) // step)


def shuttle_speed(twist):
    """Shuttle gear for a twist of 0..100: 0 (stopped) in the middle, then 2x up to 32x."""
    for limit, speed in SHUTTLE_GEARS:
        if twist < limit:
            return speed
    return 32


def letter_marks(names):
    """Where each new first letter starts in a sorted list: the dial clicks harder there."""
    marks, last = [], None
    for i, n in enumerate(names):
        c = n.strip()[:1].upper()
        k = c if c.isalpha() else "#"
        if k != last:
            marks.append(i)
            last = k
    return marks[:64]


def wifi_icon(d, x, y, on):
    """12 x 10 Wi-Fi glyph at (x, y): three arcs, or arcs with a slash when offline."""
    for r in (3, 6, 9):
        d.arc((x + 6 - r, y + 10 - r, x + 6 + r, y + 10 + r), 225, 315, fill=BLACK, width=1)
    d.rectangle((x + 5, y + 9, x + 7, y + 10), fill=BLACK)
    if not on:
        d.line((x, y + 10, x + 12, y), fill=BLACK, width=1)


BT_RUNE = ("...#...",
           "...##..",
           "...#.#.",
           "#..#..#",
           ".#.#.#.",
           "..###..",
           ".#.#.#.",
           "#..#..#",
           "...#.#.",
           "...##..",
           "...#...")
BT_PIXELS = [(x, y) for y, row in enumerate(BT_RUNE) for x, c in enumerate(row) if c == "#"]


def bt_icon(d, x, y, fill=BLACK):
    """7 x 11 Bluetooth logo (the rune) at (x, y)."""
    d.point([(x + px, y + py) for px, py in BT_PIXELS], fill=fill)


class Icon:
    """A small picture a list can show in its value column instead of text."""

    def __init__(self, name, width, draw):
        self.name, self.width, self.draw = name, width, draw

    def __repr__(self):
        return f"<{self.name}>"


BT_LOGO = Icon("bluetooth", 7, lambda d, x, y, fill: bt_icon(d, x, y + 1, fill))


def output_value():
    """OUTPUT in the menus: the Bluetooth logo, or WIRED."""
    return BT_LOGO if STATUS.get("output") == "bluetooth" else "WIRED"


def lock_icon(d, x, y):
    """9 x 11 padlock: shown while the dial is pocket-locked."""
    d.arc((x + 1, y, x + 7, y + 8), 180, 360, fill=BLACK, width=1)
    d.line((x + 1, y + 4, x + 1, y + 5), fill=BLACK)
    d.line((x + 7, y + 4, x + 7, y + 5), fill=BLACK)
    d.rectangle((x, y + 5, x + 8, y + 11), fill=BLACK)


def status_icons(d, batt):
    """Battery, Wi-Fi, BT and lock, right to left along the footer. Returns the free space's right edge."""
    battery_icon(d, W - 30, H - 16, batt)
    x = W - 50
    if STATUS.get("wifi") is not None:
        wifi_icon(d, x, H - 17, STATUS["wifi"])
        x -= 18
    if STATUS.get("output") == "bluetooth":
        bt_icon(d, x + 3, H - 17)
        x -= 13
    if STATUS.get("locked"):
        lock_icon(d, x + 2, H - 18)
        x -= 12
    return x


def scrub_badge(d, cx, y):
    """'FF 8x' / 'REW 4x' in a black box while auto-scrubbing."""
    text = STATUS.get("scrub")
    if not text:
        return
    w = tlen(F_SBOLD, text) + 12
    d.rectangle((cx - w / 2, y, cx + w / 2, y + 15), fill=BLACK)
    d.text((cx - w / 2 + 6, y + 1), text, font=F_SBOLD, fill=WHITE)


def footer(d, snap, batt, glyph=True):
    d.line((0, H - 22, W - 1, H - 22), fill=BLACK)
    right = status_icons(d, batt)
    if snap.has_track:
        text = f"{'||' if snap.paused else '>'} {snap.title}" if glyph else snap.title
        d.text((8, H - 17), fit(text, F_TINY, right - 12), font=F_TINY, fill=BLACK)


# ======================================================================
# Spotify screen (simple): big cover, info, progress
# ======================================================================
class SpotifyScreen(NowPlaying, Screen):
    def __init__(self, app):
        self.app = app
        self.static_key = None
        self.static = None
        self.playhead = 8

    def bird_layer(self, img, now):
        """The currawong: on the twig at the playhead, or on its branch when nothing's playing."""
        bird = self.app.bird
        if not bird:
            return None
        if self.app.snap.has_track and not self.app.paused_idle:
            return bird.draw_now(img, now, self.playhead)
        return bird.draw_idle(img, now)

    def handle(self, kind, arg):
        if kind == "rot":
            self.app.player.seek_by(self.app.snap, arg * self.app.settings.get("scrub_step", 2) * 1000)
        elif kind == "press" and arg == "enc_sw":
            return main_menu(self.app)
        elif kind == "press" and arg == "back" and not self.app.lock.locked(time.monotonic()):
            return main_menu(self.app)  # BACK opens the menu here too (not while pocket-locked)
        return None

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int(snap.pos // 1000), int(snap.dur // 1000),
                                                self.app.battery.state(), snap.playing, self.app.paused_idle)

    def _static_layer(self, snap):
        key = (snap.has_track, snap.uri, id(snap.cover), snap.paused, snap.source, snap.reachable,
               STATUS.get("resuming"), bool(self.app.bird), snap.playing, self.app.paused_idle)
        if key == self.static_key:
            return self.static
        self.static_key = key
        img = Image.new("1", (W, H), WHITE)
        d = Draw(img)
        idle = self.app.paused_idle
        if (snap.has_track and not idle) or not self.app.bird:  # idle: paper, for the bird's branch
            img.paste(snap.cover["large"] if snap.cover else dots(W, W), (0, 0))
        d.line((0, W, W - 1, W), fill=BLACK)
        if idle:
            d.text((8, W + 10), "PAUSED", font=F_BIG, fill=BLACK)
            d.text((8, W + 36), fit(snap.title, F_BODY, W - 16), font=F_BODY, fill=BLACK)
            d.text((8, W + 54), fit(snap.artists, F_SMALL, W - 16), font=F_SMALL, fill=BLACK)
            d.text((8, W + 82), "Play carries on from here.", font=F_SMALL, fill=BLACK)
        elif snap.has_track:
            y = W + 8
            for line in wrap(snap.title, F_BIG, W - 16, 2):
                d.text((8, y), line, font=F_BIG, fill=BLACK)
                y += 20
            d.text((8, y + 2), fit(snap.artists, F_BODY, W - 16), font=F_BODY, fill=BLACK)
            d.text((8, y + 20), fit(snap.album, F_SMALL, W - 16), font=F_SMALL, fill=BLACK)
        elif STATUS.get("resuming"):
            d.text((8, W + 10), "RESUMING", font=F_BIG, fill=BLACK)
            for n, line in enumerate(wrap(STATUS["resuming"], F_BODY, W - 16, 2)):
                d.text((8, W + 36 + n * 18), line, font=F_BODY, fill=BLACK)
            if snap.source == "spotify" or getattr(self.app.player, "hold", None) == "spotify":
                d.text((8, W + 76), "Once Spotify signs in on Wi-Fi.", font=F_SMALL, fill=BLACK)
        else:
            d.text((8, W + 10), "NOT PLAYING", font=F_BIG, fill=BLACK)
            if snap.reachable:
                for n, line in enumerate(["Pick Currawong in Spotify,", "or choose music from", "LIBRARY in the menu."]):
                    d.text((8, W + 38 + n * 16), line, font=F_SMALL, fill=BLACK)
            else:
                d.text((8, W + 38), "Waiting for the players…", font=F_SMALL, fill=BLACK)
        d.line((0, H - 22, W - 1, H - 22), fill=BLACK)
        if snap.has_track:
            d.text((8, H - 17), f"{'Playing' if snap.playing else 'Paused'} · {snap.fmt}", font=F_TINY, fill=BLACK)
        elif self.app.bird:
            d.text((8, H - 17), "Idle", font=F_TINY, fill=BLACK)
        self.static = img
        return img

    def render(self, now, snap, mode):
        img = self._static_layer(snap).copy()
        d = Draw(img)
        if snap.has_track and not self.app.paused_idle:
            self.playhead = twig(img, snap.pos / snap.dur if snap.dur else 0.0)
            d.text((8, 350), mmss(snap.pos), font=F_SMALL, fill=BLACK)
            rem = "-" + mmss(max(0, snap.dur - snap.pos))
            d.text((W - 8 - tlen(F_SMALL, rem), 350), rem, font=F_SMALL, fill=BLACK)
            scrub_badge(d, W / 2, 348)
        status_icons(d, self.app.battery)
        return img


# ======================================================================
# Local Now Playing (visual): cover + info, spectrum strips, times
# ======================================================================
SCROLL = 4                # px per frame (5 fps x 4 px = 20 px per second)
STRIP_W = STRIP_BINS      # 40
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
THRESH = [[(v + 0.5) / 16 for v in row] for row in BAYER]

COVER = 104
COVER_X, COVER_Y = 6, 6
INFO_X = 116
INFO_W = W - INFO_X - 6
HDR_BOTTOM = 117
LOW_Y = 122
LS_X = 2
RS_X = W - 2 - STRIP_W                  # 198
STRIP_Y = LOW_Y + 2
AXIS_Y = H - 16
WF_H = AXIS_Y - 2 - STRIP_Y
C_L = LS_X + STRIP_W + 1                # 43
C_R = RS_X - 2                          # 196
C_MID = (C_L + C_R) // 2
BAR_X0, BAR_X1 = C_MID - 5, C_MID + 4
BAR_Y0, BAR_Y1 = LOW_Y + 98, AXIS_Y - 22
FAST_RANGES = ((LS_X, LS_X + STRIP_W), (RS_X, RS_X + STRIP_W))


class VWaterfall:
    """Vertical spectrogram strip, newest rows at the bottom, plus a magnitude trace."""

    def __init__(self, height, mirror):
        self.h = height
        self.img = Image.new("1", (STRIP_W, height), WHITE)
        self.mirror = mirror  # True: bass on the right edge (inner side of the left strip)
        self.row = 0
        self.last_x = None

    def push(self, vals, level):
        im, h = self.img, self.h
        im.paste(im.crop((0, SCROLL, STRIP_W, h)), (0, 0))
        v = np.asarray(vals, dtype=np.float32)
        if self.mirror:
            v = v[::-1]
        rows = (self.row + np.arange(SCROLL)) & 3
        ink = (v[None, :] > TABLES.THRESH_TILE[rows]) & (v[None, :] > 0.03)
        im.paste(Image.fromarray(~ink), (0, h - SCROLL))
        self.row += SCROLL
        # Magnitude trace: draw just the newest segment; it scrolls up with the strip
        x = (1 - level) * (STRIP_W - 1) if self.mirror else level * (STRIP_W - 1)
        if self.last_x is not None:
            d = ImageDraw.Draw(im)
            seg = (self.last_x, h - 1 - SCROLL, x, h - 1)
            d.line(seg, fill=WHITE, width=3)
            d.line(seg, fill=BLACK, width=1)
        self.last_x = x

    def composite(self):
        return self.img


class LocalScreen(NowPlaying, Screen):
    def __init__(self, app):
        self.app = app
        self.wl = VWaterfall(WF_H, mirror=True)
        self.wr = VWaterfall(WF_H, mirror=False)
        self.static_key = None
        self.static = None
        self.frame_img = None

    def handle(self, kind, arg):
        if kind == "rot":
            self.app.player.seek_by(self.app.snap, arg * self.app.settings.get("scrub_step", 2) * 1000)
        elif kind == "press" and arg == "enc_sw":
            return main_menu(self.app)
        elif kind == "press" and arg == "back" and not self.app.lock.locked(time.monotonic()):
            return main_menu(self.app)  # BACK opens the menu here too (not while pocket-locked)
        return None

    def animates(self, snap, mode):
        return mode == "active" and snap.playing

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int(snap.pos // 1000), snap.fmt,
                                                self.app.battery.state())

    def key_for(self, snap):
        return (snap.reachable, snap.has_track, snap.uri, id(snap.cover), snap.paused, snap.fmt,
                STATUS.get("output"))

    def advance(self, snap):
        f = self.app.analyser.latest()
        if f is None:
            zeros = [0.0] * STRIP_BINS
            self.wl.push(zeros, 0.0)
            self.wr.push(zeros, 0.0)
        else:
            self.wl.push(f.bins[0], f.level[0])
            self.wr.push(f.bins[1], f.level[1])

    def _paste_strips(self, img):
        img.paste(self.wl.composite(), (LS_X, STRIP_Y))
        img.paste(self.wr.composite(), (RS_X, STRIP_Y))
        d = Draw(img)
        label(d, LS_X + 2, STRIP_Y + 2, "L")
        label(d, RS_X + STRIP_W - 8, STRIP_Y + 2, "R")

    def fast_columns(self):
        """Active-mode frame: only the strips change. Returns column ranges to send."""
        self._paste_strips(self.frame_img)
        return FAST_RANGES

    def _static_layer(self, snap):
        key = self.key_for(snap)
        if key == self.static_key:
            return self.static
        self.static_key = key
        img = Image.new("1", (W, H), WHITE)
        d = Draw(img)

        d.rectangle((0, 0, W - 1, HDR_BOTTOM), outline=BLACK)
        img.paste(snap.cover["small"] if snap.cover else dots(COVER, COVER), (COVER_X, COVER_Y))
        d.rectangle((COVER_X, COVER_Y, COVER_X + COVER - 1, COVER_Y + COVER - 1), outline=BLACK)
        if snap.has_track:
            for n, line in enumerate(wrap(snap.title, F_BOLD, INFO_W, 2)):
                d.text((INFO_X, 6 + n * 16), line, font=F_BOLD, fill=BLACK)
            d.text((INFO_X, 42), fit(snap.artists, F_BODY, INFO_W), font=F_BODY, fill=BLACK)
            d.text((INFO_X, 60), fit(snap.album, F_SMALL, INFO_W), font=F_SMALL, fill=BLACK)
            d.line((INFO_X, 80, W - 7, 80), fill=BLACK)
            d.text((INFO_X, 85), snap.fmt, font=F_TINY, fill=BLACK)
            state = "PAUSED" if snap.paused else "PLAYING"
            if STATUS.get("output") == "bluetooth":
                bt_icon(d, INFO_X, 99)
                d.text((INFO_X + 11, 100), state, font=F_TINY, fill=BLACK)
            else:
                d.text((INFO_X, 100), f"WIRED {state}", font=F_TINY, fill=BLACK)

        d.rectangle((0, LOW_Y, W - 1, H - 1), outline=BLACK)
        d.line((C_L, LOW_Y, C_L, H - 1), fill=BLACK)
        d.line((C_R, LOW_Y, C_R, H - 1), fill=BLACK)
        d.line((0, AXIS_Y - 1, C_L, AXIS_Y - 1), fill=BLACK)
        d.line((C_R, AXIS_Y - 1, W - 1, AXIS_Y - 1), fill=BLACK)
        d.text((LS_X + 1, AXIS_Y + 2), "10k", font=F_TINY, fill=BLACK)
        d.text((LS_X + STRIP_W - 1 - tlen(F_TINY, "50"), AXIS_Y + 2), "50", font=F_TINY, fill=BLACK)
        d.text((RS_X + 1, AXIS_Y + 2), "50", font=F_TINY, fill=BLACK)
        d.text((RS_X + STRIP_W - 1 - tlen(F_TINY, "10k"), AXIS_Y + 2), "10k", font=F_TINY, fill=BLACK)

        centred(d, C_MID, LOW_Y + 8, "ELAPSED", F_TINY)
        centred(d, C_MID, LOW_Y + 48, "REMAIN", F_TINY)
        d.line((C_L + 8, LOW_Y + 88, C_R - 8, LOW_Y + 88), fill=BLACK)
        d.rectangle((BAR_X0, BAR_Y0, BAR_X1, BAR_Y1), outline=BLACK)
        if snap.has_track and snap.dur:
            mins = int(snap.dur // 60000)
            for m in range(1, mins + 1):
                y = BAR_Y0 + int((BAR_Y1 - BAR_Y0) * m * 60000 / snap.dur)
                d.line((BAR_X0 - 5, y, BAR_X0 - 2, y), fill=BLACK)
                d.line((BAR_X1 + 2, y, BAR_X1 + 5, y), fill=BLACK)
                d.text((BAR_X1 + 8, y - 5), f"{m}m", font=F_TINY, fill=BLACK)
        self.static = img
        return img

    def render(self, now, snap, mode):
        img = self._static_layer(snap).copy()
        self._paste_strips(img)
        d = Draw(img)

        newest = STRIP_Y + WF_H
        if self.animates(snap, mode):
            for secs in (1, 5):
                y = newest - secs * FPS_ACTIVE * SCROLL
                t = f"{secs}s"
                d.line((C_L, y, C_L + 3, y), fill=BLACK)
                d.line((C_R - 3, y, C_R, y), fill=BLACK)
                d.text((C_L + 5, y - 5), t, font=F_TINY, fill=BLACK)
                d.text((C_R - 5 - tlen(F_TINY, t), y - 5), t, font=F_TINY, fill=BLACK)
        else:
            d.text((C_L + 5, newest - 12), "HOLD", font=F_TINY, fill=BLACK)

        if snap.has_track:
            centred(d, C_MID, LOW_Y + 20, mmss(snap.pos), F_HUGE)
            centred(d, C_MID, LOW_Y + 60, "-" + mmss(max(0, snap.dur - snap.pos)), F_HUGE)
            if snap.dur:
                fill_to = BAR_Y0 + int((BAR_Y1 - BAR_Y0) * snap.pos / snap.dur)
                d.rectangle((BAR_X0, BAR_Y0, BAR_X1, fill_to), fill=BLACK)
            scrub_badge(d, C_MID, LOW_Y + 81)

        battery_icon(d, W - 30, 99, self.app.battery)
        if STATUS.get("locked"):
            lock_icon(d, W - 44, 98)
        self.frame_img = img
        return img


class Home(Screen):
    """Routes to the Spotify or local screen depending on the active source."""

    def __init__(self, app):
        self.app = app
        self.spotify = SpotifyScreen(app)
        self.local = LocalScreen(app)

    def current(self, snap):
        # FAST visuals: local files get the simple big-cover screen too, so no analyser runs at all
        if self.app.paused_idle:
            return self.spotify   # paused a while: the bird's branch, whatever's playing
        if snap.source == "local" and self.app.settings.get("visuals", 1) < 2:
            return self.local
        return self.spotify


# ======================================================================
# Scope page
# ======================================================================
SC_X = 1
SC_PW = 204
SC_B = SC_X + SC_PW
SC_HDR = 60
SC_TOP = 64
LOUD_H = 62
HATCH_H = 8
RULER_H = 16
Y_LL = SC_TOP + 1
Y_H1 = Y_LL + LOUD_H
Y_LS = Y_H1 + HATCH_H
Y_RU = Y_LS + SPEC_ROWS
Y_RS = Y_RU + RULER_H
Y_H2 = Y_RS + SPEC_ROWS
Y_RL = Y_H2 + HATCH_H
LU_TOP, LU_BOT = -12.0, -25.0
PK_FLOOR = -36.0
T_MIN, T_MAX = 0.02, 8.0
CONTOURS = (0, -20, -40)



def _frame_idx():
    ages = T_MIN * (T_MAX / T_MIN) ** (1 - np.arange(SC_PW) / (SC_PW - 1))
    return np.minimum((ages * ANALYSIS_FPS).astype(int), HISTORY - 1)


class _Tables:
    """The visualisers' numpy arrays, built on first use instead of when this file loads, so numpy
    isn't needed before the first screen."""
    BUILD = {
        "THRESH_TILE": lambda: np.tile(np.array(THRESH, dtype=np.float32), (1, STRIP_BINS // 4 + 1))[:, :STRIP_BINS],
        "FRAME_IDX": _frame_idx,
        "STIP_SPEC": lambda: stipple_mask(SPEC_ROWS, SC_PW),
        "SPEC_FLOOR": lambda: np.full(SPEC_ROWS, -120.0),
        "STIP_LOUD": lambda: stipple_mask(LOUD_H, SC_PW),
        "LOUD_ROWS": lambda: np.arange(LOUD_H)[:, None],
    }

    def __getattr__(self, name):
        if name not in self.BUILD:
            raise AttributeError(name)
        value = self.BUILD[name]()
        setattr(self, name, value)
        return value


TABLES = _Tables()


def x_of_age(age):
    return (1 - math.log(age / T_MIN) / math.log(T_MAX / T_MIN)) * (SC_PW - 1)


def lu_y(v, top, mirror):
    frac = (LU_TOP - v) / (LU_TOP - LU_BOT)
    frac = min(1.0, max(0.0, frac))
    off = frac * (LOUD_H - 1)
    return top + (LOUD_H - 1 - off if mirror else off)


def spec_row(f):
    return math.log(f / F_LO) / math.log(F_HI / F_LO) * (SPEC_ROWS - 1)


def smooth(D):
    P = np.pad(D, 1, mode="edge")
    D = (P[:-2, 1:-1] + 2 * P[1:-1, 1:-1] + P[2:, 1:-1]) / 4
    P = np.pad(D, 1, mode="edge")
    return (P[1:-1, :-2] + 2 * P[1:-1, 1:-1] + P[1:-1, 2:]) / 4


def edges(A):
    e = np.zeros_like(A)
    e[:, 1:] |= A[:, 1:] != A[:, :-1]
    e[1:, :] |= A[1:, :] != A[:-1, :]
    return e


def build_scope_static():
    img = Image.new("1", (W, H), WHITE)
    d = Draw(img)
    d.rectangle((0, 0, W - 1, SC_HDR), outline=BLACK)
    for x0, x1 in ((3, 50), (53, 110), (113, W - 4)):
        d.rectangle((x0, 3, x1, SC_HDR - 3), outline=BLACK)
    d.rectangle((0, SC_TOP, W - 1, H - 1), outline=BLACK)
    d.line((SC_B, SC_TOP, SC_B, H - 1), fill=BLACK)
    img.paste(hatch(SC_PW, HATCH_H), (SC_X, Y_H1))
    img.paste(hatch(SC_PW, HATCH_H), (SC_X, Y_H2))
    for y in (Y_RU, Y_RU + RULER_H - 1):
        d.line((SC_X, y, SC_B, y), fill=BLACK)
    sx = SC_B + 1
    for v in (-15, -18, -21):
        for top, mirror in ((Y_LL, False), (Y_RL, True)):
            y = lu_y(v, top, mirror)
            d.line((sx, y, sx + 3, y), fill=BLACK)
            d.text((sx + 5, y - 6), str(v), font=F_TINY, fill=BLACK)
    for f, t in ((50, "50"), (200, "200"), (1000, "1.0k"), (5000, "5.0k"), (10000, "10k")):
        r = spec_row(f)
        for y in (Y_LS + SPEC_ROWS - 1 - r, Y_RS + r):
            d.line((sx, y, sx + 3, y), fill=BLACK)
            ty = min(max(y - 6, SC_TOP + 2), H - 13)
            d.text((sx + 5, ty), t, font=F_TINY, fill=BLACK)
    for age in (5, 2, 1, 0.5, 0.2, 0.1):
        x = SC_X + x_of_age(age)
        d.line((x, Y_RU, x, Y_RU + 3), fill=BLACK)
        if age in (5, 1):
            d.text((x + 2, Y_RU + 3), f"{age}s", font=F_TINY, fill=BLACK)
    return img


class ScopeScreen(NowPlaying, Screen):
    def __init__(self, app):
        self.app = app
        self.static = build_scope_static()
        self.plot = None

    def handle(self, kind, arg):
        if kind == "rot":
            self.app.player.seek_by(self.app.snap, arg * self.app.settings.get("scrub_step", 2) * 1000)
        elif kind == "press" and arg == "enc_sw":
            return main_menu(self.app)
        elif kind == "press" and arg == "back":
            return "pop"
        return None

    def animates(self, snap, mode):
        return mode == "active" and snap.source == "local" and snap.playing

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int(snap.pos // 1000),)

    def _plots(self):
        frames = self.app.analyser.history()[::-1]   # 0 = newest
        img = self.static.copy()
        d = Draw(img)
        if not frames:
            return img
        n = len(frames)
        idx = np.minimum(TABLES.FRAME_IDX, n - 1)
        for c, (y_spec, y_loud, mirror) in enumerate(((Y_LS, Y_LL, False), (Y_RS, Y_RL, True))):
            S = np.stack([f.spec[c] if f.spec else TABLES.SPEC_FLOOR for f in frames])
            D = smooth(smooth(S[idx].T))
            D = D if mirror else D[::-1]
            ink = (D > -20) & TABLES.STIP_SPEC
            contour_px = {}
            for lev in CONTOURS:
                e = edges(D >= lev)
                ink |= e
                contour_px[lev] = e
            img.paste(Image.fromarray(~ink), (SC_X, y_spec))
            placed = []
            for lev in CONTOURS:
                e = contour_px[lev]
                for x in range(16, int(SC_PW * 0.7), 9):
                    rows = np.nonzero(e[:, x])[0]
                    if rows.size == 0:
                        continue
                    y = int(rows[0]) + y_spec
                    box = (x, y - 6, x + 6 * len(str(lev)) + 4, y + 6)
                    if any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3])
                           for b in placed):
                        continue
                    ty = min(max(y - 6, y_spec + 1), y_spec + SPEC_ROWS - 12)
                    label(d, SC_X + x + 2, ty, str(lev))
                    placed.append(box)
                    break
            if mirror:
                label(d, SC_X + 2, y_spec + SPEC_ROWS - 13, "R SPECTRUM")
            else:
                label(d, SC_X + 2, y_spec + 2, "L SPECTRUM")

            mom = np.array([f.mom[c] for f in frames])[idx]
            short = np.array([f.short[c] for f in frames])[idx]
            peak = np.array([f.peak[c] for f in frames])[idx]
            frac = np.clip((peak - PK_FLOOR) / -PK_FLOOR, 0, 1) * 0.6
            h_pk = (frac * (LOUD_H - 1)).astype(int)
            if mirror:
                area = TABLES.LOUD_ROWS <= h_pk[None, :]
            else:
                area = TABLES.LOUD_ROWS >= (LOUD_H - 1 - h_pk)[None, :]
            loud_ink = (area & TABLES.STIP_LOUD) | edges(area)
            img.paste(Image.fromarray(~loud_ink), (SC_X, y_loud))
            d.line([(SC_X + x, lu_y(v, y_loud, mirror)) for x, v in enumerate(mom)], fill=BLACK, width=1)
            d.line([(SC_X + x, lu_y(v, y_loud, mirror)) for x, v in enumerate(short)], fill=BLACK, width=3)

            val = frames[0].short[c]
            y = min(max(lu_y(val, y_loud, mirror), y_loud + 8), y_loud + LOUD_H - 9)
            text = f"{val:.1f}LU" if val > -70 else "--.-LU"
            tw = tlen(F_SMALL, text)
            x1 = SC_B - 9
            x0 = x1 - tw - 6
            d.polygon([(x0, y - 7), (x1, y - 7), (SC_B - 1, y), (x1, y + 7), (x0, y + 7)],
                      fill=WHITE, outline=BLACK)
            d.text((x0 + 3, y - 6), text, font=F_SMALL, fill=BLACK)
            if mirror:
                d.text((SC_X + 3, y_loud + LOUD_H - 13), "R", font=F_TINY, fill=BLACK)
            else:
                d.text((SC_X + 3, y_loud + 2), "L", font=F_TINY, fill=BLACK)
        return img

    def render(self, now, snap, mode):
        if snap.source != "local":
            img, d = titled("SCOPE")
            for n, line in enumerate(["Visualisers run on", "local files from", "the LIBRARY.", "",
                                      "Spotify stays simple", "to keep the audio", "smooth."]):
                d.text((10, 44 + n * 20), line, font=F_BODY, fill=BLACK)
            footer(d, snap, self.app.battery)
            return img
        if self.animates(snap, mode) or self.plot is None:
            self.plot = self._plots()
        img = self.plot.copy()
        d = Draw(img)
        centred(d, 27, 23, f"{snap.pos / 1000:.2f}" if snap.has_track else "--.--", F_SMALL)
        status = "PAUSED" if snap.paused else "PLAYING"
        pct = f"{int(100 * snap.pos / snap.dur)}%" if snap.dur else "--%"
        centred(d, 82, 15, status, F_SMALL)
        centred(d, 82, 31, pct, F_SMALL)
        tx, tw = 118, W - 4 - 118 - 4
        d.text((tx, 8), fit(snap.title, F_SBOLD, tw), font=F_SBOLD, fill=BLACK)
        d.text((tx, 23), fit(snap.artists, F_SBOLD, tw), font=F_SBOLD, fill=BLACK)
        d.text((tx, 39), fit(("2CH " + snap.fmt.split(" ", 1)[-1]) if snap.fmt else "", F_SMALL, tw),
               font=F_SMALL, fill=BLACK)
        if not self.animates(snap, mode):
            d.text((SC_B - 30, Y_RU + 3), "HOLD", font=F_TINY, fill=BLACK)
        return img


# ======================================================================
# System helpers: commands, Wi-Fi, Bluetooth, audio output
# ======================================================================
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x01|\x02|\r")


def run(cmd, timeout=20, sudo=False):
    """Run a command, return (exit code, combined output). sudo uses -n (never prompts)."""
    if sudo:
        cmd = ["sudo", "-n"] + list(cmd)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def nm_split(line):
    """Split one line of `nmcli -t` output (colons inside values are escaped)."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


class WifiManager:
    IFACE = "wlan0"

    def __init__(self):
        self._cur = (-999.0, None, None)

    def connected(self):
        """Cheap check, safe to call every second."""
        try:
            with open(f"/sys/class/net/{self.IFACE}/operstate") as f:
                return f.read().strip() == "up"
        except OSError:
            return None

    def radio_on(self):
        rc, out = run(["nmcli", "radio", "wifi"], timeout=10)
        return "disabled" not in out  # unknown counts as on

    def set_radio(self, on):
        rc, out = run(["nmcli", "radio", "wifi", "on" if on else "off"], timeout=20, sudo=True)
        self._cur = (-999.0, None, None)
        return rc == 0

    def state(self):
        """NetworkManager's word for wlan0: connected, connecting (...), disconnected, unavailable."""
        rc, out = run(["nmcli", "-t", "-f", "DEVICE,STATE", "dev"], timeout=10)
        for line in out.splitlines():
            p = nm_split(line)
            if len(p) >= 2 and p[0] == self.IFACE:
                return p[1]
        return ""

    def profile_ssid(self, name):
        rc, out = run(["nmcli", "-t", "-g", "802-11-wireless.ssid", "con", "show", "id", name], timeout=10)
        return out.strip() or None

    def kick(self):
        """What you'd do by hand when it won't join: scan afresh, then bring up the strongest saved
        network in range. Returns True if one came up."""
        visible = {n["ssid"]: n["signal"] for n in self.scan()}
        best = None
        for name in self.saved():
            ssid = self.profile_ssid(name) or name
            if ssid in visible and (best is None or visible[ssid] > best[1]):
                best = (name, visible[ssid])
        return bool(best) and self.up(best[0])

    def cached_name(self):
        """The joined network's name without waiting: the last one read, with a background
        refresh started once it's a minute old (for the menu, which reads it every frame)."""
        if time.monotonic() - self._cur[0] > 60 and not getattr(self, "_refreshing", False):
            self._refreshing = True

            def refresh():
                try:
                    self.current()
                finally:
                    self._refreshing = False
            threading.Thread(target=refresh, name="wifi-name", daemon=True).start()
        return self._cur[1]

    def current(self, now=None):
        """(ssid, signal %) of the active network, cached for 10 s."""
        now = time.monotonic() if now is None else now
        if now - self._cur[0] > 10:
            ssid = sig = None
            rc, out = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"], timeout=10)
            for line in out.splitlines():
                p = nm_split(line)
                if len(p) >= 3 and p[0] == "yes":
                    ssid, sig = p[1], p[2]
            self._cur = (now, ssid, sig)
        return self._cur[1], self._cur[2]

    def scan(self):
        rc, out = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                       "--rescan", "yes"], timeout=40, sudo=True)
        nets = {}
        for line in out.splitlines():
            p = nm_split(line)
            if len(p) < 4 or not p[1]:
                continue
            sig = int(p[2]) if p[2].isdigit() else 0
            if p[1] not in nets or sig > nets[p[1]]["signal"]:
                nets[p[1]] = {"ssid": p[1], "signal": sig, "secure": p[3] not in ("", "--"),
                              "active": p[0] == "*"}
        return sorted(nets.values(), key=lambda n: -n["signal"])

    def saved(self):
        rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"], timeout=10)
        return [nm_split(l)[0] for l in out.splitlines() if l.endswith("802-11-wireless")]

    def connect(self, ssid, password=None):
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        cmd += ["ifname", self.IFACE]
        rc, out = run(cmd, timeout=60, sudo=True)
        self._cur = (-999.0, None, None)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        return rc == 0, (lines[-1] if lines else "")

    def up(self, name):
        rc, out = run(["nmcli", "con", "up", "id", name], timeout=60, sudo=True)
        self._cur = (-999.0, None, None)
        return rc == 0

    def forget(self, name):
        rc, out = run(["nmcli", "con", "delete", "id", name], timeout=20, sudo=True)
        return rc == 0


class BluetoothManager:
    """Drives bluetoothctl the way a person would: send a command, wait for the reply it should give
    (or a time limit), then the next. Headphones pair without a PIN, so a NoInputNoOutput agent is
    used. Every session's transcript goes to ~/currawong/bt.log: the first thing to read when
    pairing fails."""
    DEVICE = re.compile(r"Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5}) (.+)$", re.I)
    LOG = os.path.expanduser("~/currawong/bt.log")
    REASONS = [  # (pattern in bluetoothctl's output, what to tell the user), most specific first
        (r"No default controller", "No Bluetooth radio found. Run: sudo bash currawong_setup.sh --bluetooth"),
        (r"Waiting to connect to bluetoothd", "The Bluetooth service isn't running. Run the setup check."),
        (r"Blocked|rfkill", "Bluetooth is blocked. On the Pi: sudo rfkill unblock bluetooth"),
        (r"AccessDenied|Not authori[sz]ed", "Not allowed to use Bluetooth. Re-run the setup script."),
        (r"InProgress", "Still busy with the last attempt. Try again in a moment."),
        (r"NotReady", "The Bluetooth radio isn't ready yet. Try again."),
        (r"not available", "It wasn't found. Put it in pairing mode and try again."),
        (r"Authentication(Failed|Rejected|Canceled)", "It refused. Remove Currawong from its list and retry."),
        (r"Page Timeout|ConnectionAttemptFailed|page-timeout|connection-abort", "No answer. Is it close and in pairing mode?"),
        (r"profile-unavailable|Protocol not available|NotAvailable", "Paired, but no audio link. Is bluealsa running?"),
        (r"Failed to start discovery", "Scanning wouldn't start. Run the setup's Bluetooth check."),
    ]
    NO_AUDIO = "Connected, but no audio link. Run: sudo bash currawong_setup.sh --bluetooth"

    def __init__(self):
        self.last_reason = ""

    def reason(self, text):
        for pattern, message in self.REASONS:
            if re.search(pattern, text, re.I):
                return message
        return ""

    def _log(self, label, text):
        try:
            os.makedirs(os.path.dirname(self.LOG), exist_ok=True)
            with open(self.LOG, "w") as f:
                f.write(f"== {label} at {time.strftime('%Y-%m-%d %H:%M:%S')} ==\n")
                f.write("\n".join(text.splitlines()[-300:]) + "\n")
        except OSError:
            pass

    def _session(self, steps, label):
        """steps: (command, max seconds, regex that ends the wait early or None[, stop]). `stop` ends the
        whole session after this step: a regex that showed up, or "missing" if `until` never did.
        Returns the transcript."""
        try:
            p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            text = f"bluetoothctl failed to start: {e}"
            self._log(label, text)
            return text
        lines = []
        cond = threading.Condition()

        def reader():
            for line in iter(p.stdout.readline, ""):
                with cond:
                    lines.append(_ANSI.sub("", line))
                    cond.notify_all()
            with cond:
                cond.notify_all()
        threading.Thread(target=reader, name="bt-reader", daemon=True).start()
        try:
            deadline = time.monotonic() + 3
            with cond:  # let it start up, and stop at once if bluetoothd can't be reached
                while time.monotonic() < deadline and not any("Agent registered" in line for line in lines):
                    cond.wait(deadline - time.monotonic())
                hopeless = (any("Waiting to connect to bluetoothd" in line for line in lines)
                            and not any("Agent registered" in line for line in lines))
            for step in ([] if hopeless else steps):
                cmd, wait, until = step[:3]
                stop = step[3] if len(step) > 3 else None
                with cond:
                    start = len(lines)
                p.stdin.write(cmd + "\n")
                p.stdin.flush()
                pattern = re.compile(until, re.I) if until else None
                deadline = time.monotonic() + wait
                matched = False
                with cond:
                    while True:
                        if pattern and any(pattern.search(line) for line in lines[start:]):
                            matched = True
                            break
                        left = deadline - time.monotonic()
                        if left <= 0 or p.poll() is not None:
                            break
                        cond.wait(left)
                    reply = "".join(lines[start:])
                if stop == "missing" and not matched:
                    break
                if stop and stop != "missing" and re.search(stop, reply, re.I):
                    break
            p.stdin.write("quit\n")
            p.stdin.flush()
            p.wait(timeout=5)
        except Exception:
            p.kill()
        with cond:
            text = "".join(lines)
        self._log(label, text)
        return text

    def _devices(self, text):
        found = {}
        for line in text.splitlines():
            if "[CHG]" in line or "[DEL]" in line:  # property changes, e.g. "Device .. RSSI: -60"
                continue
            m = self.DEVICE.search(line)
            if m:
                mac, name = m.group(1).upper(), m.group(2).strip()
                if name.replace("-", ":").upper() == mac:  # no name has come through (yet)
                    name = None
                if name or mac not in found:
                    found[mac] = name
        return found

    _SETUP = [
        ("power on", 4, r"power on succeeded|Failed|No default controller|Blocked"),
        ("agent NoInputNoOutput", 2, r"Agent registered|already registered|Failed"),
        ("default-agent", 2, r"Default agent request successful|Failed|No agent"),
    ]
    # Classic Bluetooth only. Headphones' audio lives there; their Bluetooth LE addresses (for the
    # maker's app, fast pairing) show up as extra copies with the same name that carry no audio.
    _CLASSIC = [("menu scan", 0.5, None), ("transport bredr", 0.5, None), ("back", 0.5, None)]
    SEEN = re.compile(r"\[(?:NEW|CHG)\] Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.I)

    def is_audio(self, mac):
        rc, out = run(["bluetoothctl", "info", mac], timeout=10)
        return bool(re.search(r"Audio Sink|Icon: audio-|Headset|Handsfree", out))

    def audio_link(self, mac, wait=6):
        """True once bluealsa has an A2DP stream for this device, False if none appears, None if
        there's no way to tell (no bluealsa tool, or it can't be asked)."""
        tool = shutil.which("bluealsa-cli") or shutil.which("bluealsactl")
        if not tool:
            return None
        key = "dev_" + mac.upper().replace(":", "_")
        end = time.monotonic() + wait
        while True:
            rc, out = run([tool, "list-pcms"], timeout=5)
            if rc != 0:
                return None
            if any(key in line and "a2dp" in line.lower() for line in out.splitlines()):
                return True
            if time.monotonic() > end:
                return False
            time.sleep(1)

    def paired(self):
        rc, out = run(["bluetoothctl", "devices", "Paired"], timeout=10)
        if rc != 0 or "Invalid" in out:
            rc, out = run(["bluetoothctl", "paired-devices"], timeout=10)
        return self._devices(_ANSI.sub("", out))

    def scan(self, seconds=12):
        text = self._session(self._SETUP + self._CLASSIC + [
            ("scan on", seconds, r"Failed to start discovery|No default controller"),
            ("scan off", 2, r"Discovery stopped|Failed"),
            ("devices", 2, None),
        ], "scan")
        self.last_reason = self.reason(text)
        paired = self.paired()
        found = self._devices(text)
        seen = {m.upper() for m in self.SEEN.findall(text)}
        if seen:  # only what answered this scan, not everything BlueZ has ever cached
            found = {mac: name for mac, name in found.items() if mac in seen}
        # The same name more than once: keep the copies that can play audio (if any can).
        counts = Counter(n for n in found.values() if n)
        for name, n in counts.items():
            if n > 1:
                macs = [m for m, nm in found.items() if nm == name]
                audio = [m for m in macs if self.is_audio(m)]
                for m in macs:
                    if audio and m not in audio:
                        del found[m]
        # [(mac, name or None, already paired)]: named new ones first, then paired, then unnamed
        devices = [(mac, name or paired.get(mac), mac in paired) for mac, name in found.items()]
        return sorted(devices, key=lambda d: (d[1] is None, d[2], (d[1] or "").lower()))

    def pair(self, mac, find_secs=15):
        """Returns (paired, connected, reason)."""
        mac = mac.upper()
        text = self._session([("pairable on", 2, r"pairable on succeeded|Failed")] + self._SETUP + self._CLASSIC + [
            # BlueZ forgets an unpaired device about 30 s after a scan, so find it again first
            ("scan on", find_secs, rf"Device {mac}|Failed to start discovery", "missing"),
            ("scan off", 2, r"Discovery stopped|Failed"),
            (f"pair {mac}", 25, r"Pairing successful|Failed to pair|AlreadyExists|not available",
             r"Failed to pair|not available"),
            (f"trust {mac}", 3, r"trust succeeded|Failed|not available"),
            (f"connect {mac}", 20, r"Connection successful|Failed to connect|not available"),
        ], "pair " + mac)
        paired = bool(re.search(r"Pairing successful|AlreadyExists", text))
        connected = paired and "Connection successful" in text
        if connected and self.audio_link(mac) is False:
            connected = False  # the radio link is up, but no audio stream came with it
            self.last_reason = self.NO_AUDIO
        elif paired and connected:
            self.last_reason = ""
        elif not re.search(rf"Device {mac}", text, re.I):
            self.last_reason = "It wasn't found. Put it in pairing mode and try again."
        else:
            self.last_reason = self.reason(text) or "Pairing didn't finish. Try again."
        return paired, connected, self.last_reason

    def connected(self, mac):
        rc, out = run(["bluetoothctl", "info", mac], timeout=10)
        return "Connected: yes" in out

    def connect(self, mac):
        rc, out = run(["bluetoothctl", "connect", mac], timeout=25)
        out = _ANSI.sub("", out)
        self._log("connect " + mac, out)
        ok = "Connection successful" in out or self.connected(mac)
        self.last_reason = "" if ok else self.reason(out)
        if ok and self.audio_link(mac) is False:
            ok, self.last_reason = False, self.NO_AUDIO
        return ok

    def disconnect(self, mac):
        run(["bluetoothctl", "disconnect", mac], timeout=10)
        return True

    def remove(self, mac):
        rc, out = run(["bluetoothctl", "remove", mac], timeout=10)
        return rc == 0


class OutputManager:
    """Switches both players between the wired DAC and Bluetooth headphones."""

    def __init__(self, app):
        self.app = app

    def apply(self, which):
        dev = BT_DEVICE if which == "bluetooth" else WIRED_DEVICE
        ok = True
        try:
            with open(LIBRESPOT_CONFIG) as f:
                txt = f.read()
            new, n = re.subn(r"(?m)^audio_device:.*$", f"audio_device: {dev}", txt)
            if n == 0:
                new = txt.rstrip("\n") + f"\naudio_device: {dev}\n"
            if new != txt:
                with open(LIBRESPOT_CONFIG, "w") as f:
                    f.write(new)
                rc, _ = run(["systemctl", "restart", "go-librespot"], timeout=30, sudo=True)
                ok = ok and rc == 0
        except OSError:
            ok = False
        try:
            mpd = self.app.player.mpd
            for o in mpd.records("outputs", first="outputid"):
                name = o.get("outputname")
                if name == MPD_OUTPUT_WIRED:
                    on = which == "wired"
                elif name == MPD_OUTPUT_BT:
                    on = which == "bluetooth"
                else:
                    continue
                mpd.command(f"{'enableoutput' if on else 'disableoutput'} {o['outputid']}")
        except (MPDError, AttributeError):
            ok = False
        self.app.settings["output"] = which
        save_settings(self.app.settings)
        STATUS["output"] = which
        return ok


# ======================================================================
# Haptic dial link (ESP32 over UART)
# ======================================================================
class HapticLink:
    """Lines like $POS,3,1*4A with an NMEA-style XOR checksum. Junk (the ESP32's boot log) is ignored."""

    def __init__(self, post):
        self.post = post
        self._init_state()
        self.ser = None
        try:
            import serial
            self.ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.2)
        except Exception:
            return
        self.resync = True  # push settings once the dial says hello
        threading.Thread(target=self._reader, name="dial", daemon=True).start()
        self.send("PING")

    def _init_state(self):
        self.ok = False
        self.version = 0         # the firmware's protocol version, from its HELLO
        self.last_key = None     # the mode last sent ($CFG)
        self.last_extras = None  # landmarks and magnet last sent ($MARK, $MAG)
        self.range_sent = None   # (positions, position) last sent ($RANGE)
        self.range_at = -1e9
        self.resync = False

    @staticmethod
    def checksum(body):
        c = 0
        for b in body.encode():
            c ^= b
        return f"{c:02X}"

    def send(self, body):
        if self.ser:
            try:
                self.ser.write(f"${body}*{self.checksum(body)}\n".encode())
            except Exception:
                pass

    def _reader(self):
        buf = b""
        while True:
            try:
                chunk = self.ser.read(64)
            except Exception:
                time.sleep(1)
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.handle(line.decode("ascii", "ignore").strip())
            if len(buf) > 256:
                buf = b""

    def handle(self, line):
        i = line.rfind("$")
        if i < 0 or "*" not in line[i:]:
            return
        body, cs = line[i + 1:].rsplit("*", 1)
        if cs[:2].upper() != self.checksum(body):
            return
        parts = body.split(",")
        if parts[0] == "HELLO":
            self.ok = True
            self.version = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            self.last_key = self.last_extras = self.range_sent = None  # the dial restarted: send it all again
            self.resync = True    # ...and its settings (strength, direction, lock)
        elif parts[0] == "JOG" and len(parts) >= 2:
            try:
                self.post("jog", int(parts[1]))  # the shuttle's twist, -100..100
            except ValueError:
                pass
        elif parts[0] == "GRAB":
            self.post("grab", None)  # a hand stopped a spin or roll
        elif parts[0] == "POS" and len(parts) >= 3:
            self.ok = True
            try:
                delta = int(parts[2])
            except ValueError:
                return
            step = 1 if delta > 0 else -1
            for _ in range(min(abs(delta), 8)):
                self.post("rot", step)

    def set_profile(self, screen_id, profile, extras=None):
        kind, positions, position, sync = profile
        key = (screen_id, kind, positions, sync)
        if key != self.last_key:
            self.last_key = key
            self.send(f"CFG,{kind},{positions},{position}")
            self.last_extras = self.range_sent = None  # a new mode clears both on the dial
        extras = extras or {}
        marks = tuple(extras.get("marks") or ())[:64]
        magnet = extras.get("magnet")
        prev = self.last_extras or ((), None)
        if (marks, magnet) != prev or self.last_extras is None:
            if marks != prev[0]:
                self.send("MARK" + "".join(f",{m}" for m in marks))
            if magnet != prev[1]:
                self.send(f"MAG,{-1 if magnet is None else magnet}")
            self.last_extras = (marks, magnet)

    def set_range(self, positions, position, now, settled=True):
        """Song walls: sent at once for a new song, then kept in step with playback every few
        seconds, but only while the dial is left alone (never under a turning hand)."""
        if self.range_sent is not None and self.range_sent[0] == positions:
            if position == self.range_sent[1] or not settled or now - self.range_at < RANGE_SYNC_SECS:
                return
        self.send(f"RANGE,{positions},{position}")
        self.range_sent, self.range_at = (positions, position), now

    def set_feel(self, pct):
        """The dial's strength as a percent of normal (Settings > Dial feel). Firmware before v0.6
        only has three levels, so it gets the nearest one."""
        if self.version >= 6:
            self.send(f"FEEL,{int(pct)}")
        else:
            self.set_strength(0 if pct < 85 else 1 if pct < 115 else 2)

    def set_strength(self, level):
        self.send(f"STR,{int(level)}")

    def set_dir(self, sign):
        self.send(f"DIR,{1 if sign > 0 else -1}")

    def spin(self, direction):
        self.send(f"SPIN,{1 if direction > 0 else -1}")

    def roll(self, turns_per_sec):
        self.send(f"ROLL,{int(round(turns_per_sec * 1000))}")

    def lock(self, on):
        self.send(f"LOCK,{1 if on else 0}")

    def tick(self, n=1, strength=None):
        """One click, or an alert of n stronger ones (felt even when pocket-locked). strength (a
        percent) softens it: the button clicks. Firmware before v0.4 ignores it."""
        if strength is None:
            self.send("TICK" if n <= 1 else f"TICK,{n}")
        else:
            self.send(f"TICK,{n},{int(strength)}")


# ======================================================================
# Lists, messages, tasks, text entry, about
# ======================================================================
def resolve(result):
    """Normalise what a handler returns: (kind, payload). ("replace", x) may be nested."""
    how = "push"
    while isinstance(result, tuple) and len(result) == 2 and result[0] == "replace":
        how, result = "replace", result[1]
    return how, result


class ListScreen(Screen):
    """Encoder-driven list. items: (label, value_fn or None, action). Fast spins jump.
    marks: positions that click harder on the dial (e.g. each new letter). magnet: a position that
    pulls the dial in from one away (e.g. the playing track in Up Next)."""
    ROW = 30
    marks = ()
    magnet = None
    footer_glyph = True

    def __init__(self, app, title, items):
        self.app, self.title, self.items = app, title, items
        self.sel = 0
        self.top = 0
        self.last_rot = 0.0
        self.sel_at = time.monotonic()   # when the current item was reached (for the scrolling name)
        self.overflow = False            # the current item's name doesn't fit

    def animates(self, snap, mode):
        return self.overflow and time.monotonic() - self.sel_at < MARQUEE_SECS

    def haptic(self):
        return ("list", len(self.items), self.sel, 0)

    def haptic_extras(self):
        if self.marks or self.magnet is not None:
            return {"marks": list(self.marks), "magnet": self.magnet}
        return None

    def handle(self, kind, arg):
        if kind == "rot":
            now = time.monotonic()
            dial = self.app.haptics is not None and self.app.haptics.ok
            step = 4 if (not dial and now - self.last_rot < 0.08) else 1
            self.last_rot = now
            new = max(0, min(len(self.items) - 1, self.sel + arg * step))
            if new != self.sel:
                self.sel, self.sel_at = new, now
            self.version += 1
        elif kind == "press" and arg == "enc_sw" and self.items:
            self.version += 1
            return self.items[self.sel][2]()
        elif kind == "press" and arg == "back":
            return "pop"
        return None

    def render_key(self, snap, now):
        values = tuple(v() if v else None for _, v, _ in self.items[self.top:self.top + 12])
        return super().render_key(snap, now) + (self.sel, values, self.app.battery.state(),
                                                now - self.sel_at > MARQUEE_SECS)

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        vis = (H - 32 - 24) // self.ROW
        if 0 <= self.sel < len(self.items):
            label_, value_fn_, _ = self.items[self.sel]
            right_ = value_fn_() if value_fn_ else ">"
            self.overflow = tlen(F_BODY, label_) > W - 30 - value_width(right_)
        self.top = min(self.top, self.sel)
        self.top = max(self.top, self.sel - vis + 1)
        for row, idx in enumerate(range(self.top, min(len(self.items), self.top + vis))):
            text, value_fn, _ = self.items[idx]
            y = 32 + row * self.ROW
            fg = BLACK
            if idx == self.sel:
                d.rectangle((0, y, W - 1, y + self.ROW - 4), fill=BLACK)
                fg = WHITE
            right = value_fn() if value_fn else ">"
            rw = value_width(right)
            avail = W - 30 - rw
            elapsed = max(0.0, now - self.sel_at)
            if idx == self.sel and tlen(F_BODY, text) > avail and elapsed < MARQUEE_SECS:
                marquee(img, text, 10, y + 7, avail, elapsed, fg)  # too long: scroll it
            else:
                d.text((10, y + 7), fit(text, F_BODY, avail), font=F_BODY, fill=fg)
            if isinstance(right, Icon):
                right.draw(d, W - 10 - rw, y + 7, fg)
            else:
                d.text((W - 10 - rw, y + 7), right, font=F_BODY, fill=fg)
        if len(self.items) > vis:
            h = max(6, (H - 32 - 24) * vis // len(self.items))
            y = 32 + (H - 32 - 24 - h) * self.top // max(1, len(self.items) - vis)
            d.rectangle((W - 3, y, W - 1, y + h), fill=BLACK)
        footer(d, snap, self.app.battery, self.footer_glyph)
        return img


def value_width(v):
    return v.width if isinstance(v, Icon) else tlen(F_BODY, v)


class MenuScreen(ListScreen):
    """The main menu: the currawong stands in the title bar (inverted), and the footer is just the
    track's title (the bird shows whether it's playing)."""
    footer_glyph = False

    def bird_layer(self, img, now):
        bird = self.app.bird
        return bird.draw_menu(img, now) if bird else None


class MessageScreen(Screen):
    def __init__(self, app, title, lines):
        self.app, self.title, self.lines = app, title, lines

    def handle(self, kind, arg):
        if kind == "press" and arg in ("back", "enc_sw"):
            return "pop"
        return None

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        y = 44
        for line in self.lines:
            for part in wrap(line, F_BODY, W - 20, 3) or [""]:
                d.text((10, y), part, font=F_BODY, fill=BLACK)
                y += 20
        footer(d, snap, self.app.battery)
        return img


class TaskScreen(Screen):
    """Runs fn in the background with a waiting message, then shows done(result)."""

    def __init__(self, app, title, message, fn, done):
        self.app, self.title, self.done = app, title, done
        self.lines = message if isinstance(message, list) else [message]
        self.result = None
        self.finished = False
        self.started = time.monotonic()

        def work():
            try:
                self.result = fn()
            except Exception as e:  # shown to done() as the result
                self.result = e
            self.finished = True
            app.wake.set()
        threading.Thread(target=work, name="task", daemon=True).start()

    def handle(self, kind, arg):
        if kind == "press" and arg == "back":
            return "pop"  # leave; the work finishes quietly in the background
        return None

    def tick(self):
        if self.finished:
            return ("replace", self.done(self.result))
        return None

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int((now - self.started) * 2),)

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        y = 44
        for i, line in enumerate(self.lines):
            dots = "." * (int((now - self.started) * 2) % 4) if i == len(self.lines) - 1 else ""
            d.text((10, y), line + dots, font=F_BODY, fill=BLACK)
            y += 20
        d.text((10, y + 12), "BACK leaves it running", font=F_TINY, fill=BLACK)
        footer(d, snap, self.app.battery)
        return img


MARQUEE_DELAY = 0.8   # seconds on an item before its name starts to scroll
MARQUEE_SPEED = 24    # pixels a second
MARQUEE_GAP = 36      # pixels between the end of the name and its next pass
MARQUEE_SECS = 20     # then stop and show the shortened name (saves the battery and CPU)
MARQUEE_FPS = 4
_MARQUEE_CACHE = {}


def marquee(img, text, x, y, avail, elapsed, fill, font=F_BODY):
    """Draws text clipped to `avail` pixels, looping leftwards once `elapsed` passes the delay."""
    key = (text, id(font))
    strip = _MARQUEE_CACHE.get(key)
    if strip is None:
        if len(_MARQUEE_CACHE) > 200:
            _MARQUEE_CACHE.clear()
        m = text_mask(text, font)
        strip = Image.new("1", (2 * m.width + MARQUEE_GAP, m.height), 0)
        strip.paste(m, (0, 0))
        strip.paste(m, (m.width + MARQUEE_GAP, 0))
        _MARQUEE_CACHE[key] = strip
    loop = (strip.width - MARQUEE_GAP) // 2 + MARQUEE_GAP
    off = 0 if elapsed < MARQUEE_DELAY else int((elapsed - MARQUEE_DELAY) * MARQUEE_SPEED) % loop
    img.paste(fill, (int(x), int(y)), strip.crop((off, 0, off + int(avail), strip.height)))


CHAR_GROUPS = [
    ("abc", "abcdefghijklmnopqrstuvwxyz"),
    ("ABC", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    ("123", "0123456789"),
    ("#+=", " .-_@!?#$%&*+=/:;,'\"()[]~^"),
]
CHARSET = "".join(chars for _, chars in CHAR_GROUPS)
GROUP_STARTS = [sum(len(c) for _, c in CHAR_GROUPS[:i]) for i in range(len(CHAR_GROUPS))]


class TextEntryScreen(Screen):
    """Dial keyboard: turn to pick, centre adds, back deletes, next/prev jump groups, play is done."""
    captures = {"play", "next", "prev"}

    def __init__(self, app, title, on_done, secret=False, initial=""):
        self.app, self.title, self.on_done = app, title, on_done
        self.secret = secret
        self.text = initial
        self.idx = 0
        self.sync = 0

    def haptic(self):
        return ("list", len(CHARSET), self.idx, self.sync)

    def group(self):
        return max(i for i, s in enumerate(GROUP_STARTS) if s <= self.idx)

    def handle(self, kind, arg):
        if kind == "rot":
            self.idx = max(0, min(len(CHARSET) - 1, self.idx + arg))
        elif kind == "press" and arg == "enc_sw":
            if len(self.text) < 63:
                self.text += CHARSET[self.idx]
        elif kind == "press" and arg == "back":
            if not self.text:
                return "pop"
            self.text = self.text[:-1]
        elif kind == "press" and arg in ("next", "prev"):
            g = (self.group() + (1 if arg == "next" else -1)) % len(GROUP_STARTS)
            self.idx = GROUP_STARTS[g]
            self.sync += 1
        elif kind == "press" and arg == "play":
            return self.on_done(self.text)
        else:
            return None
        self.version += 1
        return None

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        shown = self.text
        if self.secret and shown:
            shown = "*" * (len(shown) - 1) + shown[-1]
        # tail of the text, so the end you're typing stays visible
        while shown and tlen(F_BIG, shown + "_") > W - 28:
            shown = shown[1:]
        d.rectangle((8, 40, W - 9, 72), outline=BLACK)
        d.text((14, 47), shown + "_", font=F_BIG, fill=BLACK)
        # the character reel
        cy, cell = 120, 30
        for k in range(-3, 4):
            i = self.idx + k
            if not 0 <= i < len(CHARSET):
                continue
            ch = CHARSET[i] if CHARSET[i] != " " else "SP"
            cx = W // 2 + k * cell
            if k == 0:
                d.rectangle((cx - 15, cy - 18, cx + 15, cy + 18), fill=BLACK)
                centred(d, cx, cy - 11, ch, F_BIG)
                ImageDraw.Draw(img).rectangle((cx - 15, cy - 18, cx + 15, cy + 18), outline=BLACK)
                img.paste(WHITE, (int(cx - tlen(F_BIG, ch) / 2), cy - 11), text_mask(ch, F_BIG))
            else:
                centred(d, cx, cy - 8, ch, F_BODY)
        names = "  ".join(f"[{n}]" if i == self.group() else n for i, (n, _) in enumerate(CHAR_GROUPS))
        centred(d, W / 2, 152, names, F_SMALL)
        hints = ["Turn: choose a character", "Centre: add it", "Back: delete (empty: cancel)",
                 "Next / Prev: jump group", "Play: done"]
        for n, h in enumerate(hints):
            d.text((10, 190 + n * 18), h, font=F_SMALL, fill=BLACK)
        footer(d, snap, self.app.battery)
        return img


class OptionScreen(Screen):
    """Pick one value for a setting. Two choices get the dial's binary switch (flick it across),
    more get one click per choice with walls at the ends. Centre sets it, back cancels.
    `live` (optional) is called as you turn, e.g. so the dial's strength can be felt while choosing."""

    def __init__(self, app, title, options, current, on_set, live=None):
        self.app, self.title, self.options, self.on_set, self.live = app, title, options, on_set, live
        values = [v for v, _ in options]
        self.idx = values.index(current) if current in values else 0
        self.start = self.idx

    def haptic(self):
        if len(self.options) == 2:
            return ("binary", 2, self.idx, 0)
        return ("list", len(self.options), self.idx, 0)

    def handle(self, kind, arg):
        if kind == "rot":
            new = max(0, min(len(self.options) - 1, self.idx + arg))
            if new != self.idx:
                self.idx = new
                self.version += 1
                if self.live:
                    self.live(self.options[new][0])
        elif kind == "press" and arg == "enc_sw":
            res = self.on_set(self.options[self.idx][0])
            return "pop" if res is None else res
        elif kind == "press" and arg == "back":
            if self.live and self.idx != self.start:
                self.live(self.options[self.start][0])  # put back what it was
            return "pop"
        return None

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        if len(self.options) == 2:
            # a two-way switch, drawn like one
            x0, x1, y0, y1 = 14, W - 14, 120, 176
            mid = (x0 + x1) // 2
            d.rectangle((x0, y0, x1, y1), outline=BLACK, width=2)
            for i, (_, label_) in enumerate(self.options):
                a, b = (x0, mid) if i == 0 else (mid, x1)
                if i == self.idx:
                    d.rectangle((a + 4, y0 + 4, b - 4, y1 - 4), fill=BLACK)
                    img.paste(WHITE, (int((a + b) / 2 - tlen(F_BIG, label_) / 2), y0 + 18), text_mask(label_, F_BIG))
                else:
                    centred(d, (a + b) / 2, y0 + 18, label_, F_BIG)
            centred(d, W / 2, 196, "Flick the dial across to switch", F_SMALL)
        else:
            for i, (_, label_) in enumerate(self.options):
                y = 44 + i * 30
                fg = BLACK
                if i == self.idx:
                    d.rectangle((0, y, W - 1, y + 26), fill=BLACK)
                    fg = WHITE
                d.text((14, y + 6), label_, font=F_BODY, fill=fg)
                if i == self.start:
                    d.text((W - 14 - tlen(F_SMALL, "now"), y + 7), "now", font=F_SMALL, fill=fg)
        for n, line in enumerate(("Centre: set", "Back: cancel")):
            d.text((14, H - 70 + n * 16), line, font=F_SMALL, fill=BLACK)
        footer(d, snap, self.app.battery)
        return img


class IntensityScreen(Screen):
    """A strength setting on a dial drawn like the real one. The knob clicks in 30-degree steps
    over 300 degrees with walls at both ends, and the drawn arc covers the same 300 degrees, so the
    indicator moves exactly as far as the knob does. What it sets changes as you turn (the dial's
    feel under your fingers, or a click at each strength). Centre keeps it; BACK puts it back."""
    SWEEP = 300                 # degrees of arc (and of knob travel: 10 clicks of 30 degrees)
    START = 90 + (360 - 300) / 2  # PIL's angles: 0 is 3 o'clock, clockwise; the gap is at the bottom

    def __init__(self, app, title, values, current, on_set, live=None, fmt=lambda v: f"{v}%", note=""):
        self.app, self.title, self.values, self.on_set, self.live = app, title, list(values), on_set, live
        self.fmt, self.note = fmt, note
        self.idx = min(range(len(self.values)), key=lambda i: abs(self.values[i] - current))
        self.start = self.idx

    def haptic(self):
        return ("coarse", 0, 0, f"intensity:{self.title}")  # 30-degree clicks; the walls come from dial_range

    def dial_range(self, snap):
        return len(self.values), self.idx

    def handle(self, kind, arg):
        if kind == "rot":
            new = max(0, min(len(self.values) - 1, self.idx + arg))
            if new != self.idx:
                self.idx = new
                self.version += 1
                if self.live:
                    self.live(self.values[new])
        elif kind == "press" and arg == "enc_sw":
            res = self.on_set(self.values[self.idx])
            return "pop" if res is None else res
        elif kind == "press" and arg == "back":
            if self.live and self.idx != self.start:
                self.live(self.values[self.start])  # put back what it was
            return "pop"
        return None

    def angle(self, i):
        return self.START + self.SWEEP * i / max(1, len(self.values) - 1)

    def render(self, now, snap, mode):
        img, d = titled(self.title)
        cx, cy, r = W / 2, 178, 84
        box = (cx - r, cy - r, cx + r, cy + r)
        d.arc(box, self.START, self.START + self.SWEEP, fill=BLACK, width=2)          # the track
        if self.idx:
            d.arc(box, self.START, self.angle(self.idx), fill=BLACK, width=11)        # how far it's turned
        for i in range(len(self.values)):                                            # a tick per click
            a = math.radians(self.angle(i))
            r0, r1 = r + 7, r + (14 if i in (0, len(self.values) - 1) else 11)
            d.line((cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
                   fill=BLACK, width=2)
        a = math.radians(self.angle(self.idx))                                       # the knob's mark
        mx, my = cx + r * math.cos(a), cy + r * math.sin(a)
        d.ellipse((mx - 9, my - 9, mx + 9, my + 9), fill=WHITE, outline=BLACK, width=3)
        if self.start != self.idx:                                                   # where it was
            a0 = math.radians(self.angle(self.start))
            sx, sy = cx + (r - 20) * math.cos(a0), cy + (r - 20) * math.sin(a0)
            d.ellipse((sx - 3, sy - 3, sx + 3, sy + 3), fill=BLACK)
        centred(d, cx, cy - 14, self.fmt(self.values[self.idx]), F_HUGE)
        if self.note:
            centred(d, cx, cy + 16, self.note, F_TINY)
        for n, line in enumerate(("Turn: feel it change", "Centre: keep it", "BACK: put it back")):
            centred(d, W / 2, 292 + n * 16, line, F_SMALL)
        footer(d, snap, self.app.battery)
        return img


class AboutScreen(MessageScreen):
    def __init__(self, app):
        super().__init__(app, "ABOUT", [])
        self._ip = (-999.0, "--")

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int(now // 2),)

    def ip(self, now):
        if now - self._ip[0] > 10:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))  # UDP: sends nothing, just picks a route
                self._ip = (now, s.getsockname()[0])
                s.close()
            except OSError:
                self._ip = (now, "offline")
        return self._ip[1]

    def render(self, now, snap, mode):
        lockup, mark = brand_parts()
        if lockup is not None:
            return self.render_branded(now, snap, lockup, mark)
        img, d = titled(self.title)
        ssid, sig = self.app.wifi.current(now)
        dial = self.app.haptics is not None and self.app.haptics.ok
        rows = [
            ("HOST", socket.gethostname()),
            ("IP", self.ip(now)),
            ("WI-FI", f"{ssid} {sig}%" if ssid else "not connected"),
            ("OUTPUT", "Bluetooth" if STATUS.get("output") == "bluetooth" else "Wired"),
            ("SOURCE", (snap.source or "none").upper()),
            ("DIAL", (f"haptic v0.{getattr(self.app.haptics, 'version', 0)}"
                      if getattr(self.app.haptics, "version", 0) else "haptic") if dial else "encoder"),
            ("UI", UI_BUILD),
            ("TEMP", read_temp()),
            ("LOAD", f"{os.getloadavg()[0]:.2f}"),
            ("UPTIME", read_uptime()),
            ("BATTERY", self.app.battery.text()),
            ("DRAIN (EST.)", getattr(self.app.battery, "drain_text", lambda: "--")()),
        ]
        for n, (k, v) in enumerate(rows):
            y = 38 + n * 26
            d.text((10, y), k, font=F_SMALL, fill=BLACK)
            v = fit(v, F_BODY, W - 90)
            d.text((W - 10 - tlen(F_BODY, v), y - 1), v, font=F_BODY, fill=BLACK)
            d.line((10, y + 18, W - 10, y + 18), fill=BLACK)
        centred(d, W / 2, 38 + len(rows) * 26 + 2, " ".join(ABOUT_CREDIT), F_SMALL)
        footer(d, snap, self.app.battery)
        return img


def _about_render_branded(self, now, snap, lockup, mark):
    """The lockup as the header, the details in between, the personal mark as a signature."""
    img = Image.new("1", (W, H), WHITE)
    d = Draw(img)
    img.paste(lockup, ((W - lockup.width) // 2, 4))
    ssid, sig = self.app.wifi.current(now)
    dial = self.app.haptics is not None and self.app.haptics.ok
    out = "Bluetooth" if STATUS.get("output") == "bluetooth" else "Wired"
    rows = [
        ("HOST", socket.gethostname()),
        ("IP", self.ip(now)),
        ("WI-FI", f"{ssid} {sig}%" if ssid else "not connected"),
        ("AUDIO", f"{out}, {(snap.source or 'none').lower()}"),
        ("DIAL", "haptic" if dial else "encoder"),
        ("CPU", f"{read_temp()}, load {os.getloadavg()[0]:.2f}"),
        ("UPTIME", read_uptime()),
        ("BATTERY", self.app.battery.text()),
        ("DRAIN (EST.)", getattr(self.app.battery, "drain_text", lambda: "--")()),
    ]
    top = 4 + lockup.height + 8
    for n, (k, v) in enumerate(rows):
        y = top + n * 20
        d.text((10, y), k, font=F_SMALL, fill=BLACK)
        v = fit(v, F_BODY, W - 80)
        d.text((W - 10 - tlen(F_BODY, v), y - 2), v, font=F_BODY, fill=BLACK)
        d.line((10, y + 15, W - 10, y + 15), fill=BLACK)
    y = top + len(rows) * 20 + 6
    l1, l2 = ABOUT_CREDIT
    tw = int(max(tlen(F_SMALL, l1), tlen(F_SMALL, l2)))
    if mark is not None and y + mark.height <= H - 24:
        # the personal mark with the credit beside it, the two centred together as a signature
        x0 = (W - (mark.width + 10 + tw)) // 2
        img.paste(mark, (x0, y))
        ty = y + mark.height // 2
        d.text((x0 + mark.width + 10, ty - 15), l1, font=F_SMALL, fill=BLACK)
        d.text((x0 + mark.width + 10, ty + 2), l2, font=F_SMALL, fill=BLACK)
    else:
        centred(d, W / 2, y + 2, " ".join(ABOUT_CREDIT), F_SMALL)
    footer(d, snap, self.app.battery)
    return img


AboutScreen.render_branded = _about_render_branded


ABOUT_CREDIT = ("Made by Jasper Japp,", "with love.")   # ABOUT's last word


def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f}C"
    except Exception:
        return "--"


def read_uptime():
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    except Exception:
        return "--"


_sleep_cache = {"key": None, "img": None}


def sleep_screen(snap, batt, bird=None):
    key = (snap.has_track, snap.uri, id(snap.cover), batt.state(), bool(bird), bird and bird.bowl_full)
    if key == _sleep_cache["key"]:
        return _sleep_cache["img"]
    img = Image.new("1", (W, H), WHITE)
    d = Draw(img)
    drawn = False
    if bird:   # the currawong asleep on its branch, still (nothing animates while the screen sleeps)
        try:
            from currawong_bird import draw_still
            draw_still(bird, img)
            drawn = True
        except Exception as e:  # noqa: BLE001
            print(f"Bird: the sleep picture failed ({e}): the cover instead", flush=True)
            img = Image.new("1", (W, H), WHITE)
            d = Draw(img)
    if not drawn:
        img.paste(snap.cover["large"] if snap.cover else dots(W, W), (0, 0))
    d.line((0, W, W - 1, W), fill=BLACK)
    if snap.has_track:
        y = W + 10
        for line in wrap(snap.title, F_BIG, W - 16, 2):
            d.text((8, y), line, font=F_BIG, fill=BLACK)
            y += 20
        d.text((8, y + 4), fit(snap.artists, F_BODY, W - 16), font=F_BODY, fill=BLACK)
        d.text((8, y + 22), fit(snap.album, F_SMALL, W - 16), font=F_SMALL, fill=BLACK)
    else:
        d.text((8, W + 10), "NOT PLAYING", font=F_BIG, fill=BLACK)
    d.line((0, H - 22, W - 1, H - 22), fill=BLACK)
    d.text((8, H - 17), "SLEEP · ANY BUTTON WAKES", font=F_TINY, fill=BLACK)
    battery_icon(d, W - 30, H - 16, batt)
    _sleep_cache.update(key=key, img=img)
    return img


def confirm(app, title, yes_label, action):
    return ListScreen(app, title, [("CANCEL", None, lambda: "pop"), (yes_label, None, action)])


# ======================================================================
# Library, playlists, up next, Spotify pins
# ======================================================================
def _lib(app, fn, *args):
    try:
        return fn(*args), None
    except MPDError:
        return None, MessageScreen(app, "LIBRARY", ["Can't reach MPD.", "", "Check it's running:", "systemctl status mpd"])


def _empty_library(app):
    return MessageScreen(app, "LIBRARY", ["No music found.", "", "Copy files into", "~/music, then", "LIBRARY > RESCAN."])


def library_menu(app):
    return ListScreen(app, "LIBRARY", [
        ("ARTISTS", None, lambda: artists_screen(app)),
        ("ALBUMS", None, lambda: albums_screen(app, None)),
        ("PLAYLISTS", None, lambda: playlists_screen(app)),
        ("UP NEXT", None, lambda: queue_screen(app)),
        ("SHUFFLE ALL", None, lambda: (app.player.shuffle_all(), "home")[1]),
        ("RESCAN", None, lambda: (app.player.rescan(), MessageScreen(
            app, "RESCAN", ["Rescanning ~/music.", "New music appears", "in a minute."]))[1]),
    ])


def artists_screen(app):
    names, err = _lib(app, app.player.lib_artists)
    if err:
        return err
    if not names:
        return _empty_library(app)
    screen = ListScreen(app, "ARTISTS", [(n, None, (lambda a=n: albums_screen(app, a))) for n in names])
    screen.marks = letter_marks(names)  # a firmer click at each new letter
    return screen


def albums_screen(app, artist):
    names, err = _lib(app, app.player.lib_albums, artist)
    if err:
        return err
    if not names:
        return _empty_library(app)
    screen = ListScreen(app, fit((artist or "ALBUMS").upper(), F_BIG, W - 16),
                        [(n, None, (lambda al=n: tracks_screen(app, artist, al))) for n in names])
    if artist is None:
        screen.marks = letter_marks(names)
    return screen


def song_title(r):
    return r.get("Title") or os.path.splitext(os.path.basename(r.get("file", "")))[0]


def song_dur(r):
    try:
        return mmss(float(r.get("duration") or r.get("Time") or 0) * 1000)
    except ValueError:
        return ""


class Toast(MessageScreen):
    """A short confirmation that closes itself after a moment (or on centre or BACK) and goes back
    to `back_to`, the list the action started from, refreshed."""
    SECS = 1.6

    def __init__(self, app, title, lines, back_to=None, long=False):
        super().__init__(app, title, lines)
        self.back_to = back_to
        self.at = time.monotonic()
        self.secs = self.SECS * (2.2 if long else 1)   # long: something to read, not just "done"

    def _close(self):
        st = self.app.stack
        if self.back_to is not None and self.back_to in st:
            while len(st) > 1 and st[-1] is not self.back_to:
                st.pop()
            refresh = getattr(self.back_to, "refresh", None)
            if refresh:
                refresh()
        elif len(st) > 1 and st[-1] is self:
            st.pop()
        return None

    def handle(self, kind, arg):
        if kind == "press" and arg in ("back", "enc_sw"):
            return self._close()
        return None

    def tick(self):
        if time.monotonic() - self.at > self.secs:
            return self._close()
        return None


ADD_SONGS_HINT = ["To add songs, hold", "centre on any song."]


class SongListScreen(ListScreen):
    """A list of songs (an album, a playlist, Up Next): centre plays; holding centre opens that
    song's options. recs[i] is row i's song; rows after the songs (album actions) have none."""

    def __init__(self, app, title, items, recs, context):
        super().__init__(app, title, items)
        self.recs, self.context = recs, context

    def hold_select(self):
        if 0 <= self.sel < len(self.recs):
            return song_options(self.app, self, self.sel)
        return None


def tracks_screen(app, artist, album):
    recs, err = _lib(app, app.player.lib_tracks, artist, album)
    if err:
        return err
    if not recs:
        return _empty_library(app)
    files = [r["file"] for r in recs]
    items = []
    for i, r in enumerate(recs):
        items.append((f"{i + 1:02d} {song_title(r)}", (lambda d=song_dur(r): d),
                      (lambda idx=i: (app.player.play_files(files, idx), "home")[1])))
    screen = SongListScreen(app, fit(album.upper(), F_BIG, W - 16), items, recs, "album")
    screen.items.append(("+ ADD ALBUM TO PLAYLIST", None, lambda: add_to_playlist_screen(app, files, back_to=screen)))
    screen.items.append(("+ ADD ALBUM TO UP NEXT", None, lambda: (
        app.player.queue_add(files), Toast(app, "UP NEXT", [f"Added {len(files)} songs."], back_to=screen))[1]))
    return screen


def song_options(app, lst, idx):
    """Hold centre on a song: what can be done with it, depending on where it's listed."""
    rec = lst.recs[idx]
    uri, p = rec["file"], app.player

    def done(title, *lines):
        return Toast(app, title, list(lines), back_to=lst)
    items = []
    if lst.context == "playlist":
        items.append(("MOVE", None, lambda: (lst.start_move(idx), "pop")[1]))
        items.append(("REMOVE FROM PLAYLIST", None, lambda: (
            p.playlist_remove(lst.name, idx), done("PLAYLISTS", "Removed from", f"\"{fit(lst.name, F_BODY, W - 40)}\"."))[1]))
    if lst.context == "queue":
        if idx != lst.cur:
            items.append(("PLAY NEXT", None, lambda: (p.queue_move_next(idx), done("UP NEXT", "Plays next."))[1]))
        items.append(("REMOVE FROM UP NEXT", None, lambda: (p.queue_remove(idx), done("UP NEXT", "Removed."))[1]))
    else:
        items.append(("PLAY NEXT", None, lambda: (p.queue_add_next([uri]), done("UP NEXT", "Plays next."))[1]))
        items.append(("ADD TO UP NEXT", None, lambda: (p.queue_add([uri]), done("UP NEXT", "Added to Up Next."))[1]))
    items.append(("ADD TO PLAYLIST", None, lambda: add_to_playlist_screen(app, [uri], back_to=lst)))
    return ListScreen(app, fit(song_title(rec).upper(), F_BIG, W - 16), items)


class PlaylistsScreen(ListScreen):
    """Your playlists, each with its number of songs."""

    def __init__(self, app, names):
        super().__init__(app, "PLAYLISTS", [])
        self.refresh(names)

    def refresh(self, names=None):
        if names is None:
            names, err = _lib(self.app, self.app.player.lib_playlists)
            names = names or []
        names = names or []
        counts = {}
        if len(names) <= 40:   # a quick look each; skipped for a very long list
            for n in names:
                try:
                    files = self.app.player.playlist_files(n)
                except MPDError:
                    files = None
                if files is not None:
                    counts[n] = len(files)
        self.items = [(n, (lambda c=counts.get(n): "" if c is None else str(c)),
                       (lambda nm=n: playlist_actions(self.app, nm, self))) for n in names]
        self.items.append(("+ NEW PLAYLIST...", None, lambda: TextEntryScreen(
            self.app, "NEW PLAYLIST", self.new_done)))
        self.items.append(("+ SAVE UP NEXT AS...", None, lambda: TextEntryScreen(
            self.app, "NEW PLAYLIST", lambda t: save_queue_done(self.app, t))))
        self.sel = min(self.sel, len(self.items) - 1)
        self.version += 1

    def new_done(self, text):
        """An empty playlist, made without touching Up Next or what's playing."""
        text = text.strip()
        if not text:
            return "pop"
        ok, why = self.app.player.playlist_create(text)
        if ok:
            return Toast(self.app, "PLAYLISTS", [f"Made \"{fit(text, F_BODY, W - 40)}\".", ""] + ADD_SONGS_HINT,
                         back_to=self, long=True)
        return Toast(self.app, "PLAYLISTS", ["Couldn't make it.", "", why], back_to=self, long=True)


def playlists_screen(app):
    names, err = _lib(app, app.player.lib_playlists)
    if err:
        return err
    return PlaylistsScreen(app, names)


def playlist_actions(app, name, parent=None):
    p = app.player
    try:
        files = p.playlist_files(name)
    except MPDError:
        files = None
    info = {"n": None if files is None else len(files)}

    def gone(*lines):
        return Toast(app, "PLAYLISTS", list(lines), back_to=parent)

    def renamed(text):
        text = text.strip()
        if not text or text == name:
            return "pop"
        ok, why = p.playlist_rename(name, text)
        return gone(f"Renamed to \"{fit(text, F_BODY, W - 40)}\"." if ok else "Couldn't rename it.", "" if ok else why)
    def unless_empty(then):
        """Nothing in it yet: say so, rather than clearing Up Next for an empty playlist."""
        if info["n"] == 0:
            return Toast(app, "PLAYLISTS", ["No songs in it yet.", ""] + ADD_SONGS_HINT, long=True)
        return then()
    return ListScreen(app, fit(name.upper(), F_BIG, W - 16), [
        ("PLAY", None, lambda: unless_empty(lambda: (p.play_playlist(name), "home")[1])),
        ("SHUFFLE", None, lambda: unless_empty(lambda: (p.play_playlist(name, True), "home")[1])),
        ("SONGS", lambda: "" if info["n"] is None else str(info["n"]), lambda: PlaylistSongsScreen(app, name, info)),
        ("ADD TO UP NEXT", None, lambda: unless_empty(lambda: (
            p.playlist_to_queue(name), Toast(app, "UP NEXT", ["Added to Up Next."]))[1])),
        ("RENAME", None, lambda: TextEntryScreen(app, "RENAME", renamed, initial=name)),
        ("DELETE", None, lambda: confirm(app, "DELETE?", "DELETE " + name.upper(),
                                         lambda: (p.playlist_delete(name), gone(f"Deleted \"{fit(name, F_BODY, W - 40)}\"."))[1])),
    ])


class PlaylistSongsScreen(SongListScreen):
    """A playlist's songs: centre plays the playlist from that song; hold centre for options,
    MOVE among them: then the dial moves the song, centre puts it down, BACK leaves it be."""

    def __init__(self, app, name, info=None):
        super().__init__(app, fit(name.upper(), F_BIG, W - 16), [], [], "playlist")
        self.name, self.info = name, info if info is not None else {}
        self.moving = self.move_from = None
        self.refresh()

    def refresh(self):
        recs, err = _lib(self.app, self.app.player.playlist_songs, self.name)
        self.recs = recs or []
        self.info["n"] = len(self.recs)
        self._relabel()
        self.sel = min(self.sel, max(0, len(self.items) - 1))
        self.version += 1

    def _relabel(self):
        self.items = [(("⇅ " if i == self.moving else "") + song_title(r), (lambda d=song_dur(r): d),
                       (lambda i=i: (self.app.player.play_playlist_from(self.name, i), "home")[1]))
                      for i, r in enumerate(self.recs)]
        if not self.items:
            self.items = [("(no songs yet)", None, lambda: Toast(
                self.app, "PLAYLISTS", ["No songs in it yet.", ""] + ADD_SONGS_HINT, long=True))]

    def start_move(self, i):
        self.moving = self.move_from = self.sel = i
        self._title = self.title
        self.title = "MOVE SONG"
        self._relabel()
        self.version += 1

    def _end_move(self):
        self.moving = self.move_from = None
        self.title = getattr(self, "_title", self.title)

    def handle(self, kind, arg):
        if self.moving is None:
            return super().handle(kind, arg)
        if kind == "rot":
            new = max(0, min(len(self.recs) - 1, self.moving + arg))
            if new != self.moving:
                self.recs.insert(new, self.recs.pop(self.moving))
                self.moving = self.sel = new
                self._relabel()
                self.version += 1
        elif kind == "press" and arg == "enc_sw":   # put it down here
            src, dst = self.move_from, self.moving
            self._end_move()
            if src != dst:
                self.app.player.playlist_move(self.name, src, dst)
            self.refresh()
        elif kind == "press" and arg == "back":      # leave it where it was
            self._end_move()
            self.refresh()
        return None

    def hold_select(self):
        return None if self.moving is not None else super().hold_select()


def save_queue_done(app, text):
    text = text.strip()
    if not text:
        return "pop"
    ok, why = app.player.save_queue(text)
    return ("replace", MessageScreen(app, "PLAYLISTS", [f"Saved \"{text}\"." if ok else "Couldn't save it.",
                                                        "" if ok else why]))


class QueueScreen(SongListScreen):
    """Up Next: centre plays from that song; hold centre to play a song next or take it out."""

    def __init__(self, app, recs, cur):
        super().__init__(app, "UP NEXT", [], [], "queue")
        self._fill(recs, cur)
        self.sel = max(0, cur)

    def _fill(self, recs, cur):
        self.recs, self.cur = recs, cur
        self.items = [(("> " if i == cur else "") + song_title(r), None,
                       (lambda pos=i: (self.app.player.play_pos(pos), "home")[1])) for i, r in enumerate(recs)]
        if not self.items:
            self.items = [("(nothing queued)", None, lambda: None)]
        self.marks, self.magnet = ([cur], cur) if cur >= 0 else ((), None)   # the playing track pulls the dial in
        self.sel = min(self.sel, len(self.items) - 1)
        self.version += 1

    def refresh(self):
        res, err = _lib(self.app, self.app.player.queue)
        self._fill(*(res if res else ([], -1)))


def queue_screen(app):
    res, err = _lib(app, app.player.queue)
    if err:
        return err
    recs, cur = res
    if not recs:
        return MessageScreen(app, "UP NEXT", ["Nothing queued.", "", "Pick an album or", "playlist first."])
    return QueueScreen(app, recs, cur)


def add_to_playlist_screen(app, uris, back_to=None):
    """Pick a playlist (the last one used first) or make a new one, for one song or several."""
    if isinstance(uris, str):
        uris = [uris]
    back_to = back_to if back_to is not None else app.top()
    names, err = _lib(app, app.player.lib_playlists)
    if err:
        return err
    last = app.settings.get("last_playlist")
    if last in names:
        names = [last] + [n for n in names if n != last]

    def add(name):
        added, had = app.player.playlist_add_many(name, uris)
        short = fit(name, F_BODY, W - 40)
        if added is None:
            return Toast(app, "PLAYLISTS", ["Couldn't add to", f"\"{short}\"."], back_to=back_to)
        if app.settings.get("last_playlist") != name:
            app.settings["last_playlist"] = name
            save_settings(app.settings)
        if len(uris) == 1:
            lines = ["Already in" if had else "Added to", f"\"{short}\"."]
        else:
            lines = [f"Added {added} song{'' if added == 1 else 's'} to", f"\"{short}\"."]
            if had:
                lines += ["", f"({had} {'was' if had == 1 else 'were'} already there)"]
        return Toast(app, "PLAYLISTS", lines, back_to=back_to)
    items = [(n, None, (lambda nm=n: add(nm))) for n in names]
    items.append(("+ NEW PLAYLIST...", None, lambda: TextEntryScreen(
        app, "NEW PLAYLIST", lambda t: add(t.strip()) if t.strip() else "pop")))
    return ListScreen(app, "ADD TO PLAYLIST", items)


def load_pins():
    """Pinned Spotify playlists: [{"name": "...", "uri": "spotify:playlist:..."}] in PINS_FILE."""
    def as_uri(u):
        # accept share links too: https://open.spotify.com/playlist/<id>?si=...
        m = re.search(r"open\.spotify\.com/(playlist|album|artist)/([A-Za-z0-9]+)", u)
        return f"spotify:{m.group(1)}:{m.group(2)}" if m else u
    try:
        with open(PINS_FILE) as f:
            return [(p["name"], as_uri(p["uri"])) for p in json.load(f) if p.get("name") and p.get("uri")]
    except Exception:
        return []


def spotify_menu(app):
    pins = load_pins()
    if not pins:
        return MessageScreen(app, "SPOTIFY", ["No pinned playlists yet.", "",
                                              "Add them to", "~/.config/currawong/", "spotify_pins.json"])

    def play(uri):
        return TaskScreen(app, "SPOTIFY", "Starting", lambda: app.player.spotify_play(uri),
                          lambda ok: "home" if ok is True else MessageScreen(app, "SPOTIFY", [
                              "Couldn't start it.", "", "Pick Currawong in the", "Spotify app once,",
                              "then try again."]))
    return ListScreen(app, "SPOTIFY", [(name, None, (lambda u=uri: play(u))) for name, uri in pins])


def track_menu(app):
    s = app.snap
    if not s.has_track:
        return MessageScreen(app, "TRACK", ["Nothing playing."])
    items = [
        ("SHUFFLE", lambda: "ON" if app.snap.shuffle else "OFF", lambda: (app.player.toggle_shuffle(app.snap), None)[1]),
        ("REPEAT", lambda: "ON" if app.snap.repeat else "OFF", lambda: (app.player.toggle_repeat(app.snap), None)[1]),
    ]
    if s.source == "local":
        uri, album = s.uri, s.album
        items.append(("ADD TO PLAYLIST", None, lambda: add_to_playlist_screen(app, uri)))
        if album:
            items.append(("GO TO ALBUM", None, lambda: tracks_screen(app, None, album)))
    return ListScreen(app, "TRACK", items)


# ======================================================================
# Wi-Fi, Bluetooth, output
# ======================================================================
def wifi_menu(app):
    w = app.wifi

    def status():
        def show(res):
            ssid, sig = res if isinstance(res, tuple) else (None, None)
            lines = [f"Connected to {ssid}", f"Signal {sig}%"] if ssid else ["Not connected."]
            return ("replace", MessageScreen(app, "WI-FI", lines))
        return TaskScreen(app, "WI-FI", "Checking", lambda: w.current(-1e9), show)

    def networks(nets):
        if isinstance(nets, Exception) or not nets:
            return ("replace", MessageScreen(app, "WI-FI", ["No networks found.", "", "Move closer and", "try again."]))
        items = [(("* " if n["active"] else "") + n["ssid"],
                  (lambda n=n: f"{n['signal']}%" if n["secure"] else f"open {n['signal']}%"),
                  (lambda n=n: wifi_join(app, n))) for n in nets[:30]]
        return ("replace", ListScreen(app, "NETWORKS", items))

    def saved(names):
        if isinstance(names, Exception) or not names:
            return ("replace", MessageScreen(app, "WI-FI", ["No saved networks."]))
        return ("replace", ListScreen(app, "SAVED", [(n, None, (lambda n=n: wifi_saved_actions(app, n))) for n in names]))

    def set_radio(on):
        if on == STATUS.get("radio", True):
            return None
        if on:
            return ("replace", wifi_on_task(app))
        app.set_radio(False)
        return None

    return ListScreen(app, "WI-FI", [
        ("WI-FI", lambda: "ON" if STATUS.get("radio", True) else "OFF",
         lambda: OptionScreen(app, "WI-FI", [(False, "OFF"), (True, "ON")], STATUS.get("radio", True), set_radio)),
        ("STATUS", lambda: "ON" if STATUS.get("wifi") else "OFF", status),
        ("JOIN A NETWORK", None, lambda: TaskScreen(app, "WI-FI", "Scanning", w.scan, networks)),
        ("SAVED NETWORKS", None, lambda: TaskScreen(app, "WI-FI", "Loading", w.saved, saved)),
    ])


# ======================================================================
# Equaliser
# ======================================================================
# The ALSA "equal" plugin (alsaequal + CAPS Eq10, set up by currawong_setup.sh) sits in front of
# the DAC and Bluetooth for both MPD and go-librespot. Its ten bands are used in pairs as five.
EQ_BANDS = ["BASS", "LOW", "MID", "HIGH", "TREBLE"]   # ~45 Hz, 180 Hz, 700 Hz, 2.8 kHz, 11 kHz
EQ_PAIRS = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)]
EQ_RANGE = 6           # dB either way, in 1 dB clicks
EQ_DB_PER_STEP = 0.72  # one control step moves every band together by this much (measured)
# The plugin's own "flat" (66 on every band) is about +3.2 dB, which clipped loud tracks: the
# muddy distortion at volume. These per-band values are measured instead: 63 Hz to 11 kHz
# within 0.7 dB, everything at or below -0.6 dB, so even a full-scale track can't clip.
EQ_FLAT = [61, 60, 60, 60, 60, 60, 60, 59, 60, 62]
EQ_MARGIN = 1.0        # extra headroom whenever anything's boosted: neighbouring boosts overlap
# Gain measured at each pair's centre for a control value (relative to 66), the others at 66 (alsaequal 0.6,
# caps 0.9.26). The bands overlap, so the plugin's own -48..+24 dB scale overstates what one
# pair does; these are what actually comes out.
EQ_TABLE = [
    [(40, -6.2), (44, -5.9), (48, -5.5), (52, -4.8), (56, -3.9), (60, -2.6), (64, -1.0), (66, 0.0), (68, 1.0), (72, 3.3), (76, 5.7), (80, 8.3), (84, 11.0)],
    [(40, -8.4), (44, -7.7), (48, -6.8), (52, -5.7), (56, -4.4), (60, -2.8), (64, -1.0), (66, 0.0), (68, 1.0), (72, 3.3), (76, 5.6), (80, 8.1), (84, 10.7)],
    [(40, -8.5), (44, -7.8), (48, -6.9), (52, -5.7), (56, -4.4), (60, -2.8), (64, -1.0), (66, 0.0), (68, 1.0), (72, 3.3), (76, 5.6), (80, 8.1), (84, 10.7)],
    [(40, -8.0), (44, -7.4), (48, -6.5), (52, -5.5), (56, -4.2), (60, -2.7), (64, -1.0), (66, 0.0), (68, 1.0), (72, 3.2), (76, 5.5), (80, 7.9), (84, 10.5)],
    [(40, -7.7), (44, -7.3), (48, -6.7), (52, -5.8), (56, -4.6), (60, -3.0), (64, -1.1), (66, 0.0), (68, 1.1), (72, 3.6), (76, 6.2), (80, 8.9), (84, 11.5)],
]
EQ_PRESETS = {
    "FLAT": [0, 0, 0, 0, 0],
    "BASS BOOST": [5, 3, 0, 0, 0],
    "WARM": [3, 2, 0, -1, -2],
    "BRIGHT": [0, 0, 0, 2, 4],
    "VOCAL": [-2, -1, 2, 3, 1],
    "V-SHAPE": [4, 1, -2, 1, 4],
}


def eq_preset_name(gains):
    return next((name for name, g in EQ_PRESETS.items() if list(g) == list(gains)), "CUSTOM")


def _eq_value(k, db):
    """Control value for pair k to give `db` at its centre (the measured table, interpolated)."""
    table = EQ_TABLE[k]
    if db <= table[0][1]:
        return table[0][0]
    for (v0, g0), (v1, g1) in zip(table, table[1:]):
        if db <= g1:
            return v0 + (v1 - v0) * (db - g0) / (g1 - g0)
    return table[-1][0]


def eq_headroom(gains):
    """How far everything is turned down so the boosts can't clip (dB)."""
    top = max(0.0, max(gains))
    return top + EQ_MARGIN if top > 0 else 0.0


def eq_controls(gains):
    """Five band gains (dB) -> the plugin's ten control values, starting from the measured flat.
    Every boost is taken back off all bands together (headroom), so nothing ends up above flat
    and nothing clips: the shape is the same, just a little quieter."""
    shift = -eq_headroom(gains) / EQ_DB_PER_STEP
    out = list(EQ_FLAT)
    for k, g in enumerate(gains):
        lift = _eq_value(k, g) - 66   # what this band needs, in control steps
        for b in EQ_PAIRS[k]:
            out[b] = max(0, min(100, round(EQ_FLAT[b] + lift + shift)))
    return out


class EqManager:
    """Applies the equaliser from a background thread: a fast turn is folded into a few amixer
    calls, and the UI never waits on one."""

    def __init__(self, runner=None):
        self.available = None   # checked by the worker on first use
        self.wanted = self.applied = None
        self.cond = threading.Condition()
        self.runner = runner or self._amixer
        self.thread = None

    @staticmethod
    def check():
        rc, out = run(["amixer", "-D", "equal", "controls"], timeout=5)
        return rc == 0 and "Playback Volume" in out

    @staticmethod
    def _amixer(values):
        lines = "".join(f"cset numid={i + 1} {v},{v}\n" for i, v in enumerate(values))
        try:
            subprocess.run(["amixer", "-q", "-D", "equal", "-s"], input=lines.encode(),
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def set(self, gains):
        with self.cond:
            self.wanted = list(gains)
            self.cond.notify()
        if self.thread is None:
            self.thread = threading.Thread(target=self._worker, name="eq", daemon=True)
            self.thread.start()

    def _worker(self):
        if self.available is None:
            self.available = self.check()
        while True:
            with self.cond:
                while self.wanted is None or self.wanted == self.applied:
                    self.cond.wait()
                gains = list(self.wanted)
            if self.available:
                self.runner(eq_controls(gains))
            self.applied = gains
            time.sleep(0.12)


class EqualiserScreen(Screen):
    """Five bands drawn as a curve with a handle on each. Turn to choose PRESET or a band, press
    to adjust it: the dial then clicks once per dB, walls at +-6 dB, and flat clicks harder and
    pulls the dial in from 1 dB either side. Press again (or back) when done."""

    def __init__(self, app):
        self.app = app
        eq = app.settings.get("eq") or {}
        self.gains = [int(g) for g in eq.get("gains", [0] * 5)][:5]
        self.sel = 1        # 0 = PRESET, 1..5 = the bands
        self.editing = False

    def haptic(self):
        if self.editing:
            return ("list", 2 * EQ_RANGE + 1, self.gains[self.sel - 1] + EQ_RANGE, f"eq{self.sel}")
        return ("list", 1 + len(EQ_BANDS), self.sel, "eq")

    def haptic_extras(self):
        if self.editing:
            return {"marks": [EQ_RANGE], "magnet": EQ_RANGE}
        return None

    def save(self):
        self.app.settings["eq"] = {"preset": eq_preset_name(self.gains), "gains": list(self.gains)}
        save_settings(self.app.settings)

    def set_preset(self, name):
        self.gains = list(EQ_PRESETS[name])
        self.app.eq.set(self.gains)
        self.save()
        self.version += 1

    def handle(self, kind, arg):
        if kind == "rot":
            if self.editing:
                k = self.sel - 1
                g = max(-EQ_RANGE, min(EQ_RANGE, self.gains[k] + arg))
                if g != self.gains[k]:
                    self.gains[k] = g
                    self.app.eq.set(self.gains)
            else:
                self.sel = max(0, min(len(EQ_BANDS), self.sel + arg))
            self.version += 1
        elif kind == "press" and arg == "enc_sw":
            if self.sel == 0:
                name = eq_preset_name(self.gains)
                return OptionScreen(self.app, "PRESET", [(n, n) for n in EQ_PRESETS],
                                    name if name in EQ_PRESETS else "FLAT", self.set_preset)
            self.editing = not self.editing
            if not self.editing:
                self.save()
            self.version += 1
        elif kind == "press" and arg == "back":
            if self.editing:
                self.editing = False
                self.save()
                self.version += 1
                return None
            return "pop"
        return None

    def render(self, now, snap, mode):
        img, d = titled("EQUALISER")
        name = eq_preset_name(self.gains)
        # the preset row
        y = 38
        fg = BLACK
        if self.sel == 0:
            d.rectangle((0, y - 4, W - 1, y + 20), fill=BLACK)
            fg = WHITE
        d.text((10, y), "PRESET", font=F_BODY, fill=fg)
        d.text((W - 10 - tlen(F_BODY, name), y), name, font=F_BODY, fill=fg)
        # the graph: +-6 dB, 0 dB across the middle
        x0, x1, y0, y1 = 32, 230, 78, 234
        mid = (y0 + y1) / 2
        per_db = (y1 - y0) / (2 * EQ_RANGE)
        for db in (-6, -3, 3, 6):
            yy = round(mid - db * per_db)
            for x in range(x0, x1, 4):
                d.point((x, yy), fill=BLACK)
            if abs(db) == 6:
                d.text((4, yy - 6), f"{db:+d}", font=F_TINY, fill=BLACK)
        d.line((x0, mid, x1, mid), fill=BLACK)
        d.text((12, mid - 6), "0", font=F_TINY, fill=BLACK)
        step = (x1 - x0) / len(EQ_BANDS)
        pts = [(x0 + (k + 0.5) * step, mid - g * per_db) for k, g in enumerate(self.gains)]
        # a smooth curve through the handles, flat beyond the ends
        path = [(x0, pts[0][1])]
        ext = [(x0, pts[0][1])] + pts + [(x1, pts[-1][1])]
        for (xa, ya), (xb, yb) in zip(ext, ext[1:]):
            for i in range(1, 9):
                t = i / 8
                u = (1 - math.cos(math.pi * t)) / 2
                path.append((xa + (xb - xa) * t, ya + (yb - ya) * u))
        d.line(path, fill=BLACK, width=2)
        for k, (px, py) in enumerate(pts):
            chosen = self.sel == k + 1
            if chosen and self.editing:
                for yy in range(y0, y1, 3):
                    d.point((round(px), yy), fill=BLACK)
                d.ellipse((px - 7, py - 7, px + 7, py + 7), fill=BLACK)
            elif chosen:
                d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=BLACK)
            else:
                d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=WHITE, outline=BLACK, width=2)
        # band names
        for k, label in enumerate(EQ_BANDS):
            cx = x0 + (k + 0.5) * step
            if self.sel == k + 1:
                wl = tlen(F_TINY, label)
                d.rectangle((cx - wl / 2 - 3, y1 + 8, cx + wl / 2 + 2, y1 + 22), fill=BLACK)
                img.paste(WHITE, (round(cx - wl / 2), y1 + 9), text_mask(label, F_TINY))
            else:
                centred(d, cx, y1 + 9, label, F_TINY)
        # the readout and what to do next
        if STATUS.get("output") == "bluetooth":
            centred(d, W / 2, 300, "Wired only: Bluetooth plays without it", F_TINY)
        if not self.app.eq_ok():
            lines = ("The equaliser isn't installed.", "Run the setup script again.")
        elif self.sel == 0:
            centred(d, W / 2, 268, name, F_HUGE)
            lines = ("Centre: choose a preset", "Turn: go to the bands")
        else:
            g = self.gains[self.sel - 1]
            centred(d, W / 2, 268, f"{EQ_BANDS[self.sel - 1]} {g:+d} dB" if g else f"{EQ_BANDS[self.sel - 1]} FLAT", F_HUGE)
            lines = (("Turn: cut or boost", "Centre: done") if self.editing
                     else ("Turn: choose a band", "Centre: adjust it"))
        head = eq_headroom(self.gains)
        if head and self.app.eq_ok() and STATUS.get("output") != "bluetooth":
            centred(d, W / 2, 296, f"level -{head:.0f} dB: room for the boosts", F_TINY)
        for n, line in enumerate(lines):
            centred(d, W / 2, 324 + n * 16, line, F_SMALL)
        footer(d, snap, self.app.battery)
        return img


# ======================================================================
# Add music (the page itself is currawong_upload.py)
# ======================================================================
def local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))  # UDP: sends nothing, just picks the route out
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return None


def qr_image(text, size):
    """A QR code for `text`, about `size` pixels square, or None without python3-qrcode."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    scale = max(1, size // len(m))
    img = Image.new("1", (len(m) * scale, len(m) * scale), WHITE)
    d = Draw(img)
    for y, row in enumerate(m):
        for x, ink in enumerate(row):
            if ink:
                d.rectangle((x * scale, y * scale, x * scale + scale - 1, y * scale + scale - 1), fill=BLACK)
    return img


class AddMusicScreen(Screen):
    """Where to point a phone or computer to add music: the name, the address, and a QR code."""

    def __init__(self, app):
        self.app = app
        self._ip = (-999.0, None)
        self._qr = (None, None)

    def render_key(self, snap, now):
        return super().render_key(snap, now) + (int(now // 5), STATUS.get("wifi"))

    def ip(self, now):
        if now - self._ip[0] > 10:
            self._ip = (now, local_ip())
        return self._ip[1]

    def handle(self, kind, arg):
        if kind == "press" and arg == "enc_sw" and not STATUS.get("radio", True):
            return wifi_on_task(self.app, lambda: AddMusicScreen(self.app))
        if kind == "press" and arg == "back":
            return "pop"
        return None

    def render(self, now, snap, mode):
        img, d = titled("ADD MUSIC")
        ip = self.ip(now)
        if not ip or not STATUS.get("wifi"):
            lines = ["Wi-Fi isn't connected.", ""]
            lines += ["Centre: turn Wi-Fi on"] if not STATUS.get("radio", True) else ["Check WI-FI in the menu."]
            for n, line in enumerate(lines):
                centred(d, W / 2, 150 + n * 20, line, F_BODY)
        else:
            url = f"http://{ip}/"
            centred(d, W / 2, 42, "On a phone or computer on", F_SMALL)
            centred(d, W / 2, 57, "the same Wi-Fi, open:", F_SMALL)
            centred(d, W / 2, 80, f"{socket.gethostname()}.local", F_HUGE)
            centred(d, W / 2, 108, f"or {url.rstrip('/')}", F_SMALL)
            if self._qr[0] != url:
                self._qr = (url, qr_image(url, 150))
            qr = self._qr[1]
            if qr is not None:
                img.paste(qr, ((W - qr.width) // 2, 130))
                centred(d, W / 2, 136 + qr.height, "or scan it with a phone camera", F_TINY)
            for n, line in enumerate(("Choose songs or a folder.", "They'll appear in Library.")):
                centred(d, W / 2, 330 + n * 16, line, F_SMALL)
        footer(d, snap, self.app.battery)
        return img


# ======================================================================
# Volume
# ======================================================================
class VolumeManager:
    """Volume for whichever output is in use. Bluetooth: bluealsa's volume for the connected
    headphones (passed on as their own volume when they support it). Wired: the DAC's hardware
    volume, when it offers one to Linux; otherwise the DAC's own buttons do it. Changes go out
    from a background thread, so turning the dial never waits on a command."""
    STEPS = 30  # one dial click (10 degrees) = 3.3%: 300 degrees of the knob from silent to full

    def __init__(self):
        self.cached = None        # last known level, 0..1 (for the menu)
        self._target = (None, -1e9)
        self._dac_ctl = None
        self.wanted = self.applied = None
        self.cond = threading.Condition()
        self.thread = None

    @staticmethod
    def _bt_tool():
        return shutil.which("bluealsa-cli") or shutil.which("bluealsactl")

    def _dac_control(self):
        """The DAC's playback volume control name, or "" when it has none."""
        if self._dac_ctl is None:
            rc, out = run(["amixer", "-c", DAC_CARD, "scontrols"], timeout=5)
            self._dac_ctl = ""
            for name in re.findall(r"Simple mixer control '([^']+)',\d+", out):
                rc, info = run(["amixer", "-c", DAC_CARD, "sget", name], timeout=5)
                if "Playback" in info and "%" in info:
                    self._dac_ctl = name
                    break
        return self._dac_ctl

    def target(self, fresh=False):
        """("bt", tool, pcm path), ("dac", control), or None when there's nothing to set."""
        now = time.monotonic()
        if not fresh and now - self._target[1] < 10:
            return self._target[0]
        t = None
        if STATUS.get("output") == "bluetooth":
            tool = self._bt_tool()
            if tool:
                rc, out = run([tool, "list-pcms"], timeout=5)
                for line in out.splitlines():
                    if "/a2dpsrc/" in line:
                        t = ("bt", tool, line.strip())
                        break
        else:
            ctl = self._dac_control()
            if ctl:
                t = ("dac", ctl)
        self._target = (t, now)
        return t

    def get(self):
        """The current level, 0..1, or None when it can't be set from here."""
        t = self.target(fresh=True)
        level = None
        if t and t[0] == "bt":
            rc, out = run([t[1], "volume", t[2]], timeout=5)
            m = re.search(r"Volume:\s*(?:L:\s*)?(\d+)", out)
            level = int(m.group(1)) / 127 if m else None
        elif t:
            rc, out = run(["amixer", "-c", DAC_CARD, "-M", "sget", t[1]], timeout=5)
            m = re.search(r"\[(\d+)%\]", out)
            level = int(m.group(1)) / 100 if m else None
        if level is not None:
            self.cached = level
        return level

    def set(self, level):
        self.cached = level
        with self.cond:
            self.wanted = level
            self.cond.notify()
        if self.thread is None:
            self.thread = threading.Thread(target=self._worker, name="volume", daemon=True)
            self.thread.start()

    def _apply(self, level):
        t = self.target()
        if t and t[0] == "bt":
            v = str(round(level * 127))
            run([t[1], "volume", t[2], v, v], timeout=5)
        elif t:
            run(["amixer", "-q", "-c", DAC_CARD, "-M", "sset", t[1], f"{round(level * 100)}%"], timeout=5)

    def _worker(self):
        while True:
            with self.cond:
                while self.wanted is None or self.wanted == self.applied:
                    self.cond.wait()
                level = self.wanted
            self._apply(level)
            self.applied = level
            time.sleep(0.08)


def ring_bar(d, cx, cy, r, frac, thick=18, start=120, sweep=300):
    """A circular bar: an outlined ring from `start` round `sweep` degrees (PIL's angles: 0 is
    3 o'clock, clockwise, so the gap is at the bottom), filled smoothly from its start to frac."""
    ri = r - thick
    d.arc((cx - r, cy - r, cx + r, cy + r), start, start + sweep, fill=BLACK, width=2)
    d.arc((cx - ri, cy - ri, cx + ri, cy + ri), start, start + sweep, fill=BLACK, width=2)
    for a in (start, start + sweep):                    # the ends
        t = math.radians(a)
        d.line((cx + (ri - 1) * math.cos(t), cy + (ri - 1) * math.sin(t),
                cx + r * math.cos(t), cy + r * math.sin(t)), fill=BLACK, width=2)
    if frac > 0:                                        # the fill, 3 px clear of both outlines
        rf = r - 5
        d.arc((cx - rf, cy - rf, cx + rf, cy + rf), start, start + sweep * min(1.0, frac),
              fill=BLACK, width=thick - 8)


class VolumeScreen(Screen):
    """Hold BACK on Now Playing (or Menu > Volume): the dial sets the volume on a circular bar.
    The knob clicks lightly every 10 degrees (3.3%) over 300 degrees with walls at silent and
    full, and the ring covers the same 300 degrees, so the fill follows your hand exactly.
    Closes itself a few seconds after the last turn, or on centre or BACK."""
    CLOSE_SECS = 3.0
    CX, CY, R = W // 2, 172, 92

    def __init__(self, app):
        self.app = app
        level = app.volume.get()
        self.idx = None if level is None else round(level * VolumeManager.STEPS)
        self.last = time.monotonic()

    def haptic(self):
        if self.idx is None:
            return ("list", 1, 0, "vol-none")
        return ("fine", 0, 0, "volume")   # light 10-degree clicks; the walls come from dial_range

    def dial_range(self, snap):
        return None if self.idx is None else (VolumeManager.STEPS + 1, self.idx)

    def handle(self, kind, arg):
        if kind == "rot" and self.idx is not None:
            new = max(0, min(VolumeManager.STEPS, self.idx + arg))
            if new != self.idx:
                self.idx = new
                self.app.volume.set(new / VolumeManager.STEPS)
            self.last = time.monotonic()
            self.version += 1
        elif kind == "press":
            return "pop"  # centre or BACK (play and skip still act on the music)
        return None

    def tick(self):
        if time.monotonic() - self.last > self.CLOSE_SECS:
            return "pop"
        return None

    def render(self, now, snap, mode):
        img, d = titled("VOLUME")
        bt = STATUS.get("output") == "bluetooth"
        if self.idx is None:
            centred(d, W / 2, 50, "BLUETOOTH" if bt else "WIRED", F_BODY)
            lines = (["The headphones' volume", "can't be reached.", "", "Check they're connected."] if bt
                     else ["This DAC sets its own volume:", "use the buttons on it."])
            for n, line in enumerate(lines):
                centred(d, W / 2, 150 + n * 18, line, F_SMALL)
        else:
            cx, cy = self.CX, self.CY
            ring_bar(d, cx, cy, self.R, self.idx / VolumeManager.STEPS)
            centred(d, cx, cy - 18, f"{round(100 * self.idx / VolumeManager.STEPS)}%", F_HUGE)
            label_ = "BLUETOOTH" if bt else "WIRED"
            lw = tlen(F_SMALL, label_) + (11 if bt else 0)
            x = cx - lw / 2
            if bt:
                bt_icon(d, int(x), cy + 10)
                x += 11
            d.text((x, cy + 10), label_, font=F_SMALL, fill=BLACK)
            centred(d, W / 2, 300, "Turn to change", F_SMALL)
            centred(d, W / 2, 316, "Centre or BACK closes", F_SMALL)
        footer(d, snap, self.app.battery)
        return img


def wifi_on_task(app, then=None):
    """Radio on, wait for a network, restart go-librespot so Spotify reconnects at once, then a
    double tick on the dial to say it's ready."""
    def done(ok):
        if ok is True:
            app.alert(2)
            return ("replace", then() if then else MessageScreen(app, "WI-FI", ["Wi-Fi is on.", "", "Spotify is ready."]))
        return ("replace", MessageScreen(app, "WI-FI", ["Wi-Fi is on, but it", "hasn't joined a network.", "",
                                                        "Try SAVED NETWORKS."]))
    return TaskScreen(app, "WI-FI", ["Turning Wi-Fi on", "Waiting for Spotify"], app.wifi_up, done)


def online_then(app, then):
    """Spotify needs the network: if Wi-Fi is off, turn it on first."""
    if STATUS.get("radio", True):
        return then()
    return wifi_on_task(app, then)


def wifi_join(app, net):
    if not net["secure"]:
        return wifi_connect_task(app, net["ssid"], None)
    return TextEntryScreen(app, "PASSWORD", lambda pw: ("replace", wifi_connect_task(app, net["ssid"], pw)),
                           secret=True)


def wifi_connect_task(app, ssid, password):
    def done(res):
        ok, msg = res if isinstance(res, tuple) else (False, str(res))
        lines = ["Connected to", ssid] if ok else ["Couldn't connect.", "", msg or "Check the password."]
        return ("replace", MessageScreen(app, "WI-FI", lines))
    return TaskScreen(app, "WI-FI", ["Joining", fit(ssid, F_BODY, W - 40)], lambda: app.wifi.connect(ssid, password), done)


def wifi_saved_actions(app, name):
    def connect():
        return TaskScreen(app, "WI-FI", ["Connecting to", name], lambda: app.wifi.up(name),
                          lambda ok: ("replace", MessageScreen(app, "WI-FI", ["Connected." if ok is True else "Couldn't connect."])))

    def forget():
        return TaskScreen(app, "WI-FI", "Forgetting", lambda: app.wifi.forget(name),
                          lambda ok: "home")
    return ListScreen(app, fit(name.upper(), F_BIG, W - 16), [
        ("CONNECT", None, connect),
        ("FORGET", None, lambda: confirm(app, "FORGET?", "FORGET NETWORK", forget)),
    ])


def bt_menu(app):
    def found(devs):
        if isinstance(devs, Exception) or not devs:
            why = app.bt.last_reason or "Put the headphones in pairing mode, then scan again."
            return ("replace", MessageScreen(app, "BLUETOOTH", ["Nothing found.", "", why, "", "Log: ~/currawong/bt.log"]))
        items = []
        for mac, name, was_paired in devs:
            label_ = name or "No name yet " + mac[-5:]
            if was_paired:
                items.append((label_, lambda: "PAIRED", (lambda m=mac, n=label_: bt_device_actions(app, m, n))))
            else:
                items.append((label_, None, (lambda m=mac, n=label_: bt_pair(app, m, n))))
        return ("replace", ListScreen(app, "FOUND", items))

    def paired(devs):
        if isinstance(devs, Exception) or not devs:
            return ("replace", MessageScreen(app, "BLUETOOTH", ["No paired devices yet."]))
        return ("replace", ListScreen(app, "PAIRED", [(name or mac, None, (lambda m=mac, n=name or mac: bt_device_actions(app, m, n)))
                                                      for mac, name in devs.items()]))

    return ListScreen(app, "BLUETOOTH", [
        ("PAIR NEW DEVICE", None, lambda: TaskScreen(
            app, "BLUETOOTH", ["Put the headphones in", "pairing mode.", "Scanning"], lambda: bt_scan_marked(app), found)),
        ("PAIRED DEVICES", None, lambda: TaskScreen(app, "BLUETOOTH", "Loading", app.bt.paired, paired)),
        ("AUDIO OUTPUT", output_value, lambda: output_menu(app)),
    ])


def bt_scan_marked(app):
    """app.bt.scan(), with the menu's BLUETOOTH value saying "Searching…" meanwhile."""
    STATUS["bt_scan"] = True
    try:
        return app.bt.scan()
    finally:
        STATUS["bt_scan"] = False


def use_for_audio(app, name):
    def use():
        STATUS["bt_name"] = app.settings["bt_name"] = name  # for the menu's BLUETOOTH value
        return switch_output(app, "bluetooth")
    return ListScreen(app, "CONNECTED", [
        ("USE " + name.upper(), None, use),
        ("KEEP WIRED", None, lambda: "home"),
    ])


def bt_pair(app, mac, name):
    def done(res):
        paired, connected, why = (tuple(res) + ("",))[:3] if isinstance(res, tuple) else (False, False, str(res))
        if paired and connected:
            app.alert(2)
            return ("replace", use_for_audio(app, name))
        head = "Paired, but not connected yet." if paired else "Pairing failed."
        lines = [head, "", why or "Check it's in pairing mode and try again.", "", "Log: ~/currawong/bt.log"]
        return ("replace", MessageScreen(app, "BLUETOOTH", lines))
    return TaskScreen(app, "BLUETOOTH", ["Pairing", fit(name, F_BODY, W - 40)], lambda: app.bt.pair(mac), done)


def bt_device_actions(app, mac, name):
    def connected(ok):
        if ok is True:
            app.alert(2)
        return ok

    def connect():
        return TaskScreen(app, "BLUETOOTH", ["Connecting", fit(name, F_BODY, W - 40)], lambda: connected(app.bt.connect(mac)),
                          lambda ok: ("replace", use_for_audio(app, name) if ok is True else
                                      MessageScreen(app, "BLUETOOTH", ["Couldn't connect.", "",
                                                                       app.bt.last_reason or "Is it switched on and close by?",
                                                                       "", "Log: ~/currawong/bt.log"])))

    def disconnect():
        if STATUS.get("output") == "bluetooth":
            OutputManager(app).apply("wired")
        return TaskScreen(app, "BLUETOOTH", "Disconnecting", lambda: app.bt.disconnect(mac), lambda ok: "home")

    def forget():
        return TaskScreen(app, "BLUETOOTH", "Forgetting", lambda: app.bt.remove(mac), lambda ok: "home")
    return ListScreen(app, fit(name.upper(), F_BIG, W - 16), [
        ("CONNECT", None, connect),
        ("DISCONNECT", None, disconnect),
        ("FORGET", None, lambda: confirm(app, "FORGET?", "FORGET DEVICE", forget)),
    ])


def switch_output(app, which):
    label = "Bluetooth" if which == "bluetooth" else "Wired"

    def done(ok):
        lines = [f"{label} output on."] if ok is True else ["Switched, with errors.", "", "Check go-librespot", "and MPD outputs."]
        return ("replace", MessageScreen(app, "OUTPUT", lines + ["", "Spotify restarts", "briefly on a switch."]))
    return TaskScreen(app, "OUTPUT", "Switching", lambda: app.output.apply(which), done)


def output_menu(app):
    cur = lambda: STATUS.get("output")
    return ListScreen(app, "OUTPUT", [
        ("WIRED (DAC)", lambda: "ON" if cur() != "bluetooth" else "", lambda: switch_output(app, "wired")),
        ("BLUETOOTH", lambda: "ON" if cur() == "bluetooth" else "", lambda: switch_output(app, "bluetooth")),
    ])


# ======================================================================
# Menus and settings
# ======================================================================
def main_menu(app):
    items = [
        ("NOW PLAYING", None, lambda: "home"),
        ("LIBRARY", None, lambda: library_menu(app)),
        ("ADD MUSIC", None, lambda: AddMusicScreen(app)),
        ("SPOTIFY", None, lambda: online_then(app, lambda: spotify_menu(app))),
        ("TRACK OPTIONS", None, lambda: track_menu(app)),
    ]
    if app.settings.get("visuals", 1) == 0:   # the Scope page only in FULL visuals
        items.append(("SCOPE", None, lambda: app.scope))
    items += [
        ("BLUETOOTH", bt_value, lambda: bt_menu(app)),
        ("WI-FI", lambda: wifi_value(app), lambda: wifi_menu(app)),
        ("OUTPUT", output_value, lambda: output_menu(app)),
        ("VOLUME", lambda: ">" if app.volume.cached is None else f"{round(app.volume.cached * 100)}%",
         lambda: VolumeScreen(app)),
        ("EQUALISER", lambda: eq_preset_name((app.settings.get("eq") or {}).get("gains", [0] * 5)),
         lambda: EqualiserScreen(app)),
        ("SETTINGS", None, lambda: settings_menu(app)),
        ("ABOUT", lambda: UI_BUILD, lambda: AboutScreen(app)),
        ("POWER OFF", None, lambda: confirm(app, "POWER OFF?", "POWER OFF", app.power_off)),
    ]
    return MenuScreen(app, "MENU", items)


def bt_value():
    """The menu's BLUETOOTH value: the headphones in use, "Searching…" during a scan, else ">"."""
    if STATUS.get("output") == "bluetooth":
        return fit(STATUS.get("bt_name") or "ON", F_BODY, 110)
    return "Searching…" if STATUS.get("bt_scan") else ">"


def wifi_value(app):
    """The menu's WI-FI value: the network's name when joined, OFF with the radio off, else ">".
    Never waits on nmcli (the menu reads its values every frame)."""
    if not STATUS.get("radio", True):
        return "OFF"
    name = app.wifi.cached_name() if STATUS.get("wifi") else None
    return fit(name, F_BODY, 110) if name else ">"


SETTINGS_EVERYDAY = ["DIAL FEEL", "BUTTON CLICK", "SPIN ON SKIP", "POCKET LOCK", "SLEEP AFTER", "AUTO OFF",
                     "ON POWER UP", "WI-FI SAVER"]
SETTINGS_ADVANCED = ["SCRUB STYLE", "SCRUB CLICK", "VISUALS", "ANIMATE FOR", "DIAL DIRECTION", "SCREEN", "BATTERY"]


def _setting_rows(app):
    """Every settings row, by name: settings_menu and advanced_menu each pick theirs."""
    s = app.settings
    h = lambda: app.haptics  # noqa: E731

    def setting(key, title, options, after=None, live=None):
        """A row that shows the current value; centre opens the dial picker for it."""
        def label():
            return next((text for value, text in options if value == s.get(key)), str(s.get(key)))

        def set_value(v):
            s[key] = v
            save_settings(s)
            if after:
                after(v)
        return (title, label, lambda: OptionScreen(app, title, options, s.get(key), set_value, live))

    dial_version = getattr(app.haptics, "version", 0) or 0

    def feel(v):
        if h():
            h().set_feel(v)

    def click(v):
        if v and h() and getattr(h(), "ok", False):
            h().tick(1, v)

    def intensity(key, title, values, live, fmt=lambda v: f"{v}%", note=""):
        """A row whose value is set on the drawn intensity dial."""
        def set_value(v):
            s[key] = v
            save_settings(s)
            if key == "feel":
                live(v)  # the dial keeps its feel; a click strength is just used for the next click
        return (title, lambda: fmt(s.get(key, 0)),
                lambda: IntensityScreen(app, title, values, s.get(key, 0), set_value, live=live, fmt=fmt, note=note))

    rows = [
        intensity("feel", "DIAL FEEL", range(50, 151, 10), live=feel, note="normal is 100%"),
        intensity("click", "BUTTON CLICK", range(0, 101, 10), live=click,
                  fmt=lambda v: "OFF" if not v else f"{v}%", note="0 is off") if dial_version >= 4
        else setting("click", "BUTTON CLICK", [(0, "OFF"), (s.get("click") or 100, "ON")]),  # older firmware: one strength
        setting("skip_spin", "SPIN ON SKIP", [(0, "OFF"), (1, "ON")]),
        setting("pocket_secs", "POCKET LOCK", [(0, "OFF"), (15, "15s"), (30, "30s"), (60, "60s")]),
        setting("sleep_secs", "SLEEP AFTER", [(60, "1m"), (180, "3m"), (300, "5m"), (600, "10m")]),
        setting("auto_off_mins", "AUTO OFF", [(15, "15m"), (30, "30m"), (60, "60m"), (0, "NEVER")]),
        setting("resume", "ON POWER UP", [("ready", "READY"), ("play", "PLAY"), ("off", "OFF")]),
        setting("wifi_saver", "WI-FI SAVER", [(0, "OFF"), (1, "ON")]),
        setting("scrub_style", "SCRUB STYLE", [("clicks", "CLICKS"), ("shuttle", "SHUTTLE")]),
        setting("scrub_step", "SCRUB CLICK", [(1, "1s"), (2, "2s"), (5, "5s")]),
        setting("visuals", "VISUALS", [(0, "FULL"), (1, "REGULAR"), (2, "FAST")], lambda v: app.drop_scope()),
        setting("active_secs", "ANIMATE FOR", [(15, "15s"), (30, "30s"), (60, "60s"), (0, "ALWAYS")]),
        setting("dial_dir", "DIAL DIRECTION", [(1, "NORMAL"), (-1, "REVERSED")], lambda v: h() and h().set_dir(v)),
        setting("rotate", "SCREEN", [(270, "270°"), (90, "90°")], lambda v: app.rotated(v)),
        setting("battery_mah", "BATTERY", [(2000, "2000 mAh"), (5000, "5000 mAh")]
                + ([(s["battery_mah"], f"{s['battery_mah']} mAh")] if s.get("battery_mah") not in (2000, 5000) else []),
                lambda v: app.configure_battery()),
    ]
    return {row[0]: row for row in rows}


def settings_menu(app):
    """The everyday settings, with ADVANCED (tuning, and things set once) at the bottom."""
    rows = _setting_rows(app)
    return ListScreen(app, "SETTINGS", [rows[n] for n in SETTINGS_EVERYDAY]
                      + [("ADVANCED", None, lambda: advanced_menu(app)),
                         ("REPORT BUGS", None, lambda: MessageScreen(app, "REPORT BUGS", ["", "Talk to Jasper."]))])


def advanced_menu(app):
    rows = _setting_rows(app)
    return ListScreen(app, "ADVANCED", [rows[n] for n in SETTINGS_ADVANCED]
                      + [("RESET SETTINGS", None, lambda: confirm(app, "RESET SETTINGS", "RESET ALL",
                                                                  lambda: reset_settings(app)))])


def reset_settings(app):
    """Every setting back to how it came out of the box (the music, Wi-Fi and pairings stay)."""
    fresh = json.loads(json.dumps(DEFAULT_SETTINGS))
    fresh["settings_v"] = SETTINGS_V
    app.settings.clear()            # in place: other parts of the UI hold this same dict
    app.settings.update(fresh)
    save_settings(app.settings)
    h = app.haptics
    if h:
        h.set_feel(app.settings["feel"])
        h.set_dir(app.settings["dial_dir"])
    app.rotated(app.settings["rotate"])
    if getattr(app, "eq", None):
        app.eq.set(app.settings["eq"]["gains"])
    app.configure_battery()
    app.drop_scope()
    return ("replace", MessageScreen(app, "SETTINGS", ["Every setting is back", "to how it came out", "of the box.", "",
                                                       "Your music, Wi-Fi and", "headphones are untouched."]))


# ======================================================================
# Auto-scrub and pocket lock
# ======================================================================
class Scrub:
    """Hold next or previous to start. The same button again goes faster; anything else stops it.
    Playback keeps running between jumps, so you hear short snippets, like a CD player scanning."""

    def __init__(self, app):
        self.app = app
        self.dir = 0
        self.level = 0
        self.pos = 0.0
        self.next_at = 0.0
        self.uri = None
        self.shuttle_speed = 0   # set while the shuttle dial drives it, instead of the buttons

    def active(self):
        return self.dir != 0

    def speed(self):
        return self.shuttle_speed or SCRUB_SPEEDS[self.level]

    def label(self):
        return f"{'FF' if self.dir > 0 else 'REW'} {self.speed()}x" if self.dir else ""

    def start(self, direction, now, snap):
        if not snap.has_track or not snap.dur:
            return
        self.dir, self.level, self.pos, self.next_at, self.uri = direction, 0, float(snap.pos), now, snap.uri
        self.shuttle_speed = 0
        self._roll()

    def shuttle(self, twist, now, snap):
        """The shuttle dial's twist (-100..100): a bigger twist scrubs faster, a click marks each
        change of gear, and letting it spring back to the middle stops."""
        speed = shuttle_speed(abs(twist))
        if not speed:
            if self.shuttle_speed:
                self.stop(tell_dial=False)
            return
        if not snap.has_track or not snap.dur:
            return
        h = self.app.haptics
        if not self.dir:
            self.pos, self.next_at, self.uri = float(snap.pos), now, snap.uri
        elif not self.shuttle_speed and h:
            h.roll(0)        # the buttons' scrub was turning the dial: the hand has it now
        elif speed != self.shuttle_speed and h:
            h.tick()         # a notch between gears
        self.dir = 1 if twist > 0 else -1
        self.shuttle_speed = speed

    def faster(self):
        self.level = min(self.level + 1, len(SCRUB_SPEEDS) - 1)
        self._roll()

    def stop(self, tell_dial=True):
        if self.dir and tell_dial and not self.shuttle_speed and self.app.haptics:
            self.app.haptics.roll(0)
        self.dir = 0
        self.shuttle_speed = 0

    def _roll(self):
        if self.app.haptics and self.app.haptics.ok:
            self.app.haptics.roll(self.dir * ROLL_RATES[self.level])

    def tick(self, now, snap):
        if not self.dir:
            return
        if not snap.has_track or not snap.dur or snap.uri != self.uri:
            self.stop()  # the track ended or changed underneath us
            return
        if now < self.next_at:
            return
        step = SCRUB_TICK.get(snap.source, 0.5)
        self.next_at = now + step
        self.pos += self.dir * self.speed() * step * 1000
        end = snap.dur - 1500
        if self.pos >= end or self.pos <= 0:
            self.app.player.seek_to(snap, min(max(self.pos, 0), end))
            self.stop()
            return
        self.app.player.seek_to(snap, self.pos)


class PocketLock:
    """Pocket mode: the dial ignores turns (and its motor stays off) until the centre button is
    pressed, then works until it has been left alone for the time set in Settings."""

    def __init__(self, app):
        self.app = app
        self.until = 0.0
        self.sent = None

    def secs(self):
        return self.app.settings.get("pocket_secs", 0)

    def locked(self, now):
        return self.secs() > 0 and now >= self.until

    def touch(self, now):
        if self.secs() > 0 and now < self.until:
            self.until = now + self.secs()

    def unlock(self, now):
        self.until = now + self.secs()

    def sync(self, now, force=False):
        state = self.locked(now)
        STATUS["locked"] = state
        h = self.app.haptics
        if h and (force or state != self.sent):
            h.lock(state)
            if not state and self.sent:
                h.tick()  # one click to say the dial is live again
            self.sent = state


# ======================================================================
# App: input, modes, event-driven main loop
# ======================================================================
class App:
    def __init__(self, player, display, battery, analyser, wifi=None, bt=None):
        self.player, self.display = player, display
        self.battery, self.analyser = battery, analyser
        self.wifi = wifi or WifiManager()
        self.bt = bt or BluetoothManager()
        self.output = OutputManager(self)
        self.haptics = None
        self.events = queue.Queue()
        self.wake = threading.Event()
        self.settings = load_settings()
        STATUS["output"] = self.settings.get("output", "wired")
        STATUS["bt_name"] = self.settings.get("bt_name")
        self.configure_battery()
        self.bird = None             # the currawong: loaded after the first screen (load_bird)
        self.bird_base = None        # the screen's own frame, which the bird is drawn over
        self.bird_boxes = []         # what the bird covered last tick
        self.bird_tick = None
        self.bird_key = None         # the screen state bird_base was drawn for
        self.bird_showing = False
        self.paused_since = None     # when playback last stopped with a track still loaded
        self.paused_idle = False     # paused a while, nothing touched: the branch scene
        self.home = Home(self)
        self.scope = ScopeScreen(self)
        self.stack = [self.home]
        now = time.monotonic()
        self.last_input = self.last_playing = self.track_changed = now
        self.last_full = -999.0
        self.last_key = None
        self.last_uri = None
        self.mode = "active"
        self.snap = Snap()
        self.shutting_down = False
        self.animating = False
        self.next_wifi_check = 0.0
        self.scrub = Scrub(self)
        self.lock = PocketLock(self)
        self.first_shown = False
        self.resume = None   # main() sets it up: only the real player remembers and resumes
        self.volume = VolumeManager()
        self.eq = EqManager()
        # Always, flat included: the plugin's file keeps whatever was last written, and flat means
        # EQ_FLAT, not the plugin's own (louder) default.
        self.eq.set((self.settings.get("eq") or {}).get("gains") or [0] * 5)
        self.background_after_start = False  # main() sets it: drop to Nice 10 after the first screen
        self.step_at = 0.0
        self.batt_alert = 0          # 1 after the 10% alert, 2 after the 5% one
        self.last_spotify = now      # Spotify last playing (Wi-Fi saver)
        self.radio_on_at = now
        self.radio_busy = False
        STATUS["radio"] = True  # main() checks for real in the background (start_wifi_check)

    def configure_battery(self):
        """The cell's capacity (for the drain estimate) and the voltage cutoff, from the settings."""
        configure = getattr(self.battery, "configure", None)
        if configure:
            configure(self.settings.get("battery_mah", BATTERY_MAH), self.settings.get("cutoff_volts", BATT_CUTOFF_VOLTS))

    def load_bird(self):
        """The character layer (currawong_bird.py and assets/). Optional: without it, or with a
        currawong_bird.py older than this UI, the UI runs as it was, with the plain progress bar."""
        try:
            import inspect
            import currawong_bird
            missing = [what for what, ok in (
                ("Bird.observe(idle=...)", "idle" in inspect.signature(currawong_bird.Bird.observe).parameters),
                ("draw_still()", hasattr(currawong_bird, "draw_still")),
                ("draw_twig()", hasattr(currawong_bird, "draw_twig"))) if not ok]
            if missing:
                print(f"Bird: not loaded: {currawong_bird.__file__} is older than this UI (it has no "
                      f"{', '.join(missing)}). Copy the new currawong_bird.py next to currawong_ui.py.", flush=True)
                return
            self.bird = currawong_bird.Bird()
            print(f"Bird: loaded from {self.bird.dir}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"Bird: not loaded ({e})", flush=True)

    def bird_failed(self, where):
        """The bird's code raised: log it once and carry on without the bird (it's decoration)."""
        import traceback
        print(f"Bird: switched off after an error in {where}:\n{traceback.format_exc()}", flush=True)
        self.bird = None
        self.bird_base = self.bird_key = None
        self.bird_boxes = []
        self.bird_showing = False
        self.paused_idle = False
        self.last_key = None   # redraw the whole screen, without it

    def bird_draw(self, bird_layer, frame, now):
        """bird_layer(frame, now), or None if it raised (the bird is then off for good)."""
        try:
            return bird_layer(frame, now) or []
        except Exception:  # noqa: BLE001
            self.bird_failed("drawing")
            return None

    def start_wifi_check(self, settle=90):
        """At startup, in the background: NetworkManager remembers the radio being off across a
        restart (the Wi-Fi switch, or Wi-Fi saver), which left the player with no network after
        a reboot. So switch it back on, then if no network has joined after `settle` seconds and
        NetworkManager has given up, scan and join a saved one (what used to fix it by hand)."""
        def check():
            self.radio_busy = True
            try:
                if not self.wifi.radio_on():
                    self.wifi.set_radio(True)
                STATUS["radio"] = True
                self.radio_on_at = time.monotonic()
            finally:
                self.radio_busy = False
            end = time.monotonic() + settle
            while time.monotonic() < end:
                if self.wifi.connected():
                    return
                time.sleep(3)
            if self.wifi.state().startswith("disconnected"):
                self.wifi.kick()
        threading.Thread(target=check, name="wifi-check", daemon=True).start()

    def eq_ok(self):
        """False only once the equaliser is known to be missing (unknown counts as fine)."""
        return self.eq.available is not False

    def rotated(self, rot):
        """Settings > Screen: turn the display now, and redo the boot animation and splash."""
        self.display.set_rotation(rot)
        if isinstance(self.display, SharpDisplay):
            save_splash(self.display, splash_screen(), rot)
            threading.Thread(target=build_boot_anim, args=(rot,), name="boot-anim", daemon=True).start()

    def click(self, now):
        """Settings > Button click: a soft tick through the dial (not while pocket-locked)."""
        pct = self.settings.get("click", 30)
        h = self.haptics
        if pct and h and getattr(h, "ok", False) and not self.lock.locked(now):
            h.tick(1, pct)

    def alert(self, n):
        """n strong ticks on the dial: 2 = something's ready, 3 = battery nearly flat."""
        h = self.haptics
        if h and getattr(h, "ok", False):
            h.tick(n)

    def set_radio(self, on):
        STATUS["radio"] = on
        if on:
            self.radio_on_at = time.monotonic()
        threading.Thread(target=self.wifi.set_radio, args=(on,), name="radio", daemon=True).start()

    def restart_spotify(self, wait=20):
        """Restart go-librespot after the network comes back, and wait for its API to answer."""
        run(["systemctl", "restart", "go-librespot"], timeout=40, sudo=True)
        api = getattr(self.player, "api", None)
        end = time.monotonic() + wait
        while api is not None and time.monotonic() < end:
            try:
                api.request("GET", "/status", timeout=2)
                return
            except OSError:
                time.sleep(1)

    def wifi_up(self, wait=45, settle=3, kick_after=15):
        """Radio on, then wait for a network and for Spotify. Runs in a TaskScreen."""
        self.radio_busy = True
        try:
            self.wifi.set_radio(True)
            STATUS["radio"] = True
            self.radio_on_at = time.monotonic()
            start = time.monotonic()
            kicked = False
            while not self.wifi.connected():
                waited = time.monotonic() - start
                if waited > wait:
                    return False
                if not kicked and waited > kick_after:  # not joined by itself: scan and join a saved one
                    kicked = True
                    self.wifi.kick()
                time.sleep(1)
            time.sleep(settle)  # an address and DNS come a moment after the link
            self.restart_spotify()
            return True
        finally:
            self.radio_busy = False

    def sync_dial(self, now, screen):
        """Keep the dial's song walls in step with playback (CLICKS scrub style)."""
        h = self.haptics
        if not (h and getattr(h, "ok", False) and hasattr(h, "set_range")):
            return
        r = screen.dial_range(self.snap)
        if r is None:
            return
        so = getattr(self.player, "seek_override", None)
        seeking = isinstance(so, tuple) and now < so[1]
        settled = now - self.last_input > 1.5 and not seeking and not self.scrub.active()
        h.set_range(r[0], r[1], now, settled)

    def drop_scope(self):
        """Leave the Scope page if Settings > Visuals just hid it."""
        if self.settings.get("visuals", 1) != 0 and self.scope in self.stack:
            self.stack.remove(self.scope)

    def _spin(self, direction, now):
        """Turn the dial a full revolution on a track skip (Settings > Spin on skip)."""
        h = self.haptics
        if self.settings.get("skip_spin", 1) and h and h.ok and not self.lock.locked(now):
            h.spin(direction)

    def top(self):
        s = self.stack[-1]
        return s.current(self.snap) if s is self.home else s

    def post_event(self, kind, arg):
        self.events.put((kind, arg))
        self.wake.set()

    def apply_result(self, result, replace=False):
        how, value = resolve(result)
        replace = replace or how == "replace"
        if value is None:
            return
        if value == "pop":
            if len(self.stack) > 1:
                self.stack.pop()
        elif value == "home":
            del self.stack[1:]
        elif value is self.scope:
            del self.stack[1:]
            self.stack.append(self.scope)
        elif replace and len(self.stack) > 1:
            self.stack[-1] = value
        else:
            self.stack.append(value)

    def dispatch(self, kind, arg, now=None):
        now = time.monotonic() if now is None else now
        top = self.top()
        captures = getattr(top, "captures", set())
        if kind == "jog":  # the shuttle dial's twist
            if getattr(top, "now_playing", False) and self.settings.get("scrub_style") == "shuttle":
                self.scrub.shuttle(arg, now, self.snap)
            return
        # Auto-scrub running: the same button goes faster; any other press or a turn stops it.
        if self.scrub.active():
            same = (arg == "next" and self.scrub.dir > 0) or (arg == "prev" and self.scrub.dir < 0)
            if kind in ("press", "hold") and same:
                self.scrub.faster()
                return
            self.scrub.stop()
            if kind == "rot" or (kind in ("press", "hold") and arg in ("next", "prev", "play")):
                return  # that input only stopped the scrub
        if kind == "hold" and arg in ("next", "prev") and arg not in captures:
            self.scrub.start(1 if arg == "next" else -1, now, self.snap)
            return
        if kind == "press" and arg in captures:
            self.apply_result(top.handle(kind, arg))
            return
        if kind == "press" and arg == "play":
            self.player.play_pause(self.snap)
            return
        if kind == "press" and arg == "next":
            self.player.next(self.snap)
            self._spin(1, now)
            return
        if kind == "press" and arg == "prev":
            self.player.prev(self.snap)
            self._spin(-1, now)
            return
        if kind == "hold" and arg == "back":
            if getattr(top, "now_playing", False):  # on Now Playing: the volume
                if not self.lock.locked(now):
                    self.stack.append(VolumeScreen(self))
                return
            del self.stack[1:]                      # anywhere else: home, as before
            return
        if kind == "hold" and arg == "enc_sw":
            hold = getattr(top, "hold_select", None)
            if hold is not None:   # a song list: hold centre for the song's options
                self.apply_result(hold())
                return
            if self.settings.get("visuals", 1) != 0:
                return  # the Scope page is hidden in REGULAR and FAST visuals
            on_scope = self.stack[-1] is self.scope
            del self.stack[1:]
            if not on_scope:
                self.stack.append(self.scope)
            return
        self.apply_result(top.handle(kind, arg))

    def update_mode(self, now):
        s = self.snap
        if s.uri != self.last_uri:
            self.last_uri, self.track_changed = s.uri, now
        if s.playing:
            self.last_playing = now
        recent = now - max(self.last_input, self.track_changed)
        act = self.settings["active_secs"]
        if s.playing and (act == 0 or recent < act):
            return "active"
        if now - self.last_input < 15:
            return "active"
        if not s.playing and now - max(self.last_playing, self.last_input) > self.settings["sleep_secs"]:
            return "sleep"
        return "glance"

    def power_off(self, reason=None):
        if self.shutting_down:
            return None
        self.shutting_down = True
        if self.resume is not None:
            self.resume.save(self.snap)  # the exact place, whatever the last save missed
        self.display.show(powered_off_screen(reason))
        subprocess.Popen(["sudo", "-n", "poweroff"])
        return None

    def housekeeping(self, now):
        if self.resume is not None:
            self.resume.tick(now, self.snap)
        h = self.haptics
        if h and getattr(h, "resync", False):
            h.resync = False
            h.set_feel(self.settings.get("feel", 100))
            h.set_dir(self.settings.get("dial_dir", 1))
            self.lock.sync(now, force=True)
        self.lock.sync(now)
        self.scrub.tick(now, self.snap)
        STATUS["scrub"] = self.scrub.label()
        if now >= self.next_wifi_check:
            self.next_wifi_check = now + 5
            STATUS["wifi"] = self.wifi.connected()
        self.battery.update(now)
        b = self.battery
        if b.source == "gauge" and b.pct is not None:  # dial alerts on the way down, once each
            if b.charging or b.pct >= 15:
                self.batt_alert = 0
            elif b.pct <= 5 and self.batt_alert < 2:
                self.alert(3)
                self.batt_alert = 2
            elif b.pct <= 10 and self.batt_alert < 1:
                self.alert(2)
                self.batt_alert = 1
        if self.snap.source == "spotify" and self.snap.playing:
            self.last_spotify = now
        if (self.settings.get("wifi_saver") and STATUS.get("radio", True) and not self.radio_busy
                and now - max(self.last_spotify, self.radio_on_at) > WIFI_SAVER_SECS
                and now - self.last_input > 60):
            self.set_radio(False)  # Spotify brings it back on (Menu > Spotify)
        due = getattr(self.battery, "shutdown_due", None)
        why = due(now) if due else ("charge" if self.battery.low_since
                                    and now - self.battery.low_since > LOW_BATT_SHUTDOWN_SECS else None)
        if why:
            print(f"Battery: switching off ({'charge' if why == 'charge' else 'cell voltage'} too low)", flush=True)
            self.power_off("BATTERY LOW")
            return
        mins = self.settings.get("auto_off_mins", 0)
        idle = now - max(self.last_playing, self.last_input)
        if (mins and self.battery.source and not self.battery.charging and not self.snap.playing
                and idle > mins * 60):
            self.power_off("AUTO OFF")

    def step(self, now):
        self.wake.clear()
        had_input = False
        while True:
            try:
                kind, arg = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "grab":  # a hand stopped the dial's spin or roll
                self.scrub.stop(tell_dial=False)
                continue
            if kind in ("down", "hold"):
                self.click(now)  # a soft tick as a button goes down, and again when a hold registers
                if kind == "down":
                    continue     # the press itself still comes on release, as before
            if self.lock.locked(now):
                if kind in ("rot", "jog"):
                    continue  # pocket-locked: turns do nothing, and don't even wake the screen
                if kind == "press" and arg == "enc_sw":
                    self.lock.unlock(now)
                    self.last_input = now
                    had_input = True
                    if self.mode == "sleep":
                        self.mode = "active"
                    continue  # this press only unlocks
            else:
                self.lock.touch(now)
            sleeping = self.mode == "sleep"
            self.last_input = now
            had_input = True
            if sleeping:
                self.mode = "active"  # first input only wakes the screen
                continue
            if self.paused_idle and kind in ("rot", "jog") and self.stack[-1] is self.home:
                self.paused_idle = False  # a turn on the branch scene just brings Now Playing back
                continue                  # (rather than also seeking the paused song)
            self.dispatch(kind, arg, now)
        if self.shutting_down:
            return

        tick = getattr(self.stack[-1], "tick", None)
        if tick:
            res = tick()
            if res is not None:
                self.apply_result(res, replace=True)
                had_input = True

        self.snap = self.player.snapshot()
        s = self.snap
        if s.has_track and not s.playing:
            self.paused_since = self.paused_since or now
        else:
            self.paused_since = None
        self.paused_idle = bool(self.bird and self.paused_since is not None
                                and now - max(self.paused_since, self.last_input) >= PAUSED_IDLE_SECS)
        if self.bird:
            try:
                self.bird.observe(now, s.playing, s.has_track, s.uri or s.title,
                                  idle=not s.has_track or self.paused_idle)
            except Exception:  # noqa: BLE001
                self.bird_failed("observe")
        mode = self.update_mode(now)
        mode_changed = mode != self.mode
        self.mode = mode
        self.player.interval = POLL_EVERY[mode]
        self.housekeeping(now)
        if self.shutting_down:
            return

        if mode == "sleep":
            self.animating = False
            self.bird_showing = False  # the screen sleeps: so does the bird's animation
            self.analyser.enabled = False
            img = sleep_screen(self.snap, self.battery, self.bird)
            key = ("sleep", id(img))
            if key != self.last_key:
                self.display.show(img)
                self.last_key = key
            else:
                self.display.vcom_only()
            return

        screen = self.top()
        if self.haptics:
            self.haptics.set_profile(id(screen), screen.haptic(), screen.haptic_extras())
            self.sync_dial(now, screen)
        self.animating = screen.animates(self.snap, mode)
        self.analyser.enabled = self.animating and screen in (self.home.local, self.scope)
        self.analyser.need_spec = screen is self.scope

        if self.animating and screen is self.home.local:
            screen.advance(self.snap)
            fast_ok = (not had_input and not mode_changed and screen.frame_img is not None
                       and now - self.last_full < 1.0
                       and screen.static_key == screen.key_for(self.snap)
                       and self.last_key and self.last_key[0] == id(screen))
            if fast_ok:
                self.display.show_columns(screen.frame_img, screen.fast_columns())
                return

        key = (id(screen), mode, screen.render_key(self.snap, now))
        bird_layer = getattr(screen, "bird_layer", None) if self.bird else None
        self.bird_showing = bird_layer is not None
        if bird_layer and not self.animating and key == self.last_key and key == self.bird_key:
            tick = int(now // BIRD_TICK)
            if tick != self.bird_tick:   # only the bird moved: redraw it over the cached frame
                frame = self.bird_base.copy()
                boxes = self.bird_draw(bird_layer, frame, now)
                if boxes is None:        # the bird failed: next step redraws the screen without it
                    return
                cols = bird_columns(self.bird_boxes + boxes)
                self.display.show_changed(frame, cols)
                self.bird_boxes, self.bird_tick = boxes, tick
            else:
                self.display.vcom_only()
            return
        if self.animating or key != self.last_key:
            frame = screen.render(now, self.snap, mode)
            self.bird_key = None
            if bird_layer:   # keep the screen's own frame: later ticks redraw just the bird over it
                base = frame
                self.bird_base, self.bird_key = base, key
                frame = base.copy()
                boxes = self.bird_draw(bird_layer, frame, now)
                if boxes is None:
                    frame = base         # show the screen as it is; the next step redraws it without the bird
                else:
                    self.bird_boxes = boxes
                    self.bird_tick = int(now // BIRD_TICK)
            self.display.show(frame)
            if not self.first_shown:  # for the boot-time report (currawong_setup.sh --boot)
                self.first_shown = True
                print(f"Boot timing: first screen {uptime():.1f} s after the kernel started", flush=True)
                if self.background_after_start:
                    sd_notify("READY=1")  # MPD and go-librespot start after this (set by the setup)
                    lower_priority()
                    threading.Thread(target=warm_up, args=(self.settings.get("rotate", ROTATE), self),
                                     name="warm-up", daemon=True).start()
            self.last_key = key
            self.last_full = now
        else:
            self.display.vcom_only()

    def frame_period(self):
        period = self._frame_period()
        if self.bird_showing:  # wake on the bird's next 125 ms tick
            period = min(period, BIRD_TICK - (self.step_at % BIRD_TICK) + 0.002)
        return period

    def _frame_period(self):
        if isinstance(self.stack[-1], TaskScreen):
            return 0.5
        if self.scrub.active():
            return 0.25
        if self.animating and isinstance(self.stack[-1], ListScreen):
            return 1.0 / MARQUEE_FPS
        if not self.animating:
            return 1.0
        return 1.0 / (FPS_SCOPE if self.stack[-1] is self.scope else FPS_ACTIVE)

    def run(self):
        while True:
            now = self.step_at = time.monotonic()
            self.step(now)
            self.wake.wait(max(0.0, self.frame_period() - (time.monotonic() - now)))


def start_inputs(post):
    """Presses fire on release (so a hold doesn't also count as a press)."""
    from gpiozero import Button, RotaryEncoder

    enc = RotaryEncoder(PINS["enc_a"], PINS["enc_b"], max_steps=0)
    cw, ccw = (-1, 1) if ENC_REVERSE else (1, -1)
    enc.when_rotated_clockwise = lambda: post("rot", cw)
    enc.when_rotated_counter_clockwise = lambda: post("rot", ccw)
    buttons = []
    for name in ("enc_sw", "back", "play", "next", "prev"):
        b = Button(PINS[name], pull_up=True, bounce_time=0.03, hold_time=0.8)
        held = {"v": False}

        def on_press(n=name, h=held):
            h["v"] = False
            post("down", n)  # only for the button click on the dial; the press comes on release

        def on_held(n=name, h=held):
            h["v"] = True
            post("hold", n)

        def on_release(n=name, h=held):
            if not h["v"]:
                post("press", n)

        b.when_pressed, b.when_held, b.when_released = on_press, on_held, on_release
        buttons.append(b)
    return enc, buttons  # keep references so gpiozero doesn't close them


# ======================================================================
# Preview mode: render example screens to PNG without any hardware
# ======================================================================
class _FakePlayer:
    def __init__(self, snap):
        self._snap = snap
        self.interval = 1.0

    def snapshot(self):
        return self._snap

    def lib_artists(self):
        return ["Alexander Panos", "Ciauru", "deadmau5", "xmunashe"]

    def lib_albums(self, artist=None):
        return ["Make Yourself At Home", "Instinctual Audio Manipulation"]

    def lib_tracks(self, artist=None, album=None):
        return [{"file": f"x/{i}.flac", "Title": t, "duration": str(d)}
                for i, (t, d) in enumerate([("B Side - Live in Melbourne", 312.4),
                                            ("Smelting A Cloud", 214.0), ("Night Bus", 187.9)])]

    def lib_playlists(self):
        return ["Late drives", "Running", "Sunday"]

    def queue(self):
        return self.lib_tracks(), 1

    def __getattr__(self, name):
        return lambda *a, **k: None


class _FakeWifi(WifiManager):
    radio = True

    def connected(self):
        return self.radio

    def radio_on(self):
        return self.radio

    def set_radio(self, on):
        self.radio = on
        return True

    def current(self, now=None):
        return "Newtown-5G", "72"

    def scan(self):
        return [{"ssid": "Newtown-5G", "signal": 72, "secure": True, "active": True},
                {"ssid": "Telstra1234", "signal": 51, "secure": True, "active": False},
                {"ssid": "Cafe Free WiFi", "signal": 33, "secure": False, "active": False}]


class _FakeBT(BluetoothManager):
    def scan(self, seconds=12):
        return [("AA:BB:CC:DD:EE:01", "WH-1000XM5", False), ("AA:BB:CC:DD:EE:02", "Kitchen speaker", False)]

    def paired(self):
        return {"AA:BB:CC:DD:EE:01": "WH-1000XM5"}

    def pair(self, mac, find_secs=15):
        return True, True, ""


class _FakeBattery:
    source, pct, low, low_since, charging, rate = "gauge", 70, False, None, True, 9.0
    volts, capacity_mah, cutoff_volts = 3.9, BATTERY_MAH, BATT_CUTOFF_VOLTS

    def configure(self, capacity_mah=None, cutoff_volts=None):
        self.capacity_mah = capacity_mah or BATTERY_MAH

    def shutdown_due(self, now):
        return None

    def drain_text(self):
        return Battery.drain_text(self)

    def update(self, now):
        pass

    def state(self):
        return (self.source, self.pct, self.low, self.charging)

    def text(self):
        return f"{self.pct}%"


class _CaptureDisplay:
    def __init__(self):
        self.last = None
        self.vcom_count = 0

    def show(self, img):
        self.last = img

    def show_columns(self, img, ranges):
        self.last = img

    def show_changed(self, img, ranges):
        self.last = img
        self.changed_ranges = ranges

    def vcom_only(self):
        self.vcom_count += 1

    def set_rotation(self, deg):
        pass


def _fake_cover():
    im = Image.new("L", (300, 300))
    d = ImageDraw.Draw(im)
    for y in range(300):
        d.line((0, y, 299, y), fill=int(40 + 180 * y / 300))
    d.ellipse((70, 50, 230, 210), fill=235)
    d.ellipse((115, 95, 185, 165), fill=30)
    d.rectangle((0, 230, 299, 299), fill=60)
    buf = BytesIO()
    im.save(buf, "PNG")
    return make_covers(buf.getvalue())


def preview(outdir="preview"):
    os.makedirs(outdir, exist_ok=True)
    s = Snap()
    s.reachable = s.has_track = s.playing = True
    s.source = "spotify"
    s.uri = "spotify:track:demo"
    s.title = "Smelting A Cloud"
    s.artists = "Alexander Panos"
    s.album = "Demo Album"
    s.pos, s.dur = 83_000, 214_000
    s.fmt = SPOTIFY_FORMAT
    s.cover = _fake_cover()

    def save(img, name):
        img.resize((W * 2, H * 2), Image.NEAREST).save(os.path.join(outdir, name))

    def wait_task(app, t):
        for _ in range(50):
            if isinstance(app.stack[-1], TaskScreen) and not app.stack[-1].finished:
                time.sleep(0.02)
        app.step(t)

    analyser = Analyser(threaded=False)
    synth = SynthSource()
    disp = _CaptureDisplay()
    app = App(_FakePlayer(s), disp, _FakeBattery(), analyser, wifi=_FakeWifi(), bt=_FakeBT())
    t = time.monotonic()
    app.step(t)
    save(disp.last, "01_spotify.png")
    before = disp.vcom_count
    app.step(t + 0.2)
    assert disp.vcom_count == before + 1, "expected a VCOM-only update"

    s.source, s.uri, s.fmt = "local", "x/B Side.flac", "FLAC 48k/24"
    s.title, s.artists, s.album = "B Side - Live in Melbourne", "xmunashe", "Make Yourself At Home"
    for i in range(140):
        analyser.process(synth.read_block(HOP))
        app.step(t + 1 + i / FPS_ACTIVE)
    app.last_full = -999
    app.step(t + 30)
    save(disp.last, "02_local.png")

    def go(label):
        """Turn to the list item with this label and press the centre button."""
        top = app.top()
        labels = [it[0] for it in top.items]
        app.dispatch("rot", labels.index(label) - top.sel)
        app.dispatch("press", "enc_sw")

    def menu(*path):
        app.dispatch("hold", "back")
        app.dispatch("press", "enc_sw")
        for label in path:
            go(label)

    app.dispatch("press", "enc_sw")              # main menu
    app.step(t + 31)
    save(disp.last, "03_menu.png")
    menu("WI-FI", "JOIN A NETWORK")
    wait_task(app, t + 32)
    save(disp.last, "04_wifi_networks.png")
    app.dispatch("press", "enc_sw")              # pick the first network -> password
    for _ in range(4):
        app.dispatch("rot", 1)
        app.dispatch("press", "enc_sw")
    app.dispatch("press", "next")
    app.dispatch("rot", 2)
    app.dispatch("press", "enc_sw")
    app.step(t + 33)
    save(disp.last, "05_password.png")

    menu("BLUETOOTH", "PAIR NEW DEVICE")
    wait_task(app, t + 34)
    save(disp.last, "06_bt_found.png")
    app.dispatch("press", "enc_sw")              # pair the first one
    wait_task(app, t + 35)
    save(disp.last, "07_bt_paired.png")

    menu("LIBRARY", "PLAYLISTS")
    app.step(t + 36)
    save(disp.last, "08_playlists.png")

    menu("TRACK OPTIONS")
    app.step(t + 37)
    save(disp.last, "09_track.png")

    menu("SETTINGS")
    app.step(t + 38)
    save(disp.last, "10_settings.png")

    menu("ABOUT")
    app.step(t + 39)
    save(disp.last, "11_about.png")

    menu("SETTINGS", "SPIN ON SKIP")
    app.step(t + 39.1)
    save(disp.last, "14_setting_binary.png")
    menu("SETTINGS", "POCKET LOCK")
    app.dispatch("rot", 2)
    app.step(t + 39.2)
    save(disp.last, "15_setting_list.png")
    app.dispatch("press", "back")
    long_list = ListScreen(app, "NETWORKS", [("Cafe Free WiFi - Guest Network, Level 2", lambda: "72%", lambda: None),
                                             ("Newtown-5G", lambda: "51%", lambda: None)])
    app.stack.append(long_list)
    long_list.sel_at = t + 39.3 - 2.5
    app.step(t + 39.3)
    save(disp.last, "16_scrolling_name.png")
    app.stack.pop()
    save(powered_off_screen(), "17_powered_off.png")
    app.eq.available = True
    eqs = EqualiserScreen(app)
    eqs.gains, eqs.sel, eqs.editing = [2, 1, 0, 3, 5], 4, True
    save(eqs.render(t + 40, s, "active"), "18_equaliser.png")
    save(AddMusicScreen(app).render(t + 40, s, "active"), "19_add_music.png")
    save(splash_screen(), "20_splash.png")

    # pocket lock + auto-scrub, on the simple screen (FAST visuals) and the local screen
    app.dispatch("hold", "back")
    app.settings["pocket_secs"] = 30
    app.lock.until = 0
    app.scrub.start(1, t + 40, s)
    app.scrub.faster()
    app.step(t + 40)
    save(disp.last, "12_local_locked_scrub.png")
    app.settings["visuals"] = 2
    app.last_key = None
    app.step(t + 40.1)
    save(disp.last, "13_fast_visuals_scrub.png")
    app.scrub.stop()
    app.settings["visuals"], app.settings["pocket_secs"] = 1, 0
    print(f"Wrote previews to {os.path.abspath(outdir)}")


# ======================================================================
# Profiling: python3 currawong_ui.py --profile 60
# ======================================================================
def thread_cpu():
    """CPU seconds used so far by each thread of this process, keyed by Python thread name."""
    names = {t.native_id: t.name for t in threading.enumerate()}
    tick = os.sysconf("SC_CLK_TCK")
    out = {}
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            out[names.get(int(tid), f"other ({tid})")] = (int(fields[11]) + int(fields[12])) / tick
        except (OSError, IndexError, ValueError):
            continue
    return out


def run_profiled(app, seconds, path):
    """Runs the UI as normal for `seconds`, then writes where the CPU went. Use it the way you
    normally would meanwhile (play local music, open menus): that's what gets measured."""
    import cProfile
    import pstats
    prof = cProfile.Profile()
    t0, p0, c0 = time.monotonic(), time.process_time(), thread_cpu()
    prof.enable()
    try:
        while time.monotonic() - t0 < seconds:
            now = time.monotonic()
            app.step(now)
            app.wake.wait(max(0.0, app.frame_period() - (time.monotonic() - now)))
    finally:
        prof.disable()
    wall, p1, c1 = time.monotonic() - t0, time.process_time(), thread_cpu()
    with open(path, "w") as f:
        f.write(f"Currawong UI profile: {wall:.0f} s, the whole UI used {100 * (p1 - p0) / wall:.0f}% "
                f"of one core (profiling itself adds some to the main thread)\n")
        f.write(f"Visuals: {VISUALS[app.settings.get('visuals', 1)]}\n\nCPU by thread, % of one core:\n")
        for name in sorted(c1, key=lambda n: -(c1[n] - c0.get(n, 0))):
            f.write(f"  {name:20s} {100 * (c1[name] - c0.get(name, 0)) / wall:5.1f}%\n")
        stats = pstats.Stats(prof, stream=f)
        f.write("\nMain thread, functions by their own time:\n")
        stats.sort_stats("tottime").print_stats(25)
        f.write("\nMain thread, by cumulative time:\n")
        stats.sort_stats("cumulative").print_stats(25)
    print(f"Profile written to {path}")


# ======================================================================
def uptime():
    """Seconds since the kernel started (the firmware's few seconds before that aren't counted)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return -1.0


def sd_notify(message):
    """Tell systemd something (e.g. READY=1) when it started us as Type=notify; harmless otherwise."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass


class _NoSPI:
    def writebytes2(self, buf):
        pass


class _NoPin:
    def on(self):
        pass

    def off(self):
        pass


def save_splash(display, img, rot):
    """Keep the splash's exact panel bytes for the next start's early display."""
    try:
        frame = display.frame_bytes(img)
        path = splash_file(rot)
        old = open(path, "rb").read() if os.path.exists(path) else b""
        if frame != old:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(frame)
            os.replace(path + ".tmp", path)
    except OSError:
        pass


def build_boot_anim(rot, src=None, dest=None):
    """The boot animation as panel-ready packets for this rotation: for each frame, only the
    panel lines that changed, so the early player sends them at full speed without PIL. Rebuilt
    only when the animation file is newer. Returns the cache's path, or None."""
    src = src or ANIM_FILE
    dest = dest or boot_anim_file(rot)
    try:
        if os.path.getmtime(dest) >= os.path.getmtime(src):
            return dest
    except OSError:
        pass
    try:
        meta, _, raw = read_anim_asset(src)
    except (OSError, ValueError):
        return None
    disp = SharpDisplay(spi=_NoSPI(), cs=_NoPin())
    disp.set_rotation(rot)
    st, bpf, prev = disp.stride, meta["bpf"], None
    out = bytearray(b"CRWBOOT1" + struct.pack("<HI", meta["fps"], meta["frames"]))
    for i in range(meta["frames"]):
        data = anim_image(raw[i * bpf:(i + 1) * bpf], meta).transpose(disp.transpose).tobytes()
        if prev is None:
            lines = range(PANEL_H)
        else:
            lines = [y for y in range(PANEL_H) if data[y * st:(y + 1) * st] != prev[y * st:(y + 1) * st]]
        pkt = bytearray()
        if lines:
            pkt.append(0x80)
            for y in lines:
                pkt.append(disp.addr[y])
                pkt += data[y * st:(y + 1) * st]
                pkt.append(0)
            pkt.append(0)
        out += struct.pack("<I", len(pkt)) + pkt
        prev = data
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".tmp", "wb") as f:
            f.write(out)
        os.replace(dest + ".tmp", dest)
    except OSError:
        return None
    return dest


def warm_up(rot=None, app=None):
    """In the background after the first screen: load the currawong (its sprites and tables), then
    numpy and the visualiser tables, so none of them stall the UI. Then get the boot animation
    ready for the next boot."""
    if app is not None and app.bird is None:
        app.load_bird()
    for name in _Tables.BUILD:
        getattr(TABLES, name)
    if rot is not None:
        build_boot_anim(rot)


def lower_priority(niceness=10):
    """The service starts the UI at normal priority so its logo isn't starved while the system
    boots. Once the first screen is up, every thread drops to `niceness`, so audio always wins.
    (On Linux each thread has its own nice value, so they're set one by one.)"""
    try:
        tids = [int(t) for t in os.listdir("/proc/self/task")]
    except OSError:
        tids = [0]
    for tid in tids:
        try:
            if os.getpriority(os.PRIO_PROCESS, tid) < niceness:
                os.setpriority(os.PRIO_PROCESS, tid, niceness)
        except OSError:
            pass


# ======================================================================
# Updating the dial's firmware over the normal cable (firmware v0.5 and later)
# ======================================================================
DIAL_OTA_VERSION = 5   # the first firmware that can take updates this way
DIAL_CHUNK = 240       # bytes per line: 320 in base64, well inside the dial's 400-character line


class DialUpdateError(Exception):
    pass


def dial_update(path, ser=None, log=print, chunk=DIAL_CHUNK, ack_timeout=3.0, retries=5):
    """Send firmware.bin to the dial over /dev/serial0 (the UI must be stopped: it owns the port).
    The dial writes it to its spare app slot, checks the MD5 and only then switches over, so a
    failure on the way leaves it running what it had. Returns the version it restarts with."""
    import base64
    import hashlib
    data = open(path, "rb").read()
    if not data or data[0] != 0xE9:
        raise DialUpdateError(f"{path} isn't an ESP32 app image (use the build's firmware.bin)")
    md5 = hashlib.md5(data).hexdigest()
    own = ser is None
    if own:
        import serial
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.05)
    pending = bytearray()

    def send(body):
        ser.write(f"${body}*{HapticLink.checksum(body)}\n".encode())

    def wait_for(want, timeout):
        """The next line from the dial for which want(body) is true, or None after timeout."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pending.extend(ser.read(512) or b"")
            while b"\n" in pending:
                raw, _, rest = bytes(pending).partition(b"\n")
                pending[:] = rest
                line = raw.decode("ascii", "replace").strip()
                s0, star = line.rfind("$"), line.rfind("*")
                if s0 < 0 or star < s0:
                    continue  # the boot log, or noise
                body = line[s0 + 1:star]
                if line[star + 1:star + 3].upper() != HapticLink.checksum(body):
                    continue
                if want(body):
                    return body
        return None

    def hello(timeout):
        for _ in range(int(timeout / 1.5) + 1):
            send("PING")
            got = wait_for(lambda b: b.startswith("HELLO,"), 1.5)
            if got:
                parts = got.split(",")
                return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return None

    try:
        version = hello(5)
        if version is None:
            raise DialUpdateError("the dial didn't answer (is the UI still running? it holds the port)")
        if version < DIAL_OTA_VERSION:
            raise DialUpdateError(f"the dial has firmware v0.{version}, which can't take updates over the cable: "
                                  f"flash v0.{DIAL_OTA_VERSION} or later over USB once, and from then on it can")
        log(f"Dial on v0.{version}. Sending {len(data) // 1024} KB...")
        send(f"OTA,BEGIN,{len(data)},{md5}")
        got = wait_for(lambda b: b.startswith("OTA,"), 15)
        if got != "OTA,READY":
            raise DialUpdateError(f"the dial wouldn't start the update ({got or 'no answer'})")
        total = (len(data) + chunk - 1) // chunk
        seq, shown = 0, -1
        while seq < total:
            body = f"OTA,DATA,{seq},{base64.b64encode(data[seq * chunk:(seq + 1) * chunk]).decode()}"
            for _ in range(retries):
                send(body)
                got = wait_for(lambda b: b.startswith(("OTA,ACK,", "OTA,NAK,", "OTA,ERR")), ack_timeout)
                if got == f"OTA,ACK,{seq}":
                    seq += 1
                    break
                if got and got.startswith("OTA,ERR"):
                    raise DialUpdateError(f"the dial stopped the update: {got[8:]}")
                if got and got.startswith("OTA,NAK,"):
                    seq = min(seq, int(got.split(",")[2]))  # it wants an earlier chunk: go from there
                    break
            else:
                send("OTA,ABORT")
                raise DialUpdateError(f"no answer for chunk {seq} of {total}: stopped (the dial keeps its old firmware)")
            pct = 100 * seq // total
            if pct // 10 != shown:
                shown = pct // 10
                log(f"  {pct}%")
        send("OTA,END")
        got = wait_for(lambda b: b.startswith(("OTA,DONE", "OTA,ERR")), 30)
        if got != "OTA,DONE":
            raise DialUpdateError(f"the dial didn't accept the new firmware ({got or 'no answer'}); it keeps its old one")
        log("Checked and installed. Restarting the dial...")
        new = wait_for(lambda b: b.startswith("HELLO,"), 20) or ""
        parts = new.split(",")
        return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else hello(10)
    finally:
        if own:
            ser.close()


def main():
    if "--version" in sys.argv:
        print(f"Currawong UI {UI_BUILD}")
        return
    if "--preview" in sys.argv:
        preview()
        return
    if "--update-dial" in sys.argv:
        i = sys.argv.index("--update-dial")
        path = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.expanduser("~/currawong/currawong_knob.bin")
        try:
            v = dial_update(path)
            print(f"Done: the dial is now on v0.{v}." if v else "Done, but the dial hasn't said hello yet.")
        except (DialUpdateError, OSError) as e:
            sys.exit(f"Dial update: {e}")
        return
    profile_secs = 0
    if "--profile" in sys.argv:
        i = sys.argv.index("--profile")
        try:
            profile_secs = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            profile_secs = 60
    started = uptime()
    print(f"Currawong UI {UI_BUILD}", flush=True)
    rot = load_settings().get("rotate", ROTATE)  # before the first frame, so it's the right way up
    splash = splash_screen()
    boot = _BOOT
    if boot is not None:
        # The animation is playing: let it finish (it's ~10 s), then take the display over with
        # its last frame still showing.
        boot.played.wait(timeout=20)
        boot.release.set()
        boot.join(timeout=2)
        display = SharpDisplay(spi=boot.spi, cs=_LgpioPin.adopt(boot.lg, boot.h, CS_PIN))
        display.set_rotation(rot)
        display.last = bytearray(splash.transpose(display.transpose).tobytes())
    else:
        display = SharpDisplay()
        display.set_rotation(rot)
        display.show(splash)
    save_splash(display, splash, rot)
    print(f"Boot timing: splash up {uptime():.1f} s after the kernel started (UI began at {started:.1f} s)", flush=True)
    vis = VisualiserOutput(switching=bool(load_settings().get("vis_output_switch", True)))
    app = App(Player(), display, Battery(), Analyser(vis=vis))
    app.background_after_start = True
    app.resume = Resume(app.player, app.settings)
    app.start_wifi_check()
    inputs = start_inputs(app.post_event)  # noqa: F841 (must stay referenced)
    app.haptics = HapticLink(app.post_event)
    if app.haptics.ser is None:
        app.haptics = None
    else:
        app.haptics.set_feel(app.settings.get("feel", 100))
    if boot is None:
        time.sleep(1.5)  # let the splash be seen (the animation has already had its moment)

    def on_term(signum, frame):
        raise SystemExit

    signal.signal(signal.SIGTERM, on_term)
    try:
        if profile_secs:
            run_profiled(app, profile_secs, os.path.expanduser("~/currawong/profile.txt"))
            display.show(logo_screen("PROFILE SAVED", "restart the UI service"))
            return
        app.run()
    except KeyboardInterrupt:
        display.clear()
    except SystemExit:
        if not app.shutting_down and system_stopping():
            display.show(powered_off_screen())
    finally:
        app.analyser.stop()   # ends arecord cleanly (systemd would kill it anyway)


if __name__ == "__main__":
    main()
