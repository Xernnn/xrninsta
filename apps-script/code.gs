const SHEET_SONGS = 'Songs';
const SHEET_DASHBOARD = 'Dashboard';
const SHEET_ONHOLD = 'OnHold';

const GENRES = ['VPOP', 'USUK', 'JPOP', 'MISC', 'KPOP', 'GAME', 'INST', 'VOCALOID'];
// parseGenre_ strips punctuation first ("US-UK", "J-Pop", "k pop"), so only real nicknames need listing here.
const GENRE_ALIASES = { VOCA: 'VOCALOID', INSTRUMENTAL: 'INST' };
const GENRE_COLORS = { VPOP: '#f4cccc', USUK: '#cfe2f3', JPOP: '#d9d2e9', MISC: '#e6e6e6', KPOP: '#f9cb9c', GAME: '#b6d7a8', INST: '#ffe599', VOCALOID: '#b4a7d6' };
const ONHOLD_TITLE_MATCH_MIN = 0.88;
const DUP_TITLE_MIN = 0.90, DUP_ARTIST_MIN = 0.88;
const ONHOLD_PER_PAGE = 8;
const BACK_TO_STATS = { text: '↩️ Quay lại', callback_data: 'stats_menu' };

function getProps() {
    const p = PropertiesService.getScriptProperties();
    return {
        token: String(p.getProperty('BOT_TOKEN') || '').trim(),
        allowedId: String(p.getProperty('ALLOWED_CHAT_ID') || '').trim(),
        webhookKey: String(p.getProperty('WEBHOOK_KEY') || '').trim()
    };
}

function tgUrl() {
    return 'https://api.telegram.org/bot' + getProps().token;
}

function onOpen() {
    SpreadsheetApp.getUi().createMenu('🎵 Music Tools')
    .addItem('Refresh Dashboard', 'refreshDashboard')
    .addItem('Re-apply Genre Colors', 'colorSongsGenreColumn')
    .addToUi();
}

function installedEditHandler(e) {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet(), name = sheet.getName(), row = e.range.getRow(), col = e.range.getColumn();
    if (name === SHEET_SONGS) {
        if (row >= 2 && col === 4) applyGenreColorToCell_(e.range);
        if (row >= 2 && col >= 1 && col <= 4) refreshDashboard();
    } else if (name === SHEET_DASHBOARD) {
        if (row === 4 && (col === 1 || col === 2)) refreshDashboard();
    }
}

function doPost(e) {
    if (!e || !e.postData) return HtmlService.createHtmlOutput("OK");
    // Apps Script can't read Telegram's secret-token header, so the secret rides in the webhook URL (?key=), set by forceSetWebhook().
    const webhookKey = getProps().webhookKey;
    if (webhookKey && (e.parameter || {}).key !== webhookKey) return HtmlService.createHtmlOutput("OK");
    try {
        const data = JSON.parse(e.postData.contents);
        const props = getProps();

        if (data.callback_query) {
            const cb = data.callback_query;
            const chatId = String(cb.message.chat.id);
            if (props.allowedId && chatId !== props.allowedId) return HtmlService.createHtmlOutput("OK");
            // Always answer, or the tapped button keeps spinning; handleCallback_ may return a toast to show.
            let toast = '';
            try { toast = handleCallback_(cb); } finally { answerCallback_(cb.id, toast); }
        } else if (data.message && data.message.text) {
            const chatId = String(data.message.chat.id);
            if (props.allowedId && chatId !== props.allowedId) return HtmlService.createHtmlOutput("OK");
            handleMessage_(chatId, data.message);
        }
    } catch (err) {
        Logger.log(err);
        if (e && e.postData) {
            try {
                const data = JSON.parse(e.postData.contents);
                const chatId = data.message ? String(data.message.chat.id) : (data.callback_query ? String(data.callback_query.message.chat.id) : null);
                if (chatId) {
                    const props = getProps();
                    UrlFetchApp.fetch('https://api.telegram.org/bot' + props.token + '/sendMessage', {
                    method: 'post', contentType: 'application/json',
                    payload: JSON.stringify({ chat_id: chatId, text: "❌ CRASH LOG:\n" + err.toString() }),
                                      muteHttpExceptions: true
                });
            }
        } catch(e2) {}
    }
}
return HtmlService.createHtmlOutput("OK");
}

function getChatState_(userId) {
    const s = PropertiesService.getScriptProperties().getProperty('state_' + userId);
    try { return s ? JSON.parse(s) : null; } catch (e) { return null; }
}

function setChatState_(userId, state) {
    PropertiesService.getScriptProperties().setProperty('state_' + userId, JSON.stringify(state));
}

function clearChatState_(userId) {
    PropertiesService.getScriptProperties().deleteProperty('state_' + userId);
}

function handleMessage_(chatId, message) {
    // Only strip the bot mention Telegram appends to commands in groups (/search@example_song_bot), not "@" inside titles.
    const text = message.text.trim().replace(/^(\/\w+)@\w+/, '$1');
    const lower = text.toLowerCase();
    const userId = String(message.from.id);

    const state = getChatState_(userId);

    if (lower.startsWith('/start')) {
        clearChatState_(userId);
        sendMessage_(chatId, 'Chào bạn! 🎵\nGửi tên bài hát bất kỳ để thêm (Tên ⏎ Artist ⏎ Genre), hoặc dùng lệnh:\n\n/add — thêm bài mới\n/hold — đưa bài vào OnHold\n/search — tìm theo tên, artist, genre hoặc "day 26"\n/stats — thống kê & quản lý OnHold\n/cancel — huỷ thao tác đang dở');
        return;
    }
    if (lower.startsWith('/cancel')) {
        clearChatState_(userId);
        sendMessage_(chatId, '❌ Đã huỷ tác vụ.');
        return;
    }
    if (lower.startsWith('/search')) {
        clearChatState_(userId);
        const q = text.replace(/^\/search\b/i, '').trim();
        if (q) handleSearch_(chatId, q, 0);
        else {
            setChatState_(userId, { step: 'await_search' });
            sendMessage_(chatId, '🔍 Nhập tên bài hát hoặc tên artist để tìm:');
        }
        return;
    }
    if (lower.startsWith('/stats')) {
        clearChatState_(userId);
        sendInline_(chatId, buildStatsHeader_(), statsMenu_());
        return;
    }
    if (lower.startsWith('/add')) {
        clearChatState_(userId);
        const rest = text.replace(/^\/add\b/i, '').trim();
        if (rest) handleAddInput_(chatId, userId, rest);
        else sendMessage_(chatId, '➕ Nhập thông tin bài hát.\n\nCó thể nhập 1, 2 hoặc 3 dòng:\n• Tên bài\n• Artist(s)\n• Genre\n\nMẹo: gửi luôn /add kèm các dòng trên để thêm nhanh.');
        return;
    }
    if (lower.startsWith('/hold')) {
        clearChatState_(userId);
        const remaining = text.replace(/^\/hold\b/i, '').trim();
        if (remaining) {
            handleHoldInput_(chatId, userId, remaining);
        } else {
            setChatState_(userId, { step: 'await_hold' });
            sendMessage_(chatId, '📝 Đang ở chế độ <b>Hold</b>.\nVui lòng gửi thông tin bài hát theo định dạng (mỗi thông tin một dòng):\n\n<i>Tên bài hát\nTên Ca sĩ\nGhi chú (tùy chọn)</i>\n\nVí dụ:\nĐưa Nhau Đi Trốn\nĐen Vâu, Linh Cáo\nCần tìm beat chuẩn');
        }
        return;
    }
    // A mistyped command ("/serch Em") would otherwise be added as a song titled "/serch Em".
    if (text.startsWith('/')) {
        sendMessage_(chatId, '❓ Không có lệnh này. Dùng /add, /hold, /search, /stats hoặc /cancel.');
        return;
    }

    if (state) {
        if (state.step === 'await_hold') { clearChatState_(userId); handleHoldInput_(chatId, userId, text); return; }
        if (state.step === 'await_search') { clearChatState_(userId); handleSearch_(chatId, text, 0); return; }
        if (state.step === 'await_artist') {
            state.artists = text; state.step = 'await_genre';
            setChatState_(userId, state);
            sendGenreInline_(chatId, state.title, state.artists);
            return;
        }
    }

    handleAddInput_(chatId, userId, text);
}

function sendGenreInline_(chatId, title, artists) {
    const buttons = [];
    let row = [];
    GENRES.forEach((g, i) => {
        row.push({ text: g, callback_data: 'add_' + g });
        if (row.length === 3 || i === GENRES.length - 1) { buttons.push(row); row = []; }
    });
    row.push({ text: '❌ Huỷ', callback_data: 'cancel_add' });
    buttons.push(row);
    sendInline_(chatId, `🎵 <b>${esc_(title)}</b>\n👤 ${esc_(artists)}\n\nChọn Genre:`, buttons);
}

function handleAddInput_(chatId, userId, text) {
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    const title = lines[0] || '';
    const artists = lines[1] || '';
    const genre = parseGenre_(lines[2]);

    if (title && artists && genre) {
        tryAddSong_(chatId, userId, title, artists, genre);
    } else if (title && artists) {
        setChatState_(userId, { step: 'await_genre', title, artists });
        sendGenreInline_(chatId, title, artists);
    } else if (title) {
        setChatState_(userId, { step: 'await_artist', title });
        sendMessage_(chatId, `Nhập Artist(s) cho:\n🎵 <b>${esc_(title)}</b>`);
    }
}

// Returns an optional toast; doPost answers every callback so the button stops spinning.
function handleCallback_(cb) {
    const chatId = cb.message.chat.id, msgId = cb.message.message_id;
    const userId = String(cb.from.id);
    const data = cb.data;
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const expired = '⌛ Phiên đã hết hạn, gửi lại bài nhé.';

    if (data === 'cancel_add' || data === 'close_msg') {
        clearChatState_(userId);
        deleteMessage_(chatId, msgId);
        return;
    }

    if (data.startsWith('add_')) {
        const state = getChatState_(userId), genre = data.slice(4);
        if (!state || state.step !== 'await_genre' || !GENRES.includes(genre)) return expired;
        clearChatState_(userId);
        deleteMessage_(chatId, msgId);
        tryAddSong_(chatId, userId, state.title, state.artists, genre);
        return;
    }

    if (data === 'force_add' || data === 'force_hold') {
        const state = getChatState_(userId);
        if (!state || state.step !== (data === 'force_add' ? 'confirm_dup' : 'confirm_hold')) return expired;
        clearChatState_(userId);
        deleteMessage_(chatId, msgId);
        if (data === 'force_add') tryAddSong_(chatId, userId, state.title, state.artists, state.genre, true);
        else holdSong_(chatId, userId, state.title, state.artists, state.note, true);
        return;
    }

    // undo_/unhold_ carry the Songs row they were made for, so they keep working after other commands.
    if (data.startsWith('undo_') || data.startsWith('unhold_')) {
        const isUndo = data.startsWith('undo_'), ref = splitFirst_(data)[1];
        return withLock_(() => {
            const sheet = ss.getSheetByName(SHEET_SONGS), hit = readRowRef_(sheet, ref, 4);
            if (!hit) return '⚠️ Không tìm thấy bài này nữa (sheet đã thay đổi).';
            const [title, artists, day, genre] = hit.values;
            if (isUndo) {
                sheet.deleteRow(hit.row);
                markDashboardDirty_();
                editMessageText_(chatId, msgId, `🗑️ Đã hoàn tác thêm bài:\n🎵 <b>${esc_(title)}</b>`);
            } else {
                removeFromOnHoldByTitle_(String(title).trim());
                editMessageReplyMarkup_(chatId, msgId, addedText_(title, artists, day, genre) + '\n\n✅ Đã dọn dẹp khỏi OnHold!', [[{ text: '🗑️ Xóa (Hoàn tác)', callback_data: 'undo_' + ref }]]);
            }
        });
    }

    if (data.startsWith('search_')) {
        const parts = data.split('_');
        const page = parseInt(parts[1], 10);
        const q = CacheService.getScriptCache().get('search_' + parts.slice(2).join('_'));
        if (q) handleSearch_(chatId, q, page, msgId);
        else editMessageText_(chatId, msgId, '⌛ Kết quả tìm kiếm đã hết hạn. Gửi /search để tìm lại.');
        return;
    }

    if (data === 'stats_menu') { editMessageReplyMarkup_(chatId, msgId, buildStatsHeader_(), statsMenu_()); return; }
    if (data === 'stats_genre') { editMessageReplyMarkup_(chatId, msgId, buildGenreLeaderboard_(), [[BACK_TO_STATS]]); return; }
    if (data === 'stats_artist') { editMessageReplyMarkup_(chatId, msgId, buildArtistLeaderboard_(null), [[BACK_TO_STATS]]); return; }
    if (data === 'stats_artist_genre') {
        const buttons = []; let row = [];
        GENRES.forEach((g, i) => {
            row.push({ text: g, callback_data: 'statg_' + g });
            if (row.length === 3 || i === GENRES.length - 1) { buttons.push(row); row = []; }
        });
        buttons.push([BACK_TO_STATS]);
        editMessageReplyMarkup_(chatId, msgId, 'Chọn Genre để xem Artist Leaderboard:', buttons);
        return;
    }
    if (data.startsWith('statg_')) {
        editMessageReplyMarkup_(chatId, msgId, buildArtistLeaderboard_(data.slice(6)), [[{ text: '↩️ Quay lại', callback_data: 'stats_artist_genre' }]]);
        return;
    }

    // OnHold list: holdp_<page> turns pages; hadd_/hdel_/hdelok_<page>_<row ref> act on one entry.
    if (data === 'stats_onhold' || data.startsWith('holdp_')) {
        const view = renderOnHold_(data === 'stats_onhold' ? 0 : parseInt(splitFirst_(data)[1], 10));
        editMessageReplyMarkup_(chatId, msgId, view.text, view.keyboard);
        return;
    }
    if (data.startsWith('hadd_') || data.startsWith('hdel_') || data.startsWith('hdelok_')) {
        const [action, rest] = splitFirst_(data), [pageStr, ref] = splitFirst_(rest), page = parseInt(pageStr, 10);
        const holdSheet = ss.getSheetByName(SHEET_ONHOLD);
        const reload = toast => { const view = renderOnHold_(page); editMessageReplyMarkup_(chatId, msgId, view.text, view.keyboard); return toast; };
        const staleToast = '⚠️ Danh sách OnHold đã thay đổi, đã tải lại.';

        if (action === 'hdelok') {
            return withLock_(() => {
                const hit = readRowRef_(holdSheet, ref, 1);
                if (!hit) return reload(staleToast);
                holdSheet.deleteRow(hit.row);
                return reload(truncate_('🗑️ Đã xoá khỏi OnHold: ' + String(hit.values[0]).trim(), 190));
            });
        }
        const hit = readRowRef_(holdSheet, ref, 2);
        if (!hit) return reload(staleToast);
        if (action === 'hdel') {
            const view = renderOnHold_(page, ref);
            editMessageReplyMarkup_(chatId, msgId, view.text, view.keyboard);
            return;
        }
        // hadd hands the held song to the normal add flow; once added, its "Xóa khỏi OnHold" button clears the hold.
        const title = String(hit.values[0]).trim(), artists = String(hit.values[1] || '').trim();
        if (artists) {
            setChatState_(userId, { step: 'await_genre', title, artists });
            sendGenreInline_(chatId, title, artists);
        } else {
            setChatState_(userId, { step: 'await_artist', title });
            sendMessage_(chatId, `Nhập Artist(s) cho:\n🎵 <b>${esc_(title)}</b>`);
        }
        return;
    }

    return '⚠️ Nút này đã cũ, hãy mở lại menu.';
}

function tryAddSong_(chatId, userId, title, artists, genre, force) {
    const typed = artists;
    artists = canonicalArtists_(artists);
    const dup = force ? null : findDuplicate_(title, artists);
    if (dup) {
        setChatState_(userId, { step: 'confirm_dup', title, artists, genre });
        sendInline_(chatId, `⚠️ Bài này có vẻ đã tồn tại:\n🎵 <b>${esc_(dup.title)}</b>\n👤 ${esc_(dup.artists)}\n📅 Day ${esc_(dup.dayNo)} | ${esc_(dup.genre)}\n\nVẫn thêm <b>${esc_(title)}</b> (${genre})?`,
            [[{ text: '✅ Vẫn thêm', callback_data: 'force_add' }, { text: '❌ Huỷ', callback_data: 'cancel_add' }]]);
        return;
    }

    const lock = LockService.getScriptLock();
    if (lock.tryLock(10000)) {
        try {
            const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS);
            const nextDay = computeNextDay_(sheet);
            sheet.appendRow([title, artists, nextDay, genre]);
            const lastRow = sheet.getLastRow();
            applyGenreColorToCell_(sheet.getRange(lastRow, 4), genre);
            markDashboardDirty_();

            let hasHold = false;
            const holdSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_ONHOLD);
            if (holdSheet && holdSheet.getLastRow() >= 2) {
                const hValues = holdSheet.getRange(2, 1, holdSheet.getLastRow() - 1, 1).getValues();
                hasHold = hValues.some(r => similarityScore_(title, String(r[0] || '').trim()) >= ONHOLD_TITLE_MATCH_MIN);
            }

            clearChatState_(userId);
            const ref = rowRef_(lastRow, title);
            const btns = [[{ text: '🗑️ Xóa (Hoàn tác)', callback_data: 'undo_' + ref }]];
            if (hasHold) btns[0].push({ text: '🔖 Xóa khỏi OnHold', callback_data: 'unhold_' + ref });

            const renamed = splitArtists_(typed).join(', ') !== artists ? `\n<i>(Artist theo cách viết đã có; bạn nhập: ${esc_(typed)})</i>` : '';
            sendInline_(chatId, addedText_(title, artists, nextDay, genre) + renamed + (hasHold ? '\n\n<i>⚠️ Bài này đang nằm trong OnHold!</i>' : ''), btns);
        } finally { lock.releaseLock(); }
    } else {
        sendMessage_(chatId, '⚠️ Hệ thống bận, vui lòng thử lại sau.');
    }
}

function addedText_(title, artists, day, genre) {
    return `✅ Đã thêm bài mới:\n🎵 <b>${esc_(title)}</b>\n👤 ${esc_(artists)}\n📅 Day ${esc_(day)} | ${esc_(genre)}`;
}

function handleHoldInput_(chatId, userId, text) {
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    const title = lines[0] || '', artists = lines[1] || '', note = lines[2] || '';
    if (!title) { sendMessage_(chatId, '⚠️ Lỗi. Gửi tên bài để đưa vào OnHold.'); return; }
    holdSong_(chatId, userId, title, artists, note);
}

function holdSong_(chatId, userId, title, artists, note, force) {
    const conflict = force ? null : findHoldConflict_(title, artists);
    if (conflict) {
        setChatState_(userId, { step: 'confirm_hold', title, artists, note });
        const where = conflict.isHold ? '🔖 Bài này đã có trong OnHold:' : `✅ Bài này đã có trong Songs (Day ${esc_(conflict.dayNo)} | ${esc_(conflict.genre)}):`;
        sendInline_(chatId, `⚠️ ${where}\n🎵 <b>${esc_(conflict.title)}</b>` + (conflict.artists ? `\n👤 ${esc_(conflict.artists)}` : '') + `\n\nVẫn hold <b>${esc_(title)}</b>?`,
            [[{ text: '✅ Vẫn hold', callback_data: 'force_hold' }, { text: '❌ Huỷ', callback_data: 'cancel_add' }]]);
        return;
    }

    const lock = LockService.getScriptLock();
    if (lock.tryLock(10000)) {
        try {
            const sheet = getOrCreateSheet_(SpreadsheetApp.getActiveSpreadsheet(), SHEET_ONHOLD);
            ensureOnHoldSheet_(sheet);
            sheet.appendRow([title, artists, note]);
            sendMessage_(chatId, `🔖 Đã thêm vào OnHold:\n🎵 <b>${esc_(title)}</b>` + (artists ? `\n👤 ${esc_(artists)}` : '') + (note ? `\n📝 ${esc_(note)}` : ''));
        } finally { lock.releaseLock(); }
    } else {
        sendMessage_(chatId, '⚠️ Hệ thống bận, vui lòng thử lại sau.');
    }
}

function handleSearch_(chatId, query, page, msgIdToEdit) {
    const results = searchSongs_(query);
    if (!results.length) {
        const t = `🔍 Không tìm thấy bài nào khớp với "${esc_(query)}".`;
        if (msgIdToEdit) editMessageText_(chatId, msgIdToEdit, t); else sendMessage_(chatId, t);
        return;
    }
    const perPage = 5;
    const totalPages = Math.ceil(results.length / perPage);
    page = Math.min(page, totalPages - 1);
    const paged = results.slice(page * perPage, (page + 1) * perPage);

    let msg = `🔍 Kết quả: <b>${esc_(query)}</b> (Trang ${page + 1}/${totalPages})\n─────────────────\n`;
    paged.forEach((r, i) => {
        if (r.isHold) {
            msg += `${page * perPage + i + 1}. 🔖 <b>[ON HOLD] ${esc_(r.title)}</b>` + (r.artists ? `\n   👤 ${esc_(r.artists)}` : '') + (r.note ? `\n   📝 ${esc_(r.note)}` : '') + `\n   🎯 Khớp ${Math.round(r.score * 100)}%\n\n`;
        } else {
            msg += `${page * perPage + i + 1}. <b>${esc_(r.title)}</b>\n   👤 ${esc_(r.artists)}\n   📅 Day ${r.dayNo} | ${r.genre}` + (r.score === null ? '' : ` | ${Math.round(r.score * 100)}%`) + '\n\n';
        }
    });

    const btns = [];
    const key = totalPages > 1 ? cacheSearchQuery_(query) : '';
    if (page > 0) btns.push({ text: '◀ Trước', callback_data: `search_${page - 1}_${key}` });
    if (page < totalPages - 1) btns.push({ text: 'Sau ▶', callback_data: `search_${page + 1}_${key}` });

    if (msgIdToEdit) editMessageReplyMarkup_(chatId, msgIdToEdit, msg, btns.length ? [btns] : []);
    else sendInline_(chatId, msg, btns.length ? [btns] : []);
}

// Telegram caps callback_data at 64 bytes, which a long Vietnamese query easily exceeds, so the buttons
// carry a short hash and the query lives in the cache. Each page turn re-puts it, so it expires after 6h idle.
function cacheSearchQuery_(query) {
    const key = shortHash_(query, 12);
    CacheService.getScriptCache().put('search_' + key, query, 21600);
    return key;
}

function searchSongs_(query) {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const songsSheet = ss.getSheetByName(SHEET_SONGS);
    const holdSheet = ss.getSheetByName(SHEET_ONHOLD);
    // "day 26" and genre names ("inst", "J-Pop") list songs instead of fuzzy matching; a null score hides the % in results.
    const dayMatch = query.trim().match(/^day\s*(\d+)$/i), genreFilter = parseGenre_(query);
    const scored = [];

    if (songsSheet) {
        const values = songsSheet.getDataRange().getValues().slice(1);
        values.forEach(r => {
            const title = String(r[0] || '').trim(), artists = String(r[1] || '').trim(), dayNo = Number(r[2]), genre = String(r[3] || '').trim().toUpperCase();
            if (!title || !artists || !GENRES.includes(genre)) return;
            if (dayMatch || genreFilter) {
                if (dayMatch ? dayNo === Number(dayMatch[1]) : genre === genreFilter) scored.push({ title, artists, dayNo, genre, score: null, isHold: false });
                return;
            }
            const score = searchScore_(query, title, artists);
            if (score) scored.push({ title, artists, dayNo, genre, score, isHold: false });
        });
    }
    if (dayMatch || genreFilter) return scored.sort((a, b) => b.dayNo - a.dayNo);

    if (holdSheet) {
        const hValues = holdSheet.getDataRange().getValues().slice(1);
        hValues.forEach(r => {
            const title = String(r[0] || '').trim(), artists = String(r[1] || '').trim(), note = String(r[2] || '').trim();
            if (!title) return;
            const score = searchScore_(query, title, artists);
            if (score) scored.push({ title, artists, note, score, isHold: true });
        });
    }

    return scored.sort((a, b) => b.score - a.score || (a.isHold ? 1 : 0) - (b.isHold ? 1 : 0) || a.title.localeCompare(b.title));
}

// Returns 0 when the row shouldn't show up. Unlike duplicate checks, search wants partial titles,
// but only on whole words, ranked by how much of the title the query covers (exact title stays at 100%).
function searchScore_(query, title, artists) {
    const qNorm = normalizeTitle_(query), tNorm = normalizeTitle_(title);
    let titleScore = similarityScore_(query, title);
    if (containsWords_(tNorm, qNorm) || containsWords_(qNorm, tNorm)) titleScore = Math.max(titleScore, 0.80 + 0.15 * Math.min(qNorm.length, tNorm.length) / Math.max(qNorm.length, tNorm.length));
    const artistScore = bestArtistSimilarity_(query, artists);
    const artistBonus = normalizeArtistList_(artists).some(a => containsWords_(a, qNorm) || containsWords_(qNorm, a)) ? 0.15 : 0;
    if (titleScore < 0.55 && artistScore < 0.70 && !artistBonus) return 0;
    return Math.max(titleScore, artistScore * 0.8 + artistBonus);
}

function findDuplicate_(inputTitle, inputArtists) {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS);
    const values = sheet.getDataRange().getValues().slice(1);
    let best = null;

    // Threshold every row before ranking, so a near-miss with a higher combined score can't hide a real match.
    values.forEach(r => {
        const dbTitle = String(r[0] || '').trim(), dbArtists = String(r[1] || '').trim();
        const titleScore = similarityScore_(inputTitle, dbTitle);
        if (titleScore < DUP_TITLE_MIN) return;
        const artistScore = bestArtistSimilarity_(inputArtists, dbArtists);
        if (artistScore < DUP_ARTIST_MIN) return;

        const combined = titleScore * 0.72 + artistScore * 0.28;
        if (!best || combined > best.combined) best = { title: dbTitle, artists: dbArtists, dayNo: r[2], genre: String(r[3] || '').toUpperCase(), combined };
    });
    return best;
}

// Same song already held, or already in Songs. Holds may omit the artist, so a missing artist on either side only needs the title to match.
function findHoldConflict_(title, artists) {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const same = (t, a) => similarityScore_(title, t) >= DUP_TITLE_MIN && (!artists || !a || bestArtistSimilarity_(artists, a) >= DUP_ARTIST_MIN);
    const cell = v => String(v || '').trim();

    const songs = ss.getSheetByName(SHEET_SONGS);
    const song = songs && songs.getDataRange().getValues().slice(1).find(r => same(cell(r[0]), cell(r[1])));
    if (song) return { title: cell(song[0]), artists: cell(song[1]), dayNo: song[2], genre: cell(song[3]).toUpperCase(), isHold: false };

    const hold = ss.getSheetByName(SHEET_ONHOLD);
    const held = hold && hold.getDataRange().getValues().slice(1).find(r => same(cell(r[0]), cell(r[1])));
    if (held) return { title: cell(held[0]), artists: cell(held[1]), isHold: true };
    return null;
}

function computeNextDay_(sheet) {
    const days = sheet.getRange(2, 3, sheet.getLastRow()).getValues().map(r => Number(r[0])).filter(d => Number.isFinite(d) && d > 0);
    return (days.length ? Math.max.apply(null, days) : 0) + 1;
}

function getOrCreateSheet_(ss, name) {
    return ss.getSheetByName(name) || ss.insertSheet(name);
}

function ensureSongsSheet_(sheet) {
    if (sheet.getRange('A1:D1').getValues()[0].every(v => !String(v).trim())) {
        sheet.getRange('A1:D1').setValues([['Title', 'Artist(s)', 'Day No', 'Genre']]);
    }
    sheet.getRange('A1:D1').setFontWeight('bold').setBackground('#d9ead3');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 320);
    sheet.setColumnWidth(2, 280);
    sheet.setColumnWidth(3, 90);
    sheet.setColumnWidth(4, 110);
    sheet.getRange('C2:C').setNumberFormat('0');
    sheet.getRange('D2:D').setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(GENRES, true).setAllowInvalid(false).build());
}

function ensureDashboardSheet_(dash) {
    dash.clear(); dash.setHiddenGridlines(true);
    dash.setColumnWidth(1, 190);
    dash.setColumnWidth(2, 400);
    dash.setColumnWidth(3, 60);
    dash.setColumnWidth(4, 24);
    dash.setColumnWidth(5, 170);
    dash.setColumnWidth(6, 170);
    dash.getRange('A1:C1').merge().setValue('Music Dashboard').setFontSize(18).setFontWeight('bold');
    dash.getRange('A3:B3').setValues([['Genre Filter', 'Sort Day']]).setFontWeight('bold').setBackground('#d9ead3');
    dash.getRange('A4').setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(['ALL'].concat(GENRES), true).build()).setValue('ALL').setBackground('#fff2cc');
    dash.getRange('B4').setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(['Newest first', 'Oldest first'], true).build()).setValue('Newest first').setBackground('#fff2cc');
    dash.getRange('A6:C6').setValues([['Artist', 'Title', 'Day']]).setFontWeight('bold').setBackground('#cfe2f3');
    dash.setFrozenRows(6);
}

function ensureOnHoldSheet_(sheet) {
    if (sheet.getRange('A1:C1').getValues()[0].every(v => !String(v).trim())) {
        sheet.getRange('A1:C1').setValues([['Title', 'Artist(s)', 'Note']]);
    }
    sheet.getRange('A1:C1').setFontWeight('bold').setBackground('#fff2cc');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 320);
    sheet.setColumnWidth(2, 280);
    sheet.setColumnWidth(3, 320);
}

function refreshDashboard() {
    const ss = SpreadsheetApp.getActiveSpreadsheet(), songs = ss.getSheetByName(SHEET_SONGS), dash = ss.getSheetByName(SHEET_DASHBOARD);
    if (!songs || !dash) return;
    const { rows, artistMap, genreCount, artistNames } = buildData_(songs, dash);
    const sortValue = String(dash.getRange('B4').getValue() || 'Newest first').trim();
    const genreFilter = String(dash.getRange('A4').getValue() || 'ALL').trim().toUpperCase();
    const { render, meta, artistSummary } = buildRenderData_(artistNames, artistMap, sortValue);
    clearDashboard_(dash);
    writeDashboardSummary_(dash, rows.length, artistNames.length, genreFilter, artistSummary, genreCount);
    if (render.length) {
        dash.getRange(7, 1, render.length, 3).setValues(render);
        styleDashboardBody_(dash, meta);
    }
}

// Rows the bot writes don't fire the edit trigger, and a refresh is too slow to run inside the webhook,
// so bot changes only set a flag and a time trigger (installDashboardTrigger) does the refresh.
function markDashboardDirty_() {
    PropertiesService.getScriptProperties().setProperty('dashboard_dirty', '1');
}

function refreshDashboardIfDirty() {
    const p = PropertiesService.getScriptProperties();
    if (!p.getProperty('dashboard_dirty')) return;
    p.deleteProperty('dashboard_dirty');
    refreshDashboard();
}

function installDashboardTrigger() {
    ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'refreshDashboardIfDirty').forEach(t => ScriptApp.deleteTrigger(t));
    ScriptApp.newTrigger('refreshDashboardIfDirty').timeBased().everyMinutes(10).create();
}

function buildData_(songs, dash) {
    const genreFilter = String(dash.getRange('A4').getValue() || 'ALL').trim().toUpperCase();
    const rows = songs.getDataRange().getValues().slice(1).map(r => ({ title: String(r[0] || '').trim(), artistsText: String(r[1] || '').trim(), dayNo: Number(r[2]), genre: String(r[3] || '').trim().toUpperCase() }))
    .filter(r => r.title && r.artistsText && Number.isFinite(r.dayNo) && r.dayNo >= -5 && GENRES.includes(r.genre))
    .filter(r => genreFilter === 'ALL' || r.genre === genreFilter);
    const artistMap = {}, genreCount = {};
    rows.forEach(r => {
        genreCount[r.genre] = (genreCount[r.genre] || 0) + 1;
        splitArtists_(r.artistsText).forEach(artist => {
            if (!artistMap[artist]) artistMap[artist] = [];
            artistMap[artist].push(r);
        });
    });
    const artistNames = Object.keys(artistMap).sort((a, b) => artistMap[b].length - artistMap[a].length || a.localeCompare(b));
    return { rows, artistMap, genreCount, artistNames };
}

function buildRenderData_(artistNames, artistMap, sortValue) {
    const render = [], meta = [], artistSummary = [];
    let rowCursor = 7;
    artistNames.forEach(artist => {
        const songsList = artistMap[artist].slice().sort((a, b) => sortValue === 'Oldest first' ? a.dayNo - b.dayNo : b.dayNo - a.dayNo);
        artistSummary.push([artist, songsList.length]);
        render.push([`${artist} (${songsList.length})`, '', '']);
        meta.push({ row: rowCursor++, type: 'artist' });
        songsList.forEach(song => { render.push(['', song.title, 'Day ' + song.dayNo]); meta.push({ row: rowCursor++, type: 'song', genre: song.genre }); });
        render.push(['', '', '']);
        meta.push({ row: rowCursor++, type: 'separator' });
    });
    return { render, meta, artistSummary };
}

function clearDashboard_(dash) {
    const maxRows = dash.getMaxRows();
    if (maxRows >= 7) { dash.getRange(7, 1, maxRows - 6, 3).clearContent().clearFormat(); dash.setRowHeights(7, maxRows - 6, 21); }
    dash.getRange('A6:C6').setValues([['Artist', 'Title', 'Day']]).setFontWeight('bold').setBackground('#cfe2f3');
    dash.getRange('E2:F40').clearContent().clearFormat();
}

function styleDashboardBody_(dash, meta) {
    const artistA1s = [], sepA1s = [], sepRows = [], dayA1s = [], genreMap = {};
    meta.forEach(m => {
        if (m.type === 'artist') artistA1s.push(`A${m.row}:C${m.row}`);
        else if (m.type === 'separator') { sepA1s.push(`A${m.row}:C${m.row}`); sepRows.push(m.row); }
        else if (m.type === 'song') {
            const bg = GENRE_COLORS[m.genre] || '#ffffff';
    if (!genreMap[bg]) genreMap[bg] = [];
    genreMap[bg].push(`B${m.row}`);
            dayA1s.push(`C${m.row}`);
        }
    });
    if (artistA1s.length) dash.getRangeList(artistA1s).setBackground('#ead1dc').setFontWeight('bold');
    if (sepA1s.length) dash.getRangeList(sepA1s).setBackground('#ffffff');
    sepRows.forEach(r => dash.setRowHeight(r, 7));
    if (dayA1s.length) dash.getRangeList(dayA1s).setHorizontalAlignment('center').setFontWeight('bold').setFontColor('#555555');
    Object.keys(genreMap).forEach(bg => dash.getRangeList(genreMap[bg]).setBackground(bg));
}

function writeDashboardSummary_(dash, songCount, artistCount, genreFilter, artistSummary, genreCount) {
    dash.getRange('E2:F2').merge().setValue('Summary').setFontWeight('bold').setBackground('#d9ead3');
    dash.getRange('E3:F5').clearContent().clearFormat().setBorder(true, true, true, true, true, true, '#dddddd', SpreadsheetApp.BorderStyle.SOLID).setValues([['Songs shown', songCount], ['Artists shown', artistCount], ['Genre', genreFilter]]);
    dash.getRange('E3:E5').setFontWeight('bold').setBackground('#f3f3f3');
    dash.getRange('E7:F7').setValues([['Artist', 'Total Songs']]).setFontWeight('bold').setBackground('#d9ead3');
    if (artistSummary.length) {
        const top = artistSummary.slice().sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12);
        dash.getRange(8, 5, top.length, 2).setValues(top);
        dash.getRange(8, 6, top.length, 1).setHorizontalAlignment('center').setFontWeight('bold');
    }
    dash.getRange('E22:F22').setValues([['Genre', 'Song Count']]).setFontWeight('bold').setBackground('#d9ead3');
    if (genreCount) {
        const genreRows = Object.keys(genreCount).map(g => [g, genreCount[g]]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
        if (genreRows.length) {
            dash.getRange(23, 5, genreRows.length, 2).setValues(genreRows);
            dash.getRange(23, 6, genreRows.length, 1).setHorizontalAlignment('center').setFontWeight('bold');
        }
    }
}

function applyGenreConditionalFormatting_() {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS), range = sheet.getRange('D2:D');
    sheet.setConditionalFormatRules(Object.keys(GENRE_COLORS).map(g => SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(g).setBackground(GENRE_COLORS[g]).setRanges([range]).build()));
}

function colorSongsGenreColumn() {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS), lastRow = sheet.getLastRow();
    if (lastRow < 2) return;
    applyGenreConditionalFormatting_();
    const values = sheet.getRange(2, 4, lastRow - 1, 1).getValues(), colorGroups = {};
    values.forEach((r, idx) => {
        const v = String(r[0] || '').trim().toUpperCase(), bg = GENRE_COLORS[v] || '#ffffff', fw = v ? 'bold' : 'normal', key = `${bg}|${fw}`;
        if (!colorGroups[key]) colorGroups[key] = { bg, fw, a1s: [] };
        colorGroups[key].a1s.push(`D${idx + 2}`);
    });
    Object.values(colorGroups).forEach(g => sheet.getRangeList(g.a1s).setBackground(g.bg).setFontWeight(g.fw));
}

function applyGenreColorToCell_(range, value) {
    const v = String(value !== undefined ? value : range.getValue()).trim().toUpperCase();
    range.setBackground(GENRE_COLORS[v] || '#ffffff').setFontWeight(v ? 'bold' : 'normal');
}

function buildGenreLeaderboard_() {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS), values = sheet.getDataRange().getValues().slice(1), count = {};
    values.forEach(r => { const g = String(r[3] || '').trim().toUpperCase(); if (GENRES.includes(g)) count[g] = (count[g] || 0) + 1; });
    const sorted = Object.keys(count).sort((a, b) => count[b] - count[a]), total = sorted.reduce((s, g) => s + count[g], 0);
    if (!sorted.length) return '🎵 <b>Genre Leaderboard</b>\n(Không có dữ liệu)';
    // <pre> switches Telegram to a monospace font; without it padEnd/padStart can't line the columns up.
    let msg = '🎵 <b>Genre Leaderboard</b>\n<pre>';
    sorted.forEach((g, i) => { const pct = total ? Math.round(count[g] / total * 100) : 0; msg += `${i + 1}. ${g.padEnd(8)} ${String(count[g]).padStart(3)} bài ${String(pct).padStart(3)}% ${'█'.repeat(Math.round(pct / 5)) || '▏'}\n`; });
    return msg + `</pre>Tổng: ${total} bài`;
}

function buildArtistLeaderboard_(genreFilter) {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS), values = sheet.getDataRange().getValues().slice(1), count = {};
    values.forEach(r => {
        const g = String(r[3] || '').trim().toUpperCase();
        if (!GENRES.includes(g) || (genreFilter && g !== genreFilter)) return;
        splitArtists_(String(r[1] || '')).forEach(a => count[a] = (count[a] || 0) + 1);
    });
    const sorted = Object.keys(count).sort((a, b) => count[b] - count[a] || a.localeCompare(b)), top = sorted.slice(0, 15);
    let msg = (genreFilter ? `🎵 Artist Leaderboard — ${genreFilter}` : '🎵 Artist Leaderboard') + '\n─────────────────\n';
    if (!top.length) return msg + '(Không có dữ liệu)';
    top.forEach((a, i) => { msg += `${i + 1}. ${esc_(a)}\n    ${'█'.repeat(Math.round(count[a] / count[top[0]] * 8)) || '▏'} ${count[a]} bài\n`; });
    return msg + (sorted.length > 15 ? `... và ${sorted.length - 15} artist khác` : '');
}

function removeFromOnHoldByTitle_(newTitle) {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_ONHOLD);
    if (!sheet || sheet.getLastRow() < 2) return;
    const scores = sheet.getRange(2, 1, sheet.getLastRow() - 1, 1).getValues().map(r => similarityScore_(newTitle, String(r[0] || '').trim()));
    // Only the closest match (plus exact copies of it), so one tap can't sweep up every similar-looking hold.
    const best = Math.max(...scores);
    if (best < ONHOLD_TITLE_MATCH_MIN) return;
    scores.map((s, idx) => s === best ? idx + 2 : 0).filter(Boolean).reverse().forEach(r => sheet.deleteRow(r));
}

// Paged so it never hits Telegram's 4096-char message cap. confirmRef swaps that entry's buttons for a delete confirmation.
function renderOnHold_(page, confirmRef) {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_ONHOLD), items = [];
    if (sheet && sheet.getLastRow() >= 2) {
        sheet.getRange(2, 1, sheet.getLastRow() - 1, 3).getValues().forEach((r, i) => {
            const title = String(r[0] || '').trim();
            if (title) items.push({ row: i + 2, title, artists: String(r[1] || '').trim(), note: String(r[2] || '').trim() });
        });
    }
    if (!items.length) return { text: '🔖 OnHold List\n─────────────────\n(Chưa có bài nào)', keyboard: [[BACK_TO_STATS]] };

    const totalPages = Math.ceil(items.length / ONHOLD_PER_PAGE);
    page = Math.min(Math.max(page || 0, 0), totalPages - 1);
    let text = `🔖 OnHold List (${items.length} bài) — Trang ${page + 1}/${totalPages}\n─────────────────\n`;
    const keyboard = [];
    items.slice(page * ONHOLD_PER_PAGE, (page + 1) * ONHOLD_PER_PAGE).forEach((it, i) => {
        const n = page * ONHOLD_PER_PAGE + i + 1, ref = rowRef_(it.row, it.title);
        text += `${n}. <b>${esc_(it.title)}</b>` + (it.artists ? `\n    👤 ${esc_(it.artists)}` : '') + (it.note ? `\n    📝 ${esc_(truncate_(it.note, 100))}` : '') + '\n';
        keyboard.push(ref === confirmRef
            ? [{ text: `✅ Xoá #${n}`, callback_data: `hdelok_${page}_${ref}` }, { text: '✖️ Không', callback_data: `holdp_${page}` }]
            : [{ text: `➕ Thêm #${n}`, callback_data: `hadd_${page}_${ref}` }, { text: `🗑️ Xoá #${n}`, callback_data: `hdel_${page}_${ref}` }]);
    });
    if (confirmRef) text += `\n⚠️ Xoá khỏi OnHold? Bấm ✅ để xác nhận.`;
    const nav = [];
    if (page > 0) nav.push({ text: '◀ Trước', callback_data: `holdp_${page - 1}` });
    if (page < totalPages - 1) nav.push({ text: 'Sau ▶', callback_data: `holdp_${page + 1}` });
    if (nav.length) keyboard.push(nav);
    keyboard.push([BACK_TO_STATS]);
    return { text, keyboard };
}

function statsMenu_() {
    return [
        [{ text: 'Genre Leaderboard', callback_data: 'stats_genre' }],
        [{ text: 'Artist Leaderboard', callback_data: 'stats_artist' }],
        [{ text: 'Artist theo Genre', callback_data: 'stats_artist_genre' }],
        [{ text: 'OnHold List', callback_data: 'stats_onhold' }],
        [{ text: '❌ Đóng', callback_data: 'close_msg' }]
    ];
}

function buildStatsHeader_() {
    const ss = SpreadsheetApp.getActiveSpreadsheet(), hold = ss.getSheetByName(SHEET_ONHOLD);
    const rows = ss.getSheetByName(SHEET_SONGS).getDataRange().getValues().slice(1).filter(r => String(r[0] || '').trim() && GENRES.includes(String(r[3] || '').trim().toUpperCase()));
    const holdCount = hold && hold.getLastRow() >= 2 ? hold.getRange(2, 1, hold.getLastRow() - 1, 1).getValues().filter(r => String(r[0] || '').trim()).length : 0;
    let latest = null;
    rows.forEach(r => { if (Number(r[2]) > 0 && (!latest || Number(r[2]) >= Number(latest[2]))) latest = r; });
    let msg = `📊 <b>Thống kê</b>\n🎵 ${rows.length} bài · 🔖 ${holdCount} OnHold`;
    if (latest) msg += `\n🆕 Day ${latest[2]}: <b>${esc_(String(latest[0]).trim())}</b> — ${esc_(String(latest[1] || '').trim())}`;
    return msg + '\n\nChọn loại thống kê:';
}

// đ has no NFD decomposition, so map it by hand; \p{L} keeps JP/KR titles instead of stripping them to ''.
function normalizeTitle_(s) { return String(s || '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '').replace(/\u0111/g, 'd').replace(/\(.*?\)|\[.*?\]/g, ' ').replace(/\b(feat|ft)\b\.?/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim(); }
function splitArtists_(t) { return String(t || '').split(',').map(s => s.trim()).filter(Boolean); }
function normalizeArtistList_(s) { return splitArtists_(s).map(normalizeTitle_).filter(Boolean); }
// Whole-word containment on normalized strings: "em" is in "em muon duoc bay", but not in "remember".
function containsWords_(hay, needle) { return !!needle && (' ' + hay + ' ').includes(' ' + needle + ' '); }

// Every user-supplied value goes through this: messages use parse_mode HTML, and a bare < or & makes Telegram reject the whole message.
function esc_(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function truncate_(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function splitFirst_(s) { const i = s.indexOf('_'); return i < 0 ? [s, ''] : [s.slice(0, i), s.slice(i + 1)]; }

// "us-uk", "J-Pop", "voca" -> a GENRES value, or '' when it isn't a genre.
function parseGenre_(s) {
    const g = String(s || '').toUpperCase().replace(/[^A-Z]/g, '');
    return GENRES.includes(g) ? g : (GENRE_ALIASES[g] || '');
}

function shortHash_(s, len) {
    return Utilities.base64EncodeWebSafe(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, String(s), Utilities.Charset.UTF_8)).slice(0, len);
}

// Buttons that act on a sheet row carry "<row>_<title hash>", so a row that moved or was deleted since is caught instead of hitting another song.
function rowRef_(row, title) { return row + '_' + shortHash_(String(title).trim(), 8); }
function readRowRef_(sheet, ref, width) {
    const [rowStr, hash] = splitFirst_(String(ref)), row = parseInt(rowStr, 10);
    if (!sheet || !(row >= 2) || row > sheet.getLastRow()) return null;
    const values = sheet.getRange(row, 1, 1, width).getValues()[0];
    return shortHash_(String(values[0]).trim(), 8) === hash ? { row, values } : null;
}

function withLock_(fn) {
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(5000)) return '⚠️ Hệ thống bận, vui lòng thử lại sau.';
    try { return fn(); } finally { lock.releaseLock(); }
}

// Reuse the spelling already in Songs ("Vstra" -> "VSTRA", "Son Tung M-TP" -> "Sơn Tùng M-TP") so leaderboards don't split one artist in two.
// Only case, accents, punctuation and spacing are ignored; near-misses like "Hà Anh Tuấn" / "Hồ Anh Tuấn" stay distinct.
function canonicalArtists_(artists) {
    const key = a => normalizeTitle_(a).replace(/\s/g, ''), spellings = {};
    SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_SONGS).getDataRange().getValues().slice(1).forEach(r => splitArtists_(r[1]).forEach(a => {
        const k = key(a);
        if (!k) return;
        spellings[k] = spellings[k] || {};
        spellings[k][a] = (spellings[k][a] || 0) + 1;
    }));
    return splitArtists_(artists).map(a => {
        const known = spellings[key(a)];
        return known ? Object.keys(known).sort((x, y) => known[y] - known[x])[0] : a;
    }).join(', ');
}

function levenshteinDistance_(a, b) {
    if (a === b) return 0; if (!a.length) return b.length; if (!b.length) return a.length;
    const matrix = Array(b.length + 1).fill().map((_, i) => [i]);
    matrix[0] = Array(a.length + 1).fill().map((_, j) => j);
    for (let i = 1; i <= b.length; i++) {
        for (let j = 1; j <= a.length; j++) {
            matrix[i][j] = Math.min(matrix[i - 1][j] + 1, matrix[i][j - 1] + 1, matrix[i - 1][j - 1] + (b.charAt(i - 1) === a.charAt(j - 1) ? 0 : 1));
        }
    }
    return matrix[b.length][a.length];
}

// Whole-string fuzzy match (typo tolerance only). A partial title is a different song, so no substring bonus here,
// and neither is a different number: "Part 1" vs "Part 2" is one edit apart but never the same song.
function similarityScore_(a, b) {
    a = normalizeTitle_(a); b = normalizeTitle_(b);
    if (!a || !b) return 0; if (a === b) return 1;
    const score = Math.max(0, 1 - levenshteinDistance_(a, b) / Math.max(a.length, b.length));
    const numbers = s => (s.match(/\d+/g) || []).join(' ');
    return numbers(a) === numbers(b) ? score : Math.min(score, 0.8);
}

// Artists do get a partial-name bonus: "Sơn Tùng" and "Sơn Tùng M-TP" are the same person.
function artistSimilarity_(a, b) {
    const na = normalizeTitle_(a), nb = normalizeTitle_(b), score = similarityScore_(na, nb);
    return (containsWords_(na, nb) || containsWords_(nb, na)) ? Math.max(score, 0.92) : score;
}

function bestArtistSimilarity_(inputArtists, dbArtists) {
    const inputList = normalizeArtistList_(inputArtists), dbList = normalizeArtistList_(dbArtists);
    if (!inputList.length || !dbList.length) return 0;
    return inputList.reduce((sum, a) => sum + Math.max(...dbList.map(b => artistSimilarity_(a, b))), 0) / inputList.length;
}

function sendMessage_(chatId, text) {
    UrlFetchApp.fetch(tgUrl() + '/sendMessage', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ chat_id: chatId, text: text, parse_mode: 'HTML' }), muteHttpExceptions: true });
}
function sendInline_(chatId, text, inlineKeyboard) {
    UrlFetchApp.fetch(tgUrl() + '/sendMessage', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ chat_id: chatId, text: text, parse_mode: 'HTML', reply_markup: { inline_keyboard: inlineKeyboard } }), muteHttpExceptions: true });
}
function editMessageText_(chatId, msgId, text) {
    UrlFetchApp.fetch(tgUrl() + '/editMessageText', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ chat_id: chatId, message_id: msgId, text: text, parse_mode: 'HTML' }), muteHttpExceptions: true });
}
function editMessageReplyMarkup_(chatId, msgId, text, inlineKeyboard) {
    UrlFetchApp.fetch(tgUrl() + '/editMessageText', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ chat_id: chatId, message_id: msgId, text: text, parse_mode: 'HTML', reply_markup: { inline_keyboard: inlineKeyboard } }), muteHttpExceptions: true });
}
function deleteMessage_(chatId, msgId) {
    UrlFetchApp.fetch(tgUrl() + '/deleteMessage', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ chat_id: chatId, message_id: msgId }), muteHttpExceptions: true });
}
function answerCallback_(callbackId, text) {
    UrlFetchApp.fetch(tgUrl() + '/answerCallbackQuery', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ callback_query_id: callbackId, text: text || '' }), muteHttpExceptions: true });
}

// ==========================================
// DEBUGGING TOOLS
// ==========================================
function testBot() {
    const props = getProps();
    const url = 'https://api.telegram.org/bot' + props.token + '/getMe';
    const res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    SpreadsheetApp.getUi().alert('Test Bot Response:\n' + res.getContentText());
}

function testSendMessage() {
    const props = getProps();
    const url = 'https://api.telegram.org/bot' + props.token + '/sendMessage';
    const payload = { chat_id: props.allowedId, text: "✅ Test message from Apps Script! If you see this, your Token and Chat ID are correct." };
    const res = UrlFetchApp.fetch(url, {
        method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), muteHttpExceptions: true
    });
    SpreadsheetApp.getUi().alert('Test Send Message Response:\n' + res.getContentText());
}

function checkWebhookInfo() {
    const props = getProps();
    const url = 'https://api.telegram.org/bot' + props.token + '/getWebhookInfo';
    const res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    SpreadsheetApp.getUi().alert('Webhook Info:\n' + res.getContentText());
}

function forceSetWebhook() {
    const url = "https://script.google.com/macros/s/AKfycbx0CKEbQ4C3YGFekFrqA-CLlj9pHdb-ivmPt0HNYhZkEu8KUGtDxjGMGlRB9tyGWD9USg/exec";

    const props = getProps();

    // Created once and reused; doPost drops any request whose ?key= doesn't match.
    let key = props.webhookKey;
    if (!key) {
        key = Utilities.getUuid().replace(/-/g, '');
        PropertiesService.getScriptProperties().setProperty('WEBHOOK_KEY', key);
    }

    // Đã thêm lệnh drop_pending_updates=true để ép Telegram xóa toàn bộ tin nhắn tồn đọng
    const res = UrlFetchApp.fetch('https://api.telegram.org/bot' + props.token + '/setWebhook?url=' + encodeURIComponent(url + '?key=' + key) + '&drop_pending_updates=true');

    Logger.log("Kết quả set Webhook: " + res.getContentText());
}

// Fills Telegram's "/" menu with the commands and a one-line hint each.
function setBotCommands() {
    const commands = [
        { command: 'add', description: 'Thêm bài: Tên ⏎ Artist ⏎ Genre' },
        { command: 'hold', description: 'Đưa bài vào OnHold' },
        { command: 'search', description: 'Tìm theo tên, artist, genre hoặc "day 26"' },
        { command: 'stats', description: 'Thống kê & quản lý OnHold' },
        { command: 'cancel', description: 'Huỷ thao tác đang dở' },
        { command: 'start', description: 'Hướng dẫn sử dụng' }
    ];
    const res = UrlFetchApp.fetch(tgUrl() + '/setMyCommands', { method: 'post', contentType: 'application/json', payload: JSON.stringify({ commands }), muteHttpExceptions: true });
    Logger.log("Kết quả setMyCommands: " + res.getContentText());
}

// Run once from the Apps Script editor after deploying a new version.
function setupBot() {
    forceSetWebhook();
    setBotCommands();
    installDashboardTrigger();
}

