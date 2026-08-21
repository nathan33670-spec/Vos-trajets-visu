"""Lecture des exports Google « Vos trajets » / Timeline (et formats voisins).

Formats reconnus :
  * Semantic Location History : {"timelineObjects": [{"activitySegment"|"placeVisit": ...}]}
  * Timeline moderne (Android/iOS 2024+) : {"semanticSegments": [...]} ou liste racine
  * Records.json / Historique des positions : {"locations": [{latitudeE7, ...}]}
  * GeoJSON (LineString / MultiLineString / Feature[s]) et GPX
  * Archives .zip de Takeout (parcourues récursivement)
Un mode « dernier recours » scanne récursivement tout JSON inconnu à la recherche
de couples latitude/longitude datés.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .geo import haversine_m
from .models import Place, Trip

log = logging.getLogger(__name__)

MAX_MEMBER_BYTES = 400 * 1024 * 1024

ACTIVITY_MAP = {
    "WALKING": "walk", "ON_FOOT": "walk", "RUNNING": "walk", "HIKING": "walk",
    "STILL": "other", "TILTING": "other", "UNKNOWN_ACTIVITY_TYPE": "other",
    "CYCLING": "bike", "ON_BICYCLE": "bike", "IN_BICYCLE": "bike",
    "IN_PASSENGER_VEHICLE": "car", "IN_VEHICLE": "car", "DRIVING": "car",
    "MOTORCYCLING": "car", "IN_TAXI": "car", "IN_CAR": "car",
    "IN_BUS": "transit", "IN_TRAIN": "transit", "IN_SUBWAY": "transit",
    "IN_TRAM": "transit", "IN_LIGHT_RAIL": "transit", "IN_CABLECAR": "transit",
    "IN_FUNICULAR": "transit", "IN_GONDOLA_LIFT": "transit", "TRANSIT": "transit",
    "IN_FERRY": "boat", "SAILING": "boat", "BOATING": "boat", "KAYAKING": "boat",
    "FLYING": "plane", "IN_AIRPLANE": "plane",
    "SKIING": "other", "SKATING": "other", "SNOWBOARDING": "other",
}

_LATLNG_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*°?"
)


class ParseError(Exception):
    pass


# --------------------------------------------------------------- primitives

def parse_timestamp(value: Any) -> Optional[float]:
    """Convertit un horodatage Google (ISO 8601 ou epoch ms) en epoch secondes UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # heuristique : ms si trop grand pour être des secondes
        if v > 1e12:
            v /= 1000.0
        elif v > 1e10:
            v /= 1000.0
        return v
    if isinstance(value, dict):
        for key in ("timestamp", "timestampMs", "startTimestamp", "startTimestampMs"):
            if key in value:
                return parse_timestamp(value[key])
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return parse_timestamp(int(s))
    s = s.replace("Z", "+00:00")
    # tronque les fractions de seconde trop longues pour fromisoformat (py<3.11)
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _e7(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1e7 if abs(v) > 360 else v


def parse_latlng(obj: Any) -> Optional[Tuple[float, float]]:
    """Extrait (lat, lon) d'une des multiples représentations Google."""
    if obj is None:
        return None
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("geo:"):
            s = s[4:]
        m = _LATLNG_RE.search(s)
        if not m:
            return None
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if _valid(lat, lon) else None
    if isinstance(obj, dict):
        for lat_key, lon_key in (("latitudeE7", "longitudeE7"), ("latE7", "lngE7"),
                                 ("latitude", "longitude"), ("lat", "lng"),
                                 ("lat", "lon"), ("lat", "long")):
            if lat_key in obj and lon_key in obj:
                lat, lon = _e7(obj[lat_key]), _e7(obj[lon_key])
                if lat is not None and lon is not None and _valid(lat, lon):
                    return lat, lon
        for key in ("latLng", "point", "location", "placeLocation", "center",
                    "coordinate", "coordinates"):
            if key in obj:
                got = parse_latlng(obj[key])
                if got:
                    return got
        if "topCandidate" in obj:
            return parse_latlng(obj["topCandidate"])
    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        try:
            a, b = float(obj[0]), float(obj[1])
        except (TypeError, ValueError):
            return None
        # GeoJSON = [lon, lat]
        if _valid(b, a):
            return b, a
        if _valid(a, b):
            return a, b
    return None


def _valid(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0 and lon == 0)


def _activity_mode(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("type") or raw.get("activityType") or raw.get("topCandidate")
        if isinstance(raw, dict):
            raw = raw.get("type")
    if not isinstance(raw, str):
        return "other"
    return ACTIVITY_MAP.get(raw.strip().upper(), "other")


# --------------------------------------------------------------- collecteur

class Collector:
    def __init__(self) -> None:
        self.trips: List[Trip] = []
        self.places: List[Place] = []
        self.warnings: List[str] = []
        self.formats: Dict[str, int] = {}

    def note(self, fmt: str, n: int = 1) -> None:
        self.formats[fmt] = self.formats.get(fmt, 0) + n

    def add_trip(self, mode: str, pts: List[Tuple[float, float, float]], source: str,
                 fmt: str = "", label: str = "") -> None:
        """pts = [(t, lat, lon)] ; ignore les trajets dégénérés."""
        pts = [p for p in pts if p[0] is not None and _valid(p[1], p[2])]
        pts.sort(key=lambda p: p[0])
        dedup: List[Tuple[float, float, float]] = []
        for p in pts:
            if dedup and abs(p[0] - dedup[-1][0]) < 1e-6 and abs(p[1] - dedup[-1][1]) < 1e-9 \
               and abs(p[2] - dedup[-1][2]) < 1e-9:
                continue
            if dedup and p[0] <= dedup[-1][0]:
                p = (dedup[-1][0] + 0.001, p[1], p[2])
            dedup.append(p)
        if len(dedup) < 2:
            return
        trip = Trip(mode=mode or "other",
                    times=[p[0] for p in dedup],
                    lats=[p[1] for p in dedup],
                    lons=[p[2] for p in dedup],
                    source=source, label=label, fmt=fmt)
        self.trips.append(trip)


# --------------------------------------------------------------- formats

def _parse_timeline_objects(objs: Iterable[dict], c: Collector, source: str) -> None:
    n = 0
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        seg = obj.get("activitySegment")
        if isinstance(seg, dict):
            _parse_activity_segment(seg, c, source)
            n += 1
            continue
        visit = obj.get("placeVisit")
        if isinstance(visit, dict):
            loc = visit.get("location") or {}
            ll = parse_latlng(loc)
            dur = visit.get("duration") or {}
            t0 = parse_timestamp(dur.get("startTimestamp") or dur.get("startTimestampMs"))
            t1 = parse_timestamp(dur.get("endTimestamp") or dur.get("endTimestampMs"))
            if ll and t0:
                c.places.append(Place(name=str(loc.get("name") or loc.get("address") or ""),
                                      lat=ll[0], lon=ll[1], start=t0, end=t1 or t0))
            n += 1
    if n:
        c.note("semantic_location_history", n)


def _parse_activity_segment(seg: dict, c: Collector, source: str) -> None:
    dur = seg.get("duration") or {}
    t0 = parse_timestamp(dur.get("startTimestamp") or dur.get("startTimestampMs"))
    t1 = parse_timestamp(dur.get("endTimestamp") or dur.get("endTimestampMs"))
    mode = _activity_mode(seg.get("activityType") or seg.get("activities"))
    start = parse_latlng(seg.get("startLocation"))
    end = parse_latlng(seg.get("endLocation"))

    pts: List[Tuple[float, float, float]] = []
    raw = ((seg.get("simplifiedRawPath") or {}).get("points")
           or (seg.get("rawPath") or {}).get("points") or [])
    for p in raw:
        ll = parse_latlng(p)
        t = parse_timestamp(p.get("timestampMs") or p.get("timestamp")) if isinstance(p, dict) else None
        if ll:
            pts.append((t if t is not None else -1.0, ll[0], ll[1]))

    if not pts:
        way = ((seg.get("waypointPath") or {}).get("waypoints") or [])
        if not way:
            way = [s.get("stopLocation") for s in
                   ((seg.get("transitPath") or {}).get("transitStops") or [])]
        coords = [parse_latlng(w) for w in way]
        coords = [x for x in coords if x]
        if coords and t0 is not None and t1 is not None:
            full = ([start] if start else []) + coords + ([end] if end else [])
            span = max(t1 - t0, 1.0)
            for i, ll in enumerate(full):
                pts.append((t0 + span * i / max(len(full) - 1, 1), ll[0], ll[1]))

    if pts:
        # complète les horodatages manquants par interpolation linéaire
        if t0 is not None and t1 is not None:
            known = [i for i, p in enumerate(pts) if p[0] >= 0]
            if not known:
                span = max(t1 - t0, 1.0)
                pts = [(t0 + span * i / max(len(pts) - 1, 1), p[1], p[2])
                       for i, p in enumerate(pts)]
            else:
                pts = [p if p[0] >= 0 else (t0 + (t1 - t0) * i / max(len(pts) - 1, 1), p[1], p[2])
                       for i, p in enumerate(pts)]
        else:
            pts = [p for p in pts if p[0] >= 0]
        if start and pts and _valid(*start):
            pts.insert(0, (pts[0][0] - 1.0, start[0], start[1]))
        if end and pts and _valid(*end):
            pts.append((pts[-1][0] + 1.0, end[0], end[1]))
    elif start and end and t0 is not None and t1 is not None:
        pts = [(t0, start[0], start[1]), (t1, end[0], end[1])]

    c.add_trip(mode, pts, source, "semantic_location_history")


def _parse_semantic_segments(segments: Iterable[Any], c: Collector, source: str) -> None:
    n = 0
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t0 = parse_timestamp(seg.get("startTime") or seg.get("startTimestamp"))
        t1 = parse_timestamp(seg.get("endTime") or seg.get("endTimestamp"))
        path = seg.get("timelinePath") or seg.get("timelineMemory")
        if isinstance(path, list) and path:
            pts = []
            for i, p in enumerate(path):
                ll = parse_latlng(p if not isinstance(p, dict) else
                                  (p.get("point") or p.get("latLng") or p))
                if not ll:
                    continue
                t = None
                if isinstance(p, dict):
                    t = parse_timestamp(p.get("time") or p.get("timestamp"))
                    off = p.get("durationMinutesOffsetFromStartTime") or p.get("offsetFromStartTime")
                    if t is None and off is not None and t0 is not None:
                        try:
                            t = t0 + float(off) * 60.0
                        except (TypeError, ValueError):
                            t = None
                if t is None and t0 is not None and t1 is not None:
                    t = t0 + (t1 - t0) * i / max(len(path) - 1, 1)
                if t is not None:
                    pts.append((t, ll[0], ll[1]))
            mode = _activity_mode((seg.get("activity") or {}).get("topCandidate")) \
                if isinstance(seg.get("activity"), dict) else "other"
            c.add_trip(mode, pts, source, "timeline_semantic_segments")
            n += 1
            continue

        act = seg.get("activity")
        if isinstance(act, dict):
            a = parse_latlng(act.get("start"))
            b = parse_latlng(act.get("end"))
            mode = _activity_mode(act.get("topCandidate") or act.get("type"))
            if a and b and t0 is not None and t1 is not None:
                c.add_trip(mode, [(t0, a[0], a[1]), (t1, b[0], b[1])], source,
                           "timeline_semantic_segments")
                n += 1
            continue

        visit = seg.get("visit")
        if isinstance(visit, dict):
            ll = parse_latlng(visit.get("topCandidate") or visit)
            if ll and t0 is not None:
                name = ""
                cand = visit.get("topCandidate") or {}
                if isinstance(cand, dict):
                    name = str(cand.get("placeId") or cand.get("semanticType") or "")
                c.places.append(Place(name=name, lat=ll[0], lon=ll[1],
                                      start=t0, end=t1 or t0))
                n += 1
    if n:
        c.note("timeline_semantic_segments", n)


def _parse_records(locations: Iterable[Any], c: Collector, source: str,
                   gap_seconds: float = 900.0, fmt: str = "records") -> None:
    pts: List[Tuple[float, float, float]] = []
    activities: List[Tuple[float, str]] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        ll = parse_latlng(loc)
        t = parse_timestamp(loc.get("timestamp") or loc.get("timestampMs"))
        if ll and t is not None:
            pts.append((t, ll[0], ll[1]))
        for act in (loc.get("activity") or []):
            if not isinstance(act, dict):
                continue
            at = parse_timestamp(act.get("timestamp")) or t
            inner = act.get("activity") or []
            if inner and isinstance(inner, list) and isinstance(inner[0], dict) and at:
                activities.append((at, str(inner[0].get("type", ""))))
    if not pts:
        return
    pts.sort(key=lambda p: p[0])
    activities.sort()
    c.note("records", len(pts))

    def mode_at(t0: float, t1: float) -> str:
        votes: Dict[str, int] = {}
        for at, typ in activities:
            if t0 <= at <= t1:
                m = ACTIVITY_MAP.get(typ.upper(), "other")
                if m != "other":
                    votes[m] = votes.get(m, 0) + 1
        return max(votes, key=votes.get) if votes else "other"

    chunk: List[Tuple[float, float, float]] = [pts[0]]
    for prev, cur in zip(pts, pts[1:]):
        dt = cur[0] - prev[0]
        dist = haversine_m(prev[1], prev[2], cur[1], cur[2])
        if dt > gap_seconds or (dt > 120 and dist < 40):
            if len(chunk) >= 2:
                c.add_trip(mode_at(chunk[0][0], chunk[-1][0]), chunk, source, fmt)
            chunk = [cur]
        else:
            chunk.append(cur)
    if len(chunk) >= 2:
        c.add_trip(mode_at(chunk[0][0], chunk[-1][0]), chunk, source, fmt)


def _parse_geojson(obj: dict, c: Collector, source: str) -> bool:
    typ = obj.get("type")
    feats: List[dict] = []
    if typ == "FeatureCollection":
        feats = [f for f in obj.get("features", []) if isinstance(f, dict)]
    elif typ == "Feature":
        feats = [obj]
    elif typ in ("LineString", "MultiLineString"):
        feats = [{"type": "Feature", "geometry": obj, "properties": {}}]
    else:
        return False
    n = 0
    for f in feats:
        geom = f.get("geometry") or {}
        props = f.get("properties") or {}
        mode = _activity_mode(props.get("mode") or props.get("activityType") or "")
        t0 = parse_timestamp(props.get("startTime") or props.get("time") or props.get("timestamp"))
        t1 = parse_timestamp(props.get("endTime")) or (t0 + 600 if t0 else None)
        lines = []
        if geom.get("type") == "LineString":
            lines = [geom.get("coordinates") or []]
        elif geom.get("type") == "MultiLineString":
            lines = geom.get("coordinates") or []
        for line in lines:
            if t0 is None:
                continue
            pts = []
            for i, cd in enumerate(line):
                if not isinstance(cd, (list, tuple)) or len(cd) < 2:
                    continue
                lat, lon = float(cd[1]), float(cd[0])
                t = t0 + (t1 - t0) * i / max(len(line) - 1, 1)
                if len(cd) >= 4:
                    tt = parse_timestamp(cd[3])
                    if tt:
                        t = tt
                pts.append((t, lat, lon))
            c.add_trip(mode, pts, source, "geojson")
            n += 1
    if n:
        c.note("geojson", n)
    return n > 0


def _parse_gpx(data: bytes, c: Collector, source: str) -> None:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        c.warnings.append(f"{source}: GPX illisible ({exc})")
        return
    ns = {"g": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

    def find_all(elem, tag):
        return elem.findall(f"g:{tag}", ns) if ns else elem.findall(tag)

    n = 0
    for trk in find_all(root, "trk"):
        for seg in find_all(trk, "trkseg"):
            pts = []
            for i, pt in enumerate(find_all(seg, "trkpt")):
                try:
                    lat, lon = float(pt.get("lat")), float(pt.get("lon"))
                except (TypeError, ValueError):
                    continue
                tnode = find_all(pt, "time")
                t = parse_timestamp(tnode[0].text) if tnode else None
                pts.append((t if t is not None else float(i), lat, lon))
            c.add_trip("other", pts, source, "gpx")
            n += 1
    if n:
        c.note("gpx", n)


def _deep_scan(obj: Any, c: Collector, source: str, budget: int = 400000) -> int:
    """Dernier recours : collecte tous les (temps, lat, lon) trouvés dans l'arbre."""
    found: List[Tuple[float, float, float]] = []

    def walk(node: Any, inherited_t: Optional[float], depth: int) -> None:
        if len(found) >= budget or depth > 40:
            return
        if isinstance(node, dict):
            t = None
            for key in ("timestamp", "timestampMs", "time", "startTime", "startTimestamp"):
                if key in node:
                    t = parse_timestamp(node[key])
                    if t:
                        break
            t = t or inherited_t
            ll = parse_latlng(node)
            if ll and t:
                found.append((t, ll[0], ll[1]))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, t, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, inherited_t, depth + 1)

    walk(obj, None, 0)
    if len(found) >= 2:
        _parse_records([{"timestamp": t, "latitudeE7": lat, "longitudeE7": lon}
                        for t, lat, lon in found], c, source, fmt="scan_generique")
        c.note("scan_generique", len(found))
    return len(found)


# --------------------------------------------------------------- entrée

def _dispatch_json(data: Any, c: Collector, source: str) -> None:
    if isinstance(data, dict):
        if isinstance(data.get("timelineObjects"), list):
            _parse_timeline_objects(data["timelineObjects"], c, source)
            return
        if isinstance(data.get("semanticSegments"), list):
            _parse_semantic_segments(data["semanticSegments"], c, source)
            if isinstance(data.get("rawSignals"), list) and not c.trips:
                _parse_records([s.get("position", {}) | {"timestamp": (s.get("position") or {}).get("timestamp")}
                                for s in data["rawSignals"] if isinstance(s, dict)
                                and isinstance(s.get("position"), dict)], c, source)
            return
        if isinstance(data.get("locations"), list):
            _parse_records(data["locations"], c, source)
            return
        if data.get("type") in ("FeatureCollection", "Feature", "LineString", "MultiLineString"):
            if _parse_geojson(data, c, source):
                return
        if isinstance(data.get("timelineEdits"), list):
            _deep_scan(data["timelineEdits"], c, source)
            return
    if isinstance(data, list) and data:
        head = next((x for x in data if isinstance(x, dict)), None)
        if head is not None:
            if "activitySegment" in head or "placeVisit" in head:
                _parse_timeline_objects(data, c, source)
                return
            if "activity" in head or "visit" in head or "timelinePath" in head:
                _parse_semantic_segments(data, c, source)
                return
            if "latitudeE7" in head or "latitude" in head:
                _parse_records(data, c, source)
                return
    n = _deep_scan(data, c, source)
    if n < 2:
        c.warnings.append(f"{source} : aucune donnée de position reconnue.")


def _load_json(raw: bytes, source: str, c: Collector) -> Optional[Any]:
    try:
        return json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        c.warnings.append(f"{source} : JSON invalide ({exc.msg} ligne {exc.lineno}).")
        return None


def parse_bytes(raw: bytes, name: str, c: Collector) -> None:
    lower = name.lower()
    if lower.endswith(".gz"):
        try:
            raw = gzip.decompress(raw)
            lower = lower[:-3]
        except OSError:
            c.warnings.append(f"{name} : archive gzip illisible.")
            return
    if lower.endswith(".gpx") or raw[:200].lstrip().startswith(b"<gpx"):
        _parse_gpx(raw, c, name)
        return
    if lower.endswith((".json", ".geojson")) or raw[:1] in (b"{", b"["):
        data = _load_json(raw, name, c)
        if data is not None:
            _dispatch_json(data, c, name)
        return
    c.warnings.append(f"{name} : format non pris en charge (ignoré).")


def _zip_members(zf: zipfile.ZipFile, c: Collector, archive: str) -> List[zipfile.ZipInfo]:
    members = []
    for m in zf.infolist():
        if m.is_dir() or not m.filename.lower().endswith((".json", ".geojson", ".gpx", ".json.gz")):
            continue
        if m.file_size > MAX_MEMBER_BYTES:
            c.warnings.append(
                f"{os.path.basename(m.filename)} ignoré : {m.file_size // (1024 * 1024)} Mo, "
                f"au-delà de la limite de {MAX_MEMBER_BYTES // (1024 * 1024)} Mo.")
            continue
        members.append(m)
    if not members:
        c.warnings.append(f"{archive} : aucun JSON de localisation exploitable dans l'archive.")
    return members


def parse_zip(path: str, c: Collector) -> None:
    """Lit une archive Takeout en commençant par la Timeline détaillée.

    Les points bruts (Records.json, souvent plusieurs centaines de Mo) ne sont
    ouverts que si rien d'autre n'a donné de trajets : cela évite de charger
    inutilement un énorme fichier — première cause d'échec d'analyse.
    """
    archive = os.path.basename(path)
    with zipfile.ZipFile(path) as zf:
        members = _zip_members(zf, c, archive)
        raw = [m for m in members
               if re.search(r"(records|enregistrement|raw)", m.filename, re.I)]
        detailed = [m for m in members if m not in raw]
        interesting = [m for m in detailed if re.search(
            r"(timeline|location|trajet|position|semantic|historique)", m.filename, re.I)]

        for group in (interesting or detailed, raw):
            for m in sorted(group, key=lambda x: x.filename):
                try:
                    with zf.open(m) as fh:
                        parse_bytes(fh.read(), m.filename, c)
                except (zipfile.BadZipFile, RuntimeError, MemoryError, OSError) as exc:
                    c.warnings.append(f"{os.path.basename(m.filename)} : lecture impossible ({exc}).")
            if c.trips:      # la Timeline détaillée suffit : on n'ouvre pas les points bruts
                break


FORMAT_PRIORITY = {
    "semantic_location_history": 0, "timeline_semantic_segments": 0,
    "geojson": 0, "gpx": 0, "records": 1, "scan_generique": 2, "": 2,
}


def _dedupe_sources(c: Collector) -> List[Trip]:
    """Un Takeout contient souvent Records.json ET l'historique sémantique :
    on ne garde que la source la plus fiable pour ne pas doubler les trajets."""
    if not c.trips:
        return c.trips
    best = min(FORMAT_PRIORITY.get(t.fmt, 2) for t in c.trips)
    kept = [t for t in c.trips if FORMAT_PRIORITY.get(t.fmt, 2) == best]
    if len(kept) != len(c.trips):
        ignored = sorted({t.fmt for t in c.trips if FORMAT_PRIORITY.get(t.fmt, 2) != best})
        c.warnings.append(
            "Sources ignorées pour éviter les doublons : " + ", ".join(ignored) +
            " (l'historique détaillé de la Timeline a été retenu).")
    return kept


def parse_files(paths: List[str]) -> Collector:
    c = Collector()
    for path in paths:
        name = os.path.basename(path)
        try:
            if zipfile.is_zipfile(path):
                parse_zip(path, c)
            else:
                with open(path, "rb") as fh:
                    parse_bytes(fh.read(), name, c)
        except OSError as exc:
            c.warnings.append(f"{name} : {exc}")
    c.trips = _dedupe_sources(c)
    c.trips.sort(key=lambda t: t.start)
    if not c.trips:
        c.warnings.append(
            "Aucun trajet exploitable n'a été trouvé. Vérifiez que le fichier provient bien de "
            "Google Takeout → « Historique des positions (Timeline) » ou de l'export "
            "« Vos trajets » depuis l'application Google Maps.")
    return c
