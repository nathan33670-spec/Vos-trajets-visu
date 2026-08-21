# Vos trajets — générateur de vidéo accélérée

Application auto-hébergée (un seul conteneur Docker) qui transforme un export
**Google « Vos trajets » / Historique des positions (Timeline)** en **vidéo accélérée**
de tous vos déplacements : la trace se dessine dans le temps, la caméra suit le
parcours, la date et les kilomètres défilent.

Le portail web fait tout : dépôt du fichier, analyse, réglages, rendu, téléchargement.
Six réglages suffisent ; tout le reste est replié dans les options avancées.
Aucune donnée ne sort de votre serveur, à l'exception des requêtes de tuiles vers
le fournisseur de fond de carte choisi (`TILES_ENABLED=0` pour un fonctionnement
totalement hors ligne : la trace est alors dessinée en filigrane à la place).

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

## Le portail en trois écrans

**1. Récupérer votre fichier Google** — un guide numéroté, en deux versions
(ordinateur / téléphone), explique pas à pas comment obtenir l'export ; il n'y a
plus qu'à déposer l'archive sans la décompresser.

**2. Votre vidéo** — six réglages seulement sont visibles : format, durée,
mouvement de caméra, fond de carte, couleur des trajets, titre. Quatre
présélections (*Équilibré*, *Carte claire*, *Vue d'ensemble*, *Portrait*) et un
bouton **Aperçu rapide (15 s)** permettent de juger avant le rendu final.

**3. Vos vidéos** — avancement en direct, annulation, lecture dans la page,
téléchargement de la vidéo et de l'affiche.

### Options avancées (repliées par défaut)

| Rubrique | Réglages |
|---|---|
| Sélection | dates, modes de transport, distance et durée min/max, zone géographique, points aberrants, **masquage d'un rayon autour du domicile** |
| Format | largeur/hauteur libres, images par seconde, MP4 / WebM / GIF, qualité, anticrénelage |
| Rythme | temps réel, pauses compressées, trajets de durée égale, vitesse constante ; pauses de début et de fin |
| Caméra | zoom maximal, lissage, marge, dézoom final |
| Fond de carte | fournisseur de tuiles (Carto, OSM, OpenTopoMap, Esri, URL libre), opacité, noir et blanc, couleur de fond |
| Tracé | traîne cumulative / comète / éphémère, palettes, épaisseur, halo, point mobile, lieux visités |
| Habillage | sous-titre, texte de fin, filigrane, date, compteurs, légende, échelle, unités, couleurs, taille du texte |
| Sorties | musique de fond, boomerang, affiche JPEG, exports GeoJSON et GPX |

### Comment la caméra choisit son cadre

C'est le cœur du rendu, et le réglage par défaut :

- pendant la vidéo, la caméra **suit les trajets en cours** et reste assez
  proche pour qu'on lise les villes traversées ;
- à la fin, elle **dézoome d'un coup sur l'ensemble** ;
- ce plan large est cadré sur les **destinations habituelles** : un unique
  voyage à l'autre bout du monde est montré au moment où il a lieu, mais ne
  vient pas recentrer toute la vidéo sur un océan vide. Un trajet n'est écarté
  du plan large que s'il est au moins quatre fois plus éloigné que tous les
  autres — dix voyages aux États-Unis restent donc dans le cadre.

## Performances

Mesuré sur **un cœur** x86, avec un historique volumineux de **2 500 trajets /
49 000 points GPS** (≈ 3 ans de Timeline), pour une **vidéo de 30 s** :

| Rendu | Vitesse | Durée du rendu |
|---|---|---|
| 480p, 15 ips (*Aperçu rapide*) | 51 img/s | **4 s** |
| 720p 30 ips, plan fixe | 27 img/s | **34 s** |
| 1080p 30 ips, plan fixe | 14 img/s | **1 min 03** |
| 1080p 30 ips, caméra qui suit les trajets | 3,3 img/s | **4 min 37** |

### Mémoire — le point critique sur un NAS

Les gros fichiers sont lus **élément par élément** : charger l'arbre JSON complet
coûte environ sept fois la taille du fichier en mémoire, de quoi faire tuer le
conteneur (et le navigateur affiche alors « connexion interrompue »).
Mesures sur un export `Records.json` de **323 Mo / 1,2 million de points** :

| Étape | Avant | Après |
|---|---|---|
| Analyse du fichier | 2 184 Mo | **518 Mo** |
| Pic pendant le rendu | 1 011 Mo | **297 Mo** |
| Rendu 480p de 8 s | 465 s | **48 s** |

Trois mécanismes y contribuent : lecture au fil de l'eau, points stockés en
tableaux compacts (8 octets par valeur au lieu d'une trentaine), et
simplification plafonnée à 120 000 segments — au-delà, le détail n'est plus
visible à l'image mais coûte à chaque image.

Une archive Takeout n'est de plus dépliée que pour la Timeline détaillée :
`Records.json` n'est ouvert que si rien d'autre n'a donné de trajets.
**Un NAS avec 1 Go de RAM disponible suffit** ; la page d'accueil affiche la
mémoire et l'espace disque vus par le conteneur.

Pour accélérer un rendu : **Aperçu rapide** pour régler, puis anticrénelage ×1,
halo désactivé, ou caméra en plan fixe — qui n'ajoute que les nouveaux segments
à chaque image et reste rapide quelle que soit la taille de l'historique.

Le rendu tourne en tâche de fond : la page affiche l'avancement et le temps
restant estimé, et le rendu peut être annulé à tout moment.
`MAX_CONCURRENT_JOBS` limite le nombre de rendus simultanés.

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
