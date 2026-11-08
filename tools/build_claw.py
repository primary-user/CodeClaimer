"""
Build the CodeClaimer claw tiles in assets/ from the brand icon.

    python tools/build_claw.py

Writes claw-live-vN.gif, claw-claimed-vN.gif and claw-empty-vN.png. When the
art changes, bump VERSION and update the names in cards.py, because the CDN
caches files by name.


Layers are traced from codeclaimer_icon.jpg (rod, hub, left arm, right arm,
gift) so the motion uses the real artwork. Arms rotate about the hub to open
and close. Everything is drawn in 1024px icon space, supersampled, then
downscaled for clean edges.
"""
import math
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy import ndimage

import os
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "..", "assets", "codeclaimer_icon.jpg")
OUT = os.path.join(HERE, "..", "assets")
FONT = None
VERSION = 1

SKY = (62, 176, 210)
NAVY = (9, 66, 143)
SHADOW = (47, 152, 190)
SLATE_BG = (78, 80, 88)
SLATE_FG = (150, 154, 164)

# ---------------------------------------------------------------- layers
im = np.asarray(Image.open(ICON).convert("RGB")).astype(int)
mask = ((im - NAVY) ** 2).sum(-1) < ((im - SKY) ** 2).sum(-1)
lab, _ = ndimage.label(mask)
H, W = mask.shape
yy, xx = np.mgrid[0:H, 0:W]


def layer(m):
    a = Image.fromarray((m * 255).astype(np.uint8), "L")
    return a.filter(ImageFilter.GaussianBlur(0.8))


rod_m = lab == 1
body = lab == 2
gift_m = (lab == 3) | (lab == 4) | (lab == 5)
hub_m = body & (xx > 405) & (xx < 620)
neck = (yy > 355) & (xx > 412) & (xx < 612)
left_m = body & (xx < 445) & ~neck
right_m = body & (xx > 580) & ~neck

ROD, HUB, LEFT, RIGHT, GIFT = map(layer, (rod_m, hub_m, left_m, right_m, gift_m))
PIV_L = (430, 321)
PIV_R = (594, 321)
GIFT_CX, GIFT_BOTTOM = 512, 805


def paste_mask(canvas, m, dx, dy, color):
    solid = Image.new("RGB", m.size, color)
    canvas.paste(solid, (int(round(dx)), int(round(dy))), m)


def rotated(m, angle, pivot):
    return m.rotate(angle, resample=Image.BICUBIC, center=pivot)


def draw_scene(canvas, ox, oy, claw_y, open_deg, gift_dy, sway_x=0.0, fg=NAVY, shadow=SHADOW,
               show_gift=True):
    """Draw claw + gift in icon space, offset by (ox, oy) on the canvas."""
    # shadow under the gift, shrinks as the gift lifts
    if show_gift and shadow:
        lift = max(0.0, -gift_dy)
        k = max(0.25, 1 - lift / 520)
        w, h = 300 * k, 34 * k
        d = ImageDraw.Draw(canvas)
        cx, cy = ox + GIFT_CX, oy + GIFT_BOTTOM + 22
        d.ellipse([cx - w, cy - h, cx + w, cy + h], fill=shadow)
    if show_gift:
        paste_mask(canvas, GIFT, ox + sway_x * (1 if gift_dy < -5 else 0), oy + gift_dy, fg)
    cx, cy = ox + sway_x, oy + claw_y
    # rod runs up out of frame
    d = ImageDraw.Draw(canvas)
    if cy + 146 > -10:
        d.rectangle([cx + 437, -10, cx + 587, cy + 146], fill=fg)
    paste_mask(canvas, ROD, cx, cy, fg)
    paste_mask(canvas, rotated(LEFT, -open_deg, PIV_L), cx, cy, fg)
    paste_mask(canvas, rotated(RIGHT, open_deg, PIV_R), cx, cy, fg)
    paste_mask(canvas, HUB, cx, cy, fg)


# ---------------------------------------------------------------- easing
def ease(t):
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def ease_out_back(t, s=1.6):
    t = min(1.0, max(0.0, t)) - 1
    return t * t * ((s + 1) * t + s) + 1


def lerp(a, b, t):
    return a + (b - a) * t


def seg(t, a, b):
    return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)


OPEN = 16.0
TOP = -300.0

# ---------------------------------------------------------------- timelines
def live_state(t, T):
    """Tease loop: drop toward the gift, snap at it, rise back. Seamless."""
    u = t / T
    down = ease(seg(u, 0.12, 0.42))
    up = ease(seg(u, 0.62, 0.92))
    y = lerp(TOP, -95, down) if u < 0.62 else lerp(-95, TOP, up)
    snap = seg(u, 0.44, 0.52)
    rel = seg(u, 0.52, 0.60)
    ang = OPEN - 9 * math.sin(math.pi * snap) if u < 0.52 else OPEN - 9 * math.sin(math.pi * (0.5 + rel / 2)) * (1 - rel)
    sway = 14 * math.sin(2 * math.pi * u)
    return dict(claw_y=y, open_deg=ang, gift_dy=0.0, sway_x=sway)


def claimed_state(t, T):
    """Drop, grab, lift. Holds the lifted pose for the back half."""
    u = t / T
    down = ease(seg(u, 0.00, 0.22))
    grip = ease_out_back(seg(u, 0.22, 0.32))
    lift = ease(seg(u, 0.36, 0.56))
    ang = lerp(OPEN, 0.0, grip)
    y = lerp(TOP, 0.0, down) if u < 0.36 else lerp(0.0, -230.0, lift)
    gift_dy = 0.0 if u < 0.36 else lerp(0.0, -230.0, lift)
    settle = seg(u, 0.56, 1.0)
    sway = 18 * math.sin(2 * math.pi * 1.5 * settle) * (1 - settle) if u >= 0.56 else 0.0
    return dict(claw_y=y, open_deg=ang, gift_dy=gift_dy, sway_x=sway)


# ---------------------------------------------------------------- renderers
def render(kind, state_fn, seconds, fps, out_size, world_size, claw_origin, bg, fg, shadow,
           text=None, text_box=None, hold_last=0.0):
    SS = 2
    frames = []
    n = int(seconds * fps)
    for i in range(n):
        st = state_fn(i / fps, seconds)
        canvas = Image.new("RGB", world_size, bg)
        if text:
            dt = ImageDraw.Draw(canvas)
            draw_text(dt, text, text_box, fg)
        draw_scene(canvas, claw_origin[0], claw_origin[1], fg=fg, shadow=shadow, **st)
        frames.append(canvas.resize(out_size, Image.LANCZOS))
    durations = [int(1000 / fps)] * len(frames)
    if hold_last:
        durations[-1] = int(hold_last * 1000)
    pal = frames[0].quantize(colors=24, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    path = f"{OUT}/{kind}.gif"
    q[0].save(path, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True,
              disposal=1)
    frames[-1].save(f"{OUT}/{kind}_last.png")
    return path


def draw_text(d, lines, box, fg):
    x, y, size = box
    font = ImageFont.truetype(FONT, size)
    for line in lines:
        d.text((x, y), line, font=font, fill=fg)
        y += int(size * 0.92)


def static(kind, out_size, world_size, claw_origin, bg, fg, shadow, text=None, text_box=None, **st):
    canvas = Image.new("RGB", world_size, bg)
    if text:
        draw_text(ImageDraw.Draw(canvas), text, text_box, fg)
    draw_scene(canvas, claw_origin[0], claw_origin[1], fg=fg, shadow=shadow, **st)
    canvas.resize(out_size, Image.LANCZOS).save(f"{OUT}/{kind}.png")


# ---------------------------------------------------------------- claimed v2
WHITE = (255, 255, 255)
DEEP = (6, 46, 104)


def claimed2_state(t, T):
    """Drop, grip, yank the gift up and out of frame, then a check pops in."""
    u = t / T
    down = ease(seg(u, 0.00, 0.20))
    grip = ease_out_back(seg(u, 0.20, 0.30))
    yank = seg(u, 0.36, 0.58)
    yank = yank * yank * yank  # accelerate out
    ang = lerp(OPEN, 0.0, grip)
    y = lerp(TOP, 0.0, down) if u < 0.36 else lerp(0.0, -1250.0, yank)
    gift_dy = 0.0 if u < 0.36 else lerp(0.0, -1250.0, yank)
    tug = -18 * math.sin(math.pi * seg(u, 0.30, 0.36))      # small tug before the yank
    check = seg(u, 0.60, 0.74)
    return dict(claw_y=y + tug, open_deg=ang, gift_dy=gift_dy + tug, check=check)


def draw_check(canvas, ox, oy, p, color):
    if p <= 0:
        return
    pop = ease_out_back(min(1.0, p * 1.3), s=2.2)
    cx, cy = ox + 512, oy + 545
    pts = [(-200, 10), (-60, 150), (230, -160)]
    pts = [(cx + x * pop, cy + y * pop) for x, y in pts]
    # draw the stroke progressively along its length
    lens = [math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])]
    total = sum(lens) * min(1.0, p * 1.6)
    d = ImageDraw.Draw(canvas)
    w = int(110 * pop)
    path = [pts[0]]
    if total <= lens[0]:
        k = total / lens[0]
        path.append((lerp(pts[0][0], pts[1][0], k), lerp(pts[0][1], pts[1][1], k)))
    else:
        path.append(pts[1])
        k = (total - lens[0]) / lens[1]
        path.append((lerp(pts[1][0], pts[2][0], k), lerp(pts[1][1], pts[2][1], k)))
    d.line(path, fill=color, width=w, joint="curve")
    r = w / 2
    for x, y in (path[0], path[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def render_claimed2(name, seconds=3.0, fps=20, hold=8.0, size=256):
    frames = []
    for i in range(int(seconds * fps)):
        st = claimed2_state(i / fps, seconds)
        check = st.pop("check")
        canvas = Image.new("RGB", (1024, 1024), NAVY)
        # shadow fades as the gift leaves
        lift = max(0.0, -st["gift_dy"])
        if lift < 500:
            k = max(0.25, 1 - lift / 520)
            dd = ImageDraw.Draw(canvas)
            cx, cy = 512, 60 + GIFT_BOTTOM + 22
            dd.ellipse([cx - 300 * k, cy - 34 * k, cx + 300 * k, cy + 34 * k], fill=DEEP)
        paste_mask(canvas, GIFT, 0, 60 + st["gift_dy"], WHITE)
        cx, cy = 0, 60 + st["claw_y"]
        dd = ImageDraw.Draw(canvas)
        if cy + 146 > -10:
            dd.rectangle([cx + 437, -10, cx + 587, cy + 146], fill=SKY)
        paste_mask(canvas, ROD, cx, cy, SKY)
        paste_mask(canvas, rotated(LEFT, -st["open_deg"], PIV_L), cx, cy, SKY)
        paste_mask(canvas, rotated(RIGHT, st["open_deg"], PIV_R), cx, cy, SKY)
        paste_mask(canvas, HUB, cx, cy, SKY)
        draw_check(canvas, 0, 0, check, WHITE)
        frames.append(canvas.resize((size, size), Image.LANCZOS))
    durations = [int(1000 / fps)] * len(frames)
    durations[-1] = int(hold * 1000)
    pal = frames[-1].quantize(colors=24, method=Image.Quantize.MEDIANCUT)
    # make sure the palette also has the gift white and claw sky from mid frames
    picks = frames[::4] + [frames[-1]]
    pal = Image.new("RGB", (size * len(picks), size))
    for j, f in enumerate(picks):
        pal.paste(f, (j * size, 0))
    pal = pal.quantize(colors=32, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    q[0].save(f"{OUT}/{name}.gif", save_all=True, append_images=q[1:], duration=durations, loop=0,
              optimize=True, disposal=1)
    return frames


if __name__ == "__main__":
    render("_live", live_state, 3.0, 20, out_size=(256, 256), world_size=(1024, 1024), claw_origin=(0, 60),
           bg=SKY, fg=NAVY, shadow=SHADOW)
    os.replace(f"{OUT}/_live.gif", f"{OUT}/claw-live-v{VERSION}.gif")
    os.remove(f"{OUT}/_live_last.png")
    render_claimed2(f"claw-claimed-v{VERSION}")
    static(f"claw-empty-v{VERSION}", (256, 256), (1024, 1024), (0, 60), SLATE_BG, SLATE_FG, None,
           claw_y=-140, open_deg=OPEN, gift_dy=0, show_gift=False)
    print("written to", os.path.normpath(OUT))
