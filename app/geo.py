"""Projection Web Mercator et utilitaires géographiques."""
from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

EARTH_RADIUS_M = 6371008.8
MAX_LAT = 85.05112878


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def lonlat_to_world(lon: float, lat: float) -> Tuple[float, float]:
    """Projette (lon, lat) vers des coordonnées monde normalisées [0,1]x[0,1]."""
    lat = clamp(lat, -MAX_LAT, MAX_LAT)
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)
    return x, y


def world_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    lon = x * 360.0 - 180.0
    n = math.pi * (1.0 - 2.0 * y)
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en mètres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def great_circle_points(lat1, lon1, lat2, lon2, n: int) -> list:
    """Points intermédiaires sur l'orthodromie (utile pour les vols)."""
    d = haversine_m(lat1, lon1, lat2, lon2) / EARTH_RADIUS_M
    if d < 1e-9 or n < 2:
        return [(lat1, lon1), (lat2, lon2)]
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    out = []
    for i in range(n):
        f = i / (n - 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


def bounds_of_world(points: Iterable[Sequence[float]]) -> Tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) d'une liste de points monde."""
    minx = miny = 1e9
    maxx = maxy = -1e9
    for x, y in points:
        if x < minx:
            minx = x
        if x > maxx:
            maxx = x
        if y < miny:
            miny = y
        if y > maxy:
            maxy = y
    if minx > maxx:
        return 0.0, 0.0, 1.0, 1.0
    return minx, miny, maxx, maxy


def scale_for_bounds(bounds, width: int, height: int, padding: float = 0.08,
                     max_zoom: float = 19.0) -> float:
    """Échelle (pixels par unité monde) pour faire tenir `bounds` dans width x height."""
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    usable_w = max(width * (1.0 - 2 * padding), 16)
    usable_h = max(height * (1.0 - 2 * padding), 16)
    scale = min(usable_w / dx, usable_h / dy)
    return min(scale, 256.0 * (2 ** max_zoom))


def scale_to_zoom(scale: float) -> float:
    return math.log2(max(scale, 1e-6) / 256.0)


def zoom_to_scale(zoom: float) -> float:
    return 256.0 * (2.0 ** zoom)


def meters_per_pixel(lat: float, scale: float) -> float:
    return (2 * math.pi * EARTH_RADIUS_M * math.cos(math.radians(clamp(lat, -MAX_LAT, MAX_LAT)))) / scale


def rdp(points: list, epsilon: float) -> list:
    """Simplification Ramer-Douglas-Peucker sur des points (x, y) monde."""
    if len(points) < 3 or epsilon <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x1, y1 = points[start][0], points[start][1]
        x2, y2 = points[end][0], points[end][1]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best_i, best_d = -1, 0.0
        for i in range(start + 1, end):
            px, py = points[i][0], points[i][1]
            if norm < 1e-12:
                d = math.hypot(px - x1, py - y1)
            else:
                d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
            if d > best_d:
                best_d, best_i = d, i
        if best_d > epsilon and best_i > 0:
            keep[best_i] = True
            stack.append((start, best_i))
            stack.append((best_i, end))
    return [p for p, k in zip(points, keep) if k]
