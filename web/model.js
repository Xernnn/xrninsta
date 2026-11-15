// The page's arithmetic, apart from the DOM so `node --test test` can check it: what the sheet's
// Dashboard showed for a filter, and which songs the Songs tab lists, in what order.

export const TOP_ARTISTS = 12;

export const fold = (s) => String(s || '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '').replace(/đ/g, 'd');
export const splitArtists = (t) => String(t || '').split(',').map((s) => s.trim()).filter(Boolean);
const byName = (a, b) => a.localeCompare(b);

// The Dashboard: songs grouped under each of their artists, the artist with most songs first, and
// the summary beside it. Like the sheet, it leaves out songs it can't place: no title, no artist, an
// odd day or a genre the bot doesn't know.
export function dashboard(data, { genre = 'ALL', oldest = false } = {}) {
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
  const find = fold(words.trim());
  const order = oldest ? (a, b) => a.day - b.day || a.id - b.id : (a, b) => b.day - a.day || b.id - a.id;
  return data.songs
    .filter((s) => genre === 'ALL' || s.genre === genre)
    .filter((s) => !find || fold(s.title).includes(find) || fold(s.artists).includes(find))
    .sort(order);
}
