// xrninsta's page: the sheet's Dashboard, its Songs and its OnHold, on the home server.
// Everything a person typed goes in through textContent: the host is shared with other apps' pages.

const $ = (sel) => document.querySelector(sel);
const CLIENT = { 'X-Xrn-Client': 'xrninsta' };
const BOT_WORDS = {
  webhook: "The bot isn't taking messages yet: Telegram still sends them to the Apps Script. Run webhook-off on the server to move them here.",
  off: 'The bot has no token yet, so only this page is running.',
  unreachable: "The bot can't reach Telegram at the moment. It keeps trying.",
  refused: 'Telegram refused the bot',
  error: 'The bot ran into a problem',
};

let data = null; // {genres, colors, nextDay, songs, holds}
let open = null; // the record being edited: "song:<id>", "hold:<id>" or "promote:<id>"

class Stop extends Error {}
class ApiError extends Error {
  constructor(status, message, doc) { super(message); this.status = status; this.doc = doc; }
}

async function api(path, { method = 'GET', body, tag, auth } = {}) {
  const headers = { ...CLIENT };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (tag !== undefined) headers['If-Match'] = `"${tag}"`;
  if (auth) headers.Authorization = auth;
  const r = await fetch(path, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && !auth) { showSignin(); throw new Stop(); }
  const text = await r.text();
  let doc = null;
  try { doc = JSON.parse(text); } catch { /* plain text */ }
  if (!r.ok) throw new ApiError(r.status, doc && doc.error ? doc.error : text, doc);
  return doc;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

const fold = (s) => String(s || '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '').replace(/đ/g, 'd');
const splitArtists = (t) => String(t || '').split(',').map((s) => s.trim()).filter(Boolean);
const byName = (a, b) => a.localeCompare(b);

function genreChip(genre) {
  const chip = el('span', { class: 'chip', text: genre || '?' });
  const color = data.colors[genre];
  if (color) chip.style.setProperty('--genre', color);
  else chip.classList.add('odd');
  return chip;
}

function genreSelect(select, { all = false, value } = {}) {
  select.replaceChildren(...(all ? ['ALL'] : []).concat(data.genres).map((g) => el('option', { value: g, text: g === 'ALL' ? 'All genres' : g })));
  if (value && [...select.options].some((o) => o.value === value)) select.value = value;
}

// ---- signing in --------------------------------------------------------------------------------

function showSignin() {
  $('#app').hidden = true;
  $('#signin').hidden = false;
  $('#signin [name=user]').focus();
}

$('#signin').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const bytes = new TextEncoder().encode(`${form.user.value}:${form.password.value}`);
  const auth = 'Basic ' + btoa(String.fromCharCode(...bytes));
  const error = $('#signin-error');
  error.hidden = true;
  try {
    await api('session', { method: 'POST', auth });
    form.password.value = '';
    await load();
  } catch (err) {
    error.textContent = err.status === 401 ? "That name and password don't match." : err.status === 403 ? 'These songs belong to the server owner.' : err.message;
    error.hidden = false;
  }
});

$('#signout').addEventListener('click', async () => {
  await api('session', { method: 'DELETE' }).catch(() => {});
  showSignin();
});

// ---- loading -----------------------------------------------------------------------------------

async function load() {
  try {
    data = await api('api/data');
  } catch (err) {
    if (!(err instanceof Stop)) alert(err.message);
    return;
  }
  $('#signin').hidden = true;
  $('#app').hidden = false;
  if (!$('#artist-genre').options.length) {
    genreSelect($('#artist-genre'), { all: true });
    genreSelect($('#song-genre'), { all: true });
    genreSelect($('#add-song [name=genre]'));
  }
  $('#add-song [name=day]').placeholder = `Day (next: ${data.nextDay})`;
  const valid = data.songs.filter((s) => data.genres.includes(s.genre)).length;
  $('#counts').textContent = `${valid} songs · ${data.holds.length} on hold`;
  render();
  api('api/status').then(showBot).catch(() => {});
}

function showBot(status) {
  const banner = $('#bot');
  const words = BOT_WORDS[status.state];
  banner.hidden = !words;
  if (words) banner.textContent = status.state === 'refused' || status.state === 'error' ? `${words}: ${status.error}` : words;
}

function render() {
  renderArtists();
  renderSongs();
  renderHolds();
}

// ---- by artist (the sheet's Dashboard) -----------------------------------------------------------

function renderArtists() {
  const genre = $('#artist-genre').value || 'ALL';
  const oldest = $('#artist-sort').value === 'old';
  const rows = data.songs
    .filter((s) => s.title.trim() && s.artists.trim() && Number.isFinite(s.day) && s.day >= -5 && data.genres.includes(s.genre))
    .filter((s) => genre === 'ALL' || s.genre === genre);
  const byArtist = new Map();
  const genreCount = new Map();
  for (const s of rows) {
    genreCount.set(s.genre, (genreCount.get(s.genre) || 0) + 1);
    for (const a of splitArtists(s.artists)) {
      if (!byArtist.has(a)) byArtist.set(a, []);
      byArtist.get(a).push(s);
    }
  }
  const names = [...byArtist.keys()].sort((a, b) => byArtist.get(b).length - byArtist.get(a).length || byName(a, b));

  $('#artists').replaceChildren(...(names.length ? names.map((name) => {
    const songs = byArtist.get(name).slice().sort((a, b) => (oldest ? a.day - b.day : b.day - a.day));
    return el('div', { class: 'group' },
      el('h3', {}, name, el('span', { class: 'muted', text: ` (${songs.length})` })),
      el('ul', {}, songs.map((s) => {
        const li = el('li', { class: 'tinted' }, el('span', { text: s.title }), el('span', { class: 'day', text: `Day ${s.day}` }));
        li.style.setProperty('--genre', data.colors[s.genre]);
        return li;
      })));
  }) : [el('p', { class: 'muted', text: 'No songs here.' })]));

  const top = names.slice(0, 12);
  const genres = [...genreCount.entries()].sort((a, b) => b[1] - a[1] || byName(a[0], b[0]));
  $('#summary').replaceChildren(
    el('div', { class: 'card' },
      el('h3', { text: 'Summary' }),
      table([['Songs shown', rows.length], ['Artists shown', names.length], ['Genre', genre === 'ALL' ? 'All' : genre]])),
    el('div', { class: 'card' }, el('h3', { text: 'Top artists' }), table(top.map((n) => [n, byArtist.get(n).length]))),
    el('div', { class: 'card' }, el('h3', { text: 'By genre' }), table(genres.map(([g, n]) => [genreChip(g), n]))));
}

function table(rows) {
  return el('table', {}, el('tbody', {}, rows.map(([k, v]) => el('tr', {}, el('th', {}, k), el('td', { text: String(v) })))));
}

$('#artist-genre').addEventListener('change', renderArtists);
$('#artist-sort').addEventListener('change', renderArtists);

// ---- songs (the sheet's Songs tab) ------------------------------------------------------------------

function renderSongs() {
  const words = fold($('#song-filter').value.trim());
  const genre = $('#song-genre').value || 'ALL';
  const rows = data.songs
    .filter((s) => genre === 'ALL' || s.genre === genre)
    .filter((s) => !words || fold(s.title).includes(words) || fold(s.artists).includes(words))
    .sort((a, b) => b.day - a.day || b.id - a.id);
  $('#song-count').textContent = rows.length === data.songs.length ? `${rows.length} songs, newest first` : `${rows.length} of ${data.songs.length} songs`;
  $('#songs').replaceChildren(...rows.map((s) => (open === `song:${s.id}` ? songForm(s) : songRow(s))));
}

function songRow(s) {
  return el('button', { class: 'row', onclick: () => { open = `song:${s.id}`; renderSongs(); } },
    el('span', { class: 'day', text: `Day ${s.day}` }),
    el('span', { class: 'what' }, el('strong', { text: s.title }), el('span', { class: 'muted', text: s.artists })),
    genreChip(s.genre));
}

function songForm(s) {
  const genre = el('select', { name: 'genre' });
  genreSelect(genre, { value: s.genre });
  const form = el('form', { class: 'card edit' },
    el('label', {}, 'Title', el('input', { name: 'title', value: s.title, required: true, maxlength: 200 })),
    el('label', {}, 'Artist(s)', el('input', { name: 'artists', value: s.artists, required: true, maxlength: 300 })),
    el('div', { class: 'pair' },
      el('label', {}, 'Day', el('input', { name: 'day', type: 'number', step: 1, value: s.day, required: true })),
      el('label', {}, 'Genre', genre)),
    data.genres.includes(s.genre) ? null : el('p', { class: 'error', text: `"${s.genre}" isn't a genre the bot knows; pick one to save.` }),
    el('div', { class: 'actions' },
      el('button', { class: 'primary', text: 'Save' }),
      el('button', { type: 'button', class: 'quiet', text: 'Cancel', onclick: () => { open = null; renderSongs(); } }),
      el('span', { class: 'spacer' }),
      el('button', { type: 'button', class: 'danger', text: 'Delete', onclick: () => remove('songs', s, `Delete "${s.title}"?`) })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    change('songs', s, { title: f.title.value, artists: f.artists.value, day: Number(f.day.value), genre: f.genre.value });
  });
  return form;
}

$('#song-filter').addEventListener('input', renderSongs);
$('#song-genre').addEventListener('change', renderSongs);

$('#add-song').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const body = { title: f.title.value, artists: f.artists.value, genre: f.genre.value };
  if (f.day.value !== '') body.day = Number(f.day.value);
  if (await send('api/songs', { method: 'POST', body })) {
    f.title.value = f.artists.value = f.day.value = '';
    f.title.focus();
  }
});

// ---- on hold --------------------------------------------------------------------------------------

function renderHolds() {
  $('#holds').replaceChildren(...(data.holds.length ? data.holds.map((h) => {
    if (open === `hold:${h.id}`) return holdForm(h);
    if (open === `promote:${h.id}`) return promoteForm(h);
    return el('div', { class: 'card hold' },
      el('strong', { text: h.title }),
      h.artists ? el('span', { class: 'muted', text: h.artists }) : null,
      h.note ? el('p', { class: 'note', text: h.note }) : null,
      el('div', { class: 'actions' },
        el('button', { class: 'primary', text: 'Add as a song', onclick: () => { open = `promote:${h.id}`; renderHolds(); } }),
        el('button', { class: 'quiet', text: 'Edit', onclick: () => { open = `hold:${h.id}`; renderHolds(); } }),
        el('span', { class: 'spacer' }),
        el('button', { class: 'danger', text: 'Delete', onclick: () => remove('holds', h, `Take "${h.title}" off hold?`) })));
  }) : [el('p', { class: 'muted', text: 'Nothing on hold.' })]));
}

function holdForm(h) {
  const form = el('form', { class: 'card edit' },
    el('label', {}, 'Title', el('input', { name: 'title', value: h.title, required: true, maxlength: 200 })),
    el('label', {}, 'Artist(s)', el('input', { name: 'artists', value: h.artists, maxlength: 300 })),
    el('label', {}, 'Note', el('textarea', { name: 'note', maxlength: 1000, rows: 3, text: h.note })),
    el('div', { class: 'actions' },
      el('button', { class: 'primary', text: 'Save' }),
      el('button', { type: 'button', class: 'quiet', text: 'Cancel', onclick: () => { open = null; renderHolds(); } })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    change('holds', h, { title: f.title.value, artists: f.artists.value, note: f.note.value });
  });
  return form;
}

function promoteForm(h) {
  const genre = el('select', { name: 'genre' });
  genreSelect(genre);
  const form = el('form', { class: 'card edit' },
    el('strong', { text: h.title }),
    el('label', {}, 'Artist(s)', el('input', { name: 'artists', value: h.artists, required: true, maxlength: 300 })),
    el('div', { class: 'pair' },
      el('label', {}, 'Genre', genre),
      el('label', {}, 'Day', el('input', { name: 'day', type: 'number', step: 1, placeholder: `next: ${data.nextDay}` }))),
    el('div', { class: 'actions' },
      el('button', { class: 'primary', text: 'Add to songs' }),
      el('button', { type: 'button', class: 'quiet', text: 'Cancel', onclick: () => { open = null; renderHolds(); } })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    const body = { title: h.title, artists: f.artists.value, genre: f.genre.value, fromHold: h.id };
    if (f.day.value !== '') body.day = Number(f.day.value);
    send('api/songs', { method: 'POST', body });
  });
  return form;
}

$('#add-hold').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  if (await send('api/holds', { method: 'POST', body: { title: f.title.value, artists: f.artists.value, note: f.note.value } })) {
    f.title.value = f.artists.value = f.note.value = '';
  }
});

// ---- writing ----------------------------------------------------------------------------------------

async function send(path, opts) {
  try {
    await api(path, opts);
  } catch (err) {
    if (err instanceof Stop) return false;
    if (err.status === 412) alert('That changed somewhere else since you opened it. Showing it as it is now.');
    else if (err.status === 404) alert('That has gone: it was deleted somewhere else.');
    else { alert(err.message); return false; }
  }
  open = null;
  await load();
  return true;
}

function change(table, record, body) {
  return send(`api/${table}/${record.id}`, { method: 'PUT', body, tag: record.updated });
}

function remove(table, record, question) {
  if (confirm(question)) send(`api/${table}/${record.id}`, { method: 'DELETE', tag: record.updated });
}

// ---- tabs ---------------------------------------------------------------------------------------------

function showTab(name) {
  for (const b of document.querySelectorAll('[role=tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const s of document.querySelectorAll('.tab')) s.hidden = s.id !== `tab-${name}`;
  try { localStorage.setItem('xrninsta.tab', name); } catch { /* private window */ }
}

for (const b of document.querySelectorAll('[role=tab]')) b.addEventListener('click', () => showTab(b.dataset.tab));
try { const saved = localStorage.getItem('xrninsta.tab'); if (saved) showTab(saved); } catch { /* private window */ }

load();
