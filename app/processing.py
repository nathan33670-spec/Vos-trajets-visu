"""Filtrage des trajets, statistiques et construction de la chronologie vidéo."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .geo import haversine_m, lonlat_to_world, rdp
from .models import MODE_MAX_SPEED, Place, RenderOptions, Trip

CHUNK = 512


# ------------------------------------------------------------------ filtres

def trip_distance_m(trip: Trip) -> float:
    total = 0.0
    for i in range(1, len(trip.times)):
        total += haversine_m(trip.lats[i - 1], trip.lons[i - 1], trip.lats[i], trip.lons[i])
    return total


def _clean_outliers(trip: Trip, max_kmh: float) -> Optional[Trip]:
    """Retire les points impliquant une vitesse impossible."""
    keep_t, keep_a, keep_o = [trip.times[0]], [trip.lats[0]], [trip.lons[0]]
    limit = max_kmh / 3.6
    for i in range(1, len(trip.times)):
        dt = trip.times[i] - keep_t[-1]
        if dt <= 0:
            continue
        d = haversine_m(keep_a[-1], keep_o[-1], trip.lats[i], trip.lons[i])
        if d / dt > limit and d > 500:
            continue
        keep_t.append(trip.times[i])
        keep_a.append(trip.lats[i])
        keep_o.append(trip.lons[i])
    if len(keep_t) < 2:
        return None
    trip.times, trip.lats, trip.lons = keep_t, keep_a, keep_o
    return trip


def _day_bounds(date_str: str, end: bool = False) -> Optional[float]:
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    if end:
        return dt.timestamp() + 86400.0
    return dt.timestamp()


def filter_trips(trips: List[Trip], places: List[Place],
                 opt: RenderOptions) -> Tuple[List[Trip], List[Place], List[str]]:
    notes: List[str] = []
    t_min = _day_bounds(opt.date_start) if opt.date_start else None
    t_max = _day_bounds(opt.date_end, end=True) if opt.date_end else None
    modes = set(opt.modes) if opt.modes else None
    bbox = opt.bbox if (opt.bbox and len(opt.bbox) == 4) else None
    priv = None
    if opt.privacy_radius_m > 0 and opt.privacy_lat is not None and opt.privacy_lon is not None:
        priv = (opt.privacy_lat, opt.privacy_lon, opt.privacy_radius_m)

    out: List[Trip] = []
    dropped = {"date": 0, "mode": 0, "bbox": 0, "distance": 0, "duree": 0, "vide": 0}
    for trip in trips:
        if modes is not None and trip.mode not in modes:
            dropped["mode"] += 1
            continue
        if t_min is not None and trip.end < t_min:
            dropped["date"] += 1
            continue
        if t_max is not None and trip.start > t_max:
            dropped["date"] += 1
            continue
        cur = Trip(mode=trip.mode, times=list(trip.times), lats=list(trip.lats),
                   lons=list(trip.lons), source=trip.source, label=trip.label)
        if priv:
            keep = [i for i in range(len(cur.times))
                    if haversine_m(cur.lats[i], cur.lons[i], priv[0], priv[1]) > priv[2]]
            if len(keep) < 2:
                dropped["vide"] += 1
                continue
            cur.times = [cur.times[i] for i in keep]
            cur.lats = [cur.lats[i] for i in keep]
            cur.lons = [cur.lons[i] for i in keep]
        if bbox:
            inside = any(bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]
                         for la, lo in zip(cur.lats, cur.lons))
            if not inside:
                dropped["bbox"] += 1
                continue
        if opt.drop_outliers:
            cleaned = _clean_outliers(cur, MODE_MAX_SPEED.get(cur.mode, 400.0))
            if cleaned is None:
                dropped["vide"] += 1
                continue
            cur = cleaned
        dist_km = trip_distance_m(cur) / 1000.0
        if dist_km < opt.min_trip_km:
            dropped["distance"] += 1
            continue
        if opt.max_trip_km > 0 and dist_km > opt.max_trip_km:
            dropped["distance"] += 1
            continue
        if (cur.end - cur.start) < opt.min_trip_seconds:
            dropped["duree"] += 1
            continue
        out.append(cur)

    kept_places = []
    if opt.show_places:
        for p in places:
            if t_min is not None and p.end < t_min:
                continue
            if t_max is not None and p.start > t_max:
                continue
            if priv and haversine_m(p.lat, p.lon, priv[0], priv[1]) <= priv[2]:
                continue
            if bbox and not (bbox[0] <= p.lon <= bbox[2] and bbox[1] <= p.lat <= bbox[3]):
                continue
            kept_places.append(p)

    for key, n in dropped.items():
        if n:
            notes.append(f"{n} trajet(s) écarté(s) — filtre « {key} »")
    out.sort(key=lambda t: t.start)
    return out, kept_places, notes


# ------------------------------------------------------------- statistiques

def summarize(trips: List[Trip], places: List[Place]) -> Dict:
    per_mode: Dict[str, Dict[str, float]] = {}
    total_m = 0.0
    npoints = 0
    lat_min = lon_min = 1e9
    lat_max = lon_max = -1e9
    for t in trips:
        d = trip_distance_m(t)
        total_m += d
        npoints += len(t.times)
        m = per_mode.setdefault(t.mode, {"count": 0, "distance_km": 0.0, "hours": 0.0})
        m["count"] += 1
        m["distance_km"] += d / 1000.0
        m["hours"] += (t.end - t.start) / 3600.0
        lat_min = min(lat_min, min(t.lats)); lat_max = max(lat_max, max(t.lats))
        lon_min = min(lon_min, min(t.lons)); lon_max = max(lon_max, max(t.lons))
    span = (trips[0].start, trips[-1].end) if trips else (0.0, 0.0)
    return {
        "trips": len(trips),
        "points": npoints,
        "places": len(places),
        "distance_km": round(total_m / 1000.0, 1),
        "per_mode": {k: {"count": v["count"],
                         "distance_km": round(v["distance_km"], 1),
                         "hours": round(v["hours"], 1)} for k, v in sorted(per_mode.items())},
        "start": span[0], "end": span[1],
        "start_date": datetime.fromtimestamp(span[0], timezone.utc).strftime("%Y-%m-%d") if trips else None,
        "end_date": datetime.fromtimestamp(span[1], timezone.utc).strftime("%Y-%m-%d") if trips else None,
        "bbox": [lon_min, lat_min, lon_max, lat_max] if trips else None,
    }


# --------------------------------------------------------------- timeline

@dataclass
class Timeline:
    """Chronologie « pas à pas » prête pour le rendu image par image."""
    sx: List[float] = field(default_factory=list)
    sy: List[float] = field(default_factory=list)
    ex: List[float] = field(default_factory=list)
    ey: List[float] = field(default_factory=list)
    st: List[float] = field(default_factory=list)
    et: List[float] = field(default_factory=list)
    trip: List[int] = field(default_factory=list)
    gap: List[bool] = field(default_factory=list)
    speed: List[float] = field(default_factory=list)   # km/h
    dist: List[float] = field(default_factory=list)    # m
    mode: List[str] = field(default_factory=list)
    cum: List[float] = field(default_factory=list)     # poids cumulés (len = n+1)
    chunks: List[Tuple[float, float, float, float]] = field(default_factory=list)
    trip_bounds: List[Tuple[float, float, float, float]] = field(default_factory=list)
    trip_range: List[Tuple[int, int]] = field(default_factory=list)
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    total_weight: float = 0.0
    total_distance_m: float = 0.0

    def __len__(self) -> int:
        return len(self.sx)

    def locate(self, target: float) -> Tuple[int, float]:
        """Index du pas et fraction [0,1] pour un poids cumulé donné."""
        n = len(self.sx)
        if n == 0:
            return 0, 0.0
        i = bisect.bisect_right(self.cum, target) - 1
        i = max(0, min(i, n - 1))
        w = self.cum[i + 1] - self.cum[i]
        frac = 0.0 if w <= 0 else (target - self.cum[i]) / w
        return i, max(0.0, min(1.0, frac))


def build_timeline(trips: List[Trip], opt: RenderOptions,
                   simplify_px: float = 0.6) -> Timeline:
    """Transforme les trajets en une suite de segments pondérés."""
    tl = Timeline()
    if not trips:
        return tl

    # Tolérance de simplification exprimée en unités monde à l'échelle de sortie.
    world_pts_all: List[List[Tuple[float, float, float]]] = []
    minx = miny = 1e9
    maxx = maxy = -1e9
    for trip in trips:
        pts = []
        for t, la, lo in zip(trip.times, trip.lats, trip.lons):
            x, y = lonlat_to_world(lo, la)
            pts.append((x, y, t))
        world_pts_all.append(pts)
        for x, y, _ in pts:
            minx = min(minx, x); maxx = max(maxx, x)
            miny = min(miny, y); maxy = max(maxy, y)
    span = max(maxx - minx, maxy - miny, 1e-9)
    # échelle approximative si tout tient dans l'image
    approx_scale = max(opt.width, opt.height) / span
    eps = (simplify_px / approx_scale) if approx_scale > 0 else 0.0
    if opt.camera in ("follow", "auto", "trip"):
        eps *= 0.25  # on zoome : il faut garder plus de détail

    gap_cap = max(opt.gap_max_seconds, 1.0)
    prev_end: Optional[Tuple[float, float, float]] = None
    per_trip_steps: List[List[int]] = []

    for ti, (trip, pts) in enumerate(zip(trips, world_pts_all)):
        simple = rdp(pts, eps) if len(pts) > 3 else pts
        if len(simple) < 2:
            simple = pts
        start_idx = len(tl.sx)
        if prev_end is not None:
            tl.sx.append(prev_end[0]); tl.sy.append(prev_end[1])
            tl.ex.append(simple[0][0]); tl.ey.append(simple[0][1])
            tl.st.append(prev_end[2]); tl.et.append(simple[0][2])
            tl.trip.append(ti); tl.gap.append(True)
            tl.speed.append(0.0); tl.dist.append(0.0); tl.mode.append(trip.mode)
        for a, b in zip(simple, simple[1:]):
            dt = max(b[2] - a[2], 0.001)
            # distance réelle approchée à partir des lat/lon d'origine
            d = _world_distance_m(a, b)
            tl.sx.append(a[0]); tl.sy.append(a[1])
            tl.ex.append(b[0]); tl.ey.append(b[1])
            tl.st.append(a[2]); tl.et.append(b[2])
            tl.trip.append(ti); tl.gap.append(False)
            tl.speed.append(d / dt * 3.6); tl.dist.append(d); tl.mode.append(trip.mode)
        per_trip_steps.append([start_idx, len(tl.sx)])
        prev_end = simple[-1]

        xs = [p[0] for p in simple]; ys = [p[1] for p in simple]
        tl.trip_bounds.append((min(xs), min(ys), max(xs), max(ys)))
        tl.trip_range.append((start_idx, len(tl.sx)))

    n = len(tl.sx)
    ntrips = len(trips)
    weights: List[float] = [0.0] * n
    for i in range(n):
        dt = max(tl.et[i] - tl.st[i], 0.0)
        if opt.time_mode == "real":
            w = dt
        elif opt.time_mode == "compress":
            w = min(dt, gap_cap)
        elif opt.time_mode == "distance":
            w = 0.0 if tl.gap[i] else max(tl.dist[i], 1.0)
        else:  # equal : chaque trajet occupe la même durée
            w = 0.0 if tl.gap[i] else max(dt, 1.0)
        weights[i] = max(w, 0.0)

    if opt.time_mode == "equal":
        for ti, (a, b) in enumerate(tl.trip_range):
            sub = sum(weights[a:b]) or 1.0
            for i in range(a, b):
                weights[i] = weights[i] / sub / max(ntrips, 1)
    if opt.time_mode in ("distance", "equal"):
        # petite respiration entre deux trajets pour que la coupure se lise
        total_move = sum(weights) or 1.0
        gap_w = total_move * 0.01 / max(ntrips, 1)
        for i in range(n):
            if tl.gap[i]:
                weights[i] = gap_w

    cum = [0.0]
    acc = 0.0
    for w in weights:
        acc += w
        cum.append(acc)
    tl.cum = cum
    tl.total_weight = acc if acc > 0 else 1.0
    tl.total_distance_m = sum(d for d, g in zip(tl.dist, tl.gap) if not g)
    tl.bounds = (minx, miny, maxx, maxy)

    for c0 in range(0, n, CHUNK):
        c1 = min(c0 + CHUNK, n)
        xs = tl.sx[c0:c1] + tl.ex[c0:c1]
        ys = tl.sy[c0:c1] + tl.ey[c0:c1]
        tl.chunks.append((min(xs), min(ys), max(xs), max(ys)))
    return tl


def dense_bounds(tl: Timeline) -> Tuple[float, float, float, float]:
    """Emprise des trajets habituels, les destinations exceptionnelles exclues.

    On raisonne par trajet (pas par coordonnée) pour garder un cadre cohérent.
    Est exceptionnel un trajet **beaucoup** plus lointain que le précédent
    (au moins quatre fois), et seuls les tout derniers trajets du classement
    peuvent l'être : partir dix fois aux États-Unis, ce n'est plus une
    exception, et la vue doit alors les inclure.
    """
    boxes = tl.trip_bounds
    n = len(boxes)
    if n < 8:
        return tl.bounds
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    mx = sorted(c[0] for c in centres)[n // 2]
    my = sorted(c[1] for c in centres)[n // 2]
    dists = sorted((max(abs(x - mx), abs(y - my)), i) for i, (x, y) in enumerate(centres))
    values = [d for d, _ in dists]

    budget = max(2, int(n * 0.03))
    cut_at = None
    for i in range(n - 1, max(n - 1 - budget, 0), -1):
        if values[i - 1] > 0 and values[i] > values[i - 1] * 4.0:
            cut_at = i
    if cut_at is None:
        return tl.bounds
    kept = [boxes[i] for _, i in dists[:cut_at]]
    if not kept:
        return tl.bounds
    return (min(b[0] for b in kept), min(b[1] for b in kept),
            max(b[2] for b in kept), max(b[3] for b in kept))


def wide_bounds(tl: Timeline, max_ratio: float = 3.5) -> Tuple[float, float, float, float]:
    """Cadrage du plan large final : tout, sauf si une destination isolée
    réduisait le reste à un point — auquel cas on garde la zone fréquentée."""
    full = tl.bounds
    dense = dense_bounds(tl)
    span_full = max(full[2] - full[0], full[3] - full[1], 1e-9)
    span_dense = max(dense[2] - dense[0], dense[3] - dense[1], 1e-9)
    return full if span_full / span_dense <= max_ratio else dense


def _world_distance_m(a, b) -> float:
    from .geo import world_to_lonlat
    lon1, lat1 = world_to_lonlat(a[0], a[1])
    lon2, lat2 = world_to_lonlat(b[0], b[1])
    return haversine_m(lat1, lon1, lat2, lon2)
