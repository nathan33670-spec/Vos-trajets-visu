"""Portail web : téléversement de l'export Google, réglages et rendu vidéo."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import List, Optional

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .models import (MODE_COLORS, MODE_LABELS, PRESETS, TILE_PROVIDERS, RenderOptions)
from .parser import parse_files
from .processing import filter_trips, summarize
from .storage import (JOB_DIR, TILE_DIR, UPLOAD_DIR, dir_size, init_dirs, load_trips,
                      new_id, read_json, safe_dir, save_trips, write_json)
from .jobs import Job, JobManager
from .tiles import TILES_ENABLED, TileCache

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("vos-trajets")

MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "1024"))
MAX_FILES = int(os.environ.get("MAX_FILES", "40"))
CHUNK = 1024 * 1024
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
DATA_EXT = {".json", ".geojson", ".gpx", ".zip", ".gz"}

app = FastAPI(title="Vos trajets — vidéo", version="1.0",
              description="Transforme un export Google « Vos trajets » en vidéo accélérée.")

init_dirs()
tile_cache = TileCache(TILE_DIR)
manager = JobManager(tile_cache)


class RenderRequest(BaseModel):
    upload_id: str
    options: RenderOptions = Field(default_factory=RenderOptions)


class FilterRequest(BaseModel):
    options: RenderOptions = Field(default_factory=RenderOptions)


# --------------------------------------------------------------- utilitaires

def _upload_dir(upload_id: str) -> str:
    path = safe_dir(UPLOAD_DIR, upload_id)
    if not os.path.isdir(path):
        raise HTTPException(404, "Téléversement introuvable ou expiré.")
    return path


async def _save_upload(dest_dir: str, upload: UploadFile, budget: List[float]) -> str:
    name = os.path.basename(upload.filename or "fichier")
    name = "".join(c for c in name if c.isalnum() or c in "._- ") or "fichier"
    path = os.path.join(dest_dir, name)
    written = 0
    with open(path, "wb") as fh:
        while True:
            chunk = await upload.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            budget[0] += len(chunk)
            if budget[0] > MAX_UPLOAD_MB * 1024 * 1024:
                fh.close()
                os.remove(path)
                raise HTTPException(413, f"Limite de {MAX_UPLOAD_MB:.0f} Mo dépassée.")
            fh.write(chunk)
    if written == 0:
        os.remove(path)
        raise HTTPException(400, f"Fichier vide : {name}")
    return path


# --------------------------------------------------------------- endpoints

@app.get("/api/sante")
def health():
    from .render import ffmpeg_binary
    try:
        ffmpeg = ffmpeg_binary()
    except RuntimeError:
        ffmpeg = None
    return {
        "statut": "ok" if ffmpeg else "degrade",
        "ffmpeg": ffmpeg,
        "tuiles_activees": TILES_ENABLED,
        "rendus_en_cours": sum(1 for j in manager.jobs.values() if j.status == "running"),
        "stockage_octets": dir_size(UPLOAD_DIR) + dir_size(JOB_DIR),
    }


@app.get("/api/config")
def config():
    """Valeurs par défaut, listes de choix et présélections pour le formulaire."""
    schema = RenderOptions.model_json_schema()
    return {
        "defaults": RenderOptions().model_dump(),
        "descriptions": {k: v.get("description") for k, v in schema["properties"].items()
                         if v.get("description")},
        "modes": [{"id": k, "label": v, "color": MODE_COLORS[k]} for k, v in MODE_LABELS.items()],
        "presets": PRESETS,
        "tile_providers": {k: {"name": v["name"], "attribution": v["attribution"]}
                           for k, v in TILE_PROVIDERS.items()},
        "limits": {"max_upload_mb": MAX_UPLOAD_MB, "max_files": MAX_FILES},
        "tiles_enabled": TILES_ENABLED,
    }


@app.post("/api/uploads")
async def upload(fichiers: List[UploadFile] = File(..., alias="fichiers")):
    if len(fichiers) > MAX_FILES:
        raise HTTPException(400, f"{MAX_FILES} fichiers au maximum.")
    upload_id = new_id("up")
    dest = os.path.join(UPLOAD_DIR, upload_id)
    os.makedirs(dest, exist_ok=True)
    paths: List[str] = []
    budget = [0.0]
    try:
        for f in fichiers:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext and ext not in DATA_EXT:
                raise HTTPException(400, f"Extension non prise en charge : {ext} "
                                         f"(attendu : {', '.join(sorted(DATA_EXT))})")
            paths.append(await _save_upload(dest, f, budget))
        if not paths:
            raise HTTPException(400, "Aucun fichier reçu.")

        t0 = time.time()
        collector = parse_files(paths)
        if not collector.trips:
            raise HTTPException(422, " ".join(collector.warnings[-2:]) or
                                "Aucun trajet trouvé dans les fichiers fournis.")
        save_trips(os.path.join(dest, "trajets.json.gz"), collector.trips, collector.places)
        stats = summarize(collector.trips, collector.places)
        meta = {
            "id": upload_id,
            "created": time.time(),
            "files": [os.path.basename(p) for p in paths],
            "bytes": int(budget[0]),
            "formats": collector.formats,
            "warnings": collector.warnings[:20],
            "parse_seconds": round(time.time() - t0, 2),
            "stats": stats,
        }
        write_json(os.path.join(dest, "meta.json"), meta)
        for p in paths:                      # les sources brutes ne sont plus utiles
            try:
                os.remove(p)
            except OSError:
                pass
        return meta
    except HTTPException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        log.exception("Échec de l'analyse")
        raise HTTPException(500, f"Analyse impossible : {exc}") from exc


@app.post("/api/uploads/audio")
async def upload_audio(fichier: UploadFile = File(..., alias="fichier")):
    ext = os.path.splitext(fichier.filename or "")[1].lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, f"Format audio non pris en charge ({ext}).")
    audio_id = new_id("audio")
    dest = os.path.join(UPLOAD_DIR, audio_id)
    os.makedirs(dest, exist_ok=True)
    path = await _save_upload(dest, fichier, [0.0])
    return {"id": audio_id, "nom": os.path.basename(path)}


@app.get("/api/uploads/{upload_id}")
def upload_info(upload_id: str):
    meta = read_json(os.path.join(_upload_dir(upload_id), "meta.json"))
    if not meta:
        raise HTTPException(404, "Métadonnées introuvables.")
    return meta


@app.post("/api/uploads/{upload_id}/apercu")
def preview_filters(upload_id: str, req: FilterRequest = Body(default=FilterRequest())):
    """Statistiques après application des filtres (avant de lancer un rendu)."""
    trips, places = load_trips(os.path.join(_upload_dir(upload_id), "trajets.json.gz"))
    trips, places, notes = filter_trips(trips, places, req.options)
    return {"stats": summarize(trips, places), "notes": notes}


@app.post("/api/uploads/{upload_id}/export")
def export(upload_id: str, format: str = "geojson",
           req: FilterRequest = Body(default=FilterRequest())):
    if format not in ("geojson", "gpx"):
        raise HTTPException(400, "Format d'export inconnu (geojson ou gpx).")
    trips, places = load_trips(os.path.join(_upload_dir(upload_id), "trajets.json.gz"))
    trips, _, _ = filter_trips(trips, places, req.options)
    if not trips:
        raise HTTPException(422, "Aucun trajet après filtrage.")
    if format == "geojson":
        payload = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"mode": t.mode, "label": MODE_LABELS.get(t.mode, t.mode),
                               "startTime": _iso(t.start), "endTime": _iso(t.end),
                               "points": len(t.times)},
                "geometry": {"type": "LineString",
                             "coordinates": [[round(lo, 6), round(la, 6)]
                                             for la, lo in zip(t.lats, t.lons)]},
            } for t in trips],
        }
        return Response(json.dumps(payload, ensure_ascii=False),
                        media_type="application/geo+json",
                        headers={"Content-Disposition": 'attachment; filename="trajets.geojson"'})
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="vos-trajets-visu" xmlns="http://www.topografix.com/GPX/1/1">']
    for t in trips:
        lines.append(f"<trk><name>{MODE_LABELS.get(t.mode, t.mode)} {_iso(t.start)}</name><trkseg>")
        for ts, la, lo in zip(t.times, t.lats, t.lons):
            lines.append(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"><time>{_iso(ts)}</time></trkpt>')
        lines.append("</trkseg></trk>")
    lines.append("</gpx>")
    return Response("\n".join(lines), media_type="application/gpx+xml",
                    headers={"Content-Disposition": 'attachment; filename="trajets.gpx"'})


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.post("/api/jobs")
def create_job(req: RenderRequest):
    up_dir = _upload_dir(req.upload_id)
    trips_path = os.path.join(up_dir, "trajets.json.gz")
    if not os.path.exists(trips_path):
        raise HTTPException(404, "Trajets introuvables pour ce téléversement.")
    audio_path = None
    if req.options.audio_upload_id:
        adir = safe_dir(UPLOAD_DIR, req.options.audio_upload_id)
        if os.path.isdir(adir):
            files = [f for f in os.listdir(adir)]
            if files:
                audio_path = os.path.join(adir, files[0])
    job_id = new_id("job")
    workdir = os.path.join(JOB_DIR, job_id)
    os.makedirs(workdir, exist_ok=True)
    job = Job(id=job_id, upload_id=req.upload_id,
              options=req.options.model_dump(), workdir=workdir)
    write_json(os.path.join(workdir, "options.json"), job.options)
    manager.submit(job, trips_path, audio_path)
    return job.public()


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": manager.listing()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = manager.get(job_id)
    if job:
        return job.public()
    state = read_json(os.path.join(safe_dir(JOB_DIR, job_id), "etat.json"))
    if not state:
        raise HTTPException(404, "Rendu introuvable.")
    return state


@app.post("/api/jobs/{job_id}/annuler")
def cancel_job(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(409, "Ce rendu ne peut plus être annulé.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/fichier/{kind}")
def job_file(job_id: str, kind: str):
    job = manager.get(job_id)
    workdir = safe_dir(JOB_DIR, job_id)
    result = job.result if job and job.result else None
    if result is None:
        state = read_json(os.path.join(workdir, "etat.json")) or {}
        if state.get("status") != "done":
            raise HTTPException(404, "Aucun fichier disponible pour ce rendu.")
        result = {}
    mapping = {
        "video": result.get("video") or _find(workdir, ("final.", "boucle.", "trajets.")),
        "affiche": result.get("poster") or os.path.join(workdir, "affiche.jpg"),
        "apercu": result.get("thumbnail") or os.path.join(workdir, "apercu.jpg"),
        "log": os.path.join(workdir, "erreur.log"),
    }
    path = mapping.get(kind)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Fichier introuvable.")
    if kind == "log":
        return PlainTextResponse(open(path, encoding="utf-8").read())
    media = {"mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif",
             "jpg": "image/jpeg"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
    return FileResponse(path, media_type=media, filename=os.path.basename(path))


def _find(workdir: str, prefixes) -> Optional[str]:
    for pref in prefixes:
        for name in sorted(os.listdir(workdir)):
            if name.startswith(pref):
                return os.path.join(workdir, name)
    return None


@app.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: str):
    shutil.rmtree(_upload_dir(upload_id), ignore_errors=True)
    return {"ok": True}


@app.on_event("shutdown")
def _shutdown():
    manager.shutdown()
    tile_cache.close()


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
