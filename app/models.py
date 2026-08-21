"""Modèles de données et options de rendu."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------- catégories

MODE_LABELS = {
    "car": "Voiture",
    "transit": "Transports en commun",
    "bike": "Vélo",
    "walk": "Marche / course",
    "plane": "Avion",
    "boat": "Bateau",
    "other": "Autre",
}

MODE_COLORS = {
    "car": "#ff8c42",
    "transit": "#4ecdc4",
    "bike": "#a3e048",
    "walk": "#ffd166",
    "plane": "#ff5d8f",
    "boat": "#5b8dff",
    "other": "#c8c8c8",
}

# Vitesses plausibles max (km/h) par mode, pour filtrer les points aberrants.
MODE_MAX_SPEED = {
    "walk": 30.0,
    "bike": 90.0,
    "car": 250.0,
    "transit": 400.0,
    "boat": 120.0,
    "plane": 1200.0,
    "other": 400.0,
}


# ---------------------------------------------------------------- structures

@dataclass
class Trip:
    """Un trajet : une suite de points datés."""
    mode: str
    times: List[float] = field(default_factory=list)   # epoch (s, UTC)
    lats: List[float] = field(default_factory=list)
    lons: List[float] = field(default_factory=list)
    source: str = ""
    label: str = ""
    fmt: str = ""

    def __len__(self) -> int:
        return len(self.times)

    @property
    def start(self) -> float:
        return self.times[0]

    @property
    def end(self) -> float:
        return self.times[-1]


@dataclass
class Place:
    """Un lieu visité (placeVisit)."""
    name: str
    lat: float
    lon: float
    start: float
    end: float


# ---------------------------------------------------------------- options

NULLABLE_FIELDS = {"date_start", "date_end", "bbox", "privacy_lat", "privacy_lon",
                   "tile_url", "audio_upload_id"}


class RenderOptions(BaseModel):
    """Toutes les options exposées par le portail."""

    @model_validator(mode="before")
    @classmethod
    def _ignore_nulls(cls, data):
        """Un champ vide du formulaire vaut « valeur par défaut », pas None."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if v is not None or k in NULLABLE_FIELDS}
        return data

    # --- Sélection / filtres -------------------------------------------------
    date_start: Optional[str] = Field(None, description="Date de début (AAAA-MM-JJ)")
    date_end: Optional[str] = Field(None, description="Date de fin incluse (AAAA-MM-JJ)")
    modes: List[str] = Field(default_factory=list, description="Modes retenus (vide = tous)")
    min_trip_km: float = Field(0.0, ge=0, description="Distance minimale d'un trajet")
    max_trip_km: float = Field(0.0, ge=0, description="Distance maximale (0 = sans limite)")
    min_trip_seconds: float = Field(0.0, ge=0)
    bbox: Optional[List[float]] = Field(None, description="[lon_min, lat_min, lon_max, lat_max]")
    drop_outliers: bool = Field(True, description="Supprime les sauts impossibles")
    privacy_lat: Optional[float] = None
    privacy_lon: Optional[float] = None
    privacy_radius_m: float = Field(0.0, ge=0, description="Masque les points dans ce rayon")

    # --- Vidéo ---------------------------------------------------------------
    width: int = Field(1920, ge=160, le=3840)
    height: int = Field(1080, ge=160, le=2160)
    fps: int = Field(30, ge=5, le=60)
    duration: float = Field(30.0, ge=2, le=1800, description="Durée de la vidéo (s)")
    container: str = Field("mp4", pattern="^(mp4|webm|gif)$")
    quality: str = Field("high", pattern="^(low|medium|high|max)$")
    supersample: int = Field(2, ge=1, le=3, description="Anticrénelage (1 = off)")

    # --- Rythme temporel -----------------------------------------------------
    time_mode: str = Field("compress", pattern="^(real|compress|equal|distance)$")
    gap_max_seconds: float = Field(900.0, ge=0, description="Pauses compressées au-delà de N s")
    hold_first_seconds: float = Field(0.6, ge=0)
    hold_last_seconds: float = Field(1.5, ge=0)

    # --- Caméra --------------------------------------------------------------
    camera: str = Field("auto", pattern="^(fit_all|follow|trip|auto)$")
    follow_zoom: float = Field(11.0, ge=1, le=19)
    camera_smoothing: float = Field(0.85, ge=0, le=0.99)
    padding: float = Field(0.08, ge=0, le=0.4)
    zoom_out_end: bool = Field(True, description="Dézoome sur l'ensemble à la fin")

    # --- Fond de carte -------------------------------------------------------
    map_style: str = Field("tiles", pattern="^(none|ghost|grid|tiles)$")
    background: str = "#0b0f1a"
    tile_provider: str = "carto_dark"
    tile_url: Optional[str] = None
    tile_opacity: float = Field(0.85, ge=0, le=1)
    tile_grayscale: bool = False
    ghost_opacity: float = Field(0.18, ge=0, le=1)

    # --- Style du tracé ------------------------------------------------------
    color_mode: str = Field("mode", pattern="^(mode|speed|time|single)$")
    line_color: str = "#37e6c8"
    palette: str = Field("neon", pattern="^(neon|viridis|inferno|ice|fire|rainbow)$")
    line_width: float = Field(2.8, ge=0.4, le=20)
    trail_mode: str = Field("cumulative", pattern="^(cumulative|fade|comet)$")
    trail_opacity: float = Field(0.5, ge=0.02, le=1, description="Opacité de la trace déjà parcourue")
    fade_seconds: float = Field(4.0, ge=0.2, le=120, description="Longueur de la traîne (s de vidéo)")
    glow: bool = True
    glow_strength: float = Field(0.9, ge=0, le=2)
    head_dot: bool = True
    head_size: float = Field(5.5, ge=0, le=40)
    head_pulse: bool = True
    show_places: bool = Field(False, description="Points sur les lieux visités")

    # --- Habillage -----------------------------------------------------------
    show_clock: bool = True
    clock_format: str = Field("date", pattern="^(date|datetime|month|year|none)$")
    show_stats: bool = True
    show_progress: bool = True
    show_legend: bool = True
    show_scalebar: bool = True
    title: str = ""
    subtitle: str = ""
    title_seconds: float = Field(2.5, ge=0, le=15)
    outro_text: str = ""
    watermark: str = ""
    overlay_color: str = "#f2f6ff"
    font_scale: float = Field(1.0, ge=0.5, le=2.5)
    units: str = Field("metric", pattern="^(metric|imperial)$")
    fade_in: bool = True

    # --- Sorties annexes -----------------------------------------------------
    audio_upload_id: Optional[str] = None
    audio_fade: bool = True
    make_poster: bool = True
    loop_video: bool = Field(False, description="Ajoute une lecture inversée (boomerang)")

    @field_validator("modes", mode="before")
    @classmethod
    def _clean_modes(cls, v):
        if v in (None, "", []):
            return []
        if isinstance(v, str):
            v = [m.strip() for m in v.split(",")]
        return [m for m in v if m in MODE_LABELS]

    @field_validator("width", "height")
    @classmethod
    def _even(cls, v):
        return v - (v % 2)


QUALITY_CRF = {"low": 30, "medium": 25, "high": 20, "max": 16}
QUALITY_PRESET = {"low": "veryfast", "medium": "fast", "high": "medium", "max": "slow"}

TILE_PROVIDERS = {
    "carto_dark": {
        "name": "Carto Dark Matter",
        "url": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, © CARTO",
        "background": "#0b0f1a",
    },
    "carto_light": {
        "name": "Carto Positron",
        "url": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, © CARTO",
        "background": "#f4f4f4",
    },
    "carto_voyager": {
        "name": "Carto Voyager",
        "url": "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap, © CARTO",
        "background": "#e8e4de",
    },
    "osm": {
        "name": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "background": "#e8e4de",
    },
    "opentopo": {
        "name": "OpenTopoMap",
        "url": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenTopoMap (CC-BY-SA)",
        "background": "#e8e4de",
    },
    "esri_satellite": {
        "name": "Esri World Imagery",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "© Esri, Maxar, Earthstar Geographics",
        "background": "#12161c",
    },
    "custom": {
        "name": "URL personnalisée",
        "url": "",
        "attribution": "",
        "background": "#0b0f1a",
    },
}

PRESETS = {
    "defaut": {
        "label": "Équilibré",
        "description": "Caméra qui suit les trajets, fond de carte, 30 s. Le réglage conseillé.",
        "options": {
            "camera": "auto", "trail_mode": "cumulative", "map_style": "tiles",
            "tile_provider": "carto_dark", "duration": 30, "color_mode": "mode",
            "glow": True, "zoom_out_end": True, "time_mode": "compress",
        },
    },
    "carte_claire": {
        "label": "Carte claire",
        "description": "Fond de carte clair, tracé coloré — idéal pour l'impression ou un partage.",
        "options": {
            "camera": "auto", "map_style": "tiles", "tile_provider": "carto_light",
            "background": "#f2f2f2", "overlay_color": "#1b2430", "glow": False,
            "trail_opacity": 0.6, "line_width": 3.0, "duration": 30,
        },
    },
    "vue_ensemble": {
        "label": "Vue d'ensemble",
        "description": "Plan fixe sur toute la zone, la trace se dessine peu à peu.",
        "options": {
            "camera": "fit_all", "trail_mode": "cumulative", "map_style": "tiles",
            "duration": 25, "trail_opacity": 0.65, "zoom_out_end": False,
        },
    },
    "portrait": {
        "label": "Portrait (réseaux)",
        "description": "1080×1920, 20 s, texte agrandi — pour Instagram ou TikTok.",
        "options": {
            "width": 1080, "height": 1920, "duration": 20, "camera": "auto",
            "font_scale": 1.15, "show_legend": False, "show_scalebar": False,
            "map_style": "tiles",
        },
    },
}
