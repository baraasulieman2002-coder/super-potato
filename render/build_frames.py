#!/usr/bin/env python3
"""
Nebula Nest — motion-graphics cut renderer.
Covers Hook -> Weave/Wordmark -> Ending -> CTA (25s, 30fps, 1280x720).
No documentary b-roll (that needs real footage / a paid AI video model);
this renders the abstract spark/light/typography spine of the film.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math, random, sys, subprocess

W, H = 1280, 720
FPS = 30
SCALE = 4
WG, HG = W // SCALE, H // SCALE
CX, CY = W / 2, H / 2

BG = np.array([11, 11, 13], dtype=np.float32)
GOLD = np.array([201, 162, 93], dtype=np.float32)
GOLD_HOT = np.array([255, 224, 150], dtype=np.float32)
CORE = np.array([255, 245, 224], dtype=np.float32)
BONE = np.array([242, 237, 228], dtype=np.float32)

FONT_DIR = "/usr/share/fonts/truetype/montserrat/"
def font(name, size):
    return ImageFont.truetype(FONT_DIR + name, size)

F_TITLE = font("Montserrat-Bold.ttf", 92)
F_TITLE_BIG = font("Montserrat-SemiBold.ttf", 54)
F_LABEL = font("Montserrat-Regular.ttf", 26)
F_HANDLE = font("Montserrat-SemiBold.ttf", 34)

random.seed(7)

# ---------- phase timing (seconds) ----------
T_HOOK = 3.0
T_WEAVE = 11.0     # 3 -> 14
T_END = 5.0        # 14 -> 19
T_CTA = 6.0        # 19 -> 25
T_TOTAL = T_HOOK + T_WEAVE + T_END + T_CTA
N_FRAMES = int(round(T_TOTAL * FPS))

def ease_out_cubic(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3

def ease_in_out_cubic(x):
    x = min(max(x, 0.0), 1.0)
    return 4 * x**3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2

def clamp01(x):
    return min(max(x, 0.0), 1.0)

# ---------- low-res glow grid ----------
gyy, gxx = np.mgrid[0:HG, 0:WG].astype(np.float32)

def new_grid():
    return np.zeros((HG, WG, 3), dtype=np.float32)

def add_glow(grid, cx, cy, radius, color, strength):
    if radius <= 0.5 or strength <= 0:
        return
    gcx, gcy, gr = cx / SCALE, cy / SCALE, radius / SCALE
    d2 = (gxx - gcx) ** 2 + (gyy - gcy) ** 2
    falloff = np.exp(-d2 / (2 * gr * gr))
    grid += falloff[..., None] * (color * strength)

def upscale_grid(grid):
    img = Image.fromarray(np.clip(grid, 0, 255).astype(np.uint8), "RGB")
    return img.resize((W, H), Image.BICUBIC)

# ---------- particles for the weave ----------
N_PART = 16
particles = []
for i in range(N_PART):
    ang0 = (i / N_PART) * 2 * math.pi + random.uniform(-0.25, 0.25)
    r0 = random.uniform(420, 640)
    delay = random.uniform(0.0, 0.30)     # fraction of weave where it starts moving
    dur = random.uniform(0.55, 0.75)      # fraction of weave spent converging
    swirl = random.uniform(1.6, 3.2) * random.choice([-1, 1])
    particles.append(dict(ang0=ang0, r0=r0, delay=delay, dur=dur, swirl=swirl))

def particle_pos(p, wp):
    """wp: weave progress 0..1 (whole-phase). Returns (x, y, arrived_progress)."""
    local = clamp01((wp - p["delay"]) / p["dur"])
    e = ease_in_out_cubic(local)
    ang = p["ang0"] + p["swirl"] * (1 - e)
    r = p["r0"] * (1 - e)
    x = CX + r * math.cos(ang) * 1.28
    y = CY + r * math.sin(ang) * 0.82
    return x, y, e

def draw_particle_core(draw, x, y, alpha):
    if alpha <= 0.01:
        return
    a = int(255 * alpha)
    r1 = 6
    col = tuple(int(c) for c in GOLD_HOT) + (a,)
    draw.ellipse([x - r1, y - r1, x + r1, y + r1], fill=col)

def render_grain(seed_frame):
    rng = np.random.default_rng(seed_frame)
    noise = rng.normal(0, 1, (H // 2, W // 2)).astype(np.float32)
    span = noise.max() - noise.min()
    noise = np.array(Image.fromarray(((noise - noise.min()) / (span + 1e-6) * 255).astype(np.uint8)).resize((W, H), Image.BILINEAR))
    return noise.astype(np.float32)

def apply_grain(arr, seed_frame, amplitude):
    """amplitude: max +/- pixel deviation (e.g. 8-16 for subtle 35mm grain)."""
    if amplitude <= 0:
        return arr
    n = render_grain(seed_frame)
    n = (n - 127.5) / 127.5 * amplitude
    out = arr + n[..., None]
    return out

def text_layer(text, fnt, color, tracking=0):
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    if tracking:
        widths = [d.textbbox((0, 0), ch, font=fnt)[2] for ch in text]
        tw = sum(widths) + tracking * (len(text) - 1)
        asc, desc = fnt.getmetrics()
        th = asc + desc
        layer = Image.new("RGBA", (int(tw) + 20, int(th) + 20), (0, 0, 0, 0))
        dd = ImageDraw.Draw(layer)
        x = 0
        for ch, w_ in zip(text, widths):
            dd.text((x, 0), ch, font=fnt, fill=color)
            x += w_ + tracking
        return layer
    bbox = d.textbbox((0, 0), text, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    layer = Image.new("RGBA", (tw + 20, th + 40), (0, 0, 0, 0))
    dd = ImageDraw.Draw(layer)
    dd.text((10 - bbox[0], 10 - bbox[1]), text, font=fnt, fill=color)
    return layer

def paste_center(base, layer, cx, cy, scale=1.0, alpha=1.0):
    if alpha <= 0.003:
        return
    if scale != 1.0:
        nw, nh = max(1, int(layer.width * scale)), max(1, int(layer.height * scale))
        layer = layer.resize((nw, nh), Image.LANCZOS)
    if alpha < 1.0:
        a = layer.split()[3].point(lambda p: int(p * alpha))
        layer = layer.copy()
        layer.putalpha(a)
    x = int(cx - layer.width / 2)
    y = int(cy - layer.height / 2)
    base.alpha_composite(layer, (x, y))

def glow_from_alpha(layer, blur, boost, color):
    pad = int(blur * 4)
    col = tuple(int(c) for c in color)
    padded = Image.new("RGBA", (layer.width + pad * 2, layer.height + pad * 2), col + (0,))
    padded.alpha_composite(layer, (pad, pad))
    alpha = padded.split()[3]
    tinted = Image.new("RGBA", padded.size, col + (0,))
    tinted.putalpha(alpha)
    return tinted.filter(ImageFilter.GaussianBlur(blur)), boost

WORDMARK = text_layer("NEBULA NEST", F_TITLE, tuple(int(c) for c in GOLD) + (255,), tracking=10)
TITLE_END = text_layer("THIS IS ONLY THE BEGINNING.", F_TITLE_BIG, tuple(int(c) for c in BONE) + (255,), tracking=2)
CTA_FOLLOW = text_layer("Follow the journey.", F_LABEL, tuple(int(c) for c in BONE) + (230,), tracking=1)
CTA_HANDLE = text_layer("@NEBULANEST", F_HANDLE, tuple(int(c) for c in GOLD) + (255,), tracking=6)

def render_frame(fi):
    t = fi / FPS
    grid = new_grid()
    base = Image.new("RGBA", (W, H), tuple(int(c) for c in BG) + (255,))
    draw = ImageDraw.Draw(base)
    grain_amt = 4.0

    if t < T_HOOK:
        # ---------------- HOOK ----------------
        p = t / T_HOOK
        if t < 0.5:
            pass  # held black — intentional silence
        else:
            e = ease_out_cubic((t - 0.5) / (T_HOOK - 0.5))
            pulse = 1 + 0.05 * math.sin(t * 10)
            radius = 95 * e * pulse
            add_glow(grid, CX, CY, radius, GOLD, 1.35 * e)
            add_glow(grid, CX, CY, radius * 0.35, GOLD_HOT, 1.6 * e)
            glow_img = upscale_grid(grid)
            base.alpha_composite(glow_img.convert("RGBA"))
            core_r = 5 * e
            draw.ellipse([CX - core_r, CY - core_r, CX + core_r, CY + core_r],
                         fill=tuple(int(c) for c in CORE) + (int(255 * e),))
        grain_amt = 5.0

    elif t < T_HOOK + T_WEAVE:
        # ---------------- WEAVE ----------------
        wt = t - T_HOOK
        wp = wt / T_WEAVE
        lock_start, lock_end = 0.92, 1.0  # snap window near the end

        if wp < lock_start:
            for pt in particles:
                x, y, e = particle_pos(pt, wp / lock_start)
                fade = 1.0 if e < 0.97 else max(0, 1 - (e - 0.97) / 0.03)
                add_glow(grid, x, y, 34, GOLD, 0.5 * fade)
                for k in range(1, 5):
                    bt = clamp01(wp / lock_start - k * 0.012)
                    bx, by, _ = particle_pos(pt, bt)
                    add_glow(grid, bx, by, 14, GOLD, 0.14 * fade)
            add_glow(grid, CX, CY, 70, GOLD, 0.25 * ease_in_out_cubic(wp / lock_start))
            glow_img = upscale_grid(grid)
            base.alpha_composite(glow_img.convert("RGBA"))
            for pt in particles:
                x, y, e = particle_pos(pt, wp / lock_start)
                alpha = 1.0 if e < 0.97 else max(0, 1 - (e - 0.97) / 0.03)
                draw_particle_core(draw, x, y, alpha)
            grain_amt = 5.0
        else:
            snap = clamp01((wp - lock_start) / (lock_end - lock_start))
            flash = math.exp(-snap * 9) * (1 - snap) * 1.8
            add_glow(grid, CX, CY, 260, GOLD_HOT, flash)
            add_glow(grid, CX, CY, 90, GOLD, 0.55 + 0.4 * snap)
            glow_img = upscale_grid(grid)
            base.alpha_composite(glow_img.convert("RGBA"))
            wm_alpha = ease_out_cubic(snap)
            gimg, gboost = glow_from_alpha(WORDMARK, 22, 1.0, GOLD)
            paste_center(base, gimg, CX, CY, scale=1.0, alpha=wm_alpha * 0.9)
            paste_center(base, WORDMARK, CX, CY, scale=1.0, alpha=wm_alpha)
            grain_amt = 4.0 + flash * 6

    elif t < T_HOOK + T_WEAVE + T_END:
        # ---------------- ENDING ----------------
        et = t - (T_HOOK + T_WEAVE)
        ep = et / T_END
        pull = 1.0 - 0.09 * ease_out_cubic(min(ep / 0.6, 1.0))
        pulse = 0.85 + 0.15 * math.sin(et * 1.4)
        add_glow(grid, CX, CY, 110 * pull, GOLD, 0.55 * pulse)
        add_glow(grid, CX, CY, 40 * pull, GOLD_HOT, 0.5 * pulse)
        glow_img = upscale_grid(grid)
        base.alpha_composite(glow_img.convert("RGBA"))
        gimg, _ = glow_from_alpha(WORDMARK, 22, 1.0, GOLD)
        paste_center(base, gimg, CX, CY, scale=pull, alpha=0.9)
        paste_center(base, WORDMARK, CX, CY, scale=pull, alpha=1.0)

        title_a = clamp01((ep - 0.55) / 0.35)
        if title_a > 0:
            paste_center(base, TITLE_END, CX, CY + 150, alpha=title_a)
        grain_amt = 4.0

    else:
        # ---------------- CTA ----------------
        ct = t - (T_HOOK + T_WEAVE + T_END)
        cp = ct / T_CTA
        cross = clamp01(cp / 0.18)  # cross-dissolve in from Ending's held frame

        pull = 0.91
        add_glow(grid, CX, CY - 40, 60 * pull, GOLD, 0.45)
        add_glow(grid, CX, CY - 40, 22 * pull, GOLD_HOT, 0.4)
        glow_img = upscale_grid(grid)
        base.alpha_composite(glow_img.convert("RGBA"))

        mark_scale = 0.42
        gimg, _ = glow_from_alpha(WORDMARK, 16, 1.0, GOLD)
        paste_center(base, gimg, CX, CY - 40, scale=mark_scale, alpha=0.85)
        paste_center(base, WORDMARK, CX, CY - 40, scale=mark_scale, alpha=1.0)

        follow_a = clamp01((cp - 0.10) / 0.25)
        handle_a = clamp01((cp - 0.30) / 0.25)
        paste_center(base, CTA_FOLLOW, CX, CY + 55, alpha=follow_a)
        paste_center(base, CTA_HANDLE, CX, CY + 105, alpha=handle_a)

        # subtle warm light leak sweeping through, last third of CTA
        leak_a = clamp01((cp - 0.55) / 0.35) * 0.28
        if leak_a > 0.002:
            leak = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(leak)
            off = (cp - 0.55) * W * 1.6
            band = [(off - 260, -80), (off + 260, -80), (off + 900, H + 80), (off + 380, H + 80)]
            ld.polygon(band, fill=(255, 200, 120, int(255 * leak_a)))
            leak = leak.filter(ImageFilter.GaussianBlur(90))
            base.alpha_composite(leak)

        grain_amt = 4.5

        fade_out = clamp01((cp - 0.90) / 0.10)
        if fade_out > 0:
            black = Image.new("RGBA", (W, H), (0, 0, 0, int(255 * fade_out)))
            base.alpha_composite(black)

    arr = np.array(base.convert("RGB"), dtype=np.float32)
    arr = apply_grain(arr, fi, grain_amt)
    return np.clip(arr, 0, 255).astype(np.uint8)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "preview"

    if mode == "preview":
        out_dir = sys.argv[2]
        idxs = [int(x) for x in sys.argv[3:]]
        for i in idxs:
            arr = render_frame(i)
            Image.fromarray(arr).save(f"{out_dir}/preview_{i:04d}.png")
            print("wrote", i)

    elif mode == "render":
        out_path = sys.argv[2]
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
            "-i", "-",
            "-an",
            "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
            out_path,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(N_FRAMES):
            arr = render_frame(i)
            proc.stdin.write(arr.tobytes())
            if i % 60 == 0:
                print(f"frame {i}/{N_FRAMES}", file=sys.stderr)
        proc.stdin.close()
        proc.wait()
        print("done:", out_path)
