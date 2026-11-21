// The page's arithmetic (web/model.js): what the Dashboard and the Songs tab show.
//     node --test test
// Every song here is made up; never put real ones in this repo.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { botLabel, dashboard, DOUBLE_MS, failLabel, holdList, secondClick, songList, TOP_ARTISTS } from '../web/model.js';

const GENRES = ['VPOP', 'USUK', 'JPOP', 'MISC', 'KPOP', 'GAME', 'INST', 'VOCALOID'];
let nextId = 1;
const song = (title, artists, day, genre) => ({ id: nextId++, title, artists, day, genre, updated: 1 });
const sheet = (...songs) => ({ genres: GENRES, songs });

test('the Dashboard lists a song under each of its artists, the artist with most songs first', () => {
  const data = sheet(
    song('Paper Lanterns', 'Mai Linh', 3, 'VPOP'),
    song('Harbour Lights', 'Mai Linh, The Gulls', 5, 'VPOP'),
    song('Low Tide', 'The Gulls', 4, 'USUK'),
    song('Glass Garden', 'Aoi Sora', 6, 'JPOP'),
    song('Tin Robot', 'Bitwise', 7, 'GAME'),
  );
  const board = dashboard(data);
  assert.deepEqual(board.artists.map((a) => [a.name, a.songs.map((s) => s.title)]), [
    ['Mai Linh', ['Harbour Lights', 'Paper Lanterns']],
    ['The Gulls', ['Harbour Lights', 'Low Tide']],
    ['Aoi Sora', ['Glass Garden']],
    ['Bitwise', ['Tin Robot']],
  ]);
  assert.equal(board.songs, 5);
  assert.deepEqual(board.top, [['Mai Linh', 2], ['The Gulls', 2], ['Aoi Sora', 1], ['Bitwise', 1]]);
  assert.deepEqual(board.genres, [['VPOP', 2], ['GAME', 1], ['JPOP', 1], ['USUK', 1]]);
});

test("the Dashboard leaves out what the sheet couldn't place", () => {
  const data = sheet(
    song('Kept', 'Someone', 1, 'VPOP'),
    song('Odd genre', 'Someone', 2, 'POLKA'),
    song('   ', 'Someone', 3, 'VPOP'),
    song('No artist', ' ', 4, 'VPOP'),
    song('Too early', 'Someone', -6, 'VPOP'),
    song('Earliest kept', 'Someone', -5, 'VPOP'),
  );
  const board = dashboard(data);
  assert.deepEqual(board.artists.map((a) => a.songs.map((s) => s.title)), [['Kept', 'Earliest kept']]);
  assert.equal(board.songs, 2);
});

test('the genre filter narrows the songs, the artists and the counts beside them', () => {
  const data = sheet(
    song('One', 'Ada', 1, 'VPOP'),
    song('Two', 'Ada', 2, 'USUK'),
    song('Three', 'Ben', 3, 'USUK'),
  );
  const board = dashboard(data, { genre: 'USUK' });
  assert.deepEqual(board.artists.map((a) => [a.name, a.songs.length]), [['Ada', 1], ['Ben', 1]]);
  assert.equal(board.songs, 2);
  assert.deepEqual(board.genres, [['USUK', 2]]);
});

test("Sort Day turns each artist's songs oldest first", () => {
  const data = sheet(song('Middle', 'Ada', 5, 'VPOP'), song('Last', 'Ada', 9, 'VPOP'), song('First', 'Ada', 1, 'VPOP'));
  assert.deepEqual(dashboard(data).artists[0].songs.map((s) => s.day), [9, 5, 1]);
  assert.deepEqual(dashboard(data, { oldest: true }).artists[0].songs.map((s) => s.day), [1, 5, 9]);
});

test('the summary names the top artists only', () => {
  const data = sheet(...Array.from({ length: TOP_ARTISTS + 3 }, (_, i) => song(`Song ${i}`, `Artist ${String(i).padStart(2, '0')}`, i, 'MISC')));
  const board = dashboard(data);
  assert.equal(board.artists.length, TOP_ARTISTS + 3);
  assert.equal(board.top.length, TOP_ARTISTS);
});

test('Songs lists newest first, or in the sheet order, and keeps the ones to fix', () => {
  const a = song('Same day, added first', 'Ada', 4, 'VPOP');
  const b = song('Same day, added later', 'Ada', 4, 'VPOP');
  const c = song('Older', 'Ben', 2, 'POLKA');
  const d = song('Newest', 'Cy', 8, 'USUK');
  const data = sheet(a, b, c, d);
  assert.deepEqual(songList(data).map((s) => s.title), ['Newest', 'Same day, added later', 'Same day, added first', 'Older']);
  assert.deepEqual(songList(data, { oldest: true }).map((s) => s.title), ['Older', 'Same day, added first', 'Same day, added later', 'Newest']);
});

test('Find matches a title or an artist whatever the accents and case', () => {
  const data = sheet(
    song('Đêm Trăng', 'Lê Hà', 1, 'VPOP'),
    song('Moonlight', 'Đoàn Vy', 2, 'USUK'),
    song('Sunrise', 'Kim', 3, 'KPOP'),
  );
  assert.deepEqual(songList(data, { words: 'dem trang' }).map((s) => s.title), ['Đêm Trăng']);
  assert.deepEqual(songList(data, { words: '  DOAN ' }).map((s) => s.title), ['Moonlight']);
  assert.deepEqual(songList(data, { words: 'n', genre: 'KPOP' }).map((s) => s.title), ['Sunrise']);
  assert.equal(songList(data, { words: 'nothing like it' }).length, 0);
});

test("Find narrows the Dashboard to the songs it names, and the summary counts only those", () => {
  const data = sheet(
    song('Paper Lanterns', 'Mai Linh', 3, 'VPOP'),
    song('Harbour Lights', 'Mai Linh, The Gulls', 5, 'VPOP'),
    song('Low Tide', 'The Gulls', 4, 'USUK'),
    song('Đêm Trăng', 'Bitwise', 7, 'GAME'),
  );
  const byArtist = dashboard(data, { words: 'gulls' });
  assert.deepEqual(byArtist.artists.map((a) => [a.name, a.songs.map((s) => s.title)]), [
    ['The Gulls', ['Harbour Lights', 'Low Tide']],
    ['Mai Linh', ['Harbour Lights']],
  ]);
  assert.equal(byArtist.songs, 2);
  const byTitle = dashboard(data, { words: 'dem trang' });
  assert.deepEqual(byTitle.artists.map((a) => a.name), ['Bitwise'], 'found without its accents');
  assert.equal(dashboard(data, { words: '   ' }).songs, 4, 'nothing typed shows every song');
});

test('Find goes through the songs on hold too, by title, artist or note', () => {
  const data = { genres: GENRES, songs: [], holds: [
    { id: 1, title: 'Night Market', artists: 'Lantern Club', note: 'ask for the key' },
    { id: 2, title: 'Rain Song', artists: '', note: 'Phương said maybe' },
    { id: 3, title: 'Blue Hour', artists: 'Mai Linh', note: '' },
  ] };
  assert.deepEqual(holdList(data, { words: 'lantern' }).map((h) => h.id), [1]);
  assert.deepEqual(holdList(data, { words: 'phuong' }).map((h) => h.id), [2], 'a note, without its accents');
  assert.deepEqual(holdList(data, { words: '' }).map((h) => h.id), [1, 2, 3]);
});

test('a row opens on a second click on it, not on one click', () => {
  const at = 1000;
  assert.equal(secondClick(null, 'song:1', at), false, 'a first click only picks it');
  assert.equal(secondClick({ key: 'song:1', at }, 'song:1', at + 250), true);
  assert.equal(secondClick({ key: 'song:1', at }, 'song:1', at + DOUBLE_MS + 1), false, 'too slow: two single clicks');
  assert.equal(secondClick({ key: 'song:1', at }, 'song:2', at + 100), false, 'another row');
});

test('a failed load gets a short label for what went wrong, whatever the status', () => {
  assert.equal(failLabel(undefined), 'Server unreachable', 'fetch itself failed: offline');
  assert.equal(failLabel(502), 'Server unreachable', 'the proxy found the service down');
  assert.equal(failLabel(504), 'Server unreachable');
  assert.equal(failLabel(401), 'Wrong name or password');
  assert.equal(failLabel(403), 'Not the owner');
  assert.equal(failLabel(429), 'Too many tries');
  assert.equal(failLabel(500), 'Error 500');
});

test("the bot's banner says what's wrong, and a chat that isn't a chat id is one of those", () => {
  assert.equal(botLabel({ state: 'ok', error: '' }), null, 'running: no banner');
  assert.equal(botLabel({ state: 'starting', error: '' }), null);
  assert.equal(botLabel({ state: 'nochat', error: 'no "chat" in bot.json, so it answers no one' }), 'Bot off: no chat in bot.json');
  assert.equal(botLabel({ state: 'badchat', error: 'the "chat" in bot.json is "<your chat id>", which isn\'t a chat id' }),
    'Bot off: the chat in bot.json isn\'t a chat id');
  assert.equal(botLabel({ state: 'refused', error: '401: Unauthorized' }), 'Bot: Telegram refused it: 401: Unauthorized');
});
