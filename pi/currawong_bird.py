"""The currawong: the UI's character layer.

A 32x32 pixel bird (from the boot animation) that reacts to playback: it sings while music plays,
hops when a new song starts, sleeps when paused, and when nothing is playing sits on a branch,
mostly asleep, now and then flying a lap or storing a gem in its bowl.

Everything comes from assets/ next to this file:
    currawong_sheet.png   256x288, 8 x 9 frames of 32x32, ink on transparent (index = row*8 + col)
    branch_sway.png       224x320: the branch drawn 8 times (224x40 each), twigs swaying
    animation_spec.json   frames, sequences, particle rules and the idle layout (source of truth)

One tick is 125 ms (8 fps) and every sequence advances one entry per tick. The tick comes from the
clock (int(now / TICK)), so a late frame never makes the bird drift. Drawing functions draw onto a
copy of the screen's own frame and return the boxes they touched, so the caller can refresh only
those columns.

Pure 1-bit: sprites are masks pasted in black (or white when inverted); nothing is anti-aliased.
"""
import json
import math
import os

from PIL import Image

TICK = 0.125
HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIRS = (os.path.join(HERE, "assets"), os.path.expanduser("~/currawong/assets"))

BLACK, WHITE = 0, 1
W, H = 240, 400

# Now Playing: the bird stands on the twig at the playhead (sprite x = playhead - 16)
NP_TWIG_X0, NP_TWIG_LEN = 8, 224    # the twig runs 224 px from x 8 (x 8-231)
NP_TWIG_Y = 340                     # elapsed part: rows 340-342; the dashed line: row 341
NP_BIRD_Y = 316                     # sprite top: the feet (cell row 23) land on the twig
# Menu: inverted, standing on the bottom edge of the 28 px title bar
MENU_BIRD_X, MENU_BIRD_Y = 196, 4
MENU_DY = 0.4                       # jumps scaled down so they stay in the bar

Z_SWAY = (0, 1, 2, 2, 1, 0, -1, -2, -2, -1)
NOTE_WOBBLE = (0, 1, 1, 0, -1, -1)
NOTE_LIFE = 23                      # ticks

_FLIP = getattr(Image, "Transpose", Image).FLIP_LEFT_RIGHT
_NEAREST = getattr(Image, "Resampling", Image).NEAREST


def jsround(v):
    """JavaScript's Math.round (halves go up), so positions match the reference exactly."""
    return math.floor(v + 0.5)


def find_assets(path=None):
    for d in ([path] if path else []) + list(ASSET_DIRS):
        if d and os.path.exists(os.path.join(d, "currawong_sheet.png")):
            return d
    raise FileNotFoundError("currawong_sheet.png not found in " + ", ".join(ASSET_DIRS))


# ======================================================================
# Sprites
# ======================================================================
class Sheet:
    """The sprite sheet as 1-bit masks. Every variant (flipped, scaled, clipped) is built once
    and kept; drawing is then a single paste of a mask in black or white."""

    def __init__(self, path, cell=32, cols=8):
        im = Image.open(path).convert("RGBA")
        self.ink = im.split()[3].point(lambda a: 255 if a >= 128 else 0).convert("1")
        self.cell, self.cols = cell, cols
        self.count = (im.width // cell) * (im.height // cell)
        self.cache = {}

    def mask(self, frame, scale=1, flip=False, clip=None):
        """frame's ink as a mode-1 mask: scaled (nearest neighbour), mirrored, and/or cut to
        clip = (x, y, w, h) in the frame's own 32x32 coordinates."""
        key = (frame, scale, flip, clip)
        m = self.cache.get(key)
        if m is None:
            c = self.cell
            x0, y0 = (frame % self.cols) * c, (frame // self.cols) * c
            m = self.ink.crop((x0, y0, x0 + c, y0 + c))
            if flip:
                m = m.transpose(_FLIP)
            if clip:
                cx, cy, cw, ch = clip
                m = m.crop((cx, cy, cx + cw, cy + ch))
            if scale != 1:
                m = m.resize((m.width * scale, m.height * scale), _NEAREST)
            self.cache[key] = m
        return m

    def blit(self, img, frame, x, y, scale=1, flip=False, invert=False, clip=None):
        """Draw frame with its top-left at (x, y): ink in black, or in white when inverted
        (transparent pixels, the eye included, are left alone). Returns the box it covers on
        img, (x0, y0, x1, y1) with x1/y1 exclusive, or None if it's entirely off-screen."""
        m = self.mask(frame, scale, flip, clip)
        x, y = int(x), int(y)
        box = (max(0, x), max(0, y), min(img.width, x + m.width), min(img.height, y + m.height))
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        img.paste(WHITE if invert else BLACK, (x, y), m)
        return box

    def warm(self, specs):
        for s in specs:
            self.mask(*s)


class Branch:
    """branch_sway.png's 8 frames, each stretched to the screen's width (nearest neighbour)."""

    def __init__(self, path, frames=8, width=W):
        im = Image.open(path).convert("RGBA")
        ink = im.split()[3].point(lambda a: 255 if a >= 128 else 0).convert("1")
        fh = im.height // frames
        self.src_w = im.width
        self.kb = width / im.width
        self.height = round(fh * self.kb)
        self.frames = [ink.crop((0, k * fh, im.width, (k + 1) * fh)).resize((width, self.height), _NEAREST)
                       for k in range(frames)]

    def blit(self, img, k, x, y):
        m = self.frames[k % len(self.frames)]
        img.paste(BLACK, (x, y), m)
        return (max(0, x), max(0, y), min(img.width, x + m.width), min(img.height, y + m.height))


# ======================================================================
# Sequences and their precomputed tables
# ======================================================================
def run_ages(frames, ok):
    """For each position of a looping sequence: ticks since the current unbroken run of ok
    frames began (0 on its first tick), or -1 when the frame there isn't one of them."""
    n = len(frames)
    out = []
    for i in range(n):
        k = 0
        while k < n and ok(frames[(i - k) % n]):
            k += 1
        out.append(k - 1 if k else -1)
    return out


class Spec:
    def __init__(self, path):
        d = json.load(open(path))
        seq = d["sequences"]
        self.now = [tuple(e) for e in seq["nowPlayingAndMenuPlaying"]]   # (frame, dy)
        self.sleep = [tuple(e) for e in seq["sleepPaused"]]              # (frame, dy)
        self.idle = [tuple(e[:4]) + (bool(e[4]) if len(e) > 4 else False,)
                     for e in seq["idleQuiet"]]                          # (frame, x, dy, bowl, flip)
        f = d["frames"]
        self.z_frames = set(f["sleep"])                                  # 47, 48
        self.z_glyph = (d["particles"]["z"]["glyph"]["frame"],
                        tuple(d["particles"]["z"]["glyph"]["clip"][k] for k in "xywh"))
        self.note_glyph = (d["particles"]["note"]["glyph"]["frame"],
                           tuple(d["particles"]["note"]["glyph"]["clip"][k] for k in "xywh"))
        lay = d["idleLayout"]
        self.scale = lay["scale"]
        self.rest_x = lay["restX"]
        self.bowl_x = lay["bowlSpriteX"]
        self.ground = lay["groundLineY"]
        bowl = lay["bowl"]
        self.bowl_frames = (bowl["emptyFrame"], bowl["fullFrame"])
        self.bowl_clip = tuple(bowl["clip"][k] for k in "xywh")
        self.profile = lay["surfaceProfile"]["values"]
        self.sway_every = lay["branch"]["swayTickEvery"]
        # tables, worked out once
        z = self.z_frames
        self.sleep_age = run_ages([e[0] for e in self.sleep], lambda fr: fr in z)
        self.idle_age = run_ages([e[0] for e in self.idle], lambda fr: fr in z)
        nf = [e[0] for e in self.now]
        self.phrases = [i for i, fr in enumerate(nf) if fr == 3 and not 3 <= nf[i - 1] <= 5]
        self.hop_at = nf.index(12)   # a new song: straight into the hop, then the excited jump
        self.surface = [self._surf(x) for x in range(-64, W + 128)]

    def _surf(self, x):
        kb = W / len(self.profile)
        i = max(0, min(len(self.profile) - 1, math.floor(x / kb)))
        return jsround(kb * self.profile[i])

    def surf(self, x):
        return self.surface[max(0, min(len(self.surface) - 1, int(x) + 64))]


# ======================================================================
# The bird
# ======================================================================
class Bird:
    """Sprites, tables and the bird's state. observe() each step with the player's state;
    draw_now / draw_menu / draw_idle each return the boxes they drew."""

    def __init__(self, asset_dir=None):
        d = find_assets(asset_dir)
        self.dir = d
        self.sheet = Sheet(os.path.join(d, "currawong_sheet.png"))
        self.branch = Branch(os.path.join(d, "branch_sway.png"))
        self.spec = Spec(os.path.join(d, "animation_spec.json"))
        sp = self.spec
        # every variant the scenes use, built now rather than on the first frame that needs it
        frames = range(self.sheet.count)
        self.sheet.warm([(f, 1, fl, None) for f in frames for fl in (False, True)])
        self.sheet.warm([(f, sp.scale, fl, None) for f in frames for fl in (False, True)])
        zf, zc = sp.z_glyph
        nf, nc = sp.note_glyph
        self.sheet.warm([(zf, s, False, zc) for s in (1, 2, sp.scale, sp.scale * 2)])
        self.sheet.warm([(nf, s, False, nc) for s in (2, 3)])
        self.sheet.warm([(bf, sp.scale, False, sp.bowl_clip) for bf in sp.bowl_frames])
        # the idle scene's fixed geometry
        s = sp.scale
        self.idle_top = sp.ground - 24 * s                    # sprite top when standing at rest
        self.rest_surf = sp.surf(sp.rest_x + 13 * s)
        self.branch_y = sp.ground - self.rest_surf + 2
        self.bowl_dy = sp.surf(sp.bowl_x + 13 * s) - self.rest_surf
        # state
        self.started = False
        self.playing = False
        self.has_track = False
        self.idle = False
        self.bowl_full = False   # the bowl as the idle scene last showed it (for the still)
        self.track = None
        self.play_from = self.sleep_from = self.idle_from = 0
        self.sway_shown = None

    # -------------------------------------------------------------- state
    @staticmethod
    def tick(now):
        return int(math.floor(now / TICK))

    def observe(self, now, playing, has_track, track, idle=None):
        """Call every step. Playing starts the singing loop from the top, pausing settles into
        sleep, a new song while playing hops, and the idle loop (the branch) starts from the top
        whenever idle begins: nothing loaded, or (the caller decides) paused for a while."""
        t = self.tick(now)
        idle = (not has_track) if idle is None else idle
        if not self.started:
            self.started = True
            self.playing, self.has_track, self.track, self.idle = playing, has_track, track, idle
            self.play_from = self.sleep_from = self.idle_from = t
            return
        if idle != self.idle:
            self.idle = idle
            if idle:
                self.idle_from = t        # onto the branch: settle, then sleep
        if has_track != self.has_track:
            self.has_track = has_track
        if playing != self.playing:
            self.playing = playing
            if playing:
                self.play_from = t
            else:
                self.sleep_from = t
        elif playing and has_track and track != self.track and self.track is not None:
            self.play_from = t - self.spec.hop_at
        if has_track:
            self.track = track

    def pose(self, t):
        """(frame, dy, index, sequence) for Now Playing and the menu."""
        sp = self.spec
        if self.playing:
            i = (t - self.play_from) % len(sp.now)
            return sp.now[i] + (i, "now")
        i = (t - self.sleep_from) % len(sp.sleep)
        return sp.sleep[i] + (i, "sleep")

    # -------------------------------------------------------------- particles
    def _z_x(self, x, flip, u=1):
        """Where the Zs start: just in front of the sleeping head (cell x 26), mirrored to the
        other side of the cell when the bird is drawn facing the other way."""
        zw = self.spec.z_glyph[1][2]
        return x + (32 - 26 - zw) * u if flip else x + 26 * u

    def _zs(self, img, ox, oy, u, age, invert, boxes):
        """Three Zs per sleep, starting one after another, rising off the top of the screen."""
        if age < 0:
            return
        zf, zc = self.spec.z_glyph
        L = math.ceil((oy + 12) / 4)
        for k in range(3):
            tk = age - 6 - (k * L) // 2
            if tk < 0:
                continue
            p = (tk % L) / L
            z = u * (1 if p < 0.45 else 2)
            x = ox + Z_SWAY[(tk >> 1) % 10] * u
            y = jsround(oy - p * (oy + 12))
            b = self.sheet.blit(img, zf, x, y, z, False, invert, zc)
            if b:
                boxes.append(b)

    def _notes(self, img, idx, ox, oy, invert, boxes):
        """One note per sung phrase, from the first open-beak frame: up 3 px and right 2 px a tick."""
        nf, nc = self.spec.note_glyph
        n = len(self.spec.now)
        for s in self.spec.phrases:
            a = (idx - s) % n
            if a >= NOTE_LIFE:
                continue
            z = 2 if a < 8 else 3
            x = ox + a * 2 + NOTE_WOBBLE[a % 6]
            b = self.sheet.blit(img, nf, x, oy - a * 3, z, False, invert, nc)
            if b:
                boxes.append(b)

    # -------------------------------------------------------------- scenes
    def draw_now(self, img, now, playhead_x):
        """Now Playing: on the twig at the playhead, facing the way the music goes."""
        t = self.tick(now)
        frame, dy, idx, seq = self.pose(t)
        x = int(playhead_x) - 16
        boxes = []
        b = self.sheet.blit(img, frame, x, NP_BIRD_Y + dy, 1, frame <= 5)
        if b:
            boxes.append(b)
        if seq == "now":
            self._notes(img, idx, x + 22, NP_BIRD_Y + 3, False, boxes)
        else:
            self._zs(img, self._z_x(x, frame <= 5), NP_BIRD_Y + 6, 1, self.spec.sleep_age[idx], False, boxes)
        return boxes

    def draw_menu(self, img, now):
        """The menu's title bar: inverted, facing left, jumps scaled to fit the bar."""
        t = self.tick(now)
        frame, dy, idx, seq = self.pose(t)
        x, y = MENU_BIRD_X, MENU_BIRD_Y
        boxes = []
        b = self.sheet.blit(img, frame, x, y + jsround(dy * MENU_DY), 1, frame >= 7, True)
        if b:
            boxes.append(b)
        if seq == "now":
            self._notes(img, idx, x + 6, y + 3, True, boxes)
        else:   # mirrored here (facing left), so the Zs come from the left of the cell
            self._zs(img, self._z_x(x, frame >= 7), y + 6, 1, self.spec.sleep_age[idx], True, boxes)
        return boxes

    def idle_pose(self, t):
        sp = self.spec
        i = (t - self.idle_from) % len(sp.idle)
        return sp.idle[i] + (i,)

    def draw_idle(self, img, now):
        """Nothing playing: the branch, the bowl in the corner, and the bird (at 2x) on the branch."""
        t = self.tick(now)
        sp = self.spec
        s = sp.scale
        frame, ix, dy0, bowl, flip, i = self.idle_pose(t)
        if bowl != 2:
            self.bowl_full = bowl == 1
        sway = t // sp.sway_every
        box = self.branch.blit(img, sway, 0, self.branch_y)
        boxes = []
        if sway != self.sway_shown:   # the branch only moves every few ticks: report it only then
            self.sway_shown = sway
            boxes.append(box)
        if bowl != 2:   # 2: the bird's own frames are drawing the bowl
            b = self.sheet.blit(img, sp.bowl_frames[1 if bowl == 1 else 0], sp.bowl_x + 23 * s,
                                self.idle_top + self.bowl_dy + 2 + 17 * s, s, False, False, sp.bowl_clip)
            if b:
                boxes.append(b)
        flying = 15 <= frame <= 24
        dy = dy0 + (0 if flying else sp.surf(ix + 13 * s) - self.rest_surf)
        b = self.sheet.blit(img, frame, ix, self.idle_top + dy + 2, s, flip)
        if b:
            boxes.append(b)
        self._zs(img, self._z_x(ix, flip, s), self.idle_top + dy + 6 * s, s, sp.idle_age[i], False, boxes)
        return boxes


def _still_parts(bird):
    sp = bird.spec
    s = sp.scale
    return sp, s, bird.idle_top + 2


def draw_still(bird, img):
    """The sleep screen's picture: the bird asleep on its branch (frame 48 at its resting spot),
    two Zs caught mid-rise, and the bowl as the idle scene last left it. Drawn once: nothing
    moves while the screen sleeps."""
    sp, s, top = _still_parts(bird)
    bird.branch.blit(img, 0, 0, bird.branch_y)
    bird.sheet.blit(img, sp.bowl_frames[1 if bird.bowl_full else 0], sp.bowl_x + 23 * s,
                    bird.idle_top + bird.bowl_dy + 2 + 17 * s, s, False, False, sp.bowl_clip)
    bird.sheet.blit(img, 48, sp.rest_x, top, s)
    zf, zc = sp.z_glyph
    ox, oy = sp.rest_x + 26 * s, bird.idle_top + 6 * s
    bird.sheet.blit(img, zf, ox + 2 * s, oy - 34, s, False, False, zc)          # a small one, just off
    bird.sheet.blit(img, zf, ox + 6 * s, oy - 86, 2 * s, False, False, zc)      # a bigger one, higher


def draw_twig(img, frac):
    """The Now Playing progress bar as a twig: a 1 px dashed line (3 on, 2 off) 224 px long from
    x 8 at y 341, and a solid 3 px bar (y 340-342) as wide as the part already played. Returns
    the playhead x (the bird stands centred on it)."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for x in range(NP_TWIG_X0, NP_TWIG_X0 + NP_TWIG_LEN, 5):
        d.line((x, NP_TWIG_Y + 1, min(x + 2, NP_TWIG_X0 + NP_TWIG_LEN - 1), NP_TWIG_Y + 1), fill=BLACK)
    w = jsround(NP_TWIG_LEN * max(0.0, min(1.0, frac)))
    if w > 0:
        d.rectangle((NP_TWIG_X0, NP_TWIG_Y, NP_TWIG_X0 + w - 1, NP_TWIG_Y + 2), fill=BLACK)
    return NP_TWIG_X0 + w


def columns(boxes, pad=0):
    """Merge boxes into sorted, non-overlapping column ranges [(x0, x1), ...] (x1 exclusive)."""
    spans = sorted((max(0, b[0] - pad), min(W, b[2] + pad)) for b in boxes if b)
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
