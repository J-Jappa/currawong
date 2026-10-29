"""
Currawong startup animation for a 240x400 1-bit display.

Files
  currawong_240x400.bin  all frames packed back to back, 12000 bytes per frame
                         row-major, MSB first, bit 1 = ink (black), 0 = background
  frames/frame_NNN.png   the same frames as black/white PNGs
  manifest.json          fps, frame count, scene ranges

Usage
  from currawong_anim import CurrawongAnimation
  anim = CurrawongAnimation("export/animation")
  for frame in anim.frames():            # bytes, 12000 each - send straight to a memory-LCD driver
      ...
  img = anim.pil_frame(0)                 # Pillow '1' image, if Pillow is installed

Run this file directly for a Tkinter preview:  python currawong_anim.py
"""
import json, os, time

class CurrawongAnimation:
    def __init__(self, folder=os.path.dirname(os.path.abspath(__file__))):
        with open(os.path.join(folder, "manifest.json")) as f:
            self.meta = json.load(f)
        self.width, self.height = self.meta["width"], self.meta["height"]
        self.fps, self.count = self.meta["fps"], self.meta["frames"]
        self.bpf = self.meta["bytes_per_frame"]
        with open(os.path.join(folder, "currawong_240x400.bin"), "rb") as f:
            self.data = f.read()

    def frame(self, i):
        i = max(0, min(self.count - 1, i))
        return self.data[i * self.bpf:(i + 1) * self.bpf]

    def frames(self):
        for i in range(self.count):
            yield self.frame(i)

    def pixel(self, buf, x, y):
        n = y * self.width + x
        return (buf[n >> 3] >> (7 - (n & 7))) & 1

    def pil_frame(self, i):
        from PIL import Image, ImageOps
        img = Image.frombytes("1", (self.width, self.height), self.frame(i))  # 1 = white in PIL
        return ImageOps.invert(img.convert("L")).convert("1")

    def scene(self, name):
        s = next(s for s in self.meta["scenes"] if s["name"] == name)
        return range(s["start_frame"], s["end_frame"])

    def play(self, show, loop=False):
        """Call show(frame_bytes) at the animation's frame rate. Ends on the final lockup frame."""
        dt = 1.0 / self.fps
        while True:
            t0 = time.perf_counter()
            for i in range(self.count):
                show(self.frame(i))
                time.sleep(max(0, t0 + (i + 1) * dt - time.perf_counter()))
            if not loop:
                return

if __name__ == "__main__":
    import tkinter as tk
    anim = CurrawongAnimation()
    root = tk.Tk(); root.title("Currawong 240x400")
    scale = 2
    img = tk.PhotoImage(width=anim.width, height=anim.height)
    label = tk.Label(root, image=img); label.pack()
    big = None
    def draw(i=0):
        global big
        buf = anim.frame(i)
        rows = []
        for y in range(anim.height):
            rows.append("{" + " ".join("#2E2D29" if anim.pixel(buf, x, y) else "#B4B2A9" for x in range(anim.width)) + "}")
        img.put(" ".join(rows))
        big = img.zoom(scale); label.configure(image=big)
        root.after(int(1000 / anim.fps), draw, (i + 1) % anim.count)
    draw()
    root.mainloop()
