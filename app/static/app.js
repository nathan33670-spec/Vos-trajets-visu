/* Portail « Vos trajets » — formulaire déclaratif + suivi des rendus. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  kids.flat().forEach(c => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
  return n;
};

const state = { config: null, uploadId: null, meta: null, jobs: new Map(), polling: null };

/* ------------------------------------------------------------------ champs */
const SECTIONS = [
  {
    id: 'filtres', title: '🔎 Sélection des trajets', open: true, fields: [
      { k: 'date_start', t: 'date', label: 'Date de début' },
      { k: 'date_end', t: 'date', label: 'Date de fin (incluse)' },
      { k: 'modes', t: 'modes', label: 'Modes de transport', wide: true,
        desc: 'Aucun coché = tous les modes.' },
      { k: 'min_trip_km', t: 'num', label: 'Distance min. (km)', min: 0, step: 0.1 },
      { k: 'max_trip_km', t: 'num', label: 'Distance max. (km, 0 = illimité)', min: 0, step: 1 },
      { k: 'min_trip_seconds', t: 'num', label: 'Durée min. (s)', min: 0, step: 30 },
      { k: 'bbox', t: 'text', label: 'Zone géographique',
        placeholder: 'lon_min, lat_min, lon_max, lat_max', wide: true,
        desc: 'Ne garde que les trajets passant dans ce rectangle. Vide = monde entier.' },
      { k: 'drop_outliers', t: 'bool', label: 'Supprimer les points aberrants (sauts GPS)' },
      { k: 'privacy_lat', t: 'num', label: 'Confidentialité — latitude', step: 0.00001 },
      { k: 'privacy_lon', t: 'num', label: 'Confidentialité — longitude', step: 0.00001 },
      { k: 'privacy_radius_m', t: 'num', label: 'Rayon masqué (m)', min: 0, step: 50,
        desc: 'Masque les points autour du domicile avant publication.' },
    ]
  },
  {
    id: 'video', title: '🎬 Format vidéo', open: true, fields: [
      { k: '_res', t: 'select', label: 'Format de la vidéo', local: true, simple: true, options: [
        ['1920x1080', '1080p paysage'], ['1280x720', '720p paysage'],
        ['2560x1440', '1440p paysage'], ['3840x2160', '4K paysage'],
        ['1080x1920', '1080x1920 vertical'], ['1080x1080', 'Carré 1080'],
        ['854x480', '480p (test)'], ['custom', 'Personnalisée…']] },
      { k: 'width', t: 'num', label: 'Largeur (px)', min: 160, max: 3840, step: 2 },
      { k: 'height', t: 'num', label: 'Hauteur (px)', min: 160, max: 2160, step: 2 },
      { k: 'fps', t: 'range', label: 'Images / s', min: 5, max: 60, step: 1 },
      { k: 'duration', t: 'range', label: 'Durée de la vidéo (s)', min: 5, max: 300, step: 1, simple: true },
      { k: 'container', t: 'select', label: 'Format', options: [
        ['mp4', 'MP4 (H.264)'], ['webm', 'WebM (VP9)'], ['gif', 'GIF animé']] },
      { k: 'quality', t: 'select', label: 'Qualité', options: [
        ['low', 'Légère'], ['medium', 'Moyenne'], ['high', 'Haute'], ['max', 'Maximale']] },
      { k: 'supersample', t: 'select', label: 'Anticrénelage', options: [
        ['1', 'Aucun (rapide)'], ['2', '×2 (recommandé)'], ['3', '×3 (lent)']] },
    ]
  },
  {
    id: 'rythme', title: '⏱️ Rythme temporel', fields: [
      { k: 'time_mode', t: 'select', label: 'Écoulement du temps', options: [
        ['compress', 'Réel avec pauses compressées'], ['real', 'Strictement proportionnel'],
        ['equal', 'Chaque trajet dure autant'], ['distance', 'Vitesse constante (par distance)']] },
      { k: 'gap_max_seconds', t: 'num', label: 'Pause max. conservée (s)', min: 0, step: 60,
        desc: 'Au-delà, les arrêts sont compressés.' },
      { k: 'hold_first_seconds', t: 'num', label: 'Pause au début (s)', min: 0, step: 0.5 },
      { k: 'hold_last_seconds', t: 'num', label: 'Pause à la fin (s)', min: 0, step: 0.5 },
    ]
  },
  {
    id: 'camera', title: '🎥 Caméra', fields: [
      { k: 'camera', t: 'select', label: 'Mouvement de caméra', simple: true, options: [
        ['auto', 'Suit les trajets, dézoom final (conseillé)'],
        ['fit_all', 'Plan fixe sur toute la zone'],
        ['follow', 'Collée au point qui se déplace'],
        ['trip', 'Cadre chaque trajet']] },
      { k: 'follow_zoom', t: 'range', label: 'Zoom max.', min: 1, max: 19, step: 0.5 },
      { k: 'camera_smoothing', t: 'range', label: 'Lissage', min: 0, max: 0.99, step: 0.01 },
      { k: 'padding', t: 'range', label: 'Marge du cadrage', min: 0, max: 0.4, step: 0.01 },
      { k: 'zoom_out_end', t: 'bool', label: 'Dézoomer sur l\'ensemble à la fin' },
    ]
  },
  {
    id: 'carte', title: '🗺️ Fond de carte', fields: [
      { k: 'map_style', t: 'select', label: 'Fond de carte', simple: true, options: [
        ['tiles', 'Carte (recommandé)'], ['ghost', 'Trace seule (hors ligne)'],
        ['none', 'Fond uni'], ['grid', 'Grille']] },
      { k: 'tile_provider', t: 'select', label: 'Fournisseur de tuiles', options: [] },
      { k: 'tile_url', t: 'text', label: 'URL personnalisée', wide: true,
        placeholder: 'https://serveur/{z}/{x}/{y}.png' },
      { k: 'tile_opacity', t: 'range', label: 'Opacité des tuiles', min: 0, max: 1, step: 0.05 },
      { k: 'tile_grayscale', t: 'bool', label: 'Tuiles en noir et blanc' },
      { k: 'ghost_opacity', t: 'range', label: 'Opacité de la trace fantôme', min: 0, max: 1, step: 0.01 },
      { k: 'background', t: 'color', label: 'Couleur de fond' },
    ]
  },
  {
    id: 'trace', title: '✨ Style du tracé', open: true, fields: [
      { k: 'color_mode', t: 'select', label: 'Couleur des trajets', simple: true, options: [
        ['mode', 'Le mode de transport'], ['speed', 'La vitesse'],
        ['time', 'La date'], ['single', 'Couleur unique']] },
      { k: 'line_color', t: 'color', label: 'Couleur unique / accent' },
      { k: 'palette', t: 'select', label: 'Palette (vitesse / date)', options: [
        ['neon', 'Néon'], ['viridis', 'Viridis'], ['inferno', 'Inferno'],
        ['ice', 'Glace'], ['fire', 'Feu'], ['rainbow', 'Arc-en-ciel']] },
      { k: 'line_width', t: 'range', label: 'Épaisseur du trait', min: 0.4, max: 12, step: 0.2 },
      { k: 'trail_mode', t: 'select', label: 'Traîne', options: [
        ['cumulative', 'Cumulative (tout reste visible)'], ['comet', 'Comète (dégradé)'],
        ['fade', 'Éphémère (seule la traîne)']] },
      { k: 'trail_opacity', t: 'range', label: 'Opacité de l\'historique', min: 0.02, max: 1, step: 0.02 },
      { k: 'fade_seconds', t: 'range', label: 'Longueur de la traîne (s)', min: 0.2, max: 60, step: 0.2 },
      { k: 'glow', t: 'bool', label: 'Halo lumineux' },
      { k: 'glow_strength', t: 'range', label: 'Intensité du halo', min: 0, max: 2, step: 0.05 },
      { k: 'head_dot', t: 'bool', label: 'Point mobile en tête' },
      { k: 'head_size', t: 'range', label: 'Taille du point', min: 0, max: 30, step: 0.5 },
      { k: 'head_pulse', t: 'bool', label: 'Pulsation du point' },
      { k: 'show_places', t: 'bool', label: 'Afficher les lieux visités' },
    ]
  },
  {
    id: 'habillage', title: '🏷️ Habillage', fields: [
      { k: 'title', t: 'text', label: 'Titre affiché au début', simple: true,
        placeholder: 'Mes trajets 2024' },
      { k: 'subtitle', t: 'text', label: 'Sous-titre', wide: true },
      { k: 'title_seconds', t: 'num', label: 'Durée du carton titre (s)', min: 0, step: 0.5 },
      { k: 'outro_text', t: 'text', label: 'Texte de fin', wide: true },
      { k: 'watermark', t: 'text', label: 'Signature / filigrane' },
      { k: 'show_clock', t: 'bool', label: 'Afficher la date' },
      { k: 'clock_format', t: 'select', label: 'Format de date', options: [
        ['date', 'JJ/MM/AAAA'], ['datetime', 'JJ/MM/AAAA HH:MM'],
        ['month', 'Mois AAAA'], ['year', 'AAAA'], ['none', 'Aucun']] },
      { k: 'show_stats', t: 'bool', label: 'Compteur de distance' },
      { k: 'show_progress', t: 'bool', label: 'Barre de progression' },
      { k: 'show_legend', t: 'bool', label: 'Légende des modes' },
      { k: 'show_scalebar', t: 'bool', label: 'Échelle cartographique' },
      { k: 'units', t: 'select', label: 'Unités', options: [['metric', 'Kilomètres'], ['imperial', 'Miles']] },
      { k: 'overlay_color', t: 'color', label: 'Couleur du texte' },
      { k: 'font_scale', t: 'range', label: 'Taille du texte', min: 0.5, max: 2.5, step: 0.05 },
      { k: 'fade_in', t: 'bool', label: 'Fondu d\'ouverture' },
    ]
  },
  {
    id: 'extras', title: '🎵 Audio et sorties', fields: [
      { k: '_audio', t: 'file', label: 'Musique de fond', accept: 'audio/*', wide: true,
        desc: 'MP3, M4A, WAV, OGG, FLAC. La piste est bouclée puis coupée à la fin.' },
      { k: 'audio_fade', t: 'bool', label: 'Fondu audio en sortie' },
      { k: 'loop_video', t: 'bool', label: 'Boomerang (aller-retour)' },
      { k: 'make_poster', t: 'bool', label: 'Générer une affiche JPEG' },
    ]
  },
];

/* -------------------------------------------------------------- rendu form */
function buildForm(defaults) {
  const simple = $('#form-simple');
  const advanced = $('#form-advanced');
  simple.innerHTML = '';
  advanced.innerHTML = '';

  const simpleGrid = el('div', { class: 'grid' });
  for (const sec of SECTIONS) {
    const rest = sec.fields.filter(f => !f.simple);
    for (const f of sec.fields.filter(f => f.simple)) simpleGrid.appendChild(buildField(f, defaults));
    if (!rest.length) continue;
    const grid = el('div', { class: 'grid' });
    for (const f of rest) grid.appendChild(buildField(f, defaults));
    advanced.appendChild(el('details', { class: 'section' },
      el('summary', {}, sec.title), grid));
  }
  simple.appendChild(simpleGrid);

  const sel = $('[name=tile_provider]');
  if (sel) {
    sel.innerHTML = '';
    for (const [id, info] of Object.entries(state.config.tile_providers))
      sel.appendChild(el('option', { value: id }, info.name));
    sel.value = defaults.tile_provider;
  }
  syncResolution();
  for (const root of [simple, advanced]) {
    root.addEventListener('input', onFormInput);
    root.addEventListener('change', onFormInput);
  }
}

function buildField(f, defaults) {
  const wrap = el('div', { class: 'field' + (f.wide ? ' wide' : '') });
  const val = defaults[f.k];
  let input;

  if (f.t === 'bool') {
    wrap.classList.add('check');
    input = el('input', { type: 'checkbox', name: f.k, id: 'f_' + f.k });
    input.checked = !!val;
    wrap.append(input, el('label', { for: 'f_' + f.k }, f.label));
    if (f.desc) wrap.appendChild(el('div', { class: 'desc' }, f.desc));
    return wrap;
  }

  const label = el('label', { for: 'f_' + f.k }, f.label);
  if (f.t === 'range') label.appendChild(el('span', { class: 'val' }, String(val ?? '')));
  wrap.appendChild(label);

  if (f.t === 'select') {
    input = el('select', { name: f.k, id: 'f_' + f.k });
    for (const [v, txt] of (f.options || [])) input.appendChild(el('option', { value: v }, txt));
    input.value = String(val ?? '');
  } else if (f.t === 'modes') {
    input = el('div', { class: 'multi', id: 'f_' + f.k });
    for (const m of state.config.modes) {
      const cb = el('input', { type: 'checkbox', value: m.id, name: 'mode' });
      cb.checked = (defaults.modes || []).includes(m.id);
      input.appendChild(el('label', { title: m.label },
        cb, el('i', { style: `background:${m.color};width:9px;height:9px;border-radius:50%;display:inline-block` }),
        m.label));
    }
  } else if (f.t === 'file') {
    input = el('input', { type: 'file', name: f.k, id: 'f_' + f.k, accept: f.accept || '' });
  } else if (f.t === 'color') {
    input = el('input', { type: 'color', name: f.k, id: 'f_' + f.k, value: val || '#ffffff' });
  } else {
    const type = f.t === 'num' ? 'number' : f.t === 'range' ? 'range' : f.t === 'date' ? 'date' : 'text';
    input = el('input', {
      type, name: f.k, id: 'f_' + f.k,
      min: f.min, max: f.max, step: f.step, placeholder: f.placeholder || '',
      value: val === null || val === undefined ? '' : String(val),
    });
  }
  wrap.appendChild(input);
  if (f.desc) wrap.appendChild(el('div', { class: 'desc' }, f.desc));
  return wrap;
}

function onFormInput(ev) {
  const t = ev.target;
  if (t.type === 'range') {
    const v = t.closest('.field')?.querySelector('.val');
    if (v) v.textContent = t.value;
  }
  if (t.name === 'width' || t.name === 'height') syncResolution(true);
  if (t.name === '_res' && t.value !== 'custom') {
    const [w, h] = t.value.split('x');
    $('[name=width]').value = w; $('[name=height]').value = h;
  }
  if (t.name === '_audio' && t.files?.length) uploadAudio(t.files[0]);
  toggleVisibility();
}

function syncResolution(fromSize = false) {
  const sel = $('[name=_res]');
  if (!sel) return;
  const cur = `${$('[name=width]').value}x${$('[name=height]').value}`;
  const known = [...sel.options].some(o => o.value === cur);
  if (fromSize || !sel.value) sel.value = known ? cur : 'custom';
}

function toggleVisibility() {
  const o = readOptions(false);
  const show = (name, cond) => {
    const f = document.querySelector(`[name="${name}"]`)?.closest('.field');
    if (f) f.classList.toggle('hidden', !cond);
  };
  show('tile_provider', o.map_style === 'tiles');
  show('tile_url', o.map_style === 'tiles' && o.tile_provider === 'custom');
  show('tile_opacity', o.map_style === 'tiles');
  show('tile_grayscale', o.map_style === 'tiles');
  show('ghost_opacity', o.map_style === 'ghost');
  show('palette', o.color_mode === 'speed' || o.color_mode === 'time');
  show('follow_zoom', o.camera !== 'fit_all');
  show('camera_smoothing', o.camera !== 'fit_all');
  show('trail_opacity', o.trail_mode !== 'fade');
  show('glow_strength', o.glow);
  show('head_size', o.head_dot);
  show('head_pulse', o.head_dot);
  show('gap_max_seconds', o.time_mode === 'compress');
  show('title_seconds', !!o.title);
  show('clock_format', o.show_clock);
}

/* --------------------------------------------------------- lecture options */
const NULLABLE = new Set(['date_start', 'date_end', 'privacy_lat', 'privacy_lon',
  'tile_url', 'bbox', 'audio_upload_id']);

function readOptions(strict = true) {
  const o = {};
  for (const sec of SECTIONS) {
    for (const f of sec.fields) {
      if (f.local || f.k.startsWith('_')) continue;
      const node = document.querySelector(`[name="${f.k}"]`);
      if (f.t === 'modes') {
        o.modes = [...document.querySelectorAll('input[name=mode]:checked')].map(c => c.value);
        continue;
      }
      if (!node) continue;
      if (f.t === 'bool') { o[f.k] = node.checked; continue; }
      const raw = node.value;
      if (f.k === 'bbox') {
        const parts = raw.split(',').map(s => parseFloat(s.trim())).filter(n => !isNaN(n));
        o.bbox = parts.length === 4 ? parts : null;
        continue;
      }
      // Champ vide : null si le champ l'accepte, sinon on laisse le défaut serveur.
      if (raw === '') { if (NULLABLE.has(f.k)) o[f.k] = null; continue; }
      o[f.k] = (f.t === 'num' || f.t === 'range') ? parseFloat(raw)
        : (f.k === 'supersample' || ['width', 'height', 'fps'].includes(f.k)) ? parseInt(raw, 10)
          : raw;
    }
  }
  o.supersample = parseInt(document.querySelector('[name=supersample]').value, 10);
  o.audio_upload_id = state.audioId || null;
  return o;
}

function applyOptions(patch) {
  for (const [k, v] of Object.entries(patch)) {
    const node = document.querySelector(`[name="${k}"]`);
    if (!node) continue;
    if (node.type === 'checkbox') node.checked = !!v;
    else node.value = v;
    const valSpan = node.closest('.field')?.querySelector('.val');
    if (valSpan) valSpan.textContent = node.value;
  }
  syncResolution(true);
  toggleVisibility();
}

/* ------------------------------------------------------------------ réseau */
async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
  return data;
}

function humanSize(bytes) {
  return bytes > 1048576 ? (bytes / 1048576).toFixed(0) + ' Mo'
    : (bytes / 1024).toFixed(0) + ' Ko';
}

async function uploadFiles(files, attempt = 0) {
  const err = $('#upload-error');
  const status = $('#upload-status');
  err.classList.add('hidden');

  const total = [...files].reduce((n, f) => n + f.size, 0);
  const limit = (state.config?.limits?.max_upload_mb || 1024) * 1048576;
  if (total > limit) {
    err.innerHTML = `Fichier trop volumineux : ${humanSize(total)} pour une limite de ` +
      `${humanSize(limit)}.<br>Augmentez <code>MAX_UPLOAD_MB</code> dans le fichier ` +
      `<code>.env</code> du serveur, ou n'envoyez que le dossier ` +
      `« Historique des positions (Timeline) » de l'archive.`;
    err.classList.remove('hidden');
    return;
  }

  const prog = $('#upload-progress');
  prog.classList.remove('hidden');
  const bar = $('.bar', prog);
  bar.style.width = '3%';
  status.classList.remove('hidden');
  status.textContent = `Envoi de ${humanSize(total)}…`;

  const fd = new FormData();
  [...files].forEach(f => fd.append('fichiers', f));
  try {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    const meta = await new Promise((resolve, reject) => {
      xhr.open('POST', '/api/uploads');
      xhr.timeout = 0;                       // un gros fichier peut être long
      xhr.upload.onprogress = e => {
        if (!e.lengthComputable) return;
        const pct = e.loaded / e.total;
        bar.style.width = (3 + 62 * pct) + '%';
        status.textContent = pct >= 1 ? 'Analyse du fichier sur le serveur…'
          : `Envoi : ${humanSize(e.loaded)} / ${humanSize(e.total)}`;
      };
      xhr.upload.onload = () => { status.textContent = 'Analyse du fichier sur le serveur…'; };
      xhr.onload = () => {
        bar.style.width = '100%';
        let d = null;
        try { d = JSON.parse(xhr.responseText); } catch { d = null; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(d);
        else reject(Object.assign(new Error(d?.detail || `Erreur ${xhr.status}`),
          { status: xhr.status }));
      };
      xhr.onerror = () => reject(Object.assign(new Error('connexion interrompue'), { status: 0 }));
      xhr.onabort = () => reject(Object.assign(new Error('envoi annulé'), { status: -1 }));
      xhr.send(fd);
    });
    state.uploadId = meta.id;
    state.meta = meta;
    status.textContent = `${meta.stats.trips} trajets reconnus en ${meta.parse_seconds} s.`;
    showStats(meta.stats, meta);
    $('#step-config').classList.remove('hidden');
    prefillDates(meta.stats);
    $('#step-config').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    // Coupure réseau : une seconde tentative avant d'embêter l'utilisateur.
    if (e.status === 0 && attempt < 1) {
      status.textContent = 'Connexion interrompue, nouvelle tentative…';
      return uploadFiles(files, attempt + 1);
    }
    status.classList.add('hidden');
    err.innerHTML = escapeHtml(e.message) + (e.status === 0
      ? '<br>Si le NAS est derrière un reverse proxy, vérifiez sa limite de taille ' +
        "d'envoi (<code>client_max_body_size</code> sous nginx)." : '');
    err.classList.remove('hidden');
  } finally {
    state.xhr = null;
    setTimeout(() => prog.classList.add('hidden'), 800);
  }
}

async function uploadAudio(file) {
  const fd = new FormData();
  fd.append('fichier', file);
  try {
    const r = await api('/api/uploads/audio', { method: 'POST', body: fd });
    state.audioId = r.id;
    flash(`Musique « ${r.nom} » ajoutée.`, 'info');
  } catch (e) { flash('Audio refusé : ' + e.message, 'warn'); }
}

function prefillDates(stats) {
  if (stats.start_date && !$('[name=date_start]').value) $('[name=date_start]').value = stats.start_date;
  if (stats.end_date && !$('[name=date_end]').value) $('[name=date_end]').value = stats.end_date;
}

function showStats(stats, meta) {
  const box = $('#stats');
  const days = stats.start && stats.end ? Math.max(1, Math.round((stats.end - stats.start) / 86400)) : 0;
  const fr = iso => iso ? iso.split('-').reverse().join('/') : '—';
  const cards = [
    ['Trajets', stats.trips.toLocaleString('fr-FR'), false],
    ['Distance', stats.distance_km.toLocaleString('fr-FR') + ' km', false],
    ['Points GPS', stats.points.toLocaleString('fr-FR'), false],
    ['Période', days + ' j', false],
    ['Du', fr(stats.start_date), true],
    ['Au', fr(stats.end_date), true],
  ];
  box.innerHTML = '';
  cards.forEach(([k, v, small]) => box.appendChild(
    el('div', { class: 'stat' }, el('div', { class: 'v' + (small ? ' small' : '') }, String(v)),
      el('div', { class: 'k' }, k))));

  const chips = $('#mode-chips');
  chips.innerHTML = '';
  const colors = Object.fromEntries(state.config.modes.map(m => [m.id, m]));
  for (const [mode, d] of Object.entries(stats.per_mode || {})) {
    const info = colors[mode] || { label: mode, color: '#888' };
    chips.appendChild(el('span', { class: 'chip' },
      el('i', { style: `background:${info.color}` }),
      `${info.label} · ${d.count} · ${d.distance_km.toLocaleString('fr-FR')} km`));
  }

  const warn = $('#parse-warnings');
  const notes = (meta?.warnings || []).filter(Boolean);
  if (notes.length) {
    warn.innerHTML = '<strong>Remarques d\'analyse</strong><ul>' +
      notes.slice(0, 6).map(w => `<li>${escapeHtml(w)}</li>`).join('') + '</ul>';
    warn.classList.remove('hidden');
  } else warn.classList.add('hidden');
}

const escapeHtml = s => String(s).replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function flash(msg, kind = 'info') {
  const box = el('div', { class: `alert ${kind}` }, msg);
  $('#step-config').insertBefore(box, $('#form-simple'));
  setTimeout(() => box.remove(), 6000);
}

/* ------------------------------------------------------------------ rendus */
async function startRender(overrides = {}) {
  if (!state.uploadId) return;
  const options = { ...readOptions(), ...overrides };
  try {
    const job = await api('/api/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upload_id: state.uploadId, options }),
    });
    state.jobs.set(job.id, job);
    $('#step-render').classList.remove('hidden');
    renderJobs();
    $('#step-render').scrollIntoView({ behavior: 'smooth', block: 'start' });
    startPolling();
  } catch (e) {
    flash('Rendu impossible : ' + e.message, 'error');
  }
}

function startPolling() {
  if (state.polling) return;
  state.polling = setInterval(async () => {
    const pending = [...state.jobs.values()].filter(j => ['queued', 'running'].includes(j.status));
    if (!pending.length) { clearInterval(state.polling); state.polling = null; return; }
    for (const j of pending) {
      try {
        const fresh = await api('/api/jobs/' + j.id);
        state.jobs.set(fresh.id, fresh);
      } catch { /* réseau : on retentera */ }
    }
    renderJobs();
  }, 1200);
}

function renderJobs() {
  const root = $('#jobs');
  root.innerHTML = '';
  const jobs = [...state.jobs.values()].sort((a, b) => b.created - a.created);
  for (const j of jobs) {
    const o = j.options || {};
    const head = el('header', {},
      el('div', {}, el('strong', {}, `${o.width}×${o.height} · ${o.duration}s · ${o.fps} ips · ${(o.container || '').toUpperCase()}`),
        el('div', { class: 'msg' }, j.message || '')),
      el('span', { class: 'badge ' + j.status }, {
        queued: 'En attente', running: 'Rendu en cours', done: 'Terminé',
        error: 'Erreur', cancelled: 'Annulé'
      }[j.status] || j.status));
    const card = el('div', { class: 'job' }, head);

    if (['queued', 'running'].includes(j.status)) {
      const bar = el('div', { class: 'bar' });
      bar.style.width = Math.round((j.progress || 0) * 100) + '%';
      card.appendChild(el('div', { class: 'progress' }, bar));
      card.appendChild(el('button', {
        class: 'danger', onclick: async () => {
          try { await api(`/api/jobs/${j.id}/annuler`, { method: 'POST' }); } catch { }
        }
      }, 'Annuler'));
    }
    if (j.status === 'error') {
      card.appendChild(el('div', { class: 'alert error' }, j.error || 'Erreur inconnue'));
    }
    if (j.warnings?.length) {
      card.appendChild(el('div', { class: 'alert warn', html: '<ul>' +
        j.warnings.slice(0, 5).map(w => `<li>${escapeHtml(w)}</li>`).join('') + '</ul>' }));
    }
    if (j.status === 'done' && j.result) {
      if (o.container === 'gif') {
        card.appendChild(el('img', { src: j.result.video_url, style: 'width:100%;border-radius:10px;margin-top:.8rem' }));
      } else {
        const v = el('video', { controls: 'controls', playsinline: 'playsinline', preload: 'metadata',
          poster: j.result.poster_url || '' });
        v.src = j.result.video_url;
        card.appendChild(v);
      }
      const mo = (j.result.size_bytes / 1048576).toFixed(1);
      const files = el('div', { class: 'files' },
        el('a', { href: j.result.video_url, download: j.result.filename }, `⬇️ Vidéo (${mo} Mo)`));
      if (j.result.poster_url)
        files.appendChild(el('a', { href: j.result.poster_url, download: 'affiche.jpg' }, '🖼️ Affiche'));
      files.appendChild(el('a', { href: '#', onclick: e => { e.preventDefault(); applyOptions(o); flash('Réglages rechargés dans le formulaire.'); } }, '↩️ Reprendre ces réglages'));
      card.appendChild(files);
    }
    root.appendChild(card);
  }
}

/* -------------------------------------------------------------------- init */
async function init() {
  state.config = await api('/api/config');
  buildForm(state.config.defaults);
  toggleVisibility();

  const presets = $('#presets');
  for (const [id, p] of Object.entries(state.config.presets)) {
    presets.appendChild(el('button', {
      class: 'preset', onclick: () => { applyOptions(p.options); flash(`Présélection « ${p.label} » appliquée.`); }
    }, el('strong', {}, p.label), el('span', {}, p.description)));
  }

  const dz = $('#dropzone'), input = $('#file-input');
  dz.addEventListener('click', e => { if (e.target.tagName !== 'BUTTON') input.click(); });
  $('#browse').addEventListener('click', e => { e.stopPropagation(); input.click(); });
  input.addEventListener('change', () => input.files.length && uploadFiles(input.files));
  ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, e => {
    e.preventDefault(); dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, e => {
    e.preventDefault(); dz.classList.remove('over');
  }));
  dz.addEventListener('drop', e => e.dataTransfer.files.length && uploadFiles(e.dataTransfer.files));

  $('#btn-render').addEventListener('click', () => startRender());
  $('#btn-quick').addEventListener('click', () => {
    const o = readOptions();
    const vertical = o.height > o.width;
    startRender({
      width: vertical ? 480 : 854, height: vertical ? 854 : 480,
      fps: 15, duration: Math.min(15, o.duration), supersample: 1,
      quality: 'medium', glow: false,
    });
  });
  $('#btn-estimate').addEventListener('click', async () => {
    try {
      const r = await api(`/api/uploads/${state.uploadId}/apercu`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ options: readOptions() }),
      });
      showStats(r.stats, { warnings: r.notes });
    } catch (e) { flash(e.message, 'error'); }
  });
  const exportAs = async fmt => {
    const res = await fetch(`/api/uploads/${state.uploadId}/export?format=${fmt}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options: readOptions() }),
    });
    if (!res.ok) { flash('Export impossible.', 'error'); return; }
    const blob = await res.blob();
    const a = el('a', { href: URL.createObjectURL(blob), download: 'trajets.' + fmt });
    document.body.appendChild(a); a.click(); a.remove();
  };
  $('#btn-geojson').addEventListener('click', () => exportAs('geojson'));
  $('#btn-gpx').addEventListener('click', () => exportAs('gpx'));

  const advBtn = $('#toggle-advanced');
  advBtn.addEventListener('click', () => {
    const open = $('#form-advanced').classList.toggle('hidden');
    advBtn.setAttribute('aria-expanded', String(!open));
  });

  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
    $('#guide-ordi').classList.toggle('hidden', tab.dataset.tab !== 'ordi');
    $('#guide-tel').classList.toggle('hidden', tab.dataset.tab !== 'tel');
  }));

  try {
    const h = await api('/api/sante');
    const go = o => o ? (o / 1073741824).toFixed(1) + ' Go' : '?';
    const tight = h.memoire_libre_octets && h.memoire_libre_octets < 1073741824;
    $('#health').innerHTML =
      `<span class="dot ${h.ffmpeg ? '' : 'bad'}"></span>${h.ffmpeg ? 'Service prêt' : 'ffmpeg manquant'}` +
      `<br>${h.tuiles_activees ? 'Tuiles autorisées' : 'Mode hors ligne'}` +
      `<br><span class="${tight ? 'tight' : ''}">${go(h.memoire_libre_octets)} RAM · ` +
      `${go(h.disque_libre_octets)} disque</span>`;
  } catch { $('#health').textContent = ''; }

  try {
    const { jobs } = await api('/api/jobs');
    jobs.forEach(j => state.jobs.set(j.id, j));
    if (jobs.length) { $('#step-render').classList.remove('hidden'); renderJobs(); startPolling(); }
  } catch { }
}

init().catch(e => {
  document.body.prepend(el('div', { class: 'alert error' }, 'Initialisation impossible : ' + e.message));
});
