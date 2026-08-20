"""Génération de la vidéo : caméra, dessin des traces, habillage et encodage."""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .geo import (meters_per_pixel, scale_for_bounds, world_to_lonlat,
                  zoom_to_scale, lonlat_to_world)
from .models import (MODE_COLORS, MODE_LABELS, QUALITY_CRF, QUALITY_PRESET, Place,
                     RenderOptions, Trip)
from .processing import Timeline, build_timeline
from .tiles import TileCache, TileError, render_tile_background, resolve_provider

log = logging.getLogger(__name__)

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

PALETTES = {
    "neon":    [(0.0, (58, 12, 163)), (0.35, (67, 97, 238)), (0.6, (76, 201, 240)),
                (0.8, (114, 239, 221)), (1.0, (255, 214, 10))],
    "viridis": [(0.0, (68, 1, 84)), (0.25, (59, 82, 139)), (0.5, (33, 145, 140)),
                (0.75, (94, 201, 98)), (1.0, (253, 231, 37))],
    "inferno": [(0.0, (0, 0, 4)), (0.25, (87, 16, 110)), (0.5, (188, 55, 84)),
                (0.75, (249, 142, 9)), (1.0, (252, 255, 164))],
    "ice":     [(0.0, (8, 29, 88)), (0.4, (34, 94, 168)), (0.7, (65, 182, 196)),
                (1.0, (237, 248, 251))],
    "fire":    [(0.0, (60, 9, 9)), (0.4, (204, 51, 0)), (0.7, (255, 153, 0)),
                (1.0, (255, 247, 173))],
    "rainbow": [(0.0, (120, 0, 200)), (0.2, (0, 90, 255)), (0.4, (0, 200, 160)),
                (0.6, (150, 220, 0)), (0.8, (255, 150, 0)), (1.0, (255, 40, 90))],
}


# ------------------------------------------------------------------ couleurs

def hex_to_rgb(value: str, fallback=(255, 255, 255)) -> Tuple[int, int, int]:
    if not isinstance(value, str):
        return fallback
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return fallback
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return fallback


def palette_color(name: str, t: float) -> Tuple[int, int, int]:
    stops = PALETTES.get(name, PALETTES["neon"])
    t = max(0.0, min(1.0, t))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return (int(c0[0] + (c1[0] - c0[0]) * f),
                    int(c0[1] + (c1[1] - c0[1]) * f),
                    int(c0[2] + (c1[2] - c0[2]) * f))
    return stops[-1][1]


def get_font(size: int) -> ImageFont.ImageFont:
    size = max(8, int(size))
    for path in FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def ffmpeg_binary() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("ffmpeg est introuvable dans l'image Docker.") from exc


# ------------------------------------------------------------------ caméra

class Camera:
    __slots__ = ("cx", "cy", "scale")

    def __init__(self, cx: float, cy: float, scale: float):
        self.cx, self.cy, self.scale = cx, cy, scale

    def to_screen(self, x: float, y: float, w: int, h: int) -> Tuple[float, float]:
        return ((x - self.cx) * self.scale + w / 2.0,
                (y - self.cy) * self.scale + h / 2.0)


def _ema(values: List[float], alpha: float) -> List[float]:
    """Lissage exponentiel aller-retour (sans déphasage)."""
    if not values or alpha <= 0:
        return values
    a = 1.0 - alpha
    out = list(values)
    acc = out[0]
    for i, v in enumerate(out):
        acc = acc * alpha + v * a
        out[i] = acc
    acc = out[-1]
    for i in range(len(out) - 1, -1, -1):
        acc = acc * alpha + out[i] * a
        out[i] = acc
    return out


def plan_camera(tl: Timeline, opt: RenderOptions, frames: int,
                progress_of_frame: List[float]) -> List[Camera]:
    W, H = opt.width, opt.height
    global_scale = scale_for_bounds(tl.bounds, W, H, opt.padding)
    gcx = (tl.bounds[0] + tl.bounds[2]) / 2.0
    gcy = (tl.bounds[1] + tl.bounds[3]) / 2.0

    if opt.camera == "fit_all":
        return [Camera(gcx, gcy, global_scale)] * frames

    cxs: List[float] = []
    cys: List[float] = []
    scales: List[float] = []
    heads: List[Tuple[float, float]] = []
    window = tl.total_weight * min(0.06, max(0.004, opt.fade_seconds / max(opt.duration, 1.0) * 0.5))
    max_scale = zoom_to_scale(min(opt.follow_zoom, 19.0))

    for p in progress_of_frame:
        target = tl.total_weight * p
        idx, frac = tl.locate(target)
        px = tl.sx[idx] + (tl.ex[idx] - tl.sx[idx]) * frac
        py = tl.sy[idx] + (tl.ey[idx] - tl.sy[idx]) * frac
        heads.append((px, py))
        if opt.camera == "follow":
            cxs.append(px); cys.append(py); scales.append(max_scale)
            continue
        if opt.camera == "trip":
            b = tl.trip_bounds[min(tl.trip[idx], len(tl.trip_bounds) - 1)]
        else:  # auto : cadre la portion récente du parcours
            lo = max(0.0, target - window)
            i0, _ = tl.locate(lo)
            i1 = idx
            xs = tl.sx[i0:i1 + 1] + tl.ex[i0:i1 + 1] + [px]
            ys = tl.sy[i0:i1 + 1] + tl.ey[i0:i1 + 1] + [py]
            b = (min(xs), min(ys), max(xs), max(ys))
        s = min(scale_for_bounds(b, W, H, max(opt.padding, 0.12)), max_scale)
        s = max(s, global_scale)
        cxs.append((b[0] + b[2]) / 2.0)
        cys.append((b[1] + b[3]) / 2.0)
        scales.append(s)

    alpha = opt.camera_smoothing
    cxs = _ema(cxs, alpha)
    cys = _ema(cys, alpha)
    log_scales = _ema([math.log(max(s, 1e-6)) for s in scales], min(alpha + 0.05, 0.98))
    scales = [math.exp(v) for v in log_scales]

    cams = [Camera(x, y, s) for x, y, s in zip(cxs, cys, scales)]
    # Le lissage peut laisser le point mobile sortir du cadre : on recentre au minimum.
    limx, limy = W * 0.33, H * 0.33
    for cam, (hx, hy) in zip(cams, heads):
        sx = (hx - cam.cx) * cam.scale + W / 2.0
        sy = (hy - cam.cy) * cam.scale + H / 2.0
        if sx < W / 2 - limx:
            cam.cx += (sx - (W / 2 - limx)) / cam.scale
        elif sx > W / 2 + limx:
            cam.cx += (sx - (W / 2 + limx)) / cam.scale
        if sy < H / 2 - limy:
            cam.cy += (sy - (H / 2 - limy)) / cam.scale
        elif sy > H / 2 + limy:
            cam.cy += (sy - (H / 2 + limy)) / cam.scale
    if opt.zoom_out_end and frames > 12:
        tail = max(4, int(frames * 0.12))
        for k in range(tail):
            i = frames - tail + k
            f = _ease(k / max(tail - 1, 1))
            cams[i] = Camera(cams[i].cx + (gcx - cams[i].cx) * f,
                             cams[i].cy + (gcy - cams[i].cy) * f,
                             math.exp(math.log(cams[i].scale) +
                                      (math.log(global_scale) - math.log(cams[i].scale)) * f))
    return cams


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


# ------------------------------------------------------------------ dessin

Segment = Tuple[float, float, float, float, Tuple[int, int, int, int], float]


def draw_segments(segs: Sequence[Segment], width: int, height: int, ss: int,
                  bbox: Optional[Tuple[int, int, int, int]] = None
                  ) -> Optional[Tuple[Image.Image, Tuple[int, int]]]:
    """Dessine des segments (coordonnées écran finales) sur un calque RGBA.

    Retourne (calque, offset) ou None si rien n'est visible.
    """
    if not segs:
        return None
    if bbox is None:
        x0, y0, x1, y1 = 0, 0, width, height
    else:
        x0, y0, x1, y1 = bbox
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(width, x1); y1 = min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    w, h = x1 - x0, y1 - y0
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for sx, sy, ex, ey, color, lw in segs:
        ax, ay = (sx - x0) * ss, (sy - y0) * ss
        bx, by = (ex - x0) * ss, (ey - y0) * ss
        pw = max(1, int(round(lw * ss)))
        d.line((ax, ay, bx, by), fill=color, width=pw)
        if pw > 2:
            r = pw / 2.0
            d.ellipse((bx - r, by - r, bx + r, by + r), fill=color)
    if ss > 1:
        layer = layer.reduce(ss)
    return layer, (x0, y0)


def visible_bbox(segs: Sequence[Segment], pad: int, width: int, height: int):
    xs0 = min(min(s[0], s[2]) for s in segs)
    xs1 = max(max(s[0], s[2]) for s in segs)
    ys0 = min(min(s[1], s[3]) for s in segs)
    ys1 = max(max(s[1], s[3]) for s in segs)
    return (int(xs0) - pad, int(ys0) - pad, int(xs1) + pad + 1, int(ys1) + pad + 1)


# ------------------------------------------------------------------ moteur

class Renderer:
    def __init__(self, trips: List[Trip], places: List[Place], opt: RenderOptions,
                 workdir: str, tile_cache: Optional[TileCache] = None,
                 on_progress: Optional[Callable[[float, str], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 audio_path: Optional[str] = None):
        self.trips = trips
        self.places = places
        self.opt = opt
        self.workdir = workdir
        self.tile_cache = tile_cache
        self.on_progress = on_progress or (lambda p, m: None)
        self.should_cancel = should_cancel or (lambda: False)
        self.audio_path = audio_path
        self.W, self.H = opt.width, opt.height
        self.ss = opt.supersample
        self.warnings: List[str] = []
        self.font_big = get_font(int(self.H * 0.055 * opt.font_scale))
        self.font_mid = get_font(int(self.H * 0.030 * opt.font_scale))
        self.font_small = get_font(int(self.H * 0.021 * opt.font_scale))
        self.overlay_rgb = hex_to_rgb(opt.overlay_color, (242, 246, 255))
        self.bg_rgb = hex_to_rgb(opt.background, (11, 15, 26))
        self.tile_url = None
        self.tile_attr = ""
        self._tile_warned = False

    # ---------------------------------------------------------- couleurs

    def _build_colors(self, tl: Timeline) -> List[Tuple[int, int, int]]:
        """Couleur de chaque segment, calculée une seule fois pour tout le rendu."""
        opt = self.opt
        if opt.color_mode == "single":
            rgb = hex_to_rgb(opt.line_color, (55, 230, 200))
            return [rgb] * len(tl)
        if opt.color_mode == "mode":
            table = {m: hex_to_rgb(MODE_COLORS.get(m, "#c8c8c8")) for m in set(tl.mode)}
            return [table[m] for m in tl.mode]
        if opt.color_mode == "speed":
            ref = math.log10(400.0)
            return [palette_color(opt.palette, min(1.0, math.log10(max(v, 1.0)) / ref))
                    for v in tl.speed]
        span = max(tl.et[-1] - tl.st[0], 1.0)
        t0 = tl.st[0]
        return [palette_color(opt.palette, (t - t0) / span) for t in tl.st]

    # ---------------------------------------------------------- fonds

    def _background(self, cam: Camera, cache: Dict) -> Image.Image:
        """Fond de carte pour une vue donnée (mémoïsé : gratuit en caméra fixe)."""
        opt = self.opt
        key = (round(cam.cx, 7), round(cam.cy, 7), round(cam.scale, 3))
        hit = cache.get(key)
        if hit is not None:
            return hit
        base = Image.new("RGB", (self.W, self.H), self.bg_rgb)
        if opt.map_style == "tiles" and self.tile_url and self.tile_cache:
            try:
                base = render_tile_background(
                    self.tile_cache, self.tile_url, opt.tile_provider,
                    cam.cx, cam.cy, cam.scale, self.W, self.H,
                    grayscale=opt.tile_grayscale, opacity=opt.tile_opacity,
                    base_color=self.bg_rgb)
            except Exception as exc:  # réseau indisponible → fond uni
                if not self._tile_warned:
                    self.warnings.append(f"Fond de carte indisponible : {exc}")
                    self._tile_warned = True
        elif opt.map_style == "grid":
            d = ImageDraw.Draw(base)
            step = max(40, int(self.H / 12))
            grid = tuple(min(255, int(c * 0.45 + 30)) for c in self.bg_rgb)
            for x in range(0, self.W, step):
                d.line((x, 0, x, self.H), fill=grid, width=1)
            for y in range(0, self.H, step):
                d.line((0, y, self.W, y), fill=grid, width=1)
        elif opt.map_style == "ghost":
            base = Image.alpha_composite(base.convert("RGBA"), self._ghost_layer(cam)).convert("RGB")
        cache.clear()
        cache[key] = base
        return base

    def _ghost_layer(self, cam: Camera) -> Image.Image:
        """Trace complète en filigrane, sert de « carte » sans réseau."""
        tl = self.tl
        alpha = int(255 * self.opt.ghost_opacity)
        col = hex_to_rgb(self.opt.overlay_color, (200, 210, 230))
        color = (col[0], col[1], col[2], alpha)
        segs: List[Segment] = []
        lw = max(0.8, self.opt.line_width * 0.5)
        for i in range(len(tl)):
            if tl.gap[i]:
                continue
            ax, ay = cam.to_screen(tl.sx[i], tl.sy[i], self.W, self.H)
            bx, by = cam.to_screen(tl.ex[i], tl.ey[i], self.W, self.H)
            if (max(ax, bx) < -50 or min(ax, bx) > self.W + 50
                    or max(ay, by) < -50 or min(ay, by) > self.H + 50):
                continue
            segs.append((ax, ay, bx, by, color, lw))
        out = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        if segs:
            res = draw_segments(segs, self.W, self.H, min(self.ss, 2))
            if res:
                layer, off = res
                out.alpha_composite(layer, dest=off)
        return out

    # ---------------------------------------------------------- habillage

    @staticmethod
    def _aa_circle(layer: Image.Image, cx: float, cy: float, r: float,
                   color: Tuple[int, int, int, int], ss: int = 4) -> None:
        """Disque antialiasé (Pillow ne lisse pas les ellipses)."""
        if r <= 0:
            return
        pad = 2
        x0, y0 = int(cx - r) - pad, int(cy - r) - pad
        size = int(2 * (r + pad)) + 2
        W, H = layer.size
        lx0, ly0 = max(0, x0), max(0, y0)
        lx1, ly1 = min(W, x0 + size), min(H, y0 + size)
        if size <= 0 or lx1 <= lx0 or ly1 <= ly0:
            return
        tile = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        dt = ImageDraw.Draw(tile)
        ox, oy = (cx - x0) * ss, (cy - y0) * ss
        dt.ellipse((ox - r * ss, oy - r * ss, ox + r * ss, oy + r * ss), fill=color)
        small = tile.reduce(ss).crop((lx0 - x0, ly0 - y0, lx1 - x0, ly1 - y0))
        layer.alpha_composite(small, dest=(lx0, ly0))

    def _text(self, d: ImageDraw.ImageDraw, xy, text, font, anchor="la",
              alpha=255, shadow=True):
        if not text:
            return
        x, y = xy
        if shadow:
            d.text((x + 2, y + 2), text, font=font, anchor=anchor, fill=(0, 0, 0, int(alpha * 0.55)))
        d.text((x, y), text, font=font, anchor=anchor,
               fill=(*self.overlay_rgb, alpha))

    def _format_clock(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, timezone.utc)
        fmt = self.opt.clock_format
        if fmt == "none":
            return ""
        if fmt == "year":
            return dt.strftime("%Y")
        if fmt == "month":
            return dt.strftime("%B %Y").capitalize()
        if fmt == "datetime":
            return dt.strftime("%d/%m/%Y %H:%M")
        return dt.strftime("%d/%m/%Y")

    def _distance_text(self, meters: float) -> str:
        if self.opt.units == "imperial":
            miles = meters / 1609.344
            return f"{miles:,.0f} mi".replace(",", " ") if miles >= 10 else f"{miles:.1f} mi"
        km = meters / 1000.0
        return f"{km:,.0f} km".replace(",", " ") if km >= 10 else f"{km:.1f} km"

    def _draw_overlays(self, layer: Image.Image, cam: Camera, i: int, frames: int,
                       progress: float, cur_time: float, dist_m: float,
                       trip_no: int, alpha: int = 255) -> None:
        opt = self.opt
        d = ImageDraw.Draw(layer)
        m = int(self.H * 0.045)

        if opt.show_clock and opt.clock_format != "none":
            self._text(d, (m, m), self._format_clock(cur_time), self.font_big, alpha=alpha)

        if opt.show_stats:
            y = m + self.font_big.size * 1.25
            self._text(d, (m, y), self._distance_text(dist_m), self.font_mid, alpha=alpha)
            self._text(d, (m, y + self.font_mid.size * 1.3),
                       f"{trip_no} trajet{'s' if trip_no > 1 else ''}",
                       self.font_small, alpha=alpha)

        bar_h = max(3, int(self.H * 0.006))
        bottom = self.H - m - (bar_h * 3 if opt.show_progress else 0)

        if opt.show_legend:
            if opt.color_mode == "mode":
                modes = self.legend_modes
                lh = self.font_small.size * 1.6
                y = bottom - lh * len(modes)
                for mode in modes:
                    rgb = hex_to_rgb(MODE_COLORS.get(mode, "#cccccc"))
                    r = self.font_small.size * 0.32
                    cy = y + self.font_small.size * 0.55
                    self._aa_circle(layer, m + r, cy, r, (*rgb, alpha))
                    self._text(d, (m + 3 * r + 6, y), MODE_LABELS.get(mode, mode),
                               self.font_small, alpha=alpha)
                    y += lh
            elif opt.color_mode in ("speed", "time"):
                bw = int(self.W * 0.17)
                bh = max(6, int(self.font_small.size * 0.42))
                y = bottom - self.font_small.size * 1.5 - bh
                for k in range(bw):
                    rgb = palette_color(opt.palette, k / max(bw - 1, 1))
                    d.line((m + k, y, m + k, y + bh), fill=(*rgb, alpha))
                left, right = (("lent", "rapide") if opt.color_mode == "speed"
                               else (self.legend_time[0], self.legend_time[1]))
                self._text(d, (m, y + bh + 3), left, self.font_small, alpha=alpha)
                self._text(d, (m + bw, y + bh + 3), right, self.font_small,
                           anchor="ra", alpha=alpha)

        if opt.show_scalebar:
            self._draw_scalebar(d, cam, alpha, bottom)

        if opt.show_progress:
            bw = self.W - 2 * m
            bh = bar_h
            by = min(self.H - m + bh, self.H - bh - 4)
            d.rectangle((m, by, m + bw, by + bh), fill=(*self.overlay_rgb, int(alpha * 0.18)))
            accent = hex_to_rgb(opt.line_color) if opt.color_mode == "single" else (255, 255, 255)
            d.rectangle((m, by, m + bw * progress, by + bh), fill=(*accent, alpha))

        if opt.watermark:
            self._text(d, (self.W - m, m), opt.watermark, self.font_small,
                       anchor="ra", alpha=int(alpha * 0.8))
        if opt.map_style == "tiles" and self.tile_attr:
            self._text(d, (self.W - 8, self.H - m - 2), self.tile_attr, self.font_small,
                       anchor="rd", alpha=int(alpha * 0.6))

    def _draw_scalebar(self, d: ImageDraw.ImageDraw, cam: Camera, alpha: int,
                       bottom: Optional[int] = None) -> None:
        lat = world_to_lonlat(cam.cx, cam.cy)[1]
        mpp = meters_per_pixel(lat, cam.scale)
        target_px = self.W * 0.16
        raw = mpp * target_px
        nice = 1.0
        for candidate in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
                          10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000):
            if candidate >= raw:
                nice = float(candidate)
                break
        else:
            nice = 5000000.0
        px = nice / mpp if mpp > 0 else 0
        if px < 20 or px > self.W * 0.5:
            return
        m = int(self.H * 0.045)
        x = self.W - m - px
        y = (bottom if bottom is not None else self.H - m) - self.font_small.size * 0.8
        d.line((x, y, x + px, y), fill=(*self.overlay_rgb, alpha), width=2)
        d.line((x, y - 5, x, y + 5), fill=(*self.overlay_rgb, alpha), width=2)
        d.line((x + px, y - 5, x + px, y + 5), fill=(*self.overlay_rgb, alpha), width=2)
        label = self._distance_text(nice)
        self._text(d, (x + px / 2, y - self.font_small.size * 1.35), label,
                   self.font_small, anchor="ma", alpha=alpha)

    def _draw_title_card(self, img: Image.Image, opacity: float) -> None:
        if opacity <= 0:
            return
        veil = Image.new("RGBA", (self.W, self.H), (*self.bg_rgb, int(210 * opacity)))
        d = ImageDraw.Draw(veil)
        a = int(255 * opacity)
        cy = self.H / 2
        self._text(d, (self.W / 2, cy - self.font_big.size * 0.8), self.opt.title,
                   self.font_big, anchor="ma", alpha=a)
        if self.opt.subtitle:
            self._text(d, (self.W / 2, cy + self.font_big.size * 0.5), self.opt.subtitle,
                       self.font_mid, anchor="ma", alpha=a)
        img.alpha_composite(veil)

    # ---------------------------------------------------------- rendu

    def run(self) -> Dict:
        opt = self.opt
        started = time.time()
        self.on_progress(0.01, "Préparation de la chronologie…")
        self.tl = build_timeline(self.trips, opt)
        tl = self.tl
        if len(tl) == 0:
            raise ValueError("Aucun trajet à afficher après application des filtres.")

        self.colors = self._build_colors(tl)
        self.legend_modes = sorted({tl.mode[i] for i in range(len(tl)) if not tl.gap[i]})
        self.legend_time = (
            datetime.fromtimestamp(tl.st[0], timezone.utc).strftime("%m/%Y"),
            datetime.fromtimestamp(tl.et[-1], timezone.utc).strftime("%m/%Y"))
        if opt.map_style == "tiles":
            try:
                self.tile_url, self.tile_attr = resolve_provider(opt.tile_provider, opt.tile_url)
            except TileError as exc:
                self.warnings.append(str(exc))
                self.tile_url = None

        frames = max(2, int(round(opt.duration * opt.fps)))
        intro = int(opt.title_seconds * opt.fps) if opt.title else 0
        hold0 = int(opt.hold_first_seconds * opt.fps)
        hold1 = int(opt.hold_last_seconds * opt.fps)
        anim = max(2, frames - intro - hold0 - hold1)
        frames = intro + hold0 + anim + hold1

        progress_of_frame: List[float] = []
        for i in range(frames):
            k = i - intro - hold0
            if k < 0:
                progress_of_frame.append(0.0)
            elif k >= anim:
                progress_of_frame.append(1.0)
            else:
                progress_of_frame.append(k / (anim - 1))

        self.on_progress(0.04, "Calcul des mouvements de caméra…")
        cams = plan_camera(tl, opt, frames, progress_of_frame)

        dist_prefix = [0.0]
        for i in range(len(tl)):
            dist_prefix.append(dist_prefix[-1] + (0.0 if tl.gap[i] else tl.dist[i]))

        static_cam = opt.camera == "fit_all"
        incremental = static_cam and opt.trail_mode == "cumulative"
        fade_weight = tl.total_weight * min(1.0, max(0.005, opt.fade_seconds / max(opt.duration, 0.5)))
        head_rgb = hex_to_rgb(opt.line_color, (255, 255, 255))

        out_path = os.path.join(self.workdir, f"trajets.{opt.container}")
        proc = self._start_ffmpeg(out_path, opt.fps, frames)
        assert proc.stdin is not None

        accum = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)) if incremental else None
        last_idx = 0
        bg_cache: Dict = {}
        poster_img = None
        blur_radius = max(1.5, self.H * 0.006 * opt.glow_strength)
        glow_div = 3 if max(self.W, self.H) >= 1400 else 2
        report_every = max(1, frames // 100)

        try:
            for i in range(frames):
                if self.should_cancel():
                    raise InterruptedError("Rendu annulé.")
                cam = cams[i]
                p = progress_of_frame[i]
                target = tl.total_weight * p
                idx, frac = tl.locate(target)
                hx = tl.sx[idx] + (tl.ex[idx] - tl.sx[idx]) * frac
                hy = tl.sy[idx] + (tl.ey[idx] - tl.sy[idx]) * frac
                cur_time = tl.st[idx] + (tl.et[idx] - tl.st[idx]) * frac
                dist_m = dist_prefix[idx] + (0.0 if tl.gap[idx] else tl.dist[idx] * frac)

                frame = self._background(cam, bg_cache).convert("RGBA")

                # --- calque de trace ---------------------------------------
                if incremental:
                    new_segs = self._segments_between(tl, cam, last_idx, idx, frac,
                                                      int(255 * opt.trail_opacity))
                    if new_segs:
                        bbox = visible_bbox(new_segs, int(opt.line_width * 3 + 8), self.W, self.H)
                        res = draw_segments(new_segs, self.W, self.H, self.ss, bbox)
                        if res:
                            layer, off = res
                            accum.alpha_composite(layer, dest=off)
                    last_idx = idx
                    trail_layer = accum
                    lo = max(0.0, target - fade_weight)
                    i0, f0 = tl.locate(lo)
                    active = self._segments_between(tl, cam, i0, idx, frac, 255,
                                                    comet=(opt.trail_mode == "comet"),
                                                    lo=lo, span=fade_weight)
                else:
                    if opt.trail_mode == "cumulative":
                        i0, lo = 0, 0.0
                        base_alpha = int(255 * opt.trail_opacity)
                        old = self._segments_between(tl, cam, 0, idx, frac, base_alpha)
                        res = draw_segments(old, self.W, self.H, self.ss) if old else None
                        trail_layer = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                        if res:
                            trail_layer.alpha_composite(res[0], dest=res[1])
                        lo = max(0.0, target - fade_weight)
                        i0, _ = tl.locate(lo)
                        active = self._segments_between(tl, cam, i0, idx, frac, 255,
                                                        comet=(opt.trail_mode == "comet"),
                                                        lo=lo, span=fade_weight)
                    else:
                        lo = max(0.0, target - fade_weight)
                        i0, _ = tl.locate(lo)
                        trail_layer = None
                        active = self._segments_between(tl, cam, i0, idx, frac, 255,
                                                        comet=True, lo=lo, span=fade_weight)

                # Traîne vive : calque limité à sa boîte englobante (le plein
                # écran en supersampling coûterait une allocation par image).
                active_res = None
                if active:
                    pad = int(opt.line_width * 3 + (blur_radius * 2 if opt.glow else 0) + 8)
                    active_res = draw_segments(active, self.W, self.H, self.ss,
                                               visible_bbox(active, pad, self.W, self.H))

                if opt.glow and active_res is not None:
                    layer, off = active_res
                    gw, gh = layer.size
                    if gw >= glow_div * 2 and gh >= glow_div * 2:
                        glow = layer.resize((gw // glow_div, gh // glow_div), Image.BILINEAR)
                        glow = glow.filter(ImageFilter.GaussianBlur(blur_radius / glow_div))
                        glow.putalpha(glow.getchannel("A").point(
                            lambda v: int(v * 0.75 * min(opt.glow_strength, 2.0))))
                        glow = glow.resize((gw, gh), Image.BILINEAR)
                    else:
                        glow = layer.filter(ImageFilter.GaussianBlur(blur_radius))
                    frame.alpha_composite(glow, dest=off)

                if trail_layer is not None:
                    frame.alpha_composite(trail_layer)
                if active_res is not None:
                    frame.alpha_composite(active_res[0], dest=active_res[1])

                # Tout l'habillage est peint sur un calque transparent : Pillow
                # n'applique pas l'alpha quand on dessine sur une image RGBA.
                ov = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                if opt.show_places and self.places:
                    self._draw_places(ov, cam, cur_time)

                if opt.head_dot and opt.head_size > 0:
                    sx, sy = cam.to_screen(hx, hy, self.W, self.H)
                    r = opt.head_size * (1.0 + (0.25 * math.sin(i * 0.35) if opt.head_pulse else 0))
                    self._aa_circle(ov, sx, sy, r * 2.2, (*head_rgb, 45))
                    self._aa_circle(ov, sx, sy, r, (255, 255, 255, 235))
                    self._aa_circle(ov, sx, sy, r * 0.5, (*head_rgb, 255))

                overlay_alpha = 255
                if opt.fade_in and i < opt.fps * 0.5:
                    overlay_alpha = int(255 * (i / max(opt.fps * 0.5, 1)))
                self._draw_overlays(ov, cam, i, frames, p, cur_time, dist_m,
                                    tl.trip[idx] + 1, alpha=overlay_alpha)
                frame.alpha_composite(ov)

                if intro and i < intro:
                    fade = 1.0
                    tail = max(1, int(intro * 0.35))
                    if i > intro - tail:
                        fade = 1.0 - (i - (intro - tail)) / tail
                    self._draw_title_card(frame, fade)
                if opt.outro_text and i >= frames - hold1 and hold1 > 0:
                    k = (i - (frames - hold1)) / max(hold1, 1)
                    outro = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                    self._text(ImageDraw.Draw(outro), (self.W / 2, self.H * 0.5),
                               self.opt.outro_text, self.font_mid, anchor="mm",
                               alpha=int(255 * min(1.0, k * 2)))
                    frame.alpha_composite(outro)
                if opt.fade_in and i < opt.fps * 0.4:
                    k = i / max(opt.fps * 0.4, 1)
                    black = Image.new("RGBA", (self.W, self.H), (0, 0, 0, int(255 * (1 - k))))
                    frame.alpha_composite(black)

                rgb = frame.convert("RGB")
                if i == frames - 1 or (poster_img is None and p >= 0.999):
                    poster_img = rgb.copy()
                proc.stdin.write(rgb.tobytes())

                if i % report_every == 0:
                    done = (i + 1) / frames
                    elapsed = time.time() - started
                    eta = elapsed / max(done, 1e-3) - elapsed
                    self.on_progress(0.05 + 0.85 * done,
                                     f"Image {i + 1}/{frames} — reste ≈ {int(eta)} s")
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            code = proc.wait()
            err = proc.stderr.read().decode("utf-8", "replace")[-4000:] if proc.stderr else ""

        if code != 0:
            raise RuntimeError(f"ffmpeg a échoué (code {code})\n{err}")

        self.on_progress(0.92, "Finalisation (audio, boucle, affiche)…")
        final_path = self._postprocess(out_path)

        poster_path = None
        if opt.make_poster and poster_img is not None:
            poster_path = os.path.join(self.workdir, "affiche.jpg")
            poster_img.save(poster_path, "JPEG", quality=88)
        thumb_path = os.path.join(self.workdir, "apercu.jpg")
        if poster_img is not None:
            thumb = poster_img.copy()
            thumb.thumbnail((640, 640))
            thumb.save(thumb_path, "JPEG", quality=80)
        else:
            thumb_path = None

        self.on_progress(1.0, "Terminé")
        return {
            "video": final_path,
            "poster": poster_path,
            "thumbnail": thumb_path,
            "frames": frames,
            "seconds": round(time.time() - started, 1),
            "warnings": self.warnings,
            "size_bytes": os.path.getsize(final_path),
        }

    # ------------------------------------------------------ segments

    def _segments_between(self, tl: Timeline, cam: Camera, i0: int, i1: int, frac: float,
                          alpha: int, comet: bool = False, lo: float = 0.0,
                          span: float = 1.0) -> List[Segment]:
        """Construit les segments écran entre deux index de la chronologie."""
        opt = self.opt
        W, H = self.W, self.H
        segs: List[Segment] = []
        margin = 60
        # bornes monde de la vue, pour éliminer les chunks hors champ
        half_w = (W / 2 + margin) / cam.scale
        half_h = (H / 2 + margin) / cam.scale
        vx0, vx1 = cam.cx - half_w, cam.cx + half_w
        vy0, vy1 = cam.cy - half_h, cam.cy + half_h

        n = min(i1 + 1, len(tl))
        i = max(0, i0)
        from .processing import CHUNK
        while i < n:
            chunk_id = i // CHUNK
            if chunk_id < len(tl.chunks):
                bx0, by0, bx1, by1 = tl.chunks[chunk_id]
                if bx1 < vx0 or bx0 > vx1 or by1 < vy0 or by0 > vy1:
                    i = (chunk_id + 1) * CHUNK
                    continue
            if tl.gap[i]:
                i += 1
                continue
            ax, ay = cam.to_screen(tl.sx[i], tl.sy[i], W, H)
            ex, ey = tl.ex[i], tl.ey[i]
            if i == i1 and frac < 1.0:
                ex = tl.sx[i] + (ex - tl.sx[i]) * frac
                ey = tl.sy[i] + (ey - tl.sy[i]) * frac
            bx, by = cam.to_screen(ex, ey, W, H)
            if not (max(ax, bx) < -margin or min(ax, bx) > W + margin
                    or max(ay, by) < -margin or min(ay, by) > H + margin):
                a = alpha
                lw = opt.line_width
                if comet and span > 0:
                    rel = (tl.cum[i] - lo) / span
                    rel = max(0.0, min(1.0, rel))
                    a = int(alpha * (0.15 + 0.85 * rel ** 1.6))
                    lw = opt.line_width * (0.55 + 0.45 * rel)
                r, g, b = self.colors[i]
                segs.append((ax, ay, bx, by, (r, g, b, a), lw))
            i += 1
        return segs

    def _draw_places(self, layer: Image.Image, cam: Camera, cur_time: float) -> None:
        d = ImageDraw.Draw(layer)
        r = max(2.0, self.opt.line_width * 0.9)
        col = hex_to_rgb(self.opt.overlay_color)
        for p in self.places:
            if p.start > cur_time:
                continue
            wx, wy = lonlat_to_world(p.lon, p.lat)
            x, y = cam.to_screen(wx, wy, self.W, self.H)
            if -20 <= x <= self.W + 20 and -20 <= y <= self.H + 20:
                d.ellipse((x - r, y - r, x + r, y + r), fill=(*col, 130))

    # ------------------------------------------------------ encodage

    def _start_ffmpeg(self, out_path: str, fps: int, frames: int) -> subprocess.Popen:
        opt = self.opt
        cmd = [ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{self.W}x{self.H}", "-r", str(fps), "-i", "-"]
        if opt.container == "gif":
            cmd += ["-vf", "split[a][b];[a]palettegen=stats_mode=diff[p];"
                           "[b][p]paletteuse=dither=bayer:bayer_scale=3",
                    "-loop", "0"]
        elif opt.container == "webm":
            cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf",
                    str(QUALITY_CRF[opt.quality] + 8), "-row-mt", "1",
                    "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", "libx264", "-preset", QUALITY_PRESET[opt.quality],
                    "-crf", str(QUALITY_CRF[opt.quality]), "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart"]
        cmd.append(out_path)
        log.info("ffmpeg: %s", " ".join(cmd))
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)

    def _postprocess(self, path: str) -> str:
        """Boomerang et piste audio (post-traitements ffmpeg)."""
        opt = self.opt
        current = path
        if opt.loop_video and opt.container != "gif":
            if opt.duration <= 90:
                looped = os.path.join(self.workdir, f"boucle.{opt.container}")
                cmd = [ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
                       "-i", current, "-filter_complex",
                       "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[out]",
                       "-map", "[out]", "-pix_fmt", "yuv420p",
                       "-crf", str(QUALITY_CRF[opt.quality])]
                cmd += (["-c:v", "libx264", "-preset", QUALITY_PRESET[opt.quality]]
                        if opt.container == "mp4" else
                        ["-c:v", "libvpx-vp9", "-b:v", "0", "-row-mt", "1"])
                cmd.append(looped)
                if self._run(cmd):
                    current = looped
            else:
                self.warnings.append("Boomerang ignoré : la vidéo dépasse 90 s.")

        if self.audio_path and os.path.exists(self.audio_path) and opt.container != "gif":
            with_audio = os.path.join(self.workdir, f"final.{opt.container}")
            afilter = "afade=t=out:st=%.2f:d=2" % max(0.0, opt.duration - 2) if opt.audio_fade else "anull"
            cmd = [ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
                   "-i", current, "-stream_loop", "-1", "-i", self.audio_path,
                   "-filter_complex", f"[1:a]{afilter}[a]",
                   "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                   "-c:a", "libopus" if opt.container == "webm" else "aac",
                   "-b:a", "160k", "-shortest", with_audio]
            if self._run(cmd):
                current = with_audio
            else:
                self.warnings.append("La piste audio n'a pas pu être ajoutée.")
        return current

    def _run(self, cmd: List[str]) -> bool:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.warnings.append(f"Post-traitement ignoré : {exc}")
            return False
        if res.returncode != 0:
            log.warning("ffmpeg post-traitement: %s", res.stderr.decode("utf-8", "replace")[-800:])
            return False
        return True
