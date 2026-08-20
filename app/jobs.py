"""File d'attente des rendus : exécution en arrière-plan, suivi et annulation."""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import RenderOptions
from .processing import filter_trips, summarize
from .render import Renderer
from .storage import load_trips, purge_old, write_json
from .tiles import TileCache

log = logging.getLogger(__name__)

MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))


@dataclass
class Job:
    id: str
    upload_id: str
    options: Dict
    workdir: str
    status: str = "queued"          # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = "En file d'attente"
    error: Optional[str] = None
    result: Optional[Dict] = None
    warnings: List[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> Dict:
        data = {
            "id": self.id, "upload_id": self.upload_id, "status": self.status,
            "progress": round(self.progress, 4), "message": self.message,
            "error": self.error, "warnings": self.warnings,
            "created": self.created, "started": self.started, "finished": self.finished,
            "options": self.options,
        }
        if self.result:
            data["result"] = {
                "frames": self.result.get("frames"),
                "seconds": self.result.get("seconds"),
                "size_bytes": self.result.get("size_bytes"),
                "video_url": f"/api/jobs/{self.id}/fichier/video",
                "poster_url": f"/api/jobs/{self.id}/fichier/affiche" if self.result.get("poster") else None,
                "thumb_url": f"/api/jobs/{self.id}/fichier/apercu" if self.result.get("thumbnail") else None,
                "filename": os.path.basename(self.result.get("video", "trajets.mp4")),
            }
        return data


class JobManager:
    def __init__(self, tile_cache: TileCache):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT,
                                       thread_name_prefix="render")
        self.tile_cache = tile_cache
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep, daemon=True)
        self._sweeper.start()

    # ------------------------------------------------------------- API

    def submit(self, job: Job, trips_path: str, audio_path: Optional[str]) -> None:
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run, job, trips_path, audio_path)

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status in ("done", "error", "cancelled"):
            return False
        job._cancel.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.message = "Annulé"
            job.finished = time.time()
        return True

    def listing(self, limit: int = 30) -> List[Dict]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.public() for j in jobs[:limit]]

    # ------------------------------------------------------------- interne

    def _run(self, job: Job, trips_path: str, audio_path: Optional[str]) -> None:
        if job._cancel.is_set():
            return
        job.status = "running"
        job.started = time.time()
        job.progress = 0.0
        job.message = "Chargement des trajets…"
        self._persist(job)
        try:
            opt = RenderOptions(**job.options)
            trips, places = load_trips(trips_path)
            trips, places, notes = filter_trips(trips, places, opt)
            job.warnings.extend(notes)
            if not trips:
                raise ValueError("Aucun trajet ne correspond aux filtres choisis.")
            stats = summarize(trips, places)
            job.message = f"{stats['trips']} trajets · {stats['distance_km']} km"

            last_write = [0.0]

            def on_progress(p: float, msg: str) -> None:
                job.progress = p
                job.message = msg
                now = time.time()
                if now - last_write[0] > 2.0:
                    last_write[0] = now
                    self._persist(job)

            renderer = Renderer(trips, places, opt, job.workdir,
                                tile_cache=self.tile_cache,
                                on_progress=on_progress,
                                should_cancel=job._cancel.is_set,
                                audio_path=audio_path)
            result = renderer.run()
            job.result = result
            job.warnings.extend(result.get("warnings", []))
            job.status = "done"
            job.progress = 1.0
            job.message = (f"Vidéo prête · {result['frames']} images en "
                           f"{result['seconds']} s")
        except InterruptedError:
            job.status = "cancelled"
            job.message = "Rendu annulé"
        except Exception as exc:  # noqa: BLE001 - on remonte le message au portail
            log.exception("Échec du rendu %s", job.id)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = "Échec du rendu"
            job.result = None
            with open(os.path.join(job.workdir, "erreur.log"), "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        finally:
            job.finished = time.time()
            self._persist(job)

    def _persist(self, job: Job) -> None:
        try:
            write_json(os.path.join(job.workdir, "etat.json"), job.public())
        except OSError:
            pass

    def _sweep(self) -> None:
        while not self._stop.wait(1800):
            try:
                n = purge_old()
                if n:
                    log.info("Purge : %s dossier(s) supprimé(s)", n)
                cutoff = time.time() - 6 * 3600
                with self.lock:
                    for jid in [j.id for j in self.jobs.values()
                                if j.finished and j.finished < cutoff]:
                        self.jobs.pop(jid, None)
            except Exception:  # noqa: BLE001
                log.exception("Erreur pendant la purge")

    def shutdown(self) -> None:
        self._stop.set()
        for job in list(self.jobs.values()):
            job._cancel.set()
        self.pool.shutdown(wait=False)
