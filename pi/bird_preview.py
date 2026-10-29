#!/usr/bin/env python3
"""Preview the currawong on a desktop, without the device.

    python bird_preview.py --scene idle|now|menu|paused|sleep --playing 0|1

    paused: the branch scene Home shows after a minute paused (the track stays loaded)
    sleep:  the still picture on the sleep screen (one PNG; nothing moves while the screen sleeps)

Runs the UI's own render loop (currawong_ui.App.step) with a fake player and a fake clock, one
step per 125 ms tick, so what you see is what the panel gets, including the bird-only partial
updates. Writes to preview_bird/<scene>_<playing>/:
    frame_0000.png ...   every tick, enlarged (--zoom, default 2), pure black and white
    sheet.png            a contact sheet of every --sheet-every'th frame
    anim.gif             the whole run at 8 fps

Needs Pillow, and currawong_ui.py, currawong_bird.py and assets/ in this folder.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import currawong_ui as ui  # noqa: E402

DEFAULT_TICKS = {"playing": 114, "paused": 84, "idle": 907}   # a loop (two of the short sleep one)


def find_fonts():
    """The UI uses DejaVu Sans Mono from /usr/share/fonts (as on the Pi). On a desktop without
    it, try other usual places (matplotlib ships it too); otherwise Pillow's default is used."""
    names = ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")
    dirs = [ui.FONT_DIR, "/usr/share/fonts/TTF/", "/Library/Fonts/", os.path.expanduser("~/Library/Fonts/"),
            "C:/Windows/Fonts/"]
    try:
        import matplotlib
        dirs.append(os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf") + "/")
    except ImportError:
        pass
    for d in dirs:
        if all(os.path.exists(os.path.join(d, n)) for n in names):
            if d != ui.FONT_DIR:
                reg, bold = (os.path.join(d, n) for n in names)
                for name, path, size in (("F_TINY", reg, 10), ("F_SMALL", reg, 11), ("F_BODY", reg, 13),
                                         ("F_BOLD", bold, 13), ("F_SBOLD", bold, 11), ("F_BIG", bold, 16),
                                         ("F_HUGE", bold, 20)):
                    setattr(ui, name, ImageFont.truetype(path, size))
            return d
    print("DejaVu Sans Mono not found: text uses Pillow's default font (the bird is unaffected).")
    return None


def make_snap(scene, playing):
    s = ui.Snap()
    s.reachable = True
    if scene in ("now", "menu", "paused", "sleep") or playing:
        s.has_track = True
        s.playing, s.paused = playing, not playing
        s.source, s.uri, s.fmt = "spotify", "spotify:track:cloudbusting", ui.SPOTIFY_FORMAT
        s.title, s.artists, s.album = "Cloudbusting", "Kate Bush", "Hounds of Love"
        s.pos, s.dur = 102_000, 254_000
        s.cover = None if scene == "menu" else ui._fake_cover()
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scene", choices=("idle", "now", "menu", "paused", "sleep"), default="now")
    ap.add_argument("--playing", type=int, choices=(0, 1), default=1)
    ap.add_argument("--ticks", type=int, help="how many 125 ms ticks (default: one full loop)")
    ap.add_argument("--start", type=int, default=0, help="start this many ticks into the loop")
    ap.add_argument("--new-song-at", type=int, help="a new song starts at this tick (the hop)")
    ap.add_argument("--zoom", type=int, default=2)
    ap.add_argument("--sheet-every", type=int, help="contact sheet: every Nth frame (default: about 24 frames)")
    ap.add_argument("--out", default=os.path.join(HERE, "preview_bird"))
    a = ap.parse_args()

    find_fonts()
    if a.scene in ("paused", "sleep"):
        a.playing = 0
    snap = make_snap(a.scene, bool(a.playing))
    disp = ui._CaptureDisplay()
    app = ui.App(ui._FakePlayer(snap), disp, ui._FakeBattery(), ui.Analyser(threaded=False),
                 wifi=ui._FakeWifi(), bt=ui._FakeBT())
    app.settings["sleep_secs"] = 10 ** 6       # keep the screen awake for the whole preview
    app.settings["active_secs"] = 0
    app.settings["auto_off_mins"] = 0
    app.load_bird()
    if not app.bird:
        sys.exit("The bird didn't load: currawong_bird.py and assets/ need to be next to this script.")
    if a.scene == "menu":
        app.stack.append(ui.main_menu(app))
        ui.STATUS["wifi"] = True
        app.wifi._cur = (1e18, "Home", "80")   # the menu's Wi-Fi value, without nmcli

    out = os.path.join(a.out, f"{a.scene}_{a.playing}")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.endswith((".png", ".gif")):
            os.remove(os.path.join(out, f))
    if a.scene == "sleep":
        img = ui.sleep_screen(snap, ui._FakeBattery(), app.bird)
        img.resize((ui.W * a.zoom, ui.H * a.zoom), Image.NEAREST).save(os.path.join(out, "sleep.png"))
        print(f"the sleep screen -> {os.path.join(out, 'sleep.png')}")
        return

    ticks = a.ticks or DEFAULT_TICKS["idle" if a.scene in ("idle", "paused") else "playing" if a.playing else "paused"]
    t0 = 1000.0                                # the fake clock's start, on a tick boundary
    if a.scene == "paused":                    # as if paused (and untouched) for a couple of minutes
        app.last_input = app.last_playing = app.track_changed = app.paused_since = t0 - 120
    app.step(t0 - ui.BIRD_TICK * a.start)      # the bird's loop starts here, so frame 0 is entry --start
    frames, partial = [], 0
    for k in range(ticks):
        now = t0 + ui.BIRD_TICK * k
        if a.new_song_at is not None and k == a.new_song_at:
            snap.uri, snap.title, snap.pos = "spotify:track:next", "Running Up That Hill", 0
        if snap.playing:
            snap.pos = min(snap.dur, snap.pos + 125)
        before = getattr(disp, "changed_ranges", None)
        app.step(now)
        if getattr(disp, "changed_ranges", None) is not before:
            partial += 1
        img = disp.last.convert("1")
        frames.append(img)
        img.resize((ui.W * a.zoom, ui.H * a.zoom), Image.NEAREST).save(os.path.join(out, f"frame_{k:04d}.png"))

    every = a.sheet_every or max(1, len(frames) // 24)
    pick = frames[::every][:48]
    cols = min(8, len(pick))
    rows = (len(pick) + cols - 1) // cols
    sheet = Image.new("1", (cols * (ui.W + 8) + 8, rows * (ui.H + 22) + 8), 1)
    d = ImageDraw.Draw(sheet)
    for i, f in enumerate(pick):
        x, y = 8 + (i % cols) * (ui.W + 8), 8 + (i // cols) * (ui.H + 22)
        sheet.paste(f, (x, y + 14))
        d.rectangle((x - 1, y + 13, x + ui.W, y + 14 + ui.H), outline=0)
        d.text((x, y), f"tick {i * every} ({i * every * ui.BIRD_TICK:.3f} s)", fill=0)
    sheet.save(os.path.join(out, "sheet.png"))
    big = [f.resize((ui.W * a.zoom, ui.H * a.zoom), Image.NEAREST).convert("L") for f in frames]
    big[0].save(os.path.join(out, "anim.gif"), save_all=True, append_images=big[1:],
                duration=int(ui.BIRD_TICK * 1000), loop=0)
    print(f"{len(frames)} frames ({len(frames) * ui.BIRD_TICK:.1f} s) -> {out}")
    print(f"  {partial} of them were bird-only partial updates; open anim.gif or sheet.png")


if __name__ == "__main__":
    main()
