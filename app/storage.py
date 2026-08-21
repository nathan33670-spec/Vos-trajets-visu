"""Persistance : dossiers, sérialisation des trajets, purge automatique."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import time
import uuid
from typing import Dict, List, Optional, Tuple

from .models import Place, Trip, coords

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
JOB_DIR = os.path.join(DATA_DIR, "jobs")
TILE_DIR = os.path.join(DATA_DIR, "tiles")
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "48"))


def init_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, JOB_DIR, TILE_DIR):
        os.makedirs(d, exist_ok=True)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def safe_dir(base: str, ident: str) -> str:
    """Empêche toute remontée de chemin depuis un identifiant fourni par le client."""
    ident = os.path.basename(ident.strip())
    if not ident or ident in (".", "..") or "/" in ident or "\\" in ident:
        raise ValueError("Identifiant invalide.")
    path = os.path.join(base, ident)
    if os.path.commonpath([os.path.abspath(base), os.path.abspath(path)]) != os.path.abspath(base):
        raise ValueError("Identifiant invalide.")
    return path


def save_trips(path: str, trips: List[Trip], places: List[Place]) -> None:
    payload = {
        "trips": [{"m": t.mode, "s": t.source,
                   "t": [round(x, 1) for x in t.times],
                   "a": [round(x, 6) for x in t.lats],
                   "o": [round(x, 6) for x in t.lons]} for t in trips],
        "places": [{"n": p.name, "a": round(p.lat, 6), "o": round(p.lon, 6),
                    "s": round(p.start, 1), "e": round(p.end, 1)} for p in places],
    }
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def load_trips(path: str) -> Tuple[List[Trip], List[Place]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    trips = [Trip(mode=t["m"], times=coords(t["t"]), lats=coords(t["a"]),
                  lons=coords(t["o"]), source=t.get("s", ""))
             for t in payload.get("trips", [])]
    places = [Place(name=p.get("n", ""), lat=p["a"], lon=p["o"], start=p["s"], end=p["e"])
              for p in payload.get("places", [])]
    return trips, places


def write_json(path: str, data: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def read_json(path: str) -> Optional[Dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def purge_old(max_age_hours: float = RETENTION_HOURS) -> int:
    """Supprime les téléversements et rendus expirés. Retourne le nombre de dossiers effacés."""
    if max_age_hours <= 0:
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for base in (UPLOAD_DIR, JOB_DIR):
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            path = os.path.join(base, name)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    return removed
