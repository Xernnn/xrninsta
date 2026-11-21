#!/usr/bin/env python3
"""xrninsta: the Telegram bot that keeps the song-of-the-day list, and a page to look through it.

It used to be a Google Apps Script bound to a Google Sheet (apps-script/code.gs). This is the same
bot on the home server: the songs live in an SQLite file, the bot asks Telegram for new messages
itself (long polling, so it needs no public address), and the page at /xrninsta stands in for the
sheet. Standard library only, plus `bcrypt` to read the shared users file, like the sync servers.

    GET    /health                  200 "ok", no login (for monitoring)
    POST   /session                 with Basic auth: sets the shared sign-in cookie
    DELETE /session                 signs this browser out, and ends its cookie on every server
    GET    /api/data                every song and OnHold entry, the genres and their colours
    GET    /api/status              whether the bot is reaching Telegram
    POST   /api/songs               {title, artists, genre, day?, fromHold?}: day defaults to the next
    PUT    /api/songs/<id>          {title, artists, day, genre}; needs If-Match: "<updated>"
    DELETE /api/songs/<id>          needs If-Match: "<updated>"
    POST   /api/holds               {title, artists?, note?}
    PUT    /api/holds/<id>          {title, artists, note}; needs If-Match: "<updated>"
    DELETE /api/holds/<id>          needs If-Match: "<updated>"

Everything under /api needs the owner's login: the first user in the users file, by Basic auth or
the shared sign-in cookie with an X-Xrn-Client header. Any other path serves web/. Signing out ends
that one cookie: its SHA-256 goes on signed-out.json beside the users file and session.key until it
would have run out, a list that xrnhub, xrnloops-sync and xrnrule-sync (which take the same cookie)
also write to and refuse.

    python3 xrninsta.py --data ~/xrninsta/data --users ~/.config/xrn-sync/users \\
        --config ~/.config/xrninsta/bot.json [--port 5239]
    python3 xrninsta.py song-bot --config ~/.config/xrninsta/bot.json
    python3 xrninsta.py import <export.xlsx> --data ~/xrninsta/data [--replace]
    python3 xrninsta.py check --config ~/.config/xrninsta/bot.json
    python3 xrninsta.py webhook-off --config ~/.config/xrninsta/bot.json
    python3 xrninsta.py covers <zip or folder of Day_<n>.png> --data ~/xrninsta/data
    python3 xrninsta.py covers-bot --config ~/.config/xrninsta/bot.json
    python3 xrninsta.py check|webhook-off --covers --config ~/.config/xrninsta/bot.json [--data ...]

bot.json is {"token": "<from BotFather>", "chat": "<the one chat it answers>"}, readable only by
you, which `song-bot` writes. The chat is the number Telegram gives it; with none, or one that isn't
a number, the bot answers no one, says so in its status, and logs each update it ignores. `check`
says which bot the token is, the chat it answers and where Telegram sends its messages now.
`webhook-off` is the switch from Apps Script: it stops Telegram posting to the script, so the
messages come here.

The daily cover (apps-script/coverbot.gs) is a second bot, the Daily Story Bot, with its own token
under "covers" in bot.json ({"covers": {"token": ..., "chat": ...}}, which `covers-bot` writes). At
2am Sydney time it sends that day's cover from <data>/covers, which `covers` fills; `--covers`
points `check` and `webhook-off` at it.
"""
import argparse
import base64
import datetime
import fcntl
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

# ---- what the bot knows (as in code.gs) --------------------------------------------------------

GENRES = ["VPOP", "USUK", "JPOP", "MISC", "KPOP", "GAME", "INST", "VOCALOID"]
# parse_genre strips punctuation first ("US-UK", "J-Pop", "k pop"), so only real nicknames are here.
GENRE_ALIASES = {"VOCA": "VOCALOID", "INSTRUMENTAL": "INST"}
GENRE_COLORS = {"VPOP": "#f4cccc", "USUK": "#cfe2f3", "JPOP": "#d9d2e9", "MISC": "#e6e6e6", "KPOP": "#f9cb9c",
                "GAME": "#b6d7a8", "INST": "#ffe599", "VOCALOID": "#b4a7d6"}
ONHOLD_TITLE_MATCH_MIN = 0.88
DUP_TITLE_MIN, DUP_ARTIST_MIN = 0.90, 0.88
ONHOLD_PER_PAGE = 8
SEARCH_PER_PAGE = 5
SEARCH_KEEP_S = 21600  # a search's pages turn for 6 hours after the last turn
WAIT_KEEP_S = 3600  # a question waiting for typed text (an artist, a song to hold or find) lapses after an hour
BACK_TO_STATS = {"text": "↩️ Quay lại", "callback_data": "stats_menu"}
EXPIRED = "⌛ Phiên đã hết hạn, gửi lại bài nhé."
BUSY = "⚠️ Hệ thống bận, vui lòng thử lại sau."
COMMANDS = [
    {"command": "add", "description": "Thêm bài: Tên ⏎ Artist ⏎ Genre"},
    {"command": "hold", "description": "Đưa bài vào OnHold"},
    {"command": "search", "description": 'Tìm theo tên, artist, genre hoặc "day 26"'},
    {"command": "stats", "description": "Thống kê & quản lý OnHold"},
    {"command": "cancel", "description": "Huỷ thao tác đang dở"},
    {"command": "start", "description": "Hướng dẫn sử dụng"},
]

# The daily cover (apps-script/coverbot.gs, the Daily Story Bot): a bot of its own, with its own token.
COVER_ZONE = "Australia/Sydney"
COVER_AT = (2, 0)  # 2am there, daylight saving or not, when the script's trigger ran
COVER_EPOCH, COVER_EPOCH_DAY = datetime.date(2026, 10, 18), 263  # the script's START_DATE_STRING and START_DAY_NUMBER
COVER_NAME = re.compile(r"Day_([0-9]{1,6})\.png")
COVER_MAX_BYTES = 10 * 1024 * 1024  # the most Telegram takes for a photo
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COVER_COMMANDS = [
    {"command": "sendnow", "description": "Gửi ảnh của hôm nay ngay"},
    {"command": "start", "description": "Hướng dẫn sử dụng"},
]
# A chat as Telegram numbers it: a person's id, or a group's (negative). Both bots answer one each.
CHAT_ID = re.compile(r"-?[0-9]{1,20}")

# ---- the server ---------------------------------------------------------------------------------

PREFIX = "/xrninsta"
MAX_BODY = 64 * 1024
AUTH_CACHE_S = 600
FAIL_DELAY_S = 2
FAIL_LIMIT = 10
FAIL_WINDOW_S = 15 * 60
SESSION_COOKIE = "xrn_session"
SESSION_S = 30 * 86400
POLL_S = 50  # how long Telegram holds a getUpdates open when nothing comes
MAX_TITLE, MAX_ARTISTS, MAX_NOTE = 200, 300, 1000
WEB_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}
WEB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; "
        "manifest-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
    ),
}


# ---- matching, ported from code.gs line for line --------------------------------------------------
#
# The same thresholds decide what's a duplicate on both sides of the move, so these follow the
# script's arithmetic exactly. Where JavaScript and Python differ, the JavaScript wins: its \b and
# \d are ASCII, and Math.round sends a half up.

def js_round(x):
    return math.floor(x + 0.5)


def letter_number_or_space(c):
    return unicodedata.category(c)[0] in "LN" or c.isspace()


def normalize_title(s):
    """Lower case, no accents (đ by hand: it has no decomposition), no (...) or [...], no feat/ft,
    letters and numbers only. Japanese and Korean stay letters, so their titles aren't emptied."""
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.category(c).startswith("M")).replace("\u0111", "d")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(feat|ft)\b\.?", " ", s, flags=re.ASCII)
    s = "".join(c if letter_number_or_space(c) else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()


def split_artists(text):
    return [a.strip() for a in str(text or "").split(",") if a.strip()]


def normalize_artist_list(text):
    return [n for n in (normalize_title(a) for a in split_artists(text)) if n]


def contains_words(hay, needle):
    """Whole-word containment on normalized strings: "em" is in "em muon duoc bay", not in "remember"."""
    return bool(needle) and f" {needle} " in f" {hay} "


def levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(a) + 1))
    for i in range(1, len(b) + 1):
        cur = [i] + [0] * len(a)
        for j in range(1, len(a) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if b[i - 1] == a[j - 1] else 1))
        prev = cur
    return prev[len(a)]


def numbers_in(s):
    return " ".join(re.findall(r"[0-9]+", s))


def similarity(a, b):
    """Whole-string fuzzy match, for typos only. A different number is never the same song: "Part 1"
    and "Part 2" are one edit apart."""
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return 0
    if a == b:
        return 1
    score = max(0, 1 - levenshtein(a, b) / max(len(a), len(b)))
    return score if numbers_in(a) == numbers_in(b) else min(score, 0.8)


def artist_similarity(a, b):
    """Artists get a partial-name bonus: "Sơn Tùng" and "Sơn Tùng M-TP" are the same person."""
    na, nb = normalize_title(a), normalize_title(b)
    score = similarity(na, nb)
    return max(score, 0.92) if contains_words(na, nb) or contains_words(nb, na) else score


def best_artist_similarity(input_artists, db_artists):
    mine, theirs = normalize_artist_list(input_artists), normalize_artist_list(db_artists)
    if not mine or not theirs:
        return 0
    return sum(max(artist_similarity(a, b) for b in theirs) for a in mine) / len(mine)


def search_score(query, title, artists):
    """0 when the row shouldn't show. Search, unlike the duplicate checks, takes part of a title, but
    only whole words, ranked by how much of the title the query covers."""
    q, t = normalize_title(query), normalize_title(title)
    title_score = similarity(query, title)
    if contains_words(t, q) or contains_words(q, t):
        title_score = max(title_score, 0.80 + 0.15 * min(len(q), len(t)) / max(len(q), len(t)))
    artist_score = best_artist_similarity(query, artists)
    bonus = 0.15 if any(contains_words(a, q) or contains_words(q, a) for a in normalize_artist_list(artists)) else 0
    if title_score < 0.55 and artist_score < 0.70 and not bonus:
        return 0
    return max(title_score, artist_score * 0.8 + bonus)


def parse_genre(s):
    """ "us-uk", "J-Pop", "voca" -> a genre, or '' when it isn't one."""
    g = re.sub(r"[^A-Z]", "", str(s or "").upper())
    return g if g in GENRES else GENRE_ALIASES.get(g, "")


def esc(s):
    """Every value from a person goes through this: a bare < or & makes Telegram refuse the message."""
    return str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(s, n):
    s = str(s)
    return s[:n - 1] + "…" if len(s) > n else s


def split_first(s):
    head, sep, tail = s.partition("_")
    return (head, tail) if sep else (s, "")


def short_hash(s, length):
    """Apps Script's base64EncodeWebSafe(MD5(s)), cut short: the same refs the script put on buttons."""
    return base64.urlsafe_b64encode(hashlib.md5(str(s).encode("utf-8")).digest()).decode()[:length]


def ref_of(record):
    """A button's handle on a record: its id and a hash of its title, so a button made before the
    record was changed or deleted is caught instead of acting on something else. Imported rows keep
    their sheet row as their id, so the script's own buttons still work here."""
    return f"{record['id']}_{short_hash(record['title'].strip(), 8)}"


def int_or(text, fallback):
    try:
        return int(text)
    except (TypeError, ValueError):
        return fallback


def now_ms():
    return int(time.time() * 1000)


# ---- storage --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, artists TEXT NOT NULL,
    day INTEGER NOT NULL, genre TEXT NOT NULL, added INTEGER, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS holds(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, artists TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '', added INTEGER, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chat_state(user TEXT PRIMARY KEY, state TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS searches(key TEXT PRIMARY KEY, query TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
SONG_FIELDS = "id, title, artists, day, genre, added, updated"
HOLD_FIELDS = "id, title, artists, note, added, updated"


class Store:
    """The songs and the bot's memory, in one SQLite file. One lock covers every read and write, so
    the bot and the page never interleave: what the script needed LockService for."""

    def __init__(self, folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.path = os.path.join(folder, "xrninsta.sqlite")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def songs(self):
        return self.rows(f"SELECT {SONG_FIELDS} FROM songs ORDER BY id")

    def holds(self):
        return self.rows(f"SELECT {HOLD_FIELDS} FROM holds ORDER BY id")

    def song(self, sid):
        found = self.rows(f"SELECT {SONG_FIELDS} FROM songs WHERE id = ?", (sid,))
        return found[0] if found else None

    def hold(self, hid):
        found = self.rows(f"SELECT {HOLD_FIELDS} FROM holds WHERE id = ?", (hid,))
        return found[0] if found else None

    def next_day(self):
        with self.lock:
            (top,) = self.db.execute("SELECT MAX(day) FROM songs WHERE day > 0").fetchone()
        return (top or 0) + 1

    def add_song(self, title, artists, genre, day=None, from_hold=None):
        with self.lock, self.db:
            if day is None:
                day = self.next_day()
            stamp = now_ms()
            cur = self.db.execute("INSERT INTO songs(title, artists, day, genre, added, updated) VALUES(?,?,?,?,?,?)",
                                  (title, artists, day, genre, stamp, stamp))
            if from_hold is not None:
                self.db.execute("DELETE FROM holds WHERE id = ?", (from_hold,))
            return self.song(cur.lastrowid)

    def add_hold(self, title, artists, note):
        with self.lock, self.db:
            stamp = now_ms()
            cur = self.db.execute("INSERT INTO holds(title, artists, note, added, updated) VALUES(?,?,?,?,?)",
                                  (title, artists, note, stamp, stamp))
            return self.hold(cur.lastrowid)

    def update(self, table, rid, fields, expect=None):
        """The new record; None if there's no such id; False if it changed since [expect]."""
        with self.lock, self.db:
            cur = self.song(rid) if table == "songs" else self.hold(rid)
            if cur is None:
                return None
            if expect is not None and cur["updated"] != expect:
                return False
            stamp = max(now_ms(), cur["updated"] + 1)
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE {table} SET {cols}, updated = ? WHERE id = ?", (*fields.values(), stamp, rid))
            return self.song(rid) if table == "songs" else self.hold(rid)

    def delete(self, table, rid, expect=None):
        """True when deleted; None if there's no such id; False if it changed since [expect]."""
        with self.lock, self.db:
            cur = self.song(rid) if table == "songs" else self.hold(rid)
            if cur is None:
                return None
            if expect is not None and cur["updated"] != expect:
                return False
            self.db.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
            return True

    def get_state(self, user, newer_than=None):
        """The user's step in a conversation, or None; with [newer_than] (ms), None if it's older."""
        found = self.rows("SELECT state, at FROM chat_state WHERE user = ?", (user,))
        if found and newer_than is not None and found[0]["at"] < newer_than:
            return None
        try:
            return json.loads(found[0]["state"]) if found else None
        except ValueError:
            return None

    def set_state(self, user, state):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO chat_state VALUES(?,?,?)",
                            (user, json.dumps(state, ensure_ascii=False), now_ms()))

    def clear_state(self, user):
        with self.lock, self.db:
            self.db.execute("DELETE FROM chat_state WHERE user = ?", (user,))

    def put_search(self, key, query):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO searches VALUES(?,?,?)", (key, query, now_ms()))
            self.db.execute("DELETE FROM searches WHERE at < ?", (now_ms() - SEARCH_KEEP_S * 1000,))

    def get_search(self, key):
        found = self.rows("SELECT query FROM searches WHERE key = ? AND at >= ?", (key, now_ms() - SEARCH_KEEP_S * 1000))
        return found[0]["query"] if found else None

    def meta(self, key, fallback=None):
        found = self.rows("SELECT value FROM meta WHERE key = ?", (key,))
        return found[0]["value"] if found else fallback

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


# ---- Telegram ------------------------------------------------------------------------------------

class Telegram:
    base = "https://api.telegram.org"

    def __init__(self, token):
        self.token = token

    def call(self, method, payload=None, timeout=15):
        """Telegram's answer as a dict ({"ok": false, ...} on a refusal), or None if it couldn't be
        reached. Like the script's muteHttpExceptions, a failed send never stops the bot."""
        data = json.dumps(payload or {}, ensure_ascii=False).encode()
        return self.post(method, data, "application/json", timeout)

    def upload(self, method, fields, name, filename, content, timeout=60):
        """[method] with a PNG: [fields] as the form's fields and [content] as its file [name]. The
        cover goes up from disk, where the script handed Telegram a blob from Drive, so it needs no
        public address."""
        boundary = "xrninsta-" + secrets.token_hex(16)
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in fields.items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     "Content-Type: image/png\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.post(method, b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout)

    def post(self, method, data, ctype, timeout):
        req = urllib.request.Request(f"{self.base}/bot{self.token}/{method}", data=data,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                answer = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                answer = json.loads(e.read())
            except ValueError:
                answer = {"ok": False, "error_code": e.code, "description": str(e)}
        except (OSError, ValueError) as e:
            log(f"telegram {method}: {type(e).__name__}: {e}")
            return None
        if not isinstance(answer, dict):
            answer = {"ok": False, "description": "not a JSON object"}
        # A refused send left no trace: a reply over the length limit, or a rate limit (429), and
        # the chat simply heard nothing. getUpdates' refusals are the poller's to report, once each.
        if not answer.get("ok") and method != "getUpdates":
            log(f"telegram {method} refused: {answer.get('error_code')}: {answer.get('description', '')}")
        return answer


def log(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ---- the bot ---------------------------------------------------------------------------------------

class Chat:
    """The one chat a bot answers, as bot.json names it ([where] in bot.json, for [name] in the log).
    Only a chat id counts: from 18 to 21 Nov 2026 the song bot's "chat" was a pasted
    "<your chat id>", which counted as set because it wasn't empty, so the bot ignored every
    message while its log said ok. Now that answers no one and says why, at start and after. A
    chat id that is another chat's was as quiet: now each message from another chat is logged, and
    the status isn't ok until the bot's own chat is heard from again."""

    def __init__(self, chat, where, name):
        self.id = str("" if chat is None else chat).strip()
        self.where = where
        self.name = name
        self.usable = bool(CHAT_ID.fullmatch(self.id))
        self.stranger = None  # the last other chat a message came from, since this one's last

    def fault(self):
        """What's wrong with the chat bot.json names, or "" if nothing is."""
        if not self.id:
            return f'no "chat" {self.where}'
        if not self.usable:
            shown = json.dumps(truncate(self.id, 40), ensure_ascii=False)
            return f'the "chat" {self.where} is {shown}, which isn\'t a chat id (a number such as 123456789)'
        return ""

    def problem(self, does="answers no one"):
        """(state, why) for a status when the bot can't answer its chat, or None when it can."""
        fault = self.fault()
        return (("nochat" if not self.id else "badchat"), f"{fault}, so it {does}") if fault else None

    def state(self):
        """(state, why) for the poller's status after a poll that worked: a fault in bot.json, or
        another chat heard from last, or ok."""
        if self.problem():
            return self.problem()
        if self.stranger:
            return "otherchat", f'a message came from chat {self.stranger}, but the "chat" {self.where} is {self.id}'
        return ("ok",)

    def is_ours(self, chat):
        return self.usable and chat == self.id

    def lets_in(self, chat, update):
        """Whether an update from [chat] is the bot's to answer."""
        if self.is_ours(chat):
            self.stranger = None
            return True
        if not self.usable:
            log(f'{self.name}: update {update.get("update_id")} from chat {chat or "?"} ignored: {self.fault()}')
        else:
            log(f'{self.name}: update {update.get("update_id")} from chat {chat or "?"} ignored: '
                f'the "chat" {self.where} is {self.id}')
            self.stranger = chat or "?"
        return False


class Bot:
    """code.gs's doPost and everything it calls, with the sheet swapped for the store."""

    def __init__(self, store, tg, allowed_chat=""):
        self.store = store
        self.tg = tg
        self.chat = Chat(allowed_chat, "in bot.json", "bot")

    # -- sending, as the script's helpers did --

    def send(self, chat, text):
        self.tg.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML"})

    def send_inline(self, chat, text, keyboard):
        self.tg.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                     "reply_markup": {"inline_keyboard": keyboard}})

    def edit(self, chat, msg, text):
        self.tg.call("editMessageText", {"chat_id": chat, "message_id": msg, "text": text, "parse_mode": "HTML"})

    def edit_inline(self, chat, msg, text, keyboard):
        self.tg.call("editMessageText", {"chat_id": chat, "message_id": msg, "text": text, "parse_mode": "HTML",
                                         "reply_markup": {"inline_keyboard": keyboard}})

    def delete_message(self, chat, msg):
        self.tg.call("deleteMessage", {"chat_id": chat, "message_id": msg})

    def answer_callback(self, cid, text):
        self.tg.call("answerCallbackQuery", {"callback_query_id": cid, "text": text or ""})

    # -- one update --

    def refused(self, chat, update):
        """Whether [chat] isn't the one the bot answers. With no chat in bot.json it answers none:
        an empty one used to mean anyone who found the bot could read, add and delete."""
        return not self.chat.lets_in(chat, update)

    def handle(self, update):
        try:
            if update.get("callback_query"):
                cb = update["callback_query"]
                chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                if self.refused(chat, update):
                    return
                # Always answer, or the tapped button keeps spinning.
                toast = ""
                try:
                    toast = self.on_callback(cb)
                finally:
                    self.answer_callback(cb["id"], toast)
            elif (update.get("message") or {}).get("text"):
                msg = update["message"]
                chat = str(msg["chat"]["id"])
                if self.refused(chat, update):
                    return
                self.on_message(chat, msg)
        except Exception as err:  # noqa: BLE001 - as the script did: say so in the chat, keep going
            log(f"update {update.get('update_id')}: {type(err).__name__}: {err}")
            chat = None
            if update.get("message"):
                chat = str(update["message"]["chat"]["id"])
            elif update.get("callback_query"):
                chat = str(((update["callback_query"].get("message") or {}).get("chat") or {}).get("id", "")) or None
            if chat and self.chat.is_ours(chat):
                self.tg.call("sendMessage", {"chat_id": chat, "text": f"❌ CRASH LOG:\n{type(err).__name__}: {err}"})

    def on_message(self, chat, message):
        # Only the bot's name that Telegram appends to commands in groups (/search@example_song_bot).
        text = re.sub(r"^(/\w+)@\w+", r"\1", message["text"].strip(), flags=re.ASCII)
        # The command as a whole word: "/adds Em" or "/holdx" is no /add or /hold, but a mistyped
        # command, which startswith() let through to be added or held under that name.
        cmd = re.match(r"/(\w+)", text, flags=re.ASCII)
        command, rest = (cmd.group(1).lower(), text[cmd.end():].strip()) if cmd else (None, "")
        user = str(message["from"]["id"])
        # Only a recent question takes typed text as its answer: days after "Artist(s)?" went
        # unanswered, a whole new song became that old title's artist.
        state = self.store.get_state(user, newer_than=now_ms() - WAIT_KEEP_S * 1000)

        if command == "start":
            self.store.clear_state(user)
            self.send(chat, "Chào bạn! 🎵\nGửi tên bài hát bất kỳ để thêm (Tên ⏎ Artist ⏎ Genre), hoặc dùng lệnh:\n\n"
                            "/add — thêm bài mới\n/hold — đưa bài vào OnHold\n/search — tìm theo tên, artist, genre hoặc \"day 26\"\n"
                            "/stats — thống kê & quản lý OnHold\n/cancel — huỷ thao tác đang dở")
            return
        if command == "cancel":
            self.store.clear_state(user)
            self.send(chat, "❌ Đã huỷ tác vụ.")
            return
        if command == "search":
            self.store.clear_state(user)
            if rest:
                self.search(chat, rest, 0)
            else:
                self.store.set_state(user, {"step": "await_search"})
                self.send(chat, "🔍 Nhập tên bài hát hoặc tên artist để tìm:")
            return
        if command == "stats":
            self.store.clear_state(user)
            self.send_inline(chat, self.stats_header(), stats_menu())
            return
        if command == "add":
            self.store.clear_state(user)
            if rest:
                self.add_input(chat, user, rest)
            else:
                self.send(chat, "➕ Nhập thông tin bài hát.\n\nCó thể nhập 1, 2 hoặc 3 dòng:\n• Tên bài\n• Artist(s)\n• Genre\n\n"
                                "Mẹo: gửi luôn /add kèm các dòng trên để thêm nhanh.")
            return
        if command == "hold":
            self.store.clear_state(user)
            if rest:
                self.hold_input(chat, user, rest)
            else:
                self.store.set_state(user, {"step": "await_hold"})
                self.send(chat, "📝 Đang ở chế độ <b>Hold</b>.\nVui lòng gửi thông tin bài hát theo định dạng (mỗi thông tin một dòng):\n\n"
                                "<i>Tên bài hát\nTên Ca sĩ\nGhi chú (tùy chọn)</i>\n\nVí dụ:\nĐưa Nhau Đi Trốn\nĐen Vâu, Linh Cáo\nCần tìm beat chuẩn")
            return
        # A mistyped command ("/serch Em") would otherwise be added as a song called "/serch Em".
        if text.startswith("/"):
            self.send(chat, "❓ Không có lệnh này. Dùng /add, /hold, /search, /stats hoặc /cancel.")
            return

        if state:
            step = state.get("step")
            if step == "await_hold":
                self.store.clear_state(user)
                self.hold_input(chat, user, text)
                return
            if step == "await_search":
                self.store.clear_state(user)
                self.search(chat, text, 0)
                return
            if step == "await_artist":
                refusal = too_long(artists=text)
                if refusal:
                    self.send(chat, refusal)
                    return
                state["artists"] = text
                state["step"] = "await_genre"
                self.store.set_state(user, state)
                self.genre_buttons(chat, state.get("title", ""), state["artists"])
                return
        self.add_input(chat, user, text)

    def genre_buttons(self, chat, title, artists):
        buttons = rows_of_three([{"text": g, "callback_data": "add_" + g} for g in GENRES])
        buttons[-1].append({"text": "❌ Huỷ", "callback_data": "cancel_add"})
        self.send_inline(chat, f"🎵 <b>{esc(title)}</b>\n👤 {esc(artists)}\n\nChọn Genre:", buttons)

    def add_input(self, chat, user, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        title = lines[0] if lines else ""
        artists = lines[1] if len(lines) > 1 else ""
        genre = parse_genre(lines[2] if len(lines) > 2 else "")
        refusal = too_long(title=title, artists=artists)
        if refusal:
            self.send(chat, refusal)
        elif title and artists and genre:
            self.try_add(chat, user, title, artists, genre)
        elif title and artists:
            self.store.set_state(user, {"step": "await_genre", "title": title, "artists": artists})
            self.genre_buttons(chat, title, artists)
        elif title:
            self.store.set_state(user, {"step": "await_artist", "title": title})
            self.send(chat, f"Nhập Artist(s) cho:\n🎵 <b>{esc(title)}</b>")

    def on_callback(self, cb):
        """An optional toast for the tapped button."""
        chat, msg = cb["message"]["chat"]["id"], cb["message"]["message_id"]
        user = str(cb["from"]["id"])
        data = cb.get("data") or ""
        store = self.store

        if data in ("cancel_add", "close_msg"):
            store.clear_state(user)
            self.delete_message(chat, msg)
            return None

        if data.startswith("add_"):
            state, genre = store.get_state(user), data[4:]
            if not state or state.get("step") != "await_genre" or genre not in GENRES:
                return EXPIRED
            store.clear_state(user)
            self.delete_message(chat, msg)
            self.try_add(chat, user, state["title"], state["artists"], genre)
            return None

        if data in ("force_add", "force_hold"):
            state = store.get_state(user)
            if not state or state.get("step") != ("confirm_dup" if data == "force_add" else "confirm_hold"):
                return EXPIRED
            store.clear_state(user)
            self.delete_message(chat, msg)
            if data == "force_add":
                self.try_add(chat, user, state["title"], state["artists"], state["genre"], force=True)
            else:
                self.hold_song(chat, user, state["title"], state.get("artists", ""), state.get("note", ""), force=True)
            return None

        # undo_/unhold_ carry the song they were made for, so they keep working after other commands.
        if data.startswith(("undo_", "unhold_")):
            undo = data.startswith("undo_")
            with store.lock:  # found and changed in one go; Telegram is told after, outside the lock
                song = self.by_ref(store.song, split_first(data)[1])
                if song and undo:
                    store.delete("songs", song["id"])
                elif song:
                    self.remove_hold_like(song["title"].strip())
            if not song:
                return "⚠️ Không tìm thấy bài này nữa (sheet đã thay đổi)."
            if undo:
                self.edit(chat, msg, f"🗑️ Đã hoàn tác thêm bài:\n🎵 <b>{esc(song['title'])}</b>")
            else:
                self.edit_inline(chat, msg, added_text(song) + "\n\n✅ Đã dọn dẹp khỏi OnHold!",
                                 [[{"text": "🗑️ Xóa (Hoàn tác)", "callback_data": "undo_" + ref_of(song)}]])
            return None

        if data.startswith("search_"):
            parts = data.split("_")
            page = int_or(parts[1] if len(parts) > 1 else "", 0)
            q = store.get_search("_".join(parts[2:]))
            if q:
                self.search(chat, q, page, msg)
            else:
                self.edit(chat, msg, "⌛ Kết quả tìm kiếm đã hết hạn. Gửi /search để tìm lại.")
            return None

        if data == "stats_menu":
            self.edit_inline(chat, msg, self.stats_header(), stats_menu())
            return None
        if data == "stats_genre":
            self.edit_inline(chat, msg, self.genre_board(), [[BACK_TO_STATS]])
            return None
        if data == "stats_artist":
            self.edit_inline(chat, msg, self.artist_board(None), [[BACK_TO_STATS]])
            return None
        if data == "stats_artist_genre":
            buttons = rows_of_three([{"text": g, "callback_data": "statg_" + g} for g in GENRES])
            buttons.append([BACK_TO_STATS])
            self.edit_inline(chat, msg, "Chọn Genre để xem Artist Leaderboard:", buttons)
            return None
        if data.startswith("statg_"):
            self.edit_inline(chat, msg, self.artist_board(data[6:]),
                             [[{"text": "↩️ Quay lại", "callback_data": "stats_artist_genre"}]])
            return None

        # OnHold: holdp_<page> turns pages; hadd_/hdel_/hdelok_<page>_<ref> act on one entry.
        if data == "stats_onhold" or data.startswith("holdp_"):
            text, keyboard = self.onhold_view(0 if data == "stats_onhold" else int_or(split_first(data)[1], 0))
            self.edit_inline(chat, msg, text, keyboard)
            return None
        if data.startswith(("hadd_", "hdel_", "hdelok_")):
            action, rest = split_first(data)
            page_text, ref = split_first(rest)
            page = int_or(page_text, 0)

            def reload(toast):
                text, keyboard = self.onhold_view(page)
                self.edit_inline(chat, msg, text, keyboard)
                return toast

            stale = "⚠️ Danh sách OnHold đã thay đổi, đã tải lại."
            with store.lock:
                hold = self.by_ref(store.hold, ref)
                if hold and action == "hdelok":
                    store.delete("holds", hold["id"])
            if not hold:
                return reload(stale)
            if action == "hdelok":
                return reload(truncate("🗑️ Đã xoá khỏi OnHold: " + hold["title"].strip(), 190))
            if action == "hdel":
                text, keyboard = self.onhold_view(page, ref)
                self.edit_inline(chat, msg, text, keyboard)
                return None
            # hadd hands the held song to the add flow; once added, its "Xóa khỏi OnHold" clears the hold.
            title, artists = hold["title"].strip(), (hold["artists"] or "").strip()
            if artists:
                store.set_state(user, {"step": "await_genre", "title": title, "artists": artists})
                self.genre_buttons(chat, title, artists)
            else:
                store.set_state(user, {"step": "await_artist", "title": title})
                self.send(chat, f"Nhập Artist(s) cho:\n🎵 <b>{esc(title)}</b>")
            return None

        return "⚠️ Nút này đã cũ, hãy mở lại menu."

    @staticmethod
    def by_ref(find, ref):
        sid, digest = split_first(str(ref))
        record = find(int_or(sid, -1))
        return record if record and short_hash(record["title"].strip(), 8) == digest else None

    # -- songs and holds --

    def try_add(self, chat, user, title, artists, genre, force=False):
        typed = artists
        songs = self.store.songs()
        artists = canonical_artists(artists, songs)
        dup = None if force else find_duplicate(title, artists, songs)
        if dup:
            self.store.set_state(user, {"step": "confirm_dup", "title": title, "artists": artists, "genre": genre})
            self.send_inline(chat, f"⚠️ Bài này có vẻ đã tồn tại:\n🎵 <b>{esc(dup['title'])}</b>\n👤 {esc(dup['artists'])}\n"
                                   f"📅 Day {esc(dup['day'])} | {esc(dup['genre'])}\n\nVẫn thêm <b>{esc(title)}</b> ({genre})?",
                             [[{"text": "✅ Vẫn thêm", "callback_data": "force_add"}, {"text": "❌ Huỷ", "callback_data": "cancel_add"}]])
            return
        song = self.store.add_song(title, artists, genre)
        held = any(similarity(title, h["title"].strip()) >= ONHOLD_TITLE_MATCH_MIN for h in self.store.holds())
        self.store.clear_state(user)
        buttons = [[{"text": "🗑️ Xóa (Hoàn tác)", "callback_data": "undo_" + ref_of(song)}]]
        if held:
            buttons[0].append({"text": "🔖 Xóa khỏi OnHold", "callback_data": "unhold_" + ref_of(song)})
        renamed = f"\n<i>(Artist theo cách viết đã có; bạn nhập: {esc(typed)})</i>" if ", ".join(split_artists(typed)) != artists else ""
        self.send_inline(chat, added_text(song) + renamed + ("\n\n<i>⚠️ Bài này đang nằm trong OnHold!</i>" if held else ""), buttons)

    def hold_input(self, chat, user, text):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        title = lines[0] if lines else ""
        if not title:
            self.send(chat, "⚠️ Lỗi. Gửi tên bài để đưa vào OnHold.")
            return
        artists, note = lines[1] if len(lines) > 1 else "", lines[2] if len(lines) > 2 else ""
        refusal = too_long(title=title, artists=artists, note=note)
        if refusal:
            self.send(chat, refusal)
            return
        self.hold_song(chat, user, title, artists, note)

    def hold_song(self, chat, user, title, artists, note, force=False):
        conflict = None if force else find_hold_conflict(title, artists, self.store.songs(), self.store.holds())
        if conflict:
            self.store.set_state(user, {"step": "confirm_hold", "title": title, "artists": artists, "note": note})
            where = ("🔖 Bài này đã có trong OnHold:" if conflict["isHold"]
                     else f"✅ Bài này đã có trong Songs (Day {esc(conflict['day'])} | {esc(conflict['genre'])}):")
            self.send_inline(chat, f"⚠️ {where}\n🎵 <b>{esc(conflict['title'])}</b>"
                                   + (f"\n👤 {esc(conflict['artists'])}" if conflict["artists"] else "")
                                   + f"\n\nVẫn hold <b>{esc(title)}</b>?",
                             [[{"text": "✅ Vẫn hold", "callback_data": "force_hold"}, {"text": "❌ Huỷ", "callback_data": "cancel_add"}]])
            return
        self.store.add_hold(title, artists, note)
        self.send(chat, f"🔖 Đã thêm vào OnHold:\n🎵 <b>{esc(title)}</b>" + (f"\n👤 {esc(artists)}" if artists else "")
                        + (f"\n📝 {esc(note)}" if note else ""))

    def remove_hold_like(self, title):
        """Only the closest match, and exact copies of it: one tap can't sweep up every hold that
        looks a bit like the song."""
        holds = self.store.holds()
        if not holds:
            return
        scores = [similarity(title, h["title"].strip()) for h in holds]
        best = max(scores)
        if best < ONHOLD_TITLE_MATCH_MIN:
            return
        for h, s in zip(holds, scores):
            if s == best:
                self.store.delete("holds", h["id"])

    # -- search --

    def search(self, chat, query, page, edit_msg=None):
        results = search_songs(query, self.store.songs(), self.store.holds())
        if not results:
            text = f'🔍 Không tìm thấy bài nào khớp với "{esc(query)}".'
            if edit_msg:
                self.edit(chat, edit_msg, text)
            else:
                self.send(chat, text)
            return
        pages = math.ceil(len(results) / SEARCH_PER_PAGE)
        page = max(0, min(page, pages - 1))
        msg = f"🔍 Kết quả: <b>{esc(query)}</b> (Trang {page + 1}/{pages})\n─────────────────\n"
        for i, r in enumerate(results[page * SEARCH_PER_PAGE:(page + 1) * SEARCH_PER_PAGE]):
            n = page * SEARCH_PER_PAGE + i + 1
            if r["isHold"]:
                msg += (f"{n}. 🔖 <b>[ON HOLD] {esc(r['title'])}</b>" + (f"\n   👤 {esc(r['artists'])}" if r["artists"] else "")
                        + (f"\n   📝 {esc(r['note'])}" if r["note"] else "") + f"\n   🎯 Khớp {js_round(r['score'] * 100)}%\n\n")
            else:
                msg += (f"{n}. <b>{esc(r['title'])}</b>\n   👤 {esc(r['artists'])}\n   📅 Day {r['day']} | {r['genre']}"
                        + ("" if r["score"] is None else f" | {js_round(r['score'] * 100)}%") + "\n\n")
        # Telegram caps callback_data at 64 bytes, which a long Vietnamese query passes, so buttons
        # carry a hash and the query waits here. Each page turn puts it back, for 6 more hours.
        key = ""
        if pages > 1:
            key = short_hash(query, 12)
            self.store.put_search(key, query)
        nav = []
        if page > 0:
            nav.append({"text": "◀ Trước", "callback_data": f"search_{page - 1}_{key}"})
        if page < pages - 1:
            nav.append({"text": "Sau ▶", "callback_data": f"search_{page + 1}_{key}"})
        if edit_msg:
            self.edit_inline(chat, edit_msg, msg, [nav] if nav else [])
        else:
            self.send_inline(chat, msg, [nav] if nav else [])

    # -- stats --

    def stats_header(self):
        rows = [s for s in self.store.songs() if s["title"].strip() and s["genre"].strip().upper() in GENRES]
        holds = sum(1 for h in self.store.holds() if h["title"].strip())
        latest = None
        for s in rows:
            if s["day"] > 0 and (latest is None or s["day"] >= latest["day"]):
                latest = s
        msg = f"📊 <b>Thống kê</b>\n🎵 {len(rows)} bài · 🔖 {holds} OnHold"
        if latest:
            msg += f"\n🆕 Day {latest['day']}: <b>{esc(latest['title'].strip())}</b> — {esc(latest['artists'].strip())}"
        return msg + "\n\nChọn loại thống kê:"

    def genre_board(self):
        count = {}
        for s in self.store.songs():
            g = s["genre"].strip().upper()
            if g in GENRES:
                count[g] = count.get(g, 0) + 1
        ranked = sorted(count, key=lambda g: -count[g])
        total = sum(count.values())
        if not ranked:
            return "🎵 <b>Genre Leaderboard</b>\n(Không có dữ liệu)"
        # <pre> is Telegram's monospace; without it the padding can't line the columns up.
        msg = "🎵 <b>Genre Leaderboard</b>\n<pre>"
        for i, g in enumerate(ranked):
            pct = js_round(count[g] / total * 100) if total else 0
            msg += f"{i + 1}. {g:<8} {count[g]:>3} bài {pct:>3}% {'█' * js_round(pct / 5) or '▏'}\n"
        return msg + f"</pre>Tổng: {total} bài"

    def artist_board(self, genre):
        count = {}
        for s in self.store.songs():
            g = s["genre"].strip().upper()
            if g not in GENRES or (genre and g != genre):
                continue
            for a in split_artists(s["artists"]):
                count[a] = count.get(a, 0) + 1
        ranked = sorted(count, key=lambda a: (-count[a], collation(a)))
        top = ranked[:15]
        msg = (f"🎵 Artist Leaderboard — {genre}" if genre else "🎵 Artist Leaderboard") + "\n─────────────────\n"
        if not top:
            return msg + "(Không có dữ liệu)"
        for i, a in enumerate(top):
            msg += f"{i + 1}. {esc(a)}\n    {'█' * js_round(count[a] / count[top[0]] * 8) or '▏'} {count[a]} bài\n"
        return msg + (f"... và {len(ranked) - 15} artist khác" if len(ranked) > 15 else "")

    def onhold_view(self, page, confirm_ref=None):
        """Paged so it never passes Telegram's 4,096 characters. confirm_ref swaps that entry's
        buttons for a delete confirmation."""
        items = [h for h in self.store.holds() if h["title"].strip()]
        if not items:
            return "🔖 OnHold List\n─────────────────\n(Chưa có bài nào)", [[BACK_TO_STATS]]
        pages = math.ceil(len(items) / ONHOLD_PER_PAGE)
        page = min(max(page or 0, 0), pages - 1)
        text = f"🔖 OnHold List ({len(items)} bài) — Trang {page + 1}/{pages}\n─────────────────\n"
        keyboard = []
        for i, h in enumerate(items[page * ONHOLD_PER_PAGE:(page + 1) * ONHOLD_PER_PAGE]):
            n, ref = page * ONHOLD_PER_PAGE + i + 1, ref_of(h)
            artists, note = (h["artists"] or "").strip(), (h["note"] or "").strip()
            text += (f"{n}. <b>{esc(h['title'].strip())}</b>" + (f"\n    👤 {esc(artists)}" if artists else "")
                     + (f"\n    📝 {esc(truncate(note, 100))}" if note else "") + "\n")
            keyboard.append(
                [{"text": f"✅ Xoá #{n}", "callback_data": f"hdelok_{page}_{ref}"}, {"text": "✖️ Không", "callback_data": f"holdp_{page}"}]
                if ref == confirm_ref else
                [{"text": f"➕ Thêm #{n}", "callback_data": f"hadd_{page}_{ref}"}, {"text": f"🗑️ Xoá #{n}", "callback_data": f"hdel_{page}_{ref}"}])
        if confirm_ref:
            text += "\n⚠️ Xoá khỏi OnHold? Bấm ✅ để xác nhận."
        nav = []
        if page > 0:
            nav.append({"text": "◀ Trước", "callback_data": f"holdp_{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "Sau ▶", "callback_data": f"holdp_{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([BACK_TO_STATS])
        return text, keyboard


def collation(s):
    """Close enough to JavaScript's localeCompare for ordering names: accents and case set aside first."""
    return (normalize_title(s), s.casefold())


def rows_of_three(buttons):
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


def too_long(title="", artists="", note=""):
    """The bot's answer to a line longer than the page lets a record have, or None. Kept, it was a
    song the page's Fix couldn't save, and the reply naming it could pass Telegram's 4,096
    characters and be refused, so the chat heard nothing."""
    for value, label, most in ((title, "Tên bài", MAX_TITLE), (artists, "Artist", MAX_ARTISTS), (note, "Ghi chú", MAX_NOTE)):
        if len(value) > most:
            return f"⚠️ {label} dài hơn {most} ký tự."
    return None


def stats_menu():
    return [
        [{"text": "Genre Leaderboard", "callback_data": "stats_genre"}],
        [{"text": "Artist Leaderboard", "callback_data": "stats_artist"}],
        [{"text": "Artist theo Genre", "callback_data": "stats_artist_genre"}],
        [{"text": "OnHold List", "callback_data": "stats_onhold"}],
        [{"text": "❌ Đóng", "callback_data": "close_msg"}],
    ]


def added_text(song):
    return (f"✅ Đã thêm bài mới:\n🎵 <b>{esc(song['title'])}</b>\n👤 {esc(song['artists'])}\n"
            f"📅 Day {esc(song['day'])} | {esc(song['genre'])}")


def canonical_artists(artists, songs):
    """The spelling already in the list ("Vstra" -> "VSTRA"), so a leaderboard doesn't split one artist
    in two. Only case, accents, punctuation and spaces are set aside: "Hà Anh Tuấn" and "Hồ Anh Tuấn"
    stay two people."""
    def key(a):
        return re.sub(r"\s", "", normalize_title(a))

    spellings = {}
    for s in songs:
        for a in split_artists(s["artists"]):
            k = key(a)
            if k:
                spellings.setdefault(k, {})
                spellings[k][a] = spellings[k].get(a, 0) + 1
    out = []
    for a in split_artists(artists):
        known = spellings.get(key(a))
        out.append(max(known, key=lambda x: known[x]) if known else a)
    return ", ".join(out)


def find_duplicate(title, artists, songs):
    """Every row is held to both thresholds before ranking, so a near miss with a higher combined
    score can't hide a real match."""
    best = None
    for s in songs:
        t = similarity(title, s["title"].strip())
        if t < DUP_TITLE_MIN:
            continue
        a = best_artist_similarity(artists, s["artists"].strip())
        if a < DUP_ARTIST_MIN:
            continue
        combined = t * 0.72 + a * 0.28
        if best is None or combined > best["combined"]:
            best = {"title": s["title"].strip(), "artists": s["artists"].strip(), "day": s["day"],
                    "genre": s["genre"].upper(), "combined": combined}
    return best


def find_hold_conflict(title, artists, songs, holds):
    """The same song already in Songs or held. A hold may leave out the artist, so a missing one on
    either side needs only the title to match."""
    def same(t, a):
        return similarity(title, t) >= DUP_TITLE_MIN and (not artists or not a or best_artist_similarity(artists, a) >= DUP_ARTIST_MIN)

    for s in songs:
        if same(s["title"].strip(), s["artists"].strip()):
            return {"title": s["title"].strip(), "artists": s["artists"].strip(), "day": s["day"],
                    "genre": s["genre"].strip().upper(), "isHold": False}
    for h in holds:
        if same(h["title"].strip(), (h["artists"] or "").strip()):
            return {"title": h["title"].strip(), "artists": (h["artists"] or "").strip(), "isHold": True}
    return None


def search_songs(query, songs, holds):
    """ "day 26" and a genre's name list songs, newest first; anything else is a fuzzy match over
    Songs and OnHold. A score of None hides the % in the results."""
    day = re.fullmatch(r"day\s*([0-9]+)", query.strip(), flags=re.I)
    genre = parse_genre(query)
    found = []
    for s in songs:
        title, artists, genre_of = s["title"].strip(), s["artists"].strip(), s["genre"].strip().upper()
        if not title or not artists or genre_of not in GENRES:
            continue
        if day or genre:
            if (s["day"] == int(day.group(1))) if day else genre_of == genre:
                found.append({"title": title, "artists": artists, "day": s["day"], "genre": genre_of, "score": None, "isHold": False})
            continue
        score = search_score(query, title, artists)
        if score:
            found.append({"title": title, "artists": artists, "day": s["day"], "genre": genre_of, "score": score, "isHold": False})
    if day or genre:
        return sorted(found, key=lambda r: -r["day"])
    for h in holds:
        title = h["title"].strip()
        if not title:
            continue
        score = search_score(query, title, (h["artists"] or "").strip())
        if score:
            found.append({"title": title, "artists": (h["artists"] or "").strip(), "note": (h["note"] or "").strip(),
                          "score": score, "isHold": True})
    return sorted(found, key=lambda r: (-r["score"], r["isHold"], collation(r["title"])))


# ---- polling ---------------------------------------------------------------------------------------

class Poller:
    """Asks Telegram for messages, one at a time, and keeps going whatever happens. The offset is
    saved before an update is handled: one that crashes the bot is skipped next time rather than
    added twice, as the script's webhook, which always answered OK, also did."""

    def __init__(self, store, bot, name="bot", offset_key="offset", switch="webhook-off"):
        self.store = store
        self.bot = bot
        self.name = name  # in the log
        self.offset_key = offset_key  # each bot keeps its own place in its own updates
        self.switch = switch  # the xrninsta.py command that moves its messages here
        self.status = {"state": "starting", "since": now_ms(), "lastPoll": None, "error": ""}
        # A chat it can't answer is said once it starts, not first after a poll that worked.
        problem = bot.chat.problem()
        if problem:
            self.note(*problem)

    def note(self, state, error=""):
        if self.status["state"] != state or self.status["error"] != error:
            self.status = {**self.status, "state": state, "since": now_ms(), "error": error}
            log(f"{self.name}: {state}{': ' + error if error else ''}")

    def once(self):
        """One getUpdates; returns how long to wait before the next."""
        offset = int_or(self.store.meta(self.offset_key), 0)
        r = self.bot.tg.call("getUpdates", {"offset": offset, "timeout": POLL_S,
                                            "allowed_updates": ["message", "callback_query"]}, timeout=POLL_S + 15)
        if r is None:
            self.note("unreachable", "Telegram didn't answer")
            return 15
        if not r.get("ok"):
            code, said = r.get("error_code"), r.get("description", "")
            # Telegram answers 409 both while a webhook is set and while another process polls with
            # the same token (a copy left running under PID 1 after its supervisor died); only its
            # description tells them apart, and webhook-off does nothing for the second.
            if code == 409 and "webhook" in said.lower():
                self.note("webhook", "Telegram still sends this bot's messages elsewhere (the Apps Script). "
                                     f"Run `xrninsta.py {self.switch}` to move them here.")
                return 30
            if code == 409:
                self.note("twice", f"another copy of the bot is taking its messages; stop the old process ({said})")
                return 30
            self.note("refused", f"{code}: {said}")
            return 300 if code in (401, 404) else 30
        self.status["lastPoll"] = now_ms()
        for update in r.get("result") or []:
            uid = update.get("update_id")
            if isinstance(uid, int) and uid >= offset:
                self.store.set_meta(self.offset_key, uid + 1)
                self.bot.handle(update)
        # After the updates, which may have come from another chat.
        self.note(*self.bot.chat.state())
        return 0

    def run(self):
        while True:
            try:
                wait = self.once()
            except Exception as e:  # noqa: BLE001 - the loop must outlive anything one poll does
                self.note("error", f"{type(e).__name__}: {e}")
                wait = 15
            if wait:
                time.sleep(wait)


# ---- the daily cover -------------------------------------------------------------------------------

def load_zone(name):
    """The time zone, or None when Python has no data for it: Termux's has none without the tzdata
    package, which deploy.sh installs."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (ImportError, KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return None


def cover_day(date):
    """The cover's number for a day: 263 on 18 Oct 2026, one more for each day since."""
    return COVER_EPOCH_DAY + (date - COVER_EPOCH).days


def cover_file(day):
    return f"Day_{day}.png"


class Covers:
    """coverbot.gs, the Daily Story Bot: at 2am Sydney time it sends that day's cover to its one
    chat, and /sendnow sends it at once. The covers are covers/Day_<n>.png beside the songs, where
    the script searched a Drive folder. It's a bot of its own, with its own token, and only knows
    /start and /sendnow. Unlike the script it keeps a cover once sent (a year of them is 38 MB),
    and it notes the day it last sent, so a restart never sends one twice and a server that was off
    at 2am sends that day's when it's back."""

    def __init__(self, store, tg, allowed_chat, folder, zone=None):
        self.store = store
        self.tg = tg
        self.chat = Chat(allowed_chat, 'under "covers" in bot.json', "covers bot")
        self.folder = folder
        self.zone = zone or load_zone(COVER_ZONE)
        self.lock = threading.Lock()  # the 2am send and /sendnow, never both at once
        self.status = {"state": "starting", "since": now_ms(), "sent": int_or(store.meta("cover_day"), 0), "error": ""}

    def note(self, state, error=""):
        if self.status["state"] != state or self.status["error"] != error:
            self.status = {**self.status, "state": state, "since": now_ms(), "error": error}
            log(f"covers: {state}{': ' + error if error else ''}")

    def now(self):
        return datetime.datetime.now(self.zone)

    def send(self, text):
        self.tg.call("sendMessage", {"chat_id": self.chat.id, "text": text, "parse_mode": "HTML"})

    def send_cover(self, day):
        """Day <day>'s cover to the chat. True once it's settled: sent, or no such cover or a
        refusal, both said in the chat as the script did. False when Telegram couldn't be reached,
        so the next try sends it."""
        name = cover_file(day)
        try:
            with open(os.path.join(self.folder, name), "rb") as f:
                content = f.read()
        except FileNotFoundError:
            # The script's warning showed its <b> tags as they were: it sent no parse_mode.
            self.send(f"⚠️ Không tìm thấy ảnh cho hôm nay: <b>{name}</b>\nBạn hãy chép thêm ảnh lên server nhé.")
            self.note("missing", f"no {name} in {self.folder}")
            return True
        r = self.tg.upload("sendPhoto", {"chat_id": self.chat.id, "caption": f"Day {day}'s cover"},
                           "photo", name, content)
        if r is None:
            self.note("unreachable", "Telegram didn't answer")
            return False
        if not r.get("ok"):
            self.send(f"❌ Lỗi khi gửi ảnh: {esc(r.get('description', ''))}")
            self.note("refused", f"{r.get('error_code')}: {r.get('description', '')}")
            return True
        self.note("ok")
        return True

    def settle(self, day):
        """Sends Day <day>'s cover and notes it as done; False if Telegram wasn't reached."""
        with self.lock:
            if not self.send_cover(day):
                return False
            self.store.set_meta("cover_day", day)
            self.status["sent"] = day
            return True

    def first_start(self, now=None):
        """The first time it runs, after 2am, the Apps Script has sent today's cover: it takes that
        as sent, and starts with tomorrow's. /sendnow still sends it."""
        if self.zone is None or self.store.meta("cover_day") is not None:
            return
        now = now or self.now()
        if (now.hour, now.minute) >= COVER_AT:
            day = cover_day(now.date())
            self.store.set_meta("cover_day", day)
            self.status["sent"] = day
            log(f"covers: first start, after 2am: Day {day}'s cover is taken as sent by the Apps Script")

    def tick(self, now=None):
        """Sends today's cover if it's past 2am in Sydney and it hasn't gone yet; returns how long
        to wait before looking again. The clock is read each minute, so the night the clocks go
        forward (no 2am) it goes at 3, and the night they go back (2am twice) it goes once."""
        if self.zone is None:
            self.note("nozone", f"Python has no time zone data for {COVER_ZONE}; pip install tzdata")
            return 3600
        # Sent to a chat that isn't one, Telegram's refusal settled the day and the cover never went.
        problem = self.chat.problem("sends no one a cover")
        if problem:
            self.note(*problem)
            return 3600
        now = now or self.now()
        day = cover_day(now.date())
        if (now.hour, now.minute) < COVER_AT or int_or(self.store.meta("cover_day"), 0) >= day:
            return 60
        return 60 if self.settle(day) else 300

    def run(self):
        while True:
            try:
                wait = self.tick()
            except Exception as e:  # noqa: BLE001 - like the poller, the loop outlives anything one send does
                self.note("error", f"{type(e).__name__}: {e}")
                wait = 300
            time.sleep(wait)

    def handle(self, update):
        msg = update.get("message") or {}
        if not msg.get("text"):
            return
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not self.chat.lets_in(chat, update):
            return
        text = re.sub(r"^(/\w+)@\w+", r"\1", msg["text"].strip(), flags=re.ASCII)
        cmd = re.match(r"/(\w+)\b", text, flags=re.ASCII)
        command = cmd.group(1).lower() if cmd else None
        try:
            if command == "start":
                self.send("🌅 Daily Story Bot đã sẵn sàng!\nNhập lệnh /sendnow để bot lấy và gửi ảnh của ngày hôm nay ngay lập tức.")
            elif command == "sendnow":
                if self.zone is None:
                    self.send(f"⚠️ Server chưa có dữ liệu múi giờ {COVER_ZONE} (pip install tzdata).")
                    return
                self.send("⏳ Đang lấy ảnh, đợi chút nhé...")
                self.settle(cover_day(self.now().date()))
        except Exception as err:  # noqa: BLE001 - as the song bot does: say so in the chat, keep going
            log(f"covers: update {update.get('update_id')}: {type(err).__name__}: {err}")
            self.tg.call("sendMessage", {"chat_id": chat, "text": f"❌ CRASH LOG:\n{type(err).__name__}: {err}"})


def covers_poller(store, covers):
    """The Daily Story Bot's own poller: its place in its own updates, and its own switch."""
    return Poller(store, covers, name="covers bot", offset_key="covers_offset", switch="webhook-off --covers")


def cover_days(folder):
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted(int(m.group(1)) for m in map(COVER_NAME.fullmatch, names) if m)


def covers_summary(folder, today):
    """How far the covers go from [today]'s, and what's missing on the way."""
    ahead = [d for d in cover_days(folder) if d >= today]
    if not ahead:
        return f"Covers: none from Day {today} (today) on, in {folder}"
    last = ahead[-1]
    runs_out = COVER_EPOCH + datetime.timedelta(days=last - COVER_EPOCH_DAY)
    said = f"Covers: Day {today} (today) to Day {last}, the last on {runs_out.day} {runs_out:%b %Y}"
    if ahead[0] != today:
        said += f"; today's ({cover_file(today)}) is missing"
    gaps = last - today + 1 - len(ahead) - (ahead[0] != today)
    if gaps:
        said += f"; {gaps} more missing before the last"
    return said


def add_covers(folder, source):
    """Copies each Day_<n>.png in [source] (a zip, as Drive downloads a folder, or a folder) into
    the covers folder, by its name alone, wherever the zip had it. Anything else is skipped: other
    names, macOS's ._ copies, files that aren't PNGs or are over Telegram's 10 MB."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    done = {"added": 0, "replaced": 0, "skipped": []}

    def keep(name, opener):
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not COVER_NAME.fullmatch(base):
            done["skipped"].append(name)
            return
        with opener() as f:
            content = f.read(COVER_MAX_BYTES + 1)
        if not content.startswith(PNG_SIGNATURE) or len(content) > COVER_MAX_BYTES:
            done["skipped"].append(name)
            return
        path = os.path.join(folder, base)
        done["replaced" if os.path.exists(path) else "added"] += 1
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    if os.path.isdir(source):
        for name in sorted(os.listdir(source)):
            path = os.path.join(source, name)
            if os.path.isfile(path):
                keep(name, lambda p=path: open(p, "rb"))
    else:
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                if not info.is_dir():
                    keep(info.filename, lambda i=info: z.open(i))
    return done


# ---- importing the sheet ---------------------------------------------------------------------------

XLSX = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def read_xlsx(path):
    """{sheet name: [(row number, [cell values])]} from an .xlsx, with only the standard library."""
    z = zipfile.ZipFile(path)
    m = "{%s}" % XLSX["m"]
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", XLSX):
            shared.append("".join(t.text or "" for t in si.iter(m + "t")))
    rels = {r.get("Id"): r.get("Target") for r in ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheets = {}
    for s in ElementTree.fromstring(z.read("xl/workbook.xml")).find("m:sheets", XLSX):
        target = rels[s.get("{%s}id" % XLSX["r"])].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ElementTree.fromstring(z.read(target)).iter(m + "row"):
            cells = {}
            for c in row.findall("m:c", XLSX):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r")).group():
                    col = col * 26 + ord(ch) - 64
                v, kind = c.find("m:v", XLSX), c.get("t")
                if kind == "s" and v is not None:
                    value = shared[int(v.text)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(m + "t"))
                elif kind == "str" and v is not None:
                    value = v.text or ""
                elif v is not None:
                    try:
                        value = float(v.text)
                        value = int(value) if value.is_integer() else value
                    except ValueError:
                        value = v.text
                else:
                    value = None
                cells[col - 1] = value
            rows.append((int(row.get("r")), [cells.get(i) for i in range(max(cells) + 1)] if cells else []))
        sheets[s.get("name")] = rows
    return sheets


def cell(row, i):
    v = row[i] if i < len(row) else None
    return "" if v is None else str(v).strip()


def import_xlsx(store, path, replace=False):
    """Songs and OnHold from an export of the sheet. Each keeps its sheet row as its id, so the
    buttons on the script's old messages still find it. Returns what was read, for the report."""
    sheets = {name.lower(): rows for name, rows in read_xlsx(path).items()}
    if "songs" not in sheets:
        raise SystemExit("no Songs sheet in that file")
    with store.lock, store.db:
        have = store.db.execute("SELECT (SELECT COUNT(*) FROM songs) + (SELECT COUNT(*) FROM holds)").fetchone()[0]
        if have and not replace:
            raise SystemExit(f"{store.path} already holds {have} songs and holds; add --replace to start over from the file")
        store.db.execute("DELETE FROM songs")
        store.db.execute("DELETE FROM holds")
        store.db.execute("DELETE FROM sqlite_sequence WHERE name IN ('songs', 'holds')")
        stamp = now_ms()
        report = {"songs": 0, "holds": 0, "oddGenre": 0, "noDay": 0}
        for number, row in sheets["songs"]:
            title = cell(row, 0)
            if number < 2 or not title:
                continue
            day = row[2] if len(row) > 2 else None
            if isinstance(day, float):
                day = int(day)
            if not isinstance(day, int):
                day = int_or(cell(row, 2), 0)
                report["noDay"] += not cell(row, 2).lstrip("-").isdigit()
            genre = cell(row, 3).upper()
            report["oddGenre"] += genre not in GENRES
            store.db.execute("INSERT INTO songs(id, title, artists, day, genre, added, updated) VALUES(?,?,?,?,?,NULL,?)",
                             (number, title, cell(row, 1), day, genre, stamp))
            report["songs"] += 1
        for number, row in sheets.get("onhold", []):
            title = cell(row, 0)
            if number < 2 or not title:
                continue
            store.db.execute("INSERT INTO holds(id, title, artists, note, added, updated) VALUES(?,?,?,?,NULL,?)",
                             (number, title, cell(row, 1), cell(row, 2), stamp))
            report["holds"] += 1
    return report


# ---- signing in (as the sync servers do) ------------------------------------------------------------

class SignedOut:
    """The cookies signed out on any server that shares the users file and session.key (xrnhub,
    xrnloops-sync and xrnrule-sync too): signed-out.json beside them, {sha256 of the cookie: when it
    would have run out}."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seen = None  # the file (inode, mtime, size) [listed] was read from
        self.listed = {}

    def read(self):
        """The file's list: {} with no file, None (and a line in the log) for one that can't be read."""
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as e:  # ValueError: also bad UTF-8
            log(f"{self.path} can't be read, so the sign-outs read before it hold: {e}")
            return None
        return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, (int, float))}

    def hashes(self):
        """The list as the file has it now, read again only when it has changed, so a sign-out on
        another server counts here from the next request. A file that can't be read keeps the last list."""
        try:
            st = os.stat(self.path)
            now = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            now = None
        except OSError as e:
            now = ("unreadable", e.errno)
        with self.lock:
            if now != self.seen:
                self.seen = now
                listed = self.read() if now is not None else {}
                if listed is not None:
                    self.listed = listed
            return self.listed

    def add(self, token):
        """Puts a cookie on the list, dropping what has run out. Under an flock on
        signed-out.json.lock, with the file read again inside it, so two servers signing out at once
        both keep theirs; written whole to a new file that then takes the old one's place."""
        lock = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            listed = self.read()
            if listed is None:
                listed = dict(self.listed)
            listed[hashlib.sha256(token.encode()).hexdigest()] = int(token.split(".")[1])
            now = time.time()
            kept = {k: v for k, v in listed.items() if v >= now}  # run out: refused anyway
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(kept, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            st = os.stat(self.path)
            with self.lock:
                self.seen, self.listed = (st.st_ino, st.st_mtime_ns, st.st_size), kept
        finally:
            os.close(lock)  # and with it the flock


class Login:
    def __init__(self, users_path):
        self.users_path = users_path
        self.session_key = load_session_key(os.path.join(os.path.dirname(users_path), "session.key"))
        self.signed_out = SignedOut(os.path.join(os.path.dirname(users_path), "signed-out.json"))
        self.cache = {}
        self.fail_lock = threading.Lock()
        self.fail_settled = threading.Condition(self.fail_lock)  # a login being checked has been settled
        self.failures = {}  # address -> [(monotonic time, name tried)] of its recent wrong logins
        self.checking = {}  # address -> how many of its logins are being checked right now

    def names(self):
        found = []
        try:
            with open(self.users_path, encoding="utf-8") as f:
                for line in f:
                    name = line.strip().partition(":")[0]
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
                        found.append(name)
        except OSError:
            pass
        return found

    def mtime(self):
        try:
            return os.stat(self.users_path).st_mtime
        except OSError:
            return 0

    def recent_failures(self, addr, now):
        """This address's wrong logins within FAIL_WINDOW_S; older ones are forgotten. Under fail_lock."""
        recent = [f for f in self.failures.get(addr, ()) if now - f[0] < FAIL_WINDOW_S]
        if recent:
            self.failures[addr] = recent
        else:
            self.failures.pop(addr, None)
        return recent

    def reserve_guess(self, addr):
        """Waits for a place to check a password from this address in; False once the address has
        used up its wrong logins. A login being checked holds a place until settle_guess: counted only
        afterwards, guesses sent at once all passed the check while the first were still in bcrypt.
        It isn't a wrong login, though: one that finds no place waits for it to be settled rather than
        being turned away, so two sign-ins at once never lock each other out."""
        with self.fail_settled:
            now = time.monotonic()
            if len(self.failures) > 10000:  # a flood from many addresses: forget the stale ones
                for a in [a for a, fs in self.failures.items() if now - fs[-1][0] >= FAIL_WINDOW_S]:
                    del self.failures[a]
            while True:
                wrong = len(self.recent_failures(addr, time.monotonic()))
                if wrong >= FAIL_LIMIT:
                    return False
                if wrong + self.checking.get(addr, 0) < FAIL_LIMIT:
                    self.checking[addr] = self.checking.get(addr, 0) + 1
                    return True
                self.fail_settled.wait(1)

    def settle_guess(self, addr, name, right):
        """A login reserve_guess let through has been checked. A wrong one counts against the address,
        under the name it tried. A right one clears that name's mistakes from this address, and no one
        else's: a sign-in mustn't give a guesser behind the same address its guesses back. None, a
        password already found right (cached_basic) or a check that broke, settles nothing."""
        with self.fail_settled:
            left = self.checking.get(addr, 1) - 1
            if left > 0:
                self.checking[addr] = left
            else:
                self.checking.pop(addr, None)
            if right is False:
                self.failures.setdefault(addr, []).append((time.monotonic(), name[:64]))
            elif right:
                kept = [f for f in self.failures.get(addr, ()) if f[1] != name[:64]]
                if kept:
                    self.failures[addr] = kept
                else:
                    self.failures.pop(addr, None)
            self.fail_settled.notify_all()

    def locked_out(self, addr):
        with self.fail_lock:
            return len(self.recent_failures(addr, time.monotonic())) >= FAIL_LIMIT

    @staticmethod
    def basic_login(header):
        """(user, password) from `Authorization: Basic ...`, or None."""
        if not header or not header.startswith("Basic "):
            return None
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001 - any bad header is no login
            return None
        return user, password

    def cached_basic(self, header):
        """The user for a password already found right (the cache), else None: no check."""
        login = self.basic_login(header)
        if not login:
            return None
        hit = self.cache.get(hashlib.sha256(f"{login[0]}:{login[1]}".encode()).hexdigest())
        return hit[0] if hit and hit[1] > time.time() and hit[2] == self.mtime() else None

    def check_basic(self, header):
        login = self.basic_login(header)
        if not login:
            return None
        user, password = login
        key = hashlib.sha256(f"{user}:{password}".encode()).hexdigest()
        mtime = self.mtime()
        hit = self.cache.get(key)
        if hit and hit[1] > time.time() and hit[2] == mtime:
            return hit[0]
        import bcrypt  # only needed once someone signs in

        try:
            checked, spare = False, None
            with open(self.users_path, encoding="utf-8") as f:
                for line in f:
                    name, _, hashed = line.strip().partition(":")
                    spare = hashed or spare
                    if name == user and hashed:
                        checked = True
                        if bcrypt.checkpw(password.encode(), hashed.encode()):
                            self.cache[key] = (user, time.time() + AUTH_CACHE_S, mtime)
                            return user
            if not checked and spare:
                bcrypt.checkpw(password.encode(), spare.encode())  # as long as a wrong password takes
        except (OSError, ValueError):
            pass
        return None

    def password_tag(self, user):
        try:
            with open(self.users_path, encoding="utf-8") as f:
                for line in f:
                    name, _, hashed = line.strip().partition(":")
                    if name == user and hashed:
                        return hashlib.sha256(hashed.encode()).hexdigest()
        except OSError:
            pass
        return None

    def mac(self, body, tag):
        return hmac.new(self.session_key, f"{body}|{tag}".encode(), hashlib.sha256).hexdigest()

    def make_session(self, user):
        who = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        tag = self.password_tag(user)
        expires = int(time.time()) + SESSION_S
        # A cookie is made from the name, the second it runs out and the password alone, so one made
        # in the second the same one was signed out (here or on another server) would be that cookie.
        # It runs out a second later instead, which keeps the format every server reads.
        listed = self.signed_out.hashes()
        while True:
            body = f"{who}.{expires}"
            token = f"{body}.{self.mac(body, tag)}"
            if hashlib.sha256(token.encode()).hexdigest() not in listed:
                return token
            expires += 1

    @staticmethod
    def session_token(cookie_header):
        token = ""
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                token = value
        return token

    def check_session(self, cookie_header):
        token = self.session_token(cookie_header)
        try:
            who, expires, mac = token.split(".")
            user = base64.urlsafe_b64decode(who + "=" * (-len(who) % 4)).decode("utf-8")
            if int(expires) < time.time():
                return None
        except ValueError:
            return None
        if hashlib.sha256(token.encode()).hexdigest() in self.signed_out.hashes():
            return None
        tag = self.password_tag(user)
        if tag is None:
            return None
        try:
            return user if hmac.compare_digest(self.mac(f"{who}.{expires}", tag), mac) else None
        except TypeError:
            return None

    def end_session(self, cookie_header):
        """Signs out the session a request's cookie carries, on every server that takes it, until it
        would have run out anyway. The user it was, or None when it was no session of ours."""
        user = self.check_session(cookie_header)
        if not user:
            return None
        try:
            self.signed_out.add(self.session_token(cookie_header))
        except OSError as e:
            log(f"sign-out not kept: {e!r}")
        return user


def load_session_key(path):
    """Shared with the sync servers and the hub: whichever starts first makes it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    for _ in range(50):
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
        time.sleep(0.1)
    raise SystemExit(f"{path} is too short to sign with")


def session_cookie(value, max_age):
    # Path=/: one sign-in covers the hub and the other pages on this host.
    return f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"


def client_address(handler):
    """Behind the local proxy, the last X-Forwarded-For hop, the one the proxy added."""
    peer = handler.client_address[0]
    forwarded = handler.headers.get("X-Forwarded-For")
    return forwarded.split(",")[-1].strip() if forwarded and peer in ("127.0.0.1", "::1") else peer


def from_another_site(headers):
    """Whether a write came from another site's page, which a browser holding the password would
    send it with."""
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site:
        return site in ("cross-site", "same-site")
    origin = (headers.get("Origin") or "").strip().lower()
    scheme, _, host = origin.partition("://")
    if scheme not in ("http", "https"):
        return False
    ours = [headers.get("Host") or "", *(headers.get("X-Forwarded-Host") or "").split(",")]
    key = lambda h: h.strip().lower().removesuffix(":443").removesuffix(":80")  # noqa: E731
    return key(host) not in {key(h) for h in ours if h.strip()}


# ---- the page's API ---------------------------------------------------------------------------------

class BadInput(ValueError):
    pass


def clean_text(doc, name, most, required):
    value = doc.get(name, "")
    if not isinstance(value, str):
        raise BadInput(f"{name} must be text")
    value = re.sub(r"\s+", " ", value).strip() if name != "note" else value.strip()
    if any(unicodedata.category(c) == "Cc" and c != "\n" for c in value):
        raise BadInput(f"{name} has control characters")
    # JSON takes "\ud800" on its own; SQLite can't store it, and failed outside this check.
    if any(unicodedata.category(c) == "Cs" for c in value):
        raise BadInput(f"{name} isn't valid text")
    if required and not value:
        raise BadInput(f"{name} is needed")
    if len(value) > most:
        raise BadInput(f"{name} is longer than {most} characters")
    return value


def clean_song(doc, partial_day=False):
    if not isinstance(doc, dict):
        raise BadInput("body must be an object")
    out = {"title": clean_text(doc, "title", MAX_TITLE, True), "artists": clean_text(doc, "artists", MAX_ARTISTS, True)}
    genre = parse_genre(doc.get("genre")) if isinstance(doc.get("genre"), str) else ""
    if not genre:
        raise BadInput("genre must be one of " + ", ".join(GENRES))
    out["genre"] = genre
    day = doc.get("day")
    if day is None and partial_day:
        out["day"] = None
    elif isinstance(day, bool) or not isinstance(day, int) or not -100000 < day < 1000000:
        raise BadInput("day must be a whole number")
    else:
        out["day"] = day
    return out


def clean_hold(doc):
    if not isinstance(doc, dict):
        raise BadInput("body must be an object")
    return {"title": clean_text(doc, "title", MAX_TITLE, True), "artists": clean_text(doc, "artists", MAX_ARTISTS, False),
            "note": clean_text(doc, "note", MAX_NOTE, False)}


class Handler(BaseHTTPRequestHandler):
    server_version = "xrninsta/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 60
    app = None  # set by serve(): {"store", "login", "poller", "web"}
    served = 0  # requests answered on this connection

    def handle_one_request(self):
        """After the first request, waiting for the next is waiting on a kept-alive connection: a page
        or the laptop app holds an idle one open, and it's closed after `timeout`. One that times out
        with no new request begun has simply ended, so it closes without a word (http.server logged
        "Request timed out" for each, 131 of them on 18 Nov, as xrnhub's own log used to). A timeout
        on the first request, or partway through one, is still logged."""
        if self.served:
            try:
                begun = self.rfile.peek(1)
            except TimeoutError:
                begun = b""
            if not begun:
                self.close_connection = True
                return
        super().handle_one_request()
        self.served += 1

    def parse_request(self):
        self.body_read = False
        if not super().parse_request():
            return False
        if self.command not in ("GET", "HEAD", "OPTIONS") and from_another_site(self.headers):
            self.send(403, "refused: sent from another site's page")
            return False
        return True

    def log_message(self, fmt, *a):
        who = self.headers.get("X-Forwarded-For", self.client_address[0]) if getattr(self, "headers", None) else "-"
        line = f"{who} {fmt % a}"
        sys.stderr.write("".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in line) + "\n")

    def handle(self):
        # A caller hanging up, mid-upload or between requests, is the network, not a fault here:
        # a page closed, a phone losing its signal. One line says so; socketserver printed some 15
        # lines of traceback for each (118 on 15 and 16 Nov), which xrnhub's log page counts as
        # an error.
        try:
            super().handle()
        except ConnectionError as e:
            self.close_connection = True
            self.log_message("connection dropped: %s", type(e).__name__)

    def route(self):
        path = self.path.partition("?")[0]
        if path == PREFIX or path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):] or "/"
        return path

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Cache-Control", cache)
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        if code != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.headers.get("Content-Length") not in (None, "0") and not self.body_read:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and code != 304:
            self.wfile.write(body)

    def send_json(self, doc, code=200, headers=None):
        self.send(code, json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "application/json; charset=utf-8", headers)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send(400, "bad Content-Length")
            return None
        if length <= 0 or length > MAX_BODY:
            self.send(413, "body missing or too large")
            return None
        raw = self.rfile.read(length)
        if len(raw) < length:
            # The connection closed part way: what came is no record, so it isn't read as one.
            self.close_connection = True
            self.send(400, "the upload stopped short")
            return None
        self.body_read = True
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: nested deeper than the parser goes
            self.send(400, "body must be JSON")
            return None

    def owner(self):
        """The signed-in owner, or None once a refusal has been answered. The songs are one person's,
        the first in the users file; anyone else signed in gets 403."""
        login = self.app["login"]
        header = self.headers.get("Authorization")
        user = None
        if not header and self.headers.get("X-Xrn-Client"):
            user = login.check_session(self.headers.get("Cookie"))
        if not user:
            addr = client_address(self)
            if not (login.reserve_guess(addr) if header else not login.locked_out(addr)):
                if header:
                    time.sleep(FAIL_DELAY_S)
                self.send(429, "too many wrong passwords; try again later", headers={"Retry-After": str(FAIL_WINDOW_S)})
                return None
            right = None
            try:
                # A password already found right is a session, not a login, and settles nothing: a
                # browser or script sending it with every request cleared the count for its whole
                # address, so a guesser behind the same one never ran out of guesses.
                user = login.cached_basic(header)
                if not user:
                    user = login.check_basic(header)
                    right = user is not None
            finally:
                if header:
                    login.settle_guess(addr, (login.basic_login(header) or ("",))[0], right)
            if not user:
                if header:
                    time.sleep(FAIL_DELAY_S)
                challenge = {} if self.headers.get("X-Xrn-Client") else {"WWW-Authenticate": 'Basic realm="xrninsta"'}
                self.send(401, "login required", headers=challenge)
                return None
        if user != next(iter(login.names()), None):
            self.send(403, "these songs are the server owner's")
            return None
        return user

    def expected(self):
        """The record version an If-Match names, or None once a 428 has been answered."""
        want = (self.headers.get("If-Match") or "").strip().removeprefix("W/").strip('"')
        if not want.isdigit():
            self.send(428, 'If-Match: "<updated>" required')
            return None
        return int(want)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.route()
        if path == "/health":
            return self.send(200, "ok")
        if path in ("/api/data", "/api/status"):
            if not self.owner():
                return None
            store = self.app["store"]
            if path == "/api/status":
                status = dict(self.app["poller"].status) if self.app["poller"] else {"state": "off", "error": "no bot token"}
                if self.app.get("covers"):
                    covers, poller = self.app["covers"]
                    status["covers"] = {"bot": poller.status, "send": covers.status}
                return self.send_json(status)
            return self.send_json({"genres": GENRES, "colors": GENRE_COLORS, "nextDay": store.next_day(),
                                   "songs": store.songs(), "holds": store.holds()})
        if path.startswith("/api/"):
            return self.send(404, "not found")
        return self.serve_web(path)

    def do_POST(self):
        path = self.route()
        if path == "/session":
            user = self.owner()
            if not user:
                return None
            cookie = session_cookie(self.app["login"].make_session(user), SESSION_S)
            return self.send(200, '{"ok":true}', "application/json", {"Set-Cookie": cookie})
        if path not in ("/api/songs", "/api/holds"):
            return self.send(404, "not found")
        if not self.owner():
            return None
        doc = self.body()
        if doc is None:
            return None
        store = self.app["store"]
        try:
            if path == "/api/holds":
                h = clean_hold(doc)
                return self.send_json(store.add_hold(h["title"], h["artists"], h["note"]), 201)
            s = clean_song(doc, partial_day=True)
            from_hold = doc.get("fromHold")
            if from_hold is not None and (isinstance(from_hold, bool) or not isinstance(from_hold, int) or not store.hold(from_hold)):
                raise BadInput("fromHold names no OnHold entry")
            artists = canonical_artists(s["artists"], store.songs())
            return self.send_json(store.add_song(s["title"], artists, s["genre"], s["day"], from_hold), 201)
        except BadInput as e:
            return self.send(400, str(e))

    def do_PUT(self):
        table, rid = self.record_path()
        if table is None:
            return self.send(404, "not found")
        if not self.owner():
            return None
        expect = self.expected()
        if expect is None:
            return None
        doc = self.body()
        if doc is None:
            return None
        try:
            fields = clean_song(doc) if table == "songs" else clean_hold(doc)
        except BadInput as e:
            return self.send(400, str(e))
        return self.changed(self.app["store"].update(table, rid, fields, expect), table, rid)

    def do_DELETE(self):
        if self.route() == "/session":
            # Emptying the browser's cookie alone left any copy of it signing in for its full 30 days.
            self.app["login"].end_session(self.headers.get("Cookie"))
            return self.send(200, '{"ok":true}', "application/json", {"Set-Cookie": session_cookie("", 0)})
        table, rid = self.record_path()
        if table is None:
            return self.send(404, "not found")
        if not self.owner():
            return None
        expect = self.expected()
        if expect is None:
            return None
        return self.changed(self.app["store"].delete(table, rid, expect), table, rid)

    def changed(self, result, table, rid):
        store = self.app["store"]
        if result is None:
            return self.send(404, "no such record; it may have been deleted")
        if result is False:
            current = store.song(rid) if table == "songs" else store.hold(rid)
            return self.send_json({"error": "changed since you read it", "current": current}, 412)
        return self.send_json(result if isinstance(result, dict) else {"ok": True})

    def record_path(self):
        m = re.fullmatch(r"/api/(songs|holds)/([0-9]{1,12})", self.route())
        return (m.group(1), int(m.group(2))) if m else (None, None)

    def serve_web(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        ctype = WEB_TYPES.get(os.path.splitext(name)[1])
        full = os.path.join(self.app["web"], name)
        if not WEB_NAME.fullmatch(name) or ctype is None or not os.path.isfile(full):
            return self.send(404, "not found")
        with open(full, "rb") as f:
            data = f.read()
        tag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == tag:
            return self.send(304, headers={"ETag": tag}, cache="no-cache")
        return self.send(200, data, ctype, {"ETag": tag}, cache="no-cache")


# ---- the command line ------------------------------------------------------------------------------

def read_config(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            doc = json.load(f)
    except OSError:
        return {}
    except ValueError:
        raise SystemExit(f"{path} isn't JSON") from None
    return doc if isinstance(doc, dict) else {}


def need_token(config_path, covers=False):
    conf = read_config(config_path)
    if covers:
        conf = conf.get("covers") if isinstance(conf.get("covers"), dict) else {}
    token = str(conf.get("token") or "").strip()
    if not token and covers:
        raise SystemExit(f'no "covers" bot in {config_path}; it holds {{..., "covers": {{"token": "...", "chat": "..."}}}}')
    if not token:
        raise SystemExit(f'no bot token in {config_path}; it holds {{"token": "...", "chat": "..."}}')
    return token, str(conf.get("chat") or "").strip()


def checked_bot(token, chat):
    """The token and chat a setup command was given, stripped, or SystemExit saying what's wrong
    with them. The chat is the number Telegram gives it: a "<your chat id>" pasted where it goes
    made the song bot ignore every message for three days."""
    token, chat = token.strip(), chat.strip()
    if not token:
        raise SystemExit("No token; nothing was written.")
    if not CHAT_ID.fullmatch(chat):
        raise SystemExit("The chat id is a number, such as 123456789 (a group's starts with -); nothing was written.")
    return token, chat


def set_song_bot(config_path, token, chat):
    """Puts the song bot's token and chat in bot.json, keeping the Daily Story Bot's."""
    token, chat = checked_bot(token, chat)
    path = os.path.expanduser(config_path)
    conf = read_config(path)
    covers = conf.get("covers") if isinstance(conf.get("covers"), dict) else {}
    if token == str(covers.get("token") or "").strip():
        raise SystemExit("That's the Daily Story Bot's token; the song bot has one of its own. Nothing was written.")
    conf["token"], conf["chat"] = token, chat
    write_config(path, conf)


def set_covers_bot(config_path, token, chat):
    """Puts the Daily Story Bot's token and chat under "covers" in bot.json, keeping the rest."""
    token, chat = checked_bot(token, chat)
    path = os.path.expanduser(config_path)
    conf = read_config(path)
    if token == str(conf.get("token") or "").strip():
        raise SystemExit("That's the song bot's token; the Daily Story Bot has one of its own. Nothing was written.")
    conf["covers"] = {"token": token, "chat": chat}
    write_config(path, conf)


def write_config(path, conf):
    """bot.json, whole, readable only by its owner: written beside it and moved in place."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def start_covers(store, conf, data, song_token):
    """The Daily Story Bot, when bot.json has a "covers" bot: its 2am sends and its poller, each in
    a thread. None when there's none."""
    section = conf.get("covers") if isinstance(conf.get("covers"), dict) else {}
    token = str(section.get("token") or "").strip()
    if not token:
        return None
    if token == song_token:
        # One token polled twice is two copies of one bot, each told by Telegram the other is running.
        log('covers: the "covers" token is the song bot\'s; the Daily Story Bot has a token of its own')
        return None
    covers = Covers(store, Telegram(token), section.get("chat"), os.path.join(data, "covers"))
    covers.first_start()
    poller = covers_poller(store, covers)
    threading.Thread(target=covers.run, daemon=True).start()
    threading.Thread(target=poller.run, daemon=True).start()
    return covers, poller


def serve(args):
    store = Store(os.path.expanduser(args.data))
    conf = read_config(args.config)
    token = str(conf.get("token") or "").strip()
    poller = None
    if token:
        poller = Poller(store, Bot(store, Telegram(token), conf.get("chat")))
        threading.Thread(target=poller.run, daemon=True).start()
    else:
        log(f"no bot token in {args.config}: the page runs, the bot doesn't")
    Handler.app = {"store": store, "login": Login(os.path.expanduser(args.users)), "poller": poller,
                   "covers": start_covers(store, conf, os.path.expanduser(args.data), token),
                   "web": os.path.expanduser(args.web)}
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log(f"xrninsta on http://{args.host}:{args.port}, data in {store.path}")
    srv.serve_forever()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    here = os.path.dirname(os.path.abspath(__file__))
    if argv[:1] == ["import"]:
        p = argparse.ArgumentParser(prog="xrninsta.py import")
        p.add_argument("file")
        p.add_argument("--data", required=True)
        p.add_argument("--replace", action="store_true", help="drop what's there first")
        a = p.parse_args(argv[1:])
        r = import_xlsx(Store(os.path.expanduser(a.data)), a.file, a.replace)
        print(f"Imported {r['songs']} songs and {r['holds']} OnHold entries.")
        if r["oddGenre"] or r["noDay"]:
            print(f"Of the songs, {r['oddGenre']} have a genre the bot doesn't know and {r['noDay']} have no day; "
                  "they're kept, and the page shows them.")
        return
    if argv[:1] in (["check"], ["webhook-off"]):
        p = argparse.ArgumentParser(prog=f"xrninsta.py {argv[0]}")
        p.add_argument("--config", required=True)
        p.add_argument("--covers", action="store_true", help='the Daily Story Bot ("covers" in bot.json), not the song bot')
        p.add_argument("--data", help="with check --covers: where the covers folder is, to say how far they go")
        a = p.parse_args(argv[1:])
        token, chat = need_token(a.config, a.covers)
        tg = Telegram(token)
        if argv[0] == "check":
            me = tg.call("getMe") or {}
            hook = (tg.call("getWebhookInfo") or {}).get("result") or {}
            if not me.get("ok"):
                raise SystemExit(f"Telegram refused the token: {me.get('description', 'no answer')}")
            url = (hook.get("url") or "").partition("?")[0]  # the rest is the script's key
            fault = Chat(chat, 'under "covers" in bot.json' if a.covers else "in bot.json", "").fault()
            who = f"no one: {fault}; `xrninsta.py {'covers-bot' if a.covers else 'song-bot'}` sets it" if fault else chat
            print(f"Bot: @{me['result'].get('username')} · answers {who}")
            if url:
                print(f"Messages go to: {url} (the Apps Script) · {hook.get('pending_update_count', 0)} waiting")
            else:
                print("Messages go to: whoever asks, which is this server")
            if a.covers:
                zone = load_zone(COVER_ZONE)
                if zone is None:
                    print(f"Time zone: Python has no data for {COVER_ZONE}, so no cover goes out (pip install tzdata)")
                elif a.data:
                    today = cover_day(datetime.datetime.now(zone).date())
                    print(covers_summary(os.path.join(os.path.expanduser(a.data), "covers"), today))
            return
        r = tg.call("deleteWebhook", {"drop_pending_updates": False}) or {}
        if not r.get("ok"):
            raise SystemExit(f"Telegram didn't take it: {r.get('description', 'no answer')}")
        tg.call("setMyCommands", {"commands": COVER_COMMANDS if a.covers else COMMANDS})
        print("Done: Telegram stops posting to the Apps Script, and the bot here picks messages up within a minute.")
        return
    if argv[:1] == ["song-bot"]:
        p = argparse.ArgumentParser(prog="xrninsta.py song-bot",
                                    description="asks for the song bot's token and chat, and puts them in bot.json")
        p.add_argument("--config", required=True)
        a = p.parse_args(argv[1:])
        os.makedirs(os.path.dirname(os.path.abspath(os.path.expanduser(a.config))), mode=0o700, exist_ok=True)
        set_song_bot(a.config, getpass.getpass("Song bot token: "), input("Chat id: "))
        print(f"Written in {a.config}. Restart xrninsta to start the bot.")
        return
    if argv[:1] == ["covers-bot"]:
        p = argparse.ArgumentParser(prog="xrninsta.py covers-bot",
                                    description="asks for the Daily Story Bot's token and chat, and puts them in bot.json")
        p.add_argument("--config", required=True)
        a = p.parse_args(argv[1:])
        set_covers_bot(a.config, getpass.getpass("Daily Story Bot token: "), input("Chat id: "))
        print(f'Written under "covers" in {a.config}. Restart xrninsta to start the bot.')
        return
    if argv[:1] == ["covers"]:
        p = argparse.ArgumentParser(prog="xrninsta.py covers", description="add covers: Day_<n>.png files, from a zip or a folder")
        p.add_argument("source", help="a zip (as Drive downloads a folder) or a folder")
        p.add_argument("--data", required=True)
        a = p.parse_args(argv[1:])
        folder = os.path.join(os.path.expanduser(a.data), "covers")
        r = add_covers(folder, a.source)
        print(f"Added {r['added']} covers, replaced {r['replaced']}." +
              (f" Skipped {len(r['skipped'])}: {', '.join(r['skipped'][:5])}{' …' if len(r['skipped']) > 5 else ''}" if r["skipped"] else ""))
        zone = load_zone(COVER_ZONE)
        if zone is not None:
            print(covers_summary(folder, cover_day(datetime.datetime.now(zone).date())))
        return
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5239)
    p.add_argument("--data", required=True, help="where xrninsta.sqlite lives")
    p.add_argument("--users", required=True, help="htpasswd file with bcrypt hashes, shared with the sync servers")
    p.add_argument("--config", required=True, help='bot.json: {"token": ..., "chat": ...}')
    p.add_argument("--web", default=os.path.join(here, "web"))
    serve(p.parse_args(argv))


if __name__ == "__main__":
    main()
