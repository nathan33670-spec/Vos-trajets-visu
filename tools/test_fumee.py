#!/usr/bin/env python3
"""Test de fumée : génère un jeu d'essai, analyse chaque format et rend une vidéo.

    python tools/test_fumee.py [dossier_de_travail]
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import RenderOptions            # noqa: E402
from app.parser import parse_files              # noqa: E402
from app.processing import filter_trips, summarize  # noqa: E402
from app.render import Renderer                 # noqa: E402

OK, KO = "\033[92m✓\033[0m", "\033[91m✗\033[0m"


def main() -> int:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="trajets-")
    os.makedirs(work, exist_ok=True)
    gen = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generer_exemple.py")
    echecs = 0

    for fmt in ("semantic", "timeline", "records"):
        path = os.path.join(work, f"{fmt}.json")
        subprocess.run([sys.executable, gen, path, "--format", fmt, "--jours", "45"],
                       check=True, stdout=subprocess.DEVNULL)
        c = parse_files([path])
        stats = summarize(c.trips, c.places)
        good = stats["trips"] > 10 and stats["distance_km"] > 100
        echecs += 0 if good else 1
        print(f"{OK if good else KO} analyse {fmt:9s} : {stats['trips']} trajets, "
              f"{stats['distance_km']} km, modes {list(stats['per_mode'])}")

    c = parse_files([os.path.join(work, "semantic.json")])
    cas = {
        "mp4 plan fixe": dict(camera="fit_all"),
        "mp4 cinématique": dict(camera="auto", trail_mode="comet", color_mode="speed"),
        "webm": dict(container="webm"),
        "gif": dict(container="gif", fps=8),
        "filtres": dict(modes=["car"], min_trip_km=1, date_start="2024-01-15"),
    }
    for nom, extra in cas.items():
        opt = RenderOptions(**{**dict(width=480, height=270, fps=12, duration=4,
                                      supersample=1, quality="low"), **extra})
        trips, places, _ = filter_trips(c.trips, c.places, opt)
        out = os.path.join(work, "rendu_" + nom.split()[0])
        os.makedirs(out, exist_ok=True)
        t0 = time.time()
        try:
            res = Renderer(trips, places, opt, out, on_progress=lambda p, m: None).run()
            good = os.path.getsize(res["video"]) > 1000
            print(f"{OK if good else KO} rendu {nom:16s} : {res['size_bytes'] // 1024} Ko "
                  f"en {time.time() - t0:.1f} s")
            echecs += 0 if good else 1
        except Exception as exc:  # noqa: BLE001
            print(f"{KO} rendu {nom:16s} : {type(exc).__name__}: {exc}")
            echecs += 1

    print(("Tous les tests passent." if not echecs else f"{echecs} échec(s).") + f"  ({work})")
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
