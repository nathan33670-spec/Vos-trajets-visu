"""Fond de carte : téléchargement, cache et assemblage des tuiles raster."""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import socket
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from PIL import Image

from .models import TILE_PROVIDERS

log = logging.getLogger(__name__)

TILE_SIZE = 256
USER_AGENT = os.environ.get(
    "TILE_USER_AGENT",
    "vos-trajets-visu/1.0 (auto-hébergé ; https://github.com/nathan33670-spec/vos-trajets-visu)")
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_TILE_HOSTS", "0") == "1"
TILES_ENABLED = os.environ.get("TILES_ENABLED", "1") == "1"
MAX_TILE_ZOOM = int(os.environ.get("MAX_TILE_ZOOM", "17"))


class TileError(Exception):
    pass


def resolve_provider(provider: str, custom_url: Optional[str]) -> Tuple[str, str]:
    """Retourne (url_template, attribution)."""
    info = TILE_PROVIDERS.get(provider) or TILE_PROVIDERS["carto_dark"]
    url = custom_url.strip() if (provider == "custom" and custom_url) else info["url"]
    if not url:
        raise TileError("URL de tuiles manquante.")
    _check_url(url)
    return url, info.get("attribution", "")


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TileError("Seules les URL http(s) sont autorisées pour les tuiles.")
    host = parsed.hostname or ""
    if not host:
        raise TileError("URL de tuiles invalide.")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise TileError(f"Hôte de tuiles introuvable : {host} ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise TileError(
                f"Hôte de tuiles refusé ({ip}) : adresse privée. "
                "Définissez ALLOW_PRIVATE_TILE_HOSTS=1 pour un serveur de tuiles local.")


class TileCache:
    """Cache disque + mémoire, avec téléchargement parallèle borné."""

    def __init__(self, cache_dir: str, mem_size: int = 512, workers: int = 6):
        self.cache_dir = cache_dir
        self.mem: "OrderedDict[str, Image.Image]" = OrderedDict()
        self.mem_size = mem_size
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.failures = 0
        self.downloads = 0
        os.makedirs(cache_dir, exist_ok=True)

    def close(self) -> None:
        self.pool.shutdown(wait=False)

    def _disk_path(self, key: str, z: int, x: int, y: int) -> str:
        return os.path.join(self.cache_dir, key, str(z), str(x), f"{y}.png")

    def get(self, url_tpl: str, key: str, z: int, x: int, y: int) -> Optional[Image.Image]:
        n = 1 << z
        if not (0 <= y < n):
            return None
        x %= n
        mem_key = f"{key}/{z}/{x}/{y}"
        with self.lock:
            img = self.mem.get(mem_key)
            if img is not None:
                self.mem.move_to_end(mem_key)
                return img
        path = self._disk_path(key, z, x, y)
        img = None
        if os.path.exists(path):
            try:
                img = Image.open(path).convert("RGB")
            except OSError:
                try:
                    os.remove(path)
                except OSError:
                    pass
                img = None
        if img is None:
            if not TILES_ENABLED:
                return None
            img = self._download(url_tpl, path, z, x, y)
        if img is not None:
            with self.lock:
                self.mem[mem_key] = img
                while len(self.mem) > self.mem_size:
                    self.mem.popitem(last=False)
        return img

    def _download(self, url_tpl: str, path: str, z: int, x: int, y: int) -> Optional[Image.Image]:
        url = (url_tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
               .replace("{s}", "abc"[(x + y) % 3]).replace("{r}", ""))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "image/*"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read(4 * 1024 * 1024)
            img = Image.open(__import__("io").BytesIO(data)).convert("RGB")
        except Exception as exc:  # réseau, HTTP, image illisible
            self.failures += 1
            if self.failures <= 3:
                log.warning("Tuile %s indisponible : %s", url, exc)
            return None
        self.downloads += 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            img.save(path, "PNG")
        except OSError:
            pass
        return img

    def prefetch(self, url_tpl: str, key: str, coords) -> None:
        for z, x, y in coords:
            self.pool.submit(self.get, url_tpl, key, z, x, y)


def choose_zoom(scale: float) -> int:
    """Niveau de zoom entier le plus proche de l'échelle demandée."""
    z = math.log2(max(scale, 1.0) / TILE_SIZE)
    return max(0, min(MAX_TILE_ZOOM, int(round(z))))


def render_tile_background(cache: TileCache, url_tpl: str, key: str,
                           cx: float, cy: float, scale: float,
                           width: int, height: int,
                           grayscale: bool = False, opacity: float = 1.0,
                           base_color: Tuple[int, int, int] = (16, 20, 28)) -> Image.Image:
    """Compose le fond de carte pour une vue (centre monde, échelle px/unité)."""
    canvas = Image.new("RGB", (width, height), base_color)
    z = choose_zoom(scale)
    n = 1 << z
    tile_px = scale / n  # taille à l'écran d'une tuile
    if tile_px <= 0:
        return canvas
    left = cx - (width / 2) / scale
    top = cy - (height / 2) / scale
    x0 = math.floor(left * n)
    y0 = math.floor(top * n)
    x1 = math.floor((left + width / scale) * n)
    y1 = math.floor((top + height / scale) * n)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 400:
        return canvas  # vue trop large : on garde le fond uni

    coords = [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    cache.prefetch(url_tpl, key, coords)
    target = max(1, int(round(tile_px)))
    for z_, x, y in coords:
        img = cache.get(url_tpl, key, z_, x, y)
        if img is None:
            continue
        if img.size != (target, target):
            img = img.resize((target, target), Image.BILINEAR)
        px = int(round((x / n - left) * scale))
        py = int(round((y / n - top) * scale))
        canvas.paste(img, (px, py))

    if grayscale:
        canvas = canvas.convert("L").convert("RGB")
    if opacity < 1.0:
        canvas = Image.blend(Image.new("RGB", (width, height), base_color), canvas,
                             max(0.0, min(1.0, opacity)))
    return canvas
