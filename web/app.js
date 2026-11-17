// xrninsta's page: the sheet's Dashboard, its Songs and its OnHold, on the home server.
// Everything a person typed goes in through textContent: the host is shared with other apps' pages.

import { dashboard, holdList, secondClick, songList } from './model.js';

const $ = (sel) => document.querySelector(sel);
const CLIENT = { 'X-Xrn-Client': 'xrninsta' };
const BOT_WORDS = {
  webhook: "The bot isn't taking messages yet: Telegram still sends them to the Apps Script. Run webhook-off on the server to move them here.",
  off: 'The bot has no token yet, so only this page is running.',
  nochat: 'The bot answers no one: bot.json has no chat.',
  twice: 'Two copies of the bot are running on the server.',
  unreachable: "The bot can't reach Telegram at the moment. It keeps trying.",
  refused: 'Telegram refused the bot',
  error: 'The bot ran into a problem',
};
// A mouse and keyboard: put the cursor in a row that opens. On a phone that would pop the keyboard.
const POINTER = matchMedia('(hover: hover) and (pointer: fine)');

let data = null; // {genres, colors, nextDay, songs, holds}
let open = null; // the record being edited: "song:<id>", "hold:<id>" or "promote:<id>"
let lastClick = null; // the row clicked last, { key, at }, so a second click on it opens it

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

// Fills a cell with its genre's colour from the sheet, or marks a genre the bot doesn't know.
function tint(node, genre) {
  const color = data.colors[genre];
  node.classList.add('g');
  if (color) node.style.setProperty('--genre', color);
  else node.classList.add('odd');
  return node;
}

function genreSelect(select, { all = false, value } = {}) {
  select.replaceChildren(...(all ? ['ALL'] : []).concat(data.genres).map((g) => el('option', { value: g, text: g })));
  if (value && [...select.options].some((o) => o.value === value)) select.value = value;
}

// A field in a row being typed into. Its label shows only on a phone, where the columns above it
// don't line up with it; `size` is how much of a phone's width it takes ("half" or "third").
const field = (label, input, size) => el('label', { class: size }, el('span', { class: 'f-label', text: label }), input);
const emptyRow = (span, text) => el('tr', { class: 'empty' }, el('td', { colspan: span, class: 'muted', text }));

// A row in a table opens for fixing on a double click (a phone's double tap) anywhere in it except
// its own buttons; one click only picks it out, so reading along a list with the mouse or a finger
// doesn't open a form in the middle of it. Its Fix button still opens it at once.
function opens(key, then = () => edit(key)) {
  return (e) => {
    if (e.target.closest('button, a, input, select, textarea')) return;
    const at = performance.now();
    if (secondClick(lastClick, key, at)) {
      lastClick = null;
      then();
      return;
    }
    lastClick = { key, at };
    for (const row of document.querySelectorAll('tr.picked')) row.classList.remove('picked');
    e.currentTarget.classList.add('picked');
  };
}

// A double click would also select the word under it: the row opens instead. A drag still selects.
const keepWords = (e) => { if (e.detail > 1) e.preventDefault(); };

// Opens one row for fixing, and so closes any other, in either table.
function edit(key) {
  open = key;
  renderSongs();
  renderHolds();
  const row = document.querySelector('tr.editing');
  if (!row) return;
  row.scrollIntoView({ block: 'nearest' });
  if (POINTER.matches) row.querySelector('input:not([type=hidden]), select, textarea')?.focus({ preventScroll: true });
}

function closeEditor() {
  open = null;
  renderSongs();
  renderHolds();
}

// Escape puts a row being fixed back as it was, wherever the focus is.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !open || e.defaultPrevented) return;
  e.preventDefault();
  closeEditor();
});

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
  const day = $('#add-song [name=day]');
  day.placeholder = String(data.nextDay);
  day.title = `Leave it empty for the next day, ${data.nextDay}`;
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

// ---- the Dashboard --------------------------------------------------------------------------------

function renderArtists() {
  const genre = $('#artist-genre').value || 'ALL';
  const board = dashboard(data, { genre, oldest: $('#artist-sort').value === 'old', words: $('#find').value });

  // Each artist is a row group, as in the sheet: their name and count, then their songs, each title
  // filled with its genre's colour. A song double-clicked opens in Songs to be fixed.
  const table = $('#artists');
  for (const body of [...table.tBodies]) body.remove();
  if (!board.artists.length) table.append(el('tbody', {}, emptyRow(3, 'No songs here.')));
  for (const artist of board.artists) {
    table.append(el('tbody', { class: 'group' },
      el('tr', { class: 'artist' }, el('th', { colspan: 3, scope: 'rowgroup', text: `${artist.name} (${artist.songs.length})` })),
      artist.songs.map((s) => el('tr', { class: 'rec', title: 'Double-click to fix it in Songs', onclick: opens(`board:${s.id}`, () => openSong(s.id)), onmousedown: keepWords },
        el('td', { class: 'indent' }),
        tint(el('td', { text: s.title }), s.genre),
        el('td', { class: 'day', text: `Day ${s.day}` }))),
      el('tr', { class: 'gap', 'aria-hidden': 'true' }, el('td', { colspan: 3 }))));
  }

  $('#summary').replaceChildren(
    mini(['Summary'], [['Songs shown', board.songs], ['Artists shown', board.artists.length], ['Genre', genre]], 'kv'),
    mini(['Artist', 'Total Songs'], board.top),
    mini(['Genre', 'Song Count'], board.genres, 'legend'));
}

function mini(head, rows, kind = '') {
  return el('table', { class: `mini ${kind}` },
    el('thead', {}, el('tr', {}, head.length === 1
      ? el('th', { colspan: 2, scope: 'col', text: head[0] })
      : head.map((h) => el('th', { scope: 'col', text: h })))),
    el('tbody', {}, rows.map(([k, v]) => {
      const name = el('th', { scope: 'row', text: k });
      return el('tr', {}, kind === 'legend' ? tint(name, k) : name, el('td', { text: String(v) }));
    })));
}

function openSong(id) {
  const shown = songList(data, songFilters()).some((s) => s.id === id);
  if (!shown) {
    $('#find').value = '';
    $('#song-genre').value = 'ALL';
    render();
  }
  showTab('songs');
  edit(`song:${id}`);
  document.querySelector('#songs tr.editing')?.scrollIntoView({ block: 'center' });
}

$('#artist-genre').addEventListener('change', renderArtists);
$('#artist-sort').addEventListener('change', renderArtists);

// ---- Songs ----------------------------------------------------------------------------------------

function songFilters() {
  return { words: $('#find').value, genre: $('#song-genre').value || 'ALL', oldest: $('#song-sort').value === 'old' };
}

function renderSongs() {
  const rows = songList(data, songFilters());
  $('#song-count').textContent = rows.length === data.songs.length ? `${rows.length} songs` : `${rows.length} of ${data.songs.length} songs`;
  $('#songs').replaceChildren(...(rows.length
    ? rows.map((s) => (open === `song:${s.id}` ? songForm(s) : songRow(s)))
    : [emptyRow(5, 'No song matches.')]));
}

function songRow(s) {
  const key = `song:${s.id}`;
  return el('tr', { class: 'rec', onclick: opens(key), onmousedown: keepWords },
    el('td', { text: s.title }),
    el('td', { text: s.artists }),
    el('td', { class: 'num', text: String(s.day) }),
    tint(el('td', { class: 'genre', text: s.genre || '?' }), s.genre),
    el('td', { class: 'act' }, el('button', { type: 'button', class: 'link', text: 'Fix', 'aria-label': `Fix "${s.title}"`, onclick: () => edit(key) })));
}

function songForm(s) {
  const genre = el('select', { name: 'genre', required: true });
  genreSelect(genre, { value: s.genre });
  if (!data.genres.includes(s.genre)) genre.value = '';
  const form = el('form', { class: 'rowform', 'aria-label': `Fix "${s.title}"` },
    field('Title', el('input', { name: 'title', value: s.title, required: true, maxlength: 200 })),
    field('Artist(s)', el('input', { name: 'artists', value: s.artists, required: true, maxlength: 300 })),
    field('Day No', el('input', { name: 'day', type: 'number', step: 1, value: s.day, required: true, inputmode: 'numeric' }), 'half'),
    field('Genre', genre, 'half'),
    el('div', { class: 'actions' },
      el('button', { class: 'primary small', text: 'Save' }),
      el('button', { type: 'button', class: 'quiet small', text: 'Cancel', onclick: () => closeEditor() }),
      data.genres.includes(s.genre) ? null : el('span', { class: 'error', text: `"${s.genre}" isn't a genre the bot knows; pick one to save.` }),
      el('span', { class: 'spacer' }),
      el('button', { type: 'button', class: 'danger small', text: 'Delete', onclick: () => remove('songs', s, `Delete "${s.title}"?`) })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    change('songs', s, { title: f.title.value, artists: f.artists.value, day: Number(f.day.value), genre: f.genre.value });
  });
  return el('tr', { class: 'editing' }, el('td', { colspan: 5 }, form));
}

// Find, at the top right, narrows every tab at once, so switching tab keeps what was asked for.
$('#find').addEventListener('input', () => { if (data) render(); });
// / or Ctrl+F goes to it, as in a sheet, unless something else is being typed into.
document.addEventListener('keydown', (e) => {
  if ($('#app').hidden) return;
  const typing = e.target.closest?.('input, select, textarea, [contenteditable]');
  const slash = e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey;
  const find = (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'f';
  if (!slash && !find) return;
  e.preventDefault();
  $('#find').focus();
  $('#find').select();
});
$('#song-genre').addEventListener('change', renderSongs);
$('#song-sort').addEventListener('change', () => {
  try { localStorage.setItem('xrninsta.songSort', $('#song-sort').value); } catch { /* private window */ }
  renderSongs();
});
try { const sort = localStorage.getItem('xrninsta.songSort'); if (sort === 'old' || sort === 'new') $('#song-sort').value = sort; } catch { /* private window */ }

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

// ---- OnHold ---------------------------------------------------------------------------------------

function renderHolds() {
  const n = data.holds.length;
  const rows = holdList(data, { words: $('#find').value });
  const total = n === 1 ? '1 song on hold' : `${n} songs on hold`;
  $('#hold-count').textContent = rows.length === n ? total : `${rows.length} of ${total}`;
  $('#holds').replaceChildren(...(rows.length ? rows.map((h) => {
    if (open === `hold:${h.id}`) return holdForm(h);
    if (open === `promote:${h.id}`) return promoteForm(h);
    return holdRow(h);
  }) : [emptyRow(4, n ? 'Nothing on hold matches.' : 'Nothing on hold.')]));
}

function holdRow(h) {
  const key = `hold:${h.id}`;
  return el('tr', { class: 'rec', onclick: opens(key), onmousedown: keepWords },
    // On a phone the Note column folds away and the note sits under the title.
    el('td', {}, h.title, h.note ? el('span', { class: 'note-under', text: h.note }) : null),
    el('td', { text: h.artists }),
    el('td', { class: 'note', text: h.note }),
    el('td', { class: 'act' },
      el('button', { type: 'button', class: 'primary small', 'aria-label': `Add "${h.title}" as a song`, onclick: () => edit(`promote:${h.id}`) },
        el('span', { class: 'long', text: 'Add as a song' }), el('span', { class: 'short', text: 'Add' })),
      el('button', { type: 'button', class: 'quiet small fix', text: 'Fix', 'aria-label': `Fix "${h.title}"`, onclick: () => edit(key) }),
      el('button', { type: 'button', class: 'danger small', text: 'Delete', 'aria-label': `Delete "${h.title}"`, onclick: () => remove('holds', h, `Take "${h.title}" off hold?`) })));
}

function holdForm(h) {
  const form = el('form', { class: 'rowform', 'aria-label': `Fix "${h.title}"` },
    field('Title', el('input', { name: 'title', value: h.title, required: true, maxlength: 200 })),
    field('Artist(s)', el('input', { name: 'artists', value: h.artists, maxlength: 300 })),
    // A textarea, so a note's line breaks come back as they went in.
    field('Note', el('textarea', { name: 'note', maxlength: 1000, rows: 1, text: h.note })),
    el('span', { class: 'go' },
      el('button', { class: 'primary small', text: 'Save' }),
      el('button', { type: 'button', class: 'quiet small', text: 'Cancel', onclick: () => closeEditor() })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    change('holds', h, { title: f.title.value, artists: f.artists.value, note: f.note.value });
  });
  return el('tr', { class: 'editing' }, el('td', { colspan: 4 }, form));
}

function promoteForm(h) {
  const genre = el('select', { name: 'genre', required: true });
  genreSelect(genre);
  const form = el('form', { class: 'rowform', 'aria-label': `Add "${h.title}" as a song` },
    el('span', { class: 'fixed' }, el('span', { class: 'f-label', text: 'Title' }), el('strong', { text: h.title })),
    field('Artist(s)', el('input', { name: 'artists', value: h.artists, required: true, maxlength: 300 })),
    el('span', { class: 'pair' },
      field('Genre', genre),
      field('Day No', el('input', { name: 'day', type: 'number', step: 1, placeholder: String(data.nextDay), title: `Leave it empty for the next day, ${data.nextDay}`, inputmode: 'numeric' }))),
    el('span', { class: 'go' },
      el('button', { class: 'primary small', text: 'Add to songs' }),
      el('button', { type: 'button', class: 'quiet small', text: 'Cancel', onclick: () => closeEditor() })));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    const body = { title: h.title, artists: f.artists.value, genre: f.genre.value, fromHold: h.id };
    if (f.day.value !== '') body.day = Number(f.day.value);
    send('api/songs', { method: 'POST', body });
  });
  return el('tr', { class: 'editing' }, el('td', { colspan: 4 }, form));
}

$('#add-hold').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  if (await send('api/holds', { method: 'POST', body: { title: f.title.value, artists: f.artists.value, note: f.note.value } })) {
    f.title.value = f.artists.value = f.note.value = '';
  }
});

// On a phone the rows to add to fold into one button, to leave the screen to the list.
for (const opener of document.querySelectorAll('.opener')) {
  opener.addEventListener('click', () => {
    const row = opener.closest('tbody');
    const show = !row.classList.contains('open');
    row.classList.toggle('open', show);
    opener.setAttribute('aria-expanded', String(show));
    if (show) row.querySelector('input').focus();
  });
}

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

// ---- the sheet's tabs -----------------------------------------------------------------------------------

function showTab(name) {
  for (const b of document.querySelectorAll('[role=tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const s of document.querySelectorAll('.tab')) s.hidden = s.id !== `tab-${name}`;
  try { localStorage.setItem('xrninsta.tab', name); } catch { /* private window */ }
}

for (const b of document.querySelectorAll('[role=tab]')) b.addEventListener('click', () => showTab(b.dataset.tab));
try { const saved = localStorage.getItem('xrninsta.tab'); if (saved) showTab(saved); } catch { /* private window */ }

load();
