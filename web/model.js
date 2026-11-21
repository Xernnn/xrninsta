// The page's arithmetic, apart from the DOM so `node --test test` can check it: what the sheet's
// Dashboard showed for a filter, and which songs the Songs tab lists, in what order.

export const TOP_ARTISTS = 12;

export const fold = (s) => String(s || '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '').replace(/đ/g, 'd');
export const splitArtists = (t) => String(t || '').split(',').map((s) => s.trim()).filter(Boolean);
const byName = (a, b) => a.localeCompare(b);

// The words typed into Find, found in any of a record's texts whatever their accents or case.
export function matches(words, ...texts) {
  const find = fold(String(words || '').trim());
  return !find || texts.some((text) => fold(text).includes(find));
}

// The Dashboard: songs grouped under each of their artists, the artist with most songs first, and
// the summary beside it. Like the sheet, it leaves out songs it can't place: no title, no artist, an
// odd day or a genre the bot doesn't know. Find keeps the songs whose title or artists hold its words,
// so an artist's name brings up all of theirs, and the summary counts what's shown.
export function dashboard(data, { genre = 'ALL', oldest = false, words = '' } = {}) {
  const rows = data.songs
    .filter((s) => s.title.trim() && s.artists.trim() && Number.isFinite(s.day) && s.day >= -5 && data.genres.includes(s.genre))
    .filter((s) => genre === 'ALL' || s.genre === genre)
    .filter((s) => matches(words, s.title, s.artists));
  const byArtist = new Map();
  const genreCount = new Map();
  for (const s of rows) {
    genreCount.set(s.genre, (genreCount.get(s.genre) || 0) + 1);
    for (const a of splitArtists(s.artists)) {
      if (!byArtist.has(a)) byArtist.set(a, []);
      byArtist.get(a).push(s);
    }
  }
  const order = oldest ? (a, b) => a.day - b.day : (a, b) => b.day - a.day;
  const artists = [...byArtist.entries()]
    .map(([name, songs]) => ({ name, songs: songs.slice().sort(order) }))
    .sort((a, b) => b.songs.length - a.songs.length || byName(a.name, b.name));
  return {
    songs: rows.length,
    artists,
    top: artists.slice(0, TOP_ARTISTS).map((a) => [a.name, a.songs.length]),
    genres: [...genreCount.entries()].sort((a, b) => b[1] - a[1] || byName(a[0], b[0])),
  };
}

// The Songs tab: every song, odd ones included so they can be fixed, found by title or artist
// whatever the accents, newest first unless asked for the sheet's own order.
export function songList(data, { words = '', genre = 'ALL', oldest = false } = {}) {
  const order = oldest ? (a, b) => a.day - b.day || a.id - b.id : (a, b) => b.day - a.day || b.id - a.id;
  return data.songs
    .filter((s) => genre === 'ALL' || s.genre === genre)
    .filter((s) => matches(words, s.title, s.artists))
    .sort(order);
}

// The OnHold tab: the songs on hold, in their order, found by title, artist or note.
export function holdList(data, { words = '' } = {}) {
  return data.holds.filter((h) => matches(words, h.title, h.artists, h.note));
}

// A row opens to be fixed on a second click or tap on it within this long: one click only picks it,
// so scrolling past or reading along with a finger or the mouse doesn't open a form under it.
export const DOUBLE_MS = 400;

// Whether a click on the row [key] at [now] is the second of two: [last] is the click before, as
// { key, at }. A phone's double tap counts as well as a mouse's double click.
export function secondClick(last, key, now) {
  return Boolean(last) && last.key === key && now - last.at < DOUBLE_MS;
}

// A short label for a request that failed with HTTP [status], or with none when the server
// couldn't be reached at all: the page shows it in place of the songs, which it can't show.
export function failLabel(status) {
  if (!status || status === 502 || status === 503 || status === 504) return 'Server unreachable';
  if (status === 401) return 'Wrong name or password';
  if (status === 403) return 'Not the owner';
  if (status === 429) return 'Too many tries';
  return `Error ${status}`;
}

// What the banner under the title bar says about the bot, from /api/status, or null when it's running.
const BOT_WORDS = {
  webhook: 'Bot: messages still go to the Apps Script (run webhook-off)',
  off: 'Bot off: no token in bot.json',
  nochat: 'Bot off: no chat in bot.json',
  badchat: "Bot off: the chat in bot.json isn't a chat id",
  twice: 'Bot: two copies running',
  unreachable: "Bot: can't reach Telegram",
  refused: 'Bot: Telegram refused it',
  error: 'Bot error',
};
const BOT_SAYS_WHY = ['refused', 'error'];

export function botLabel(status) {
  const words = BOT_WORDS[status && status.state];
  if (!words) return null;
  return BOT_SAYS_WHY.includes(status.state) && status.error ? `${words}: ${status.error}` : words;
}
