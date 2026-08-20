#!/usr/bin/env python3
"""Génère un faux export Google Timeline pour tester le portail sans données réelles.

    python tools/generer_exemple.py sortie.json --format semantic|timeline|records
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone

VILLES = {
    "Paris": (48.8566, 2.3522), "Versailles": (48.8014, 2.1301),
    "Lyon": (45.7640, 4.8357), "Marseille": (43.2965, 5.3698),
    "Bordeaux": (44.8378, -0.5792), "Lille": (50.6292, 3.0573),
    "Nantes": (47.2184, -1.5536), "Strasbourg": (48.5734, 7.7521),
    "Nice": (43.7102, 7.2620), "Toulouse": (43.6047, 1.4442),
    "Lisbonne": (38.7223, -9.1393), "Berlin": (52.5200, 13.4050),
}


def trace(a, b, n, jitter=0.004):
    pts = []
    for i in range(n):
        f = i / max(n - 1, 1)
        # léger arc + bruit pour imiter une route
        arc = math.sin(f * math.pi) * 0.12
        lat = a[0] + (b[0] - a[0]) * f + arc * (b[1] - a[1]) * 0.05
        lon = a[1] + (b[1] - a[1]) * f - arc * (b[0] - a[0]) * 0.05
        pts.append((lat + random.gauss(0, jitter), lon + random.gauss(0, jitter)))
    return pts


def build(days: int, seed: int):
    random.seed(seed)
    t = datetime(2024, 1, 8, 7, 30, tzinfo=timezone.utc)
    segments = []
    home = VILLES["Paris"]
    for day in range(days):
        jour = t + timedelta(days=day)
        if jour.weekday() < 5:
            bureau = (home[0] + 0.06, home[1] + 0.09)
            for depart, (a, b), mode, minutes in (
                (jour.replace(hour=8, minute=10), (home, bureau), "IN_PASSENGER_VEHICLE", 35),
                (jour.replace(hour=12, minute=30), (bureau, (bureau[0] + .01, bureau[1] - .008)), "WALKING", 12),
                (jour.replace(hour=18, minute=45), (bureau, home), "IN_SUBWAY", 42),
            ):
                segments.append((depart, depart + timedelta(minutes=minutes), mode,
                                 trace(a, b, max(6, minutes // 2), 0.0018)))
        elif day % 14 == 6:
            ville = random.choice(list(VILLES.values()))
            dep = jour.replace(hour=9)
            dist = math.hypot(ville[0] - home[0], ville[1] - home[1])
            mins = int(dist * 45) + 60
            segments.append((dep, dep + timedelta(minutes=mins), "IN_PASSENGER_VEHICLE",
                             trace(home, ville, 80, 0.01)))
            ret = dep + timedelta(hours=mins / 60 + 6)
            segments.append((ret, ret + timedelta(minutes=mins), "IN_PASSENGER_VEHICLE",
                             trace(ville, home, 80, 0.01)))
        else:
            parc = (home[0] + random.uniform(-.05, .05), home[1] + random.uniform(-.05, .05))
            dep = jour.replace(hour=10, minute=15)
            segments.append((dep, dep + timedelta(minutes=50), "CYCLING",
                             trace(home, parc, 30, 0.001)))
    # un vol longue distance
    dep = t + timedelta(days=max(days - 3, 1), hours=6)
    segments.append((dep, dep + timedelta(hours=2, minutes=40), "FLYING",
                     trace(VILLES["Paris"], VILLES["Lisbonne"], 40, 0.02)))
    return segments


def as_semantic(segments):
    objs = []
    for start, end, mode, pts in segments:
        objs.append({"activitySegment": {
            "startLocation": {"latitudeE7": int(pts[0][0] * 1e7), "longitudeE7": int(pts[0][1] * 1e7)},
            "endLocation": {"latitudeE7": int(pts[-1][0] * 1e7), "longitudeE7": int(pts[-1][1] * 1e7)},
            "duration": {"startTimestamp": start.isoformat().replace("+00:00", "Z"),
                         "endTimestamp": end.isoformat().replace("+00:00", "Z")},
            "distance": 0, "activityType": mode, "confidence": "HIGH",
            "simplifiedRawPath": {"points": [
                {"latE7": int(la * 1e7), "lngE7": int(lo * 1e7),
                 "timestampMs": str(int((start + (end - start) * i / max(len(pts) - 1, 1)).timestamp() * 1000))}
                for i, (la, lo) in enumerate(pts)]},
        }})
    return {"timelineObjects": objs}


def as_timeline(segments):
    segs = []
    for start, end, mode, pts in segments:
        segs.append({
            "startTime": start.isoformat(), "endTime": end.isoformat(),
            "timelinePath": [
                {"point": f"{la:.7f}°, {lo:.7f}°",
                 "time": (start + (end - start) * i / max(len(pts) - 1, 1)).isoformat()}
                for i, (la, lo) in enumerate(pts)],
            "activity": {"topCandidate": {"type": mode}},
        })
    return {"semanticSegments": segs, "userLocationProfile": {"frequentPlaces": []}}


def as_records(segments):
    locs = []
    for start, end, mode, pts in segments:
        for i, (la, lo) in enumerate(pts):
            ts = start + (end - start) * i / max(len(pts) - 1, 1)
            locs.append({"latitudeE7": int(la * 1e7), "longitudeE7": int(lo * 1e7),
                         "accuracy": random.randint(5, 40),
                         "timestamp": ts.isoformat().replace("+00:00", "Z"),
                         "activity": [{"timestamp": ts.isoformat().replace("+00:00", "Z"),
                                       "activity": [{"type": mode, "confidence": 90}]}]})
    return {"locations": locs}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sortie")
    ap.add_argument("--format", choices=("semantic", "timeline", "records"), default="semantic")
    ap.add_argument("--jours", type=int, default=90)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    segments = build(args.jours, args.seed)
    data = {"semantic": as_semantic, "timeline": as_timeline, "records": as_records}[args.format](segments)
    with open(args.sortie, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(f"{args.sortie} — {len(segments)} segments, format {args.format}")


if __name__ == "__main__":
    main()
