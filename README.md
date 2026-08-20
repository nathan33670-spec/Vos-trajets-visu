# Vos trajets — générateur de vidéo accélérée

Application auto-hébergée (un seul conteneur Docker) qui transforme un export
**Google « Vos trajets » / Historique des positions (Timeline)** en **vidéo accélérée**
de tous vos déplacements : la trace se dessine dans le temps, la caméra suit le
parcours, la date et les kilomètres défilent.

Le portail web fait tout : dépôt du fichier, analyse, réglages, rendu, téléchargement.
Aucune donnée ne sort de votre serveur (sauf, si vous l'activez, les requêtes de
tuiles vers le fournisseur de fond de carte choisi).

---

## Démarrage rapide

```bash
git clone <ce dépôt> && cd vos-trajets-visu
cp .env.example .env          # facultatif : ports, quotas, mode hors ligne
docker compose up -d --build
```

Portail : <http://localhost:5080>

Sans docker compose :

```bash
docker build -t vos-trajets-visu .
docker run -d -p 5080:8000 -v vos-trajets-data:/data --name vos-trajets vos-trajets-visu
```

En développement, hors conteneur :

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

---

## Obtenir ses données Google

**Depuis un ordinateur (recommandé)**

1. <https://takeout.google.com> → *Tout désélectionner* → cocher
   **« Historique des positions (Timeline) »**.
2. Exporter au format `.zip`, télécharger l'archive.
3. Déposer l'archive **telle quelle** dans le portail.

**Depuis le téléphone** — Google Maps → photo de profil → *Vos trajets* → ⋮ →
*Paramètres de la Timeline* → **Exporter les données de la Timeline** : vous
obtenez un `Timeline.json` (ou `location-history.json`) à déposer directement.

### Formats reconnus

| Source | Fichiers |
|---|---|
| Timeline sémantique (Takeout historique) | `Semantic Location History/AAAA/AAAA_MOIS.json` |
| Timeline moderne (export mobile 2024+) | `Timeline.json`, `location-history.json` (`semanticSegments`) |
| Points bruts | `Records.json`, `Historique des positions.json` (`locations[]`) |
| Archives | `.zip` Takeout complet (exploré récursivement), `.json.gz` |
| Autres | GeoJSON (`LineString`), GPX (`trk/trkseg`) |

Quand une archive contient **à la fois** `Records.json` et la Timeline sémantique,
seule la seconde est retenue pour ne pas compter deux fois les mêmes trajets.
Un format inconnu passe par un analyseur générique qui cherche tout couple
latitude/longitude daté.

---

## Les options du portail

### Sélection des trajets
Dates de début / fin · modes de transport (voiture, transports en commun, vélo,
marche, avion, bateau) · distance et durée minimales / maximales · rectangle
géographique · suppression des points GPS aberrants ·
**confidentialité** : masquer tous les points dans un rayon donné autour d'un
lieu (domicile) avant de publier la vidéo.

### Format vidéo
Résolutions prédéfinies (720p → 4K, vertical 1080×1920, carré) ou libre ·
5 à 60 ips · 5 s à 10 min · **MP4 (H.264)**, **WebM (VP9)** ou **GIF** ·
4 niveaux de qualité · anticrénelage ×1/×2/×3.

### Rythme temporel
- **Réel avec pauses compressées** (défaut) — le temps réel défile mais les arrêts
  au-delà d'un seuil sont raccourcis.
- **Strictement proportionnel** — une seconde de vidéo = une durée réelle constante.
- **Chaque trajet dure autant** — utile quand les trajets sont très inégaux.
- **Vitesse constante** — l'avancement suit la distance, pas l'horloge.

Plus : pause au début, pause à la fin.

### Caméra
Plan fixe sur l'ensemble · **cinématique** (cadre la portion en cours, zoom et
translation lissés, recentrage automatique du point mobile) · centrée sur le point
courant · un cadrage par trajet. Zoom maximal, lissage, marge, dézoom final sur
l'ensemble.

### Fond de carte
- **Trace fantôme** (défaut) — le parcours complet en filigrane : aucun accès réseau.
- **Uni** ou **grille**.
- **Tuiles** : Carto Dark / Positron / Voyager, OpenStreetMap, OpenTopoMap,
  Esri World Imagery, ou une **URL `{z}/{x}/{y}` personnalisée**, avec opacité,
  passage en noir et blanc, cache disque et attribution incrustée.

### Style du tracé
Couleur selon le **mode de transport**, la **vitesse**, la **date** ou couleur unique ·
palettes néon / viridis / inferno / glace / feu / arc-en-ciel · épaisseur ·
traîne **cumulative / comète / éphémère** · opacité de l'historique · longueur de la
traîne · halo lumineux · point mobile pulsant · lieux visités.

### Habillage
Carton de titre + sous-titre · texte de fin · filigrane · date (4 formats) ·
compteur de distance et de trajets · barre de progression · légende (ou échelle de
couleurs) · échelle cartographique · unités km/miles · couleur et taille du texte ·
fondu d'ouverture.

### Audio et sorties
Musique de fond (MP3/M4A/WAV/OGG/FLAC, bouclée et fondue en sortie) ·
**boomerang** (lecture aller-retour) · affiche JPEG · exports **GeoJSON** et **GPX**
des trajets filtrés.

### Présélections
*Cinématique*, *Vue globale*, *Fond de carte OSM*, *Minimaliste*,
*Réseau social (vertical)*, *Aperçu rapide*. Le bouton **Aperçu rapide (480p)**
génère un test en quelques secondes avant le rendu final.

---

## Performances

Mesuré sur **un cœur** x86, avec un historique volumineux de **2 500 trajets /
49 000 points GPS** (≈ 3 ans de Timeline), pour une **vidéo de 30 s** :

| Rendu | Vitesse | Durée du rendu |
|---|---|---|
| 480p, 15 ips, sans anticrénelage ni halo (*Aperçu rapide*) | 51 img/s | **4 s** |
| 720p 30 ips, plan fixe, anticrénelage ×2 | 27 img/s | **34 s** |
| 1080p 30 ips, plan fixe, anticrénelage ×2 | 14 img/s | **1 min 03** |
| 1080p 30 ips, caméra cinématique + traîne comète | 3,3 img/s | **4 min 37** |

L'analyse d'un export de 11 Mo (49 000 points) prend ~5 s.

Pour accélérer un rendu : réduire la **longueur de la traîne** (le mode comète
redessine toute la traîne à chaque image), passer l'**anticrénelage** à ×1,
désactiver le **halo**, ou préférer le **plan fixe** — qui n'ajoute que les
nouveaux segments à chaque image et reste rapide quelle que soit la taille de
l'historique.

Le rendu tourne en tâche de fond : la page affiche l'avancement et le temps
restant estimé, et le rendu peut être annulé à tout moment.
`MAX_CONCURRENT_JOBS` limite le nombre de rendus simultanés.

Deux optimisations rendent les gros historiques utilisables : simplification
Ramer–Douglas–Peucker des traces au niveau de détail réellement visible, et
couche cumulative incrémentale en caméra fixe (chaque segment n'est dessiné
qu'une seule fois pour toute la vidéo).

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `PORT` | `5080` | Port publié sur l'hôte (docker compose) |
| `DATA_DIR` | `/data` | Téléversements, rendus et cache de tuiles |
| `RETENTION_HOURS` | `48` | Purge automatique (`0` = jamais) |
| `MAX_UPLOAD_MB` | `1024` | Taille cumulée maximale d'un dépôt |
| `MAX_FILES` | `40` | Nombre de fichiers par dépôt |
| `MAX_CONCURRENT_JOBS` | `2` | Rendus simultanés |
| `TILES_ENABLED` | `1` | `0` = aucun accès réseau sortant |
| `MAX_TILE_ZOOM` | `17` | Zoom maximal des tuiles téléchargées |
| `ALLOW_PRIVATE_TILE_HOSTS` | `0` | Autorise un serveur de tuiles en IP privée |
| `TILE_USER_AGENT` | *(voir `.env.example`)* | En-tête envoyé au fournisseur de tuiles |
| `LOG_LEVEL` | `INFO` | Verbosité |

---

## Vie privée et sécurité

- Les fichiers sources sont **supprimés dès la fin de l'analyse** ; seule une forme
  compacte des trajets est conservée le temps de la rétention.
- Téléversements et rendus sont purgés automatiquement (`RETENTION_HOURS`).
- Le filtre de confidentialité retire les points autour d'un lieu avant tout rendu
  ou export.
- Les URL de tuiles personnalisées sont refusées si elles pointent vers une adresse
  privée ou de bouclage (protection SSRF), sauf `ALLOW_PRIVATE_TILE_HOSTS=1`.
- Le conteneur tourne sous un utilisateur non privilégié ; seul `/data` est écrit.
- Aucune authentification n'est intégrée : derrière un accès public, placez
  l'application derrière un reverse proxy avec authentification.

---

## API HTTP

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/sante` | État du service, ffmpeg, stockage |
| `GET` | `/api/config` | Valeurs par défaut, modes, présélections, fournisseurs |
| `POST` | `/api/uploads` | Dépôt (multipart `fichiers`) → analyse + statistiques |
| `GET` | `/api/uploads/{id}` | Métadonnées d'un dépôt |
| `POST` | `/api/uploads/{id}/apercu` | Statistiques après filtres |
| `POST` | `/api/uploads/{id}/export?format=geojson\|gpx` | Export des trajets filtrés |
| `POST` | `/api/uploads/audio` | Dépôt d'une piste audio |
| `DELETE` | `/api/uploads/{id}` | Suppression immédiate |
| `POST` | `/api/jobs` | Lance un rendu `{upload_id, options}` |
| `GET` | `/api/jobs` · `/api/jobs/{id}` | Suivi des rendus |
| `POST` | `/api/jobs/{id}/annuler` | Annulation |
| `GET` | `/api/jobs/{id}/fichier/{video\|affiche\|apercu\|log}` | Téléchargement |

Exemple :

```bash
UP=$(curl -s -F "fichiers=@takeout.zip" http://localhost:5080/api/uploads | jq -r .id)
JOB=$(curl -s -X POST -H 'Content-Type: application/json' \
      -d "{\"upload_id\":\"$UP\",\"options\":{\"duration\":45,\"camera\":\"auto\"}}" \
      http://localhost:5080/api/jobs | jq -r .id)
curl -s http://localhost:5080/api/jobs/$JOB | jq '.status, .progress'
curl -o trajets.mp4 http://localhost:5080/api/jobs/$JOB/fichier/video
```

La documentation interactive est disponible sur `/docs`.

---

## Tester sans données personnelles

```bash
python tools/generer_exemple.py exemple.json --format semantic --jours 180
# formats disponibles : semantic | timeline | records
```

Déposez `exemple.json` dans le portail.

---

## Architecture

```
app/
  main.py        API HTTP + service des fichiers statiques
  parser.py      lecture des exports Google (tous formats) et repli générique
  processing.py  filtres, statistiques, chronologie pondérée image par image
  render.py      caméra, dessin des traces, habillage, encodage ffmpeg
  tiles.py       tuiles raster : téléchargement, cache disque, garde SSRF
  jobs.py        file d'attente des rendus, progression, annulation
  storage.py     dossiers, sérialisation compacte, purge
  static/        portail web (HTML/CSS/JS sans dépendance externe)
tools/           générateur de jeu d'essai
```

Chaîne de traitement : *fichiers → trajets datés → filtres → chronologie pondérée
(un poids par segment selon le mode temporel) → une position par image → calques
(fond / trace / traîne / halo / habillage) → flux brut vers ffmpeg*.

## Licence

MIT.
