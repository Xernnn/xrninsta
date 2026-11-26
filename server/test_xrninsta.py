"""xrninsta: the bot against a fake Telegram, the sheet import, and the page's API over HTTP.

Every song and name here is made up. Run: python3 -m unittest test_xrninsta (from server/).
"""
import base64
import concurrent.futures
import datetime
import email.parser
import email.policy
import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import xrninsta as x

CHAT, USER = "100", 7


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.answers = {}  # method -> what Telegram says to it (None: it couldn't be reached)

    def call(self, method, payload=None, timeout=15):
        self.calls.append((method, payload or {}))
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else {"ok": True, "result": []}
        return self.answers.get(method, {"ok": True, "result": {}})

    def upload(self, method, fields, name, filename, content, timeout=60):
        self.calls.append((method, {**fields, name: (filename, content)}))
        return self.answers.get(method, {"ok": True, "result": {}})

    def sent(self, method=None):
        return [p for m, p in self.calls if method is None or m == method]

    def last_text(self):
        for m, p in reversed(self.calls):
            if "text" in p and m != "answerCallbackQuery":
                return p["text"]
        return None

    def last_buttons(self):
        for m, p in reversed(self.calls):
            if "reply_markup" in p:
                return [b["callback_data"] for row in p["reply_markup"]["inline_keyboard"] for b in row]
        return []

    def toasts(self):
        return [p["text"] for m, p in self.calls if m == "answerCallbackQuery"]


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="xrninsta-")
        self.addCleanup(shutil.rmtree, self.folder)
        self.store = x.Store(os.path.join(self.folder, "data"))
        self.tg = FakeTelegram()
        self.bot = x.Bot(self.store, self.tg, CHAT)
        self.next_id = 0

    def say(self, text, chat=CHAT):
        self.next_id += 1
        self.bot.handle({"update_id": self.next_id, "message": {"message_id": self.next_id, "chat": {"id": int(chat)},
                                                                "from": {"id": USER}, "text": text}})

    def tap(self, data, chat=CHAT, msg=500):
        self.next_id += 1
        self.bot.handle({"update_id": self.next_id, "callback_query": {
            "id": f"cb{self.next_id}", "data": data, "from": {"id": USER},
            "message": {"message_id": msg, "chat": {"id": int(chat)}}}})

    def song(self, title, artists, genre="VPOP", day=None):
        return self.store.add_song(title, artists, genre, day)

    def titles(self):
        return [s["title"] for s in self.store.songs()]


class Adding(Base):
    def test_three_lines_add_a_song_on_the_next_day(self):
        self.song("Old Song", "Someone", day=4)
        self.say("Paper Lanterns\nThe Made Up Band\nj-pop")
        added = self.store.songs()[-1]
        self.assertEqual((added["title"], added["artists"], added["genre"], added["day"]),
                         ("Paper Lanterns", "The Made Up Band", "JPOP", 5))
        self.assertIn("✅ Đã thêm bài mới", self.tg.last_text())
        self.assertIn("📅 Day 5 | JPOP", self.tg.last_text())
        self.assertEqual(self.tg.last_buttons(), ["undo_" + x.ref_of(added)])

    def test_two_lines_ask_for_the_genre_then_a_tap_adds_it(self):
        self.say("Night Bus\nAlex Example")
        self.assertIn("Chọn Genre:", self.tg.last_text())
        self.assertIn("add_INST", self.tg.last_buttons())
        self.tap("add_INST")
        self.assertEqual(self.titles(), ["Night Bus"])
        self.assertEqual(self.store.songs()[0]["genre"], "INST")
        self.assertIsNone(self.store.get_state(str(USER)))
        self.tap("add_INST")  # the same button again: the moment has passed
        self.assertEqual(self.tg.toasts()[-1], x.EXPIRED)
        self.assertEqual(len(self.store.songs()), 1)

    def test_one_line_asks_for_the_artist_first(self):
        self.say("Lonely Title")
        self.assertIn("Nhập Artist(s) cho", self.tg.last_text())
        self.say("Some Singer")
        self.assertIn("Chọn Genre:", self.tg.last_text())
        self.tap("add_USUK")
        self.assertEqual(self.store.songs()[0]["artists"], "Some Singer")

    def test_a_question_left_unanswered_for_an_hour_lapses(self):
        # Days after "Artist(s)?" went unanswered, a whole new song became that old title's artist.
        self.say("Lonely Title")
        later = x.now_ms() + (x.WAIT_KEEP_S + 5) * 1000
        with mock.patch.object(x, "now_ms", return_value=later):
            self.say("Fresh Song\nSomeone\nvpop")
        self.assertEqual(self.titles(), ["Fresh Song"])
        self.store.clear_state(str(USER))
        self.say("/hold")
        with mock.patch.object(x, "now_ms", return_value=x.now_ms() + (x.WAIT_KEEP_S + 5) * 1000):
            self.say("Another\nSomeone\nkpop")
        self.assertEqual((self.titles()[-1], self.store.holds()), ("Another", []))

    def test_a_likely_duplicate_asks_first_and_can_be_added_anyway(self):
        self.song("Đường Về Nhà", "Ca Sĩ Mẫu")
        self.say("Duong ve nha\nca si mau\nvpop")
        self.assertIn("⚠️ Bài này có vẻ đã tồn tại", self.tg.last_text())
        self.assertEqual(len(self.store.songs()), 1)
        self.tap("force_add")
        self.assertEqual(len(self.store.songs()), 2)

    def test_a_different_number_is_a_different_song(self):
        self.song("Journey Part 1", "Alex Example")
        self.say("Journey Part 2\nAlex Example\nmisc")
        self.assertEqual(len(self.store.songs()), 2)

    def test_an_artist_takes_the_spelling_already_used(self):
        self.song("First", "VSTRA, Ca Sĩ Mẫu")
        self.song("Second", "VSTRA")
        self.say("Third\nvstra\nkpop")
        self.assertEqual(self.store.songs()[-1]["artists"], "VSTRA")
        self.assertIn("bạn nhập: vstra", self.tg.last_text())

    def test_undo_removes_it_and_a_stale_button_finds_nothing(self):
        self.say("Gone Soon\nSomeone\nmisc")
        undo = self.tg.last_buttons()[0]
        self.tap(undo)
        self.assertEqual(self.titles(), [])
        self.assertIn("🗑️ Đã hoàn tác", self.tg.last_text())
        self.tap(undo)
        self.assertIn("Không tìm thấy", self.tg.toasts()[-1])

    def test_a_button_for_a_song_since_renamed_does_nothing(self):
        self.say("Before\nSomeone\nmisc")
        undo = self.tg.last_buttons()[0]
        s = self.store.songs()[0]
        self.store.update("songs", s["id"], {"title": "After"})
        self.tap(undo)
        self.assertEqual(self.titles(), ["After"])

    def test_a_mistyped_command_isnt_added_as_a_song(self):
        self.say("/serch something")
        self.assertEqual(self.titles(), [])
        self.assertIn("Không có lệnh này", self.tg.last_text())

    def test_a_command_that_only_starts_like_a_real_one_is_unknown(self):
        # "/adds Em" passed startswith("/add"), missed the ^/add\b strip, and became a title.
        for text in ("/adds Em", "/holdx Rain", "/searching day 2", "/statsx", "/cancelled"):
            self.say(text)
            self.assertIn("Không có lệnh này", self.tg.last_text(), text)
        self.assertEqual((self.titles(), self.store.holds(), self.store.get_state(str(USER))), ([], [], None))
        self.say("/ADD Night Bus\nAlex Example\nvpop")  # a real one, any case
        self.say("/Hold\nLater")
        self.assertEqual((self.titles(), [h["title"] for h in self.store.holds()]), (["Night Bus"], ["Later"]))

    def test_html_in_a_title_is_escaped(self):
        self.say("<b>&Co</b>\nA & B\nmisc")
        self.assertIn("&lt;b&gt;&amp;Co&lt;/b&gt;", self.tg.last_text())

    def test_a_line_longer_than_the_page_takes_is_refused(self):
        # A pasted paragraph became a title the page's Fix couldn't save, and its reply passed
        # Telegram's limit, so nothing was said at all.
        self.say("x" * 201 + "\nSomeone\nvpop")
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.tg.last_text(), f"⚠️ Tên bài dài hơn {x.MAX_TITLE} ký tự.")
        self.say("Fine\n" + "a" * 301)
        self.assertIn(f"Artist dài hơn {x.MAX_ARTISTS}", self.tg.last_text())
        self.assertIsNone(self.store.get_state(str(USER)))
        self.say("Lonely Title")
        self.say("a" * 301)  # the answer to "Artist(s)?"
        self.assertIn(f"Artist dài hơn {x.MAX_ARTISTS}", self.tg.last_text())
        self.assertEqual(self.store.get_state(str(USER))["step"], "await_artist")
        self.say("/cancel")
        self.say("x" * 200 + "\nSomeone\nvpop")
        self.assertEqual(self.titles(), ["x" * 200])

    def test_a_hold_longer_than_the_page_takes_is_refused(self):
        self.say("/hold Later\nSomeone\n" + "n" * 1001)
        self.assertEqual(self.store.holds(), [])
        self.assertEqual(self.tg.last_text(), f"⚠️ Ghi chú dài hơn {x.MAX_NOTE} ký tự.")


class TelegramAnswers(unittest.TestCase):
    def refusal(self, method):
        body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: message is too long"}).encode()
        err = x.urllib.error.HTTPError("https://api.telegram.org/", 400, "Bad Request", {}, None)
        err.read = lambda: body
        with mock.patch.object(x.urllib.request, "urlopen", side_effect=err), mock.patch.object(x, "log") as logged:
            answer = x.Telegram("token").call(method, {"chat_id": 1, "text": "hi"})
        return answer, [c.args[0] for c in logged.call_args_list]

    def test_a_refusal_is_logged_with_the_method_and_why(self):
        answer, lines = self.refusal("sendMessage")
        self.assertFalse(answer["ok"])
        self.assertEqual(lines, ["telegram sendMessage refused: 400: Bad Request: message is too long"])

    def test_a_refused_poll_is_left_to_the_poller(self):
        # The poller notes it once per change; logging every 30 s retry would bury everything else.
        self.assertEqual(self.refusal("getUpdates")[1], [])


class Holding(Base):
    def test_hold_then_add_it_and_clear_only_the_closest_hold(self):
        self.say("/hold\n")
        self.say("Rainy Window\nAlex Example\nfind the cover")
        self.store.add_hold("Rainy Windows", "", "")
        holds = self.store.holds()
        self.assertEqual(len(holds), 2)
        self.say("Rainy Window\nAlex Example\nusuk")
        self.assertIn("đang nằm trong OnHold", self.tg.last_text())
        unhold = self.tg.last_buttons()[1]
        self.assertTrue(unhold.startswith("unhold_"))
        self.tap(unhold)
        self.assertEqual([h["title"] for h in self.store.holds()], ["Rainy Windows"])
        self.assertIn("Đã dọn dẹp khỏi OnHold", self.tg.last_text())

    def test_holding_a_song_already_added_asks_first(self):
        self.song("Kept", "Someone", day=9)
        self.say("/hold Kept")
        self.assertIn("đã có trong Songs (Day 9 | VPOP)", self.tg.last_text())
        self.assertEqual(self.store.holds(), [])
        self.tap("force_hold")
        self.assertEqual(len(self.store.holds()), 1)

    def test_the_onhold_list_pages_deletes_with_a_confirmation_and_adds(self):
        for i in range(10):
            self.store.add_hold(f"Held {i}", "Someone" if i else "", "")
        self.tap("stats_onhold")
        self.assertIn("OnHold List (10 bài) — Trang 1/2", self.tg.last_text())
        self.assertIn("holdp_1", self.tg.last_buttons())
        first = x.ref_of(self.store.holds()[0])
        self.tap(f"hdel_0_{first}")
        self.assertIn("Bấm ✅ để xác nhận", self.tg.last_text())
        self.tap(f"hdelok_0_{first}")
        self.assertEqual(len(self.store.holds()), 9)
        self.assertIn("Đã xoá khỏi OnHold: Held 0", self.tg.toasts()[-1])
        self.tap(f"hdelok_0_{first}")  # gone already
        self.assertIn("đã thay đổi", self.tg.toasts()[-1])
        self.tap(f"hadd_0_{x.ref_of(self.store.holds()[0])}")
        self.assertIn("Chọn Genre:", self.tg.last_text())


class Searching(Base):
    def setUp(self):
        super().setUp()
        self.song("Em Của Ngày Hôm Qua", "Ca Sĩ Mẫu", "VPOP", 1)
        self.song("Remember Me", "Alex Example", "USUK", 2)
        self.song("Blue Hour", "Ca Sĩ Mẫu, Alex Example", "INST", 3)

    def test_a_word_matches_whole_words_only(self):
        self.say("/search em")
        text = self.tg.last_text()
        self.assertIn("Em Của Ngày Hôm Qua", text)
        self.assertNotIn("Remember Me", text)

    def test_by_artist_by_day_and_by_genre(self):
        self.say("/search ca si mau")
        self.assertIn("Blue Hour", self.tg.last_text())
        self.say("/search day 2")
        self.assertIn("Remember Me", self.tg.last_text())
        self.assertNotIn("%", self.tg.last_text())
        self.say("/search instrumental")
        self.assertIn("Blue Hour", self.tg.last_text())

    def test_the_bots_name_after_a_command_is_ignored(self):
        self.say("/search@example_song_bot remember")
        self.assertIn("Remember Me", self.tg.last_text())

    def test_pages_turn_while_the_search_is_kept(self):
        for i in range(8):
            self.song(f"Mau Song {i}", "Ca Sĩ Mẫu", "VPOP", 10 + i)
        self.say("/search vpop")
        self.assertIn("(Trang 1/2)", self.tg.last_text())
        turn = self.tg.last_buttons()[0]
        self.tap(turn)
        self.assertIn("(Trang 2/2)", self.tg.last_text())
        with mock.patch.object(x, "now_ms", return_value=x.now_ms() + (x.SEARCH_KEEP_S + 5) * 1000):
            self.tap(turn)
        self.assertIn("đã hết hạn", self.tg.last_text())


class Stats(Base):
    def test_the_header_and_leaderboards(self):
        for i in range(7):
            self.song(f"V{i}", "Ca Sĩ Mẫu", "VPOP", i + 1)
        self.song("Last", "Alex Example, Ca Sĩ Mẫu", "JPOP", 8)
        self.store.add_hold("Waiting", "", "")
        self.say("/stats")
        self.assertIn("🎵 8 bài · 🔖 1 OnHold", self.tg.last_text())
        self.assertIn("Day 8: <b>Last</b>", self.tg.last_text())
        self.tap("stats_genre")
        # 1 of 8 is 12.5%: JavaScript's Math.round makes it 13, Python's round() would say 12.
        self.assertIn("2. JPOP       1 bài  13% ███", self.tg.last_text())
        self.tap("stats_artist")
        self.assertIn("1. Ca Sĩ Mẫu\n    ████████ 8 bài", self.tg.last_text())
        self.tap("statg_JPOP")
        self.assertIn("Artist Leaderboard — JPOP", self.tg.last_text())


class Guarding(Base):
    def test_another_chat_is_ignored(self):
        self.say("Sneaky\nSomeone\nvpop", chat="999")
        self.tap("stats_genre", chat="999")
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.tg.calls, [])

    def test_no_chat_in_bot_json_answers_no_one(self):
        # An empty "chat" (Enter at the setup prompt, or the key lost) once let anyone add, list and delete.
        self.bot = x.Bot(self.store, self.tg, "")
        with mock.patch.object(x, "log") as logged:
            self.say("Sneaky\nSomeone\nvpop", chat="999")
            self.tap("stats_genre", chat="999")
        self.assertEqual(self.titles(), [])
        self.assertEqual(self.tg.calls, [])
        lines = [c.args[0] for c in logged.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertIn('no "chat"', lines[0])
        self.assertIn("999", lines[0])
        self.assertNotIn("Sneaky", " ".join(lines))  # what was sent stays out of the log

    def test_the_status_says_when_no_chat_is_set(self):
        poller = x.Poller(self.store, x.Bot(self.store, self.tg, " "))
        poller.once()
        self.assertEqual(poller.status["state"], "nochat")

    def test_messages_from_another_chat_are_logged_and_the_status_isnt_ok_till_its_own_chat_speaks(self):
        # A chat id that is a number, but another chat's, dropped every message as quietly as
        # "<your chat id>" did: nothing in the log, and the status said ok.
        poller = x.Poller(self.store, self.bot)
        message = lambda uid, chat, text: {"update_id": uid, "message": {  # noqa: E731
            "message_id": uid, "chat": {"id": chat}, "from": {"id": USER}, "text": text}}
        self.tg.updates = [{"ok": True, "result": [message(5, 4242, "Song\nSomeone\nvpop")]},
                           {"ok": True, "result": []}]
        with mock.patch.object(x, "log") as logged:
            poller.once()
            poller.once()  # an empty poll since doesn't make it ok again
        self.assertEqual(self.titles(), [])
        self.assertEqual(poller.status["state"], "otherchat")
        self.assertIn("chat 4242", poller.status["error"])
        self.assertIn(f"in bot.json is {CHAT}", poller.status["error"])
        lines = [c.args[0] for c in logged.call_args_list]
        self.assertIn(f'bot: update 5 from chat 4242 ignored: the "chat" in bot.json is {CHAT}', lines)
        self.assertTrue(any(line.startswith("bot: otherchat: ") for line in lines), lines)
        self.assertNotIn("Song", " ".join(lines))  # what was sent stays out of the log
        self.tg.updates = [{"ok": True, "result": [message(6, int(CHAT), "/start")]}]
        with mock.patch.object(x, "log"):
            poller.once()
        self.assertEqual(poller.status["state"], "ok")

    def test_the_cover_bot_says_so_too(self):
        covers = x.Covers(self.store, self.tg, CHAT, os.path.join(self.folder, "covers"), x.load_zone(x.COVER_ZONE))
        poller = x.covers_poller(self.store, covers)
        self.tg.updates = [{"ok": True, "result": [{"update_id": 3, "message": {
            "message_id": 3, "chat": {"id": 4242}, "from": {"id": USER}, "text": "/sendnow"}}]}]
        with mock.patch.object(x, "log") as logged:
            poller.once()
        self.assertEqual(poller.status["state"], "otherchat")
        self.assertIn('under "covers" in bot.json', poller.status["error"])
        self.assertIn("covers bot: update 3 from chat 4242 ignored", "\n".join(c.args[0] for c in logged.call_args_list))
        self.assertEqual(self.tg.sent("sendPhoto"), [])

    def test_a_chat_that_isnt_a_chat_id_is_said_at_start_and_stays_in_the_status(self):
        # 18-21 Nov 2026: bot.json's "chat" held a pasted "<your chat id>". Any chat that wasn't empty
        # counted as set, so the bot ignored every message while its log and its status said ok.
        with mock.patch.object(x, "log") as logged:
            poller = x.Poller(self.store, x.Bot(self.store, self.tg, "<your chat id>"))
            started = poller.status["state"]
            self.tg.updates = [{"ok": True, "result": [{"update_id": 9, "message": {
                "message_id": 1, "chat": {"id": int(CHAT)}, "from": {"id": USER}, "text": "Song\nSomeone\nvpop"}}]}]
            poller.once()
        self.assertEqual((started, poller.status["state"]), ("badchat", "badchat"))
        self.assertIn('"<your chat id>"', poller.status["error"])
        self.assertEqual(self.titles(), [])
        lines = [c.args[0] for c in logged.call_args_list]
        self.assertTrue(lines[0].startswith("bot: badchat: "), lines)  # said once it starts, before any poll
        self.assertIn(f"bot: update 9 from chat {CHAT} ignored", "\n".join(lines))

    def test_a_chat_id_is_a_number_or_nothing(self):
        for chat in (CHAT, "-1001234567890", " 100 ", 100, -42):
            self.assertIsNone(x.Chat(chat, "in bot.json", "bot").problem(), chat)
        self.assertEqual(x.Chat("", "in bot.json", "bot").problem()[0], "nochat")
        for chat in ("<your chat id>", "12 34", "-", "1-2", "@a_channel", "100.0", "+100", "1" * 21):
            self.assertEqual(x.Chat(chat, "in bot.json", "bot").problem()[0], "badchat", chat)

    def test_check_says_when_the_chat_isnt_a_chat_id(self):
        path = os.path.join(self.folder, "bot.json")
        with open(path, "w") as f:
            json.dump({"token": "song:token", "chat": "<your chat id>"}, f)
        self.tg.answers = {"getMe": {"ok": True, "result": {"username": "example_song_bot"}},
                           "getWebhookInfo": {"ok": True, "result": {"url": ""}}}
        out = io.StringIO()
        with mock.patch.object(x, "Telegram", return_value=self.tg), mock.patch("sys.stdout", out):
            x.main(["check", "--config", path])
        self.assertIn('answers no one: the "chat" in bot.json is "<your chat id>", which isn\'t a chat id', out.getvalue())

    def test_a_crash_is_reported_in_the_chat(self):
        with mock.patch.object(self.bot, "try_add", side_effect=RuntimeError("boom")):
            self.say("A\nB\nvpop")
        self.assertEqual(self.tg.last_text(), "❌ CRASH LOG:\nRuntimeError: boom")

    def test_every_tapped_button_is_answered(self):
        self.tap("something_old")
        self.assertEqual(self.tg.toasts(), ["⚠️ Nút này đã cũ, hãy mở lại menu."])


class Polling(Base):
    def test_updates_are_handled_in_order_and_the_offset_kept(self):
        poller = x.Poller(self.store, self.bot)
        self.tg.updates = [{"ok": True, "result": [
            {"update_id": 41, "message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": USER}, "text": "One\nA\nvpop"}},
            {"update_id": 42, "message": {"message_id": 2, "chat": {"id": 100}, "from": {"id": USER}, "text": "Two\nB\nvpop"}},
        ]}]
        self.assertEqual(poller.once(), 0)
        self.assertEqual(self.titles(), ["One", "Two"])
        self.assertEqual(self.store.meta("offset"), "43")
        poller.once()
        self.assertEqual(self.tg.sent("getUpdates")[-1]["offset"], 43)

    def test_an_update_that_crashes_is_not_handled_twice(self):
        poller = x.Poller(self.store, self.bot)
        self.tg.updates = [{"ok": True, "result": [{"update_id": 5, "message": {"chat": {"id": 100}}}]}]
        poller.once()
        self.assertEqual(self.store.meta("offset"), "6")

    def test_a_webhook_still_set_is_said_plainly(self):
        poller = x.Poller(self.store, self.bot)
        self.tg.updates = [{"ok": False, "error_code": 409, "description": "Conflict: can't use getUpdates method while "
                                                                             "webhook is active; use deleteWebhook to delete the webhook first"}]
        self.assertEqual(poller.once(), 30)
        self.assertEqual(poller.status["state"], "webhook")
        self.assertIn("webhook-off", poller.status["error"])

    def test_a_second_copy_of_the_bot_is_not_taken_for_the_webhook(self):
        # Telegram answers 409 to both; a copy left running under PID 1 was told to run webhook-off.
        poller = x.Poller(self.store, self.bot)
        self.tg.updates = [{"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates "
                                                                             "request; make sure that only one bot instance is running"}]
        self.assertEqual(poller.once(), 30)
        self.assertEqual(poller.status["state"], "twice")
        self.assertNotIn("webhook", poller.status["error"])


SYDNEY = x.load_zone("Australia/Sydney")
PNG = x.PNG_SIGNATURE + b"a made-up cover "


def at(utc):
    """A UTC time ("2026-11-18 15:00") as Sydney's clock reads it."""
    return datetime.datetime.fromisoformat(utc + "+00:00").astimezone(SYDNEY)


class Covering(unittest.TestCase):
    """coverbot.gs's Daily Story Bot: today's cover at 2am Sydney time, and /sendnow."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="xrninsta-covers-")
        self.addCleanup(shutil.rmtree, self.folder)
        self.store = x.Store(os.path.join(self.folder, "data"))
        self.dir = os.path.join(self.folder, "data", "covers")
        os.makedirs(self.dir)
        self.tg = FakeTelegram()
        self.covers = x.Covers(self.store, self.tg, CHAT, self.dir, SYDNEY)
        self.next_id = 0

    def cover(self, day):
        with open(os.path.join(self.dir, f"Day_{day}.png"), "wb") as f:
            f.write(PNG + str(day).encode())

    def photos(self):
        return [p["caption"] for p in self.tg.sent("sendPhoto")]

    def say(self, text, chat=CHAT, now=None):
        self.next_id += 1
        with mock.patch.object(self.covers, "now", return_value=now or at("2026-11-19 01:00")):
            self.covers.handle({"update_id": self.next_id, "message": {
                "message_id": self.next_id, "chat": {"id": int(chat)}, "from": {"id": USER}, "text": text}})

    def test_the_day_counts_on_from_the_scripts_start(self):
        self.assertEqual(x.cover_day(datetime.date(2026, 10, 18)), 263)
        self.assertEqual(x.cover_day(datetime.date(2026, 11, 19)), 295)
        self.assertEqual(x.cover_day(datetime.date(2027, 9, 20)), 600)

    def test_todays_cover_goes_at_2am_sydney_time_and_once(self):
        self.cover(295)
        self.covers.tick(at("2026-11-18 14:59"))  # 1:59am on the 19th, in daylight saving (UTC+11)
        self.assertEqual(self.photos(), [])
        self.covers.tick(at("2026-11-18 15:00"))
        self.assertEqual(self.photos(), ["Day 295's cover"])
        sent = self.tg.sent("sendPhoto")[0]
        self.assertEqual((sent["chat_id"], sent["photo"]), (CHAT, ("Day_295.png", PNG + b"295")))
        self.covers.tick(at("2026-11-18 15:01"))
        self.covers.tick(at("2026-11-19 03:00"))  # 2pm, the same day
        self.assertEqual(self.photos(), ["Day 295's cover"])
        self.assertEqual(self.store.meta("cover_day"), "295")

    def test_2am_moves_with_daylight_saving(self):
        day = x.cover_day(datetime.date(2027, 6, 10))
        self.cover(day)
        self.covers.tick(at("2027-06-09 15:30"))  # 1:30am in winter (UTC+10)
        self.assertEqual(self.photos(), [])
        self.covers.tick(at("2027-06-09 16:00"))
        self.assertEqual(self.photos(), [f"Day {day}'s cover"])

    def test_the_nights_the_clocks_change_send_one_cover_each(self):
        spring, autumn = x.cover_day(datetime.date(2026, 10, 4)), x.cover_day(datetime.date(2027, 4, 4))
        self.cover(spring)
        self.cover(autumn)
        # 4 Oct 2026 has no 2am: 1:59 is followed by 3:00.
        self.covers.tick(at("2026-10-03 15:59"))
        self.assertEqual(self.photos(), [])
        self.covers.tick(at("2026-10-03 16:00"))
        self.assertEqual(self.photos(), [f"Day {spring}'s cover"])
        # 4 Apr 2027 has two: 2:00 in daylight saving, and an hour later 2:00 again.
        self.covers.tick(at("2027-04-03 15:00"))
        self.covers.tick(at("2027-04-03 16:00"))
        self.assertEqual(self.photos(), [f"Day {spring}'s cover", f"Day {autumn}'s cover"])

    def test_a_server_down_at_2am_sends_todays_when_back_and_never_an_old_one(self):
        self.cover(294)
        self.cover(295)
        self.store.set_meta("cover_day", 293)
        self.covers.tick(at("2026-11-18 23:00"))  # 10am on the 19th
        self.assertEqual(self.photos(), ["Day 295's cover"])

    def test_a_missing_cover_is_said_once_with_its_name_in_bold(self):
        self.covers.tick(at("2026-11-18 15:00"))
        self.covers.tick(at("2026-11-18 16:00"))
        said = self.tg.sent("sendMessage")
        self.assertEqual(len(said), 1)
        # The script sent this without parse_mode, so the chat saw the <b> tags themselves.
        self.assertEqual(said[0]["parse_mode"], "HTML")
        self.assertIn("<b>Day_295.png</b>", said[0]["text"])
        self.assertEqual(self.covers.status["state"], "missing")
        self.cover(296)
        self.covers.tick(at("2026-11-19 15:00"))
        self.assertEqual(self.photos(), ["Day 296's cover"])

    def test_telegram_out_of_reach_is_tried_again(self):
        self.cover(295)
        self.tg.answers["sendPhoto"] = None
        self.assertEqual(self.covers.tick(at("2026-11-18 15:00")), 300)
        self.assertIsNone(self.store.meta("cover_day"))
        del self.tg.answers["sendPhoto"]
        self.covers.tick(at("2026-11-18 15:05"))
        self.assertEqual(self.photos(), ["Day 295's cover", "Day 295's cover"])  # the one that failed, then the one that went
        self.assertEqual(self.store.meta("cover_day"), "295")
        self.assertEqual(self.tg.sent("sendMessage"), [])

    def test_a_refused_photo_is_said_once(self):
        self.cover(295)
        self.tg.answers["sendPhoto"] = {"ok": False, "error_code": 400, "description": "Bad Request: <IMAGE_PROCESS_FAILED>"}
        self.covers.tick(at("2026-11-18 15:00"))
        self.covers.tick(at("2026-11-18 15:01"))
        said = [p["text"] for p in self.tg.sent("sendMessage")]
        self.assertEqual(said, ["❌ Lỗi khi gửi ảnh: Bad Request: &lt;IMAGE_PROCESS_FAILED&gt;"])

    def test_sendnow_sends_at_once_and_2am_then_leaves_the_day_alone(self):
        self.cover(295)
        self.say("/sendnow")  # 1am on the 19th: the day is already Day 295's
        self.assertEqual(self.photos(), ["Day 295's cover"])
        self.covers.tick(at("2026-11-18 15:00"))
        self.assertEqual(self.photos(), ["Day 295's cover"])
        self.say("/sendnow@example_story_bot")  # asked again, it goes again
        self.assertEqual(self.photos(), ["Day 295's cover"] * 2)

    def test_it_answers_only_its_chat_and_its_own_commands(self):
        self.cover(295)
        self.say("/sendnow", chat="999")
        self.say("/sendnowplease")
        self.say("Day 295")
        self.assertEqual(self.tg.calls, [])
        self.say("/start")
        self.assertTrue(self.tg.last_text().startswith("🌅 Daily Story Bot đã sẵn sàng!"))

    def test_with_no_chat_it_sends_no_one_anything(self):
        covers = x.Covers(self.store, self.tg, "", self.dir, SYDNEY)
        self.cover(295)
        covers.tick(at("2026-11-18 15:00"))
        self.assertEqual(self.tg.calls, [])
        self.assertEqual(covers.status["state"], "nochat")

    def test_a_chat_that_isnt_a_chat_id_gets_no_cover_and_the_day_stays_unsent(self):
        # Sent to "<your chat id>", Telegram's refusal settled the day: the cover was never sent.
        covers = x.Covers(self.store, self.tg, "<your chat id>", self.dir, SYDNEY)
        self.cover(295)
        with mock.patch.object(x, "log"):
            covers.tick(at("2026-11-18 15:00"))
        self.assertEqual(self.tg.calls, [])
        self.assertEqual(covers.status["state"], "badchat")
        self.assertIsNone(self.store.meta("cover_day"))

    def test_without_time_zone_data_it_sends_nothing_and_says_why(self):
        self.cover(295)
        with mock.patch.object(x, "load_zone", return_value=None):
            covers = x.Covers(self.store, self.tg, CHAT, self.dir)
        covers.tick()
        self.assertEqual(self.tg.calls, [])
        self.assertEqual(covers.status["state"], "nozone")
        self.assertIn("tzdata", covers.status["error"])

    def test_the_first_start_after_2am_leaves_todays_to_the_apps_script(self):
        # The day of the switch, the script has sent today's already; the server mustn't send it again.
        self.cover(295)
        self.cover(296)
        self.covers.first_start(at("2026-11-19 04:00"))  # 3pm on the 19th
        self.covers.tick(at("2026-11-19 04:01"))
        self.assertEqual(self.photos(), [])
        self.covers.tick(at("2026-11-19 15:00"))  # 2am on the 20th
        self.assertEqual(self.photos(), ["Day 296's cover"])
        self.covers.first_start(at("2026-11-20 04:00"))  # a restart after that changes nothing
        self.assertEqual(self.store.meta("cover_day"), "296")

    def test_a_first_start_before_2am_sends_that_day(self):
        self.cover(295)
        self.covers.first_start(at("2026-11-18 14:00"))  # 1am on the 19th
        self.covers.tick(at("2026-11-18 15:00"))
        self.assertEqual(self.photos(), ["Day 295's cover"])

    def test_its_poller_keeps_its_own_place_and_names_its_own_switch(self):
        self.store.set_meta("offset", 7)  # the song bot's
        poller = x.covers_poller(self.store, self.covers)
        self.tg.updates = [{"ok": True, "result": [{"update_id": 30, "message": {
            "message_id": 1, "chat": {"id": int(CHAT)}, "from": {"id": USER}, "text": "/start"}}]}]
        poller.once()
        self.assertEqual((self.store.meta("covers_offset"), self.store.meta("offset")), ("31", "7"))
        self.tg.updates = [{"ok": False, "error_code": 409, "description": "Conflict: can't use getUpdates method while "
                                                                             "webhook is active; use deleteWebhook to delete the webhook first"}]
        poller.once()
        self.assertIn("xrninsta.py webhook-off --covers", poller.status["error"])

    def test_a_covers_token_thats_the_song_bots_starts_nothing(self):
        conf = {"token": "same", "chat": CHAT, "covers": {"token": "same", "chat": CHAT}}
        with mock.patch.object(x, "log"), mock.patch.object(x.threading, "Thread") as thread:
            self.assertIsNone(x.start_covers(self.store, conf, os.path.join(self.folder, "data"), "same"))
        thread.assert_not_called()


class CoverBotSetting(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="xrninsta-coverbot-")
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = os.path.join(self.folder, "bot.json")
        with open(self.path, "w") as f:
            json.dump({"token": "song:token", "chat": CHAT}, f)

    def test_it_goes_under_covers_and_keeps_the_song_bot(self):
        x.set_covers_bot(self.path, " story:token ", f" {CHAT}\n")
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"token": "song:token", "chat": CHAT,
                                            "covers": {"token": "story:token", "chat": CHAT}})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_wrong_chat_or_the_song_bots_token_writes_nothing(self):
        for token, chat in (("story:token", "me"), ("song:token", CHAT), ("", CHAT)):
            with self.assertRaises(SystemExit):
                x.set_covers_bot(self.path, token, chat)
        with open(self.path) as f:
            self.assertNotIn("covers", json.load(f))


class SongBotSetting(unittest.TestCase):
    """`song-bot`, the song bot's setup: it asks for the token and the chat, as covers-bot does."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="xrninsta-songbot-")
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = os.path.join(self.folder, "bot.json")

    def test_it_writes_the_song_bot_and_keeps_the_cover_bot(self):
        with open(self.path, "w") as f:
            json.dump({"token": "old:token", "chat": "1", "covers": {"token": "story:token", "chat": CHAT}}, f)
        x.set_song_bot(self.path, " song:token ", f" {CHAT}\n")
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"token": "song:token", "chat": CHAT,
                                            "covers": {"token": "story:token", "chat": CHAT}})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_first_bot_json_is_made_owner_only(self):
        x.set_song_bot(self.path, "song:token", CHAT)
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"token": "song:token", "chat": CHAT})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_chat_that_isnt_a_chat_id_or_the_cover_bots_token_writes_nothing(self):
        with open(self.path, "w") as f:
            json.dump({"covers": {"token": "story:token", "chat": CHAT}}, f)
        for token, chat in (("song:token", "<your chat id>"), ("song:token", ""), ("song:token", "12 34"),
                            ("story:token", CHAT), ("", CHAT)):
            with self.assertRaises(SystemExit):
                x.set_song_bot(self.path, token, chat)
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"covers": {"token": "story:token", "chat": CHAT}})

    def test_the_command_asks_for_both(self):
        with mock.patch.object(x.getpass, "getpass", return_value="song:token"), \
                mock.patch("builtins.input", return_value=CHAT), mock.patch("sys.stdout", io.StringIO()):
            x.main(["song-bot", "--config", self.path])
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"token": "song:token", "chat": CHAT})


class CoverUpload(unittest.TestCase):
    """The photo goes to Telegram as a form with the file in it, as the script's blob did."""

    def test_the_fields_and_the_file_arrive_whole(self):
        seen = {}

        class Telegram(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["type"] = self.headers["Content-Type"]
                seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"ok": true, "result": {"message_id": 1}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Telegram)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        tg = x.Telegram("123:abc")
        tg.base = f"http://127.0.0.1:{srv.server_address[1]}"
        content = PNG + bytes(range(256)) + b"\r\n--"  # every byte, and what looks like a boundary's start
        answer = tg.upload("sendPhoto", {"chat_id": CHAT, "caption": "Day 295's cover"}, "photo", "Day_295.png", content)
        self.assertTrue(answer["ok"])
        self.assertEqual(seen["path"], "/bot123:abc/sendPhoto")
        form = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + seen["type"].encode() + b"\r\n\r\n" + seen["body"])
        parts = {p.get_param("name", header="content-disposition"): p for p in form.iter_parts()}
        self.assertEqual(parts["chat_id"].get_payload(decode=True), CHAT.encode())
        self.assertEqual(parts["caption"].get_payload(decode=True), b"Day 295's cover")
        self.assertEqual(parts["photo"].get_filename(), "Day_295.png")
        self.assertEqual(parts["photo"].get_content_type(), "image/png")
        self.assertEqual(parts["photo"].get_payload(decode=True), content)


class AddingCovers(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="xrninsta-addcovers-")
        self.addCleanup(shutil.rmtree, self.folder)
        self.dir = os.path.join(self.folder, "data", "covers")

    def zip_of(self, files):
        path = os.path.join(self.folder, "covers.zip")
        with zipfile.ZipFile(path, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return path

    def test_a_drive_zip_goes_into_the_folder_by_name(self):
        source = self.zip_of({
            "output_stories/Day_294.png": PNG + b"294",
            "output_stories/Day_295.png": PNG + b"295",
            "__MACOSX/output_stories/._Day_295.png": b"\x00\x05\x16\x07",
            "output_stories/notes.txt": b"made up",
            "output_stories/Day_296.png": b"not a png",
            "../../Day_297.png": PNG + b"297",  # a name with a path lands in the folder, nowhere else
        })
        r = x.add_covers(self.dir, source)
        self.assertEqual((r["added"], r["replaced"], len(r["skipped"])), (3, 0, 3))
        self.assertEqual(sorted(os.listdir(self.dir)), ["Day_294.png", "Day_295.png", "Day_297.png"])
        with open(os.path.join(self.dir, "Day_295.png"), "rb") as f:
            self.assertEqual(f.read(), PNG + b"295")
        self.assertEqual(os.stat(os.path.join(self.dir, "Day_295.png")).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Day_297.png")))
        self.assertEqual(x.add_covers(self.dir, source)["replaced"], 3)

    def test_a_folder_works_too_and_an_oversized_file_is_skipped(self):
        src = os.path.join(self.folder, "src")
        os.makedirs(src)
        for name, content in {"Day_300.png": PNG, "Day_301.png": PNG + b"x" * x.COVER_MAX_BYTES}.items():
            with open(os.path.join(src, name), "wb") as f:
                f.write(content)
        r = x.add_covers(self.dir, src)
        self.assertEqual((r["added"], r["skipped"]), (1, ["Day_301.png"]))

    def test_the_summary_says_how_far_the_covers_go(self):
        os.makedirs(self.dir)
        for day in (290, 295, 296, 298):
            open(os.path.join(self.dir, f"Day_{day}.png"), "wb").close()
        self.assertEqual(x.covers_summary(self.dir, 295),
                         "Covers: Day 295 (today) to Day 298, the last on 22 Nov 2026; 1 more missing before the last")
        self.assertEqual(x.covers_summary(self.dir, 294),
                         "Covers: Day 294 (today) to Day 298, the last on 22 Nov 2026; today's (Day_294.png) is missing; "
                         "1 more missing before the last")
        self.assertEqual(x.covers_summary(self.dir, 299), f"Covers: none from Day 299 (today) on, in {self.dir}")


class DeployClone(unittest.TestCase):
    """deploy.sh in a throwaway clone of this repo, with an ssh that only notes it was called."""

    def setUp(self):
        import subprocess
        self.run_ = subprocess.run
        self.folder = tempfile.mkdtemp(prefix="xrninsta-deploy-")
        self.addCleanup(shutil.rmtree, self.folder)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.clone = os.path.join(self.folder, "repo")
        subprocess.run(["git", "clone", "-q", repo, self.clone], check=True)
        shutil.copy(os.path.join(repo, "server", "deploy.sh"), os.path.join(self.clone, "server"))  # as it is now
        # deploy.sh runs the tests first; these would run this test again.
        with open(os.path.join(self.clone, "server", "test_xrninsta.py"), "w") as f:
            f.write("import unittest\n\n\nclass Stand(unittest.TestCase):\n    def test_in(self):\n        pass\n")
        bin_ = os.path.join(self.folder, "bin")
        os.makedirs(bin_)
        self.calls = os.path.join(self.folder, "ssh-calls")
        with open(os.path.join(bin_, "ssh"), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {self.calls}\ncat >/dev/null\nexit 1\n')
        os.chmod(os.path.join(bin_, "ssh"), 0o755)
        self.env = {**os.environ, "PATH": bin_ + os.pathsep + os.environ["PATH"]}

    def deploy(self, *args):
        r = self.run_(["sh", os.path.join(self.clone, "server", "deploy.sh"), *args], env=self.env,
                      capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr, os.path.exists(self.calls)


class Deploying(DeployClone):
    def test_uncommitted_changes_to_what_goes_up_are_refused(self):
        # It copied the working tree as it was: a half-done edit went live with no commit naming it.
        with open(os.path.join(self.clone, "web", "app.js"), "a") as f:
            f.write("// half done\n")
        code, said, reached = self.deploy("somehost")
        self.assertEqual((code, reached), (1, False), said)
        self.assertIn("web/app.js", said)
        code, said, reached = self.deploy("--force", "somehost")
        self.assertTrue(reached, said)  # past the check, to the (stub) server
        self.assertIn("with uncommitted changes", said)

    def test_a_clean_tree_goes_up_and_says_which_commit(self):
        head = self.run_(["git", "-C", self.clone, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        code, said, reached = self.deploy("somehost")
        self.assertTrue(reached, said)
        self.assertIn(f"at {head}", said)


class DeployingToAPhone(DeployClone):
    """deploy.sh against a made-up phone: ssh runs each command here, in a home and a $PREFIX of its
    own, with sv, curl, tailscale and pip as stand-ins. What it writes there is read back."""

    FAKES = {
        "ssh": '#!/bin/sh\nshift\nexec sh -c "$*"\n',
        "sv": '#!/bin/sh\necho "sv $*" >> "$HOME/calls"\n',
        "curl": "#!/bin/sh\nprintf ok\n",
        "tailscale": '#!/bin/sh\necho "tailscale $*" >> "$HOME/calls"\n',
        "pip": "#!/bin/sh\nexit 0\n",
    }

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.folder, "home")
        self.prefix = os.path.join(self.folder, "usr")
        self.service = os.path.join(self.prefix, "var", "service")
        os.makedirs(self.home)
        os.makedirs(self.service)
        for name, body in self.FAKES.items():
            path = os.path.join(self.folder, "bin", name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.env.update(HOME=self.home, PREFIX=self.prefix)
        self.guard(0)

    def guard(self, exits):
        """A stand-in for home-server's sv-guard on the phone, which notes it ran and exits [exits]."""
        bin_ = os.path.join(self.home, ".local", "bin")
        os.makedirs(bin_, exist_ok=True)
        with open(os.path.join(bin_, "sv-guard"), "w") as f:
            f.write(f'#!/bin/sh\necho "sv-guard $*" >> "$HOME/started"\nexit {exits}\n')
        os.chmod(os.path.join(bin_, "sv-guard"), 0o755)

    def run_script(self, guard_exit):
        """The service's run script, run as runsv would, with sv-guard exiting [guard_exit] and a
        python that only notes how it was started."""
        self.guard(guard_exit)
        with open(os.path.join(self.folder, "bin", "python"), "w") as f:
            f.write('#!/bin/sh\necho "python $1" >> "$HOME/started"\n')
        os.chmod(os.path.join(self.folder, "bin", "python"), 0o755)
        started = os.path.join(self.home, "started")
        if os.path.exists(started):
            os.remove(started)
        r = self.run_(["sh", os.path.join(self.service, "xrninsta", "run")], env=self.env, capture_output=True,
                      text=True, timeout=30)
        with open(started) as f:
            return r.returncode, f.read().splitlines()

    def test_a_new_service_clears_a_leftover_copy_off_its_port_before_it_starts(self):
        # Every other service's run script calls sv-guard just before its exec line; this one
        # didn't, so a copy left behind by a dead runsv held port 5239 and the new one crashed
        # once a second.
        code, said, _ = self.deploy("phone")
        self.assertEqual(code, 0, said)
        with open(os.path.join(self.service, "xrninsta", "run")) as f:
            run = f.read()
        program = f"{self.home}/xrninsta/xrninsta.py"
        self.assertIn(f"{self.home}/.local/bin/sv-guard 5239 {program} || exit 1\nexec python {program} \\\n", run)
        self.assertEqual(self.run_script(0), (0, [f"sv-guard 5239 {program}", f"python {program}"]))
        self.assertEqual(self.run_script(1), (1, [f"sv-guard 5239 {program}"]))  # the port's taken: not started

    def test_a_new_service_writes_its_own_log_script(self):
        # It copied xrnwallet-sync's, so it failed on a phone without that service, and would
        # once xrnwallet-sync is renamed xrnwal-sync.
        code, said, _ = self.deploy("phone")
        self.assertEqual(code, 0, said)
        log_dir = f"{self.prefix}/var/log/sv/xrninsta"
        with open(os.path.join(self.service, "xrninsta", "log", "run")) as f:
            self.assertEqual(f.read(), f'#!{self.prefix}/bin/sh\nmkdir -p "{log_dir}"\nexec svlogd -tt "{log_dir}"\n')
        self.assertEqual(os.stat(os.path.join(self.service, "xrninsta", "log", "run")).st_mode & 0o777, 0o755)
        self.assertEqual(sorted(os.listdir(self.service)), ["xrninsta"])  # no half-built copy left

    def test_a_phone_without_sv_guard_gets_no_service(self):
        os.remove(os.path.join(self.home, ".local", "bin", "sv-guard"))
        code, said, _ = self.deploy("phone")
        self.assertEqual(code, 1, said)
        self.assertIn("phone/install-sv-guard", said)
        self.assertEqual(os.listdir(self.service), [])

    def test_an_update_restarts_the_service_and_leaves_its_run_script_alone(self):
        self.assertEqual(self.deploy("phone")[0], 0)
        run = os.path.join(self.service, "xrninsta", "run")
        with open(run, "a") as f:
            f.write("# kept\n")
        code, said, _ = self.deploy("phone")
        self.assertEqual(code, 0, said)
        with open(run) as f:
            self.assertTrue(f.read().endswith("# kept\n"))
        with open(os.path.join(self.home, "calls")) as f:
            self.assertIn("sv restart xrninsta", f.read().splitlines())


def write_xlsx(path, sheets):
    """A minimal .xlsx: {name: {row number: [values]}}, strings inline."""
    def col(i):
        name = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            name = chr(65 + r) + name
        return name

    with zipfile.ZipFile(path, "w") as z:
        names = list(sheets)
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names)))
                   + "</Relationships>")
        for i, n in enumerate(names):
            rows = ""
            for number, values in sorted(sheets[n].items()):
                cells = ""
                for c, v in enumerate(values):
                    ref = f"{col(c)}{number}"
                    if isinstance(v, (int, float)):
                        cells += f'<c r="{ref}"><v>{v}</v></c>'
                    elif v is not None:
                        cells += f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
                rows += f'<row r="{number}">{cells}</row>'
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                       f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>')


class Importing(Base):
    def sheet(self):
        path = os.path.join(self.folder, "export.xlsx")
        write_xlsx(path, {
            "Dashboard": {1: ["Music Dashboard"]},
            "Songs": {1: [None, "Artist(s)", "Day No", "Genre"], 2: ["Early One", "Someone", -5, "VPOP"],
                      3: ["Second", "Alex Example", 1, "usuk"], 5: ["After A Gap", "Someone", 2, "Rock"]},
            "Onhold": {1: ["Title", "Artist(s)", "Note"], 2: ["Maybe", "", "check the key"]},
        })
        return path

    def test_rows_keep_their_sheet_row_as_their_id(self):
        report = x.import_xlsx(self.store, self.sheet())
        self.assertEqual((report["songs"], report["holds"], report["oddGenre"]), (3, 1, 1))
        self.assertEqual([(s["id"], s["title"], s["day"], s["genre"]) for s in self.store.songs()],
                         [(2, "Early One", -5, "VPOP"), (3, "Second", 1, "USUK"), (5, "After A Gap", 2, "ROCK")])
        self.assertEqual(self.store.holds()[0]["note"], "check the key")
        self.assertEqual(self.store.next_day(), 3)  # days below 1 don't count, as in the script
        self.assertEqual(self.song("New", "X")["id"], 6)

    def test_a_button_the_script_sent_still_works(self):
        x.import_xlsx(self.store, self.sheet())
        # The script's undo_<sheet row>_<hash of the title>.
        self.tap(f"undo_3_{x.short_hash('Second', 8)}")
        self.assertEqual(self.titles(), ["Early One", "After A Gap"])

    def test_it_wont_import_over_songs_without_being_told(self):
        x.import_xlsx(self.store, self.sheet())
        with self.assertRaises(SystemExit):
            x.import_xlsx(self.store, self.sheet())
        self.song("Extra", "X")
        x.import_xlsx(self.store, self.sheet(), replace=True)
        self.assertEqual(len(self.store.songs()), 3)


class Version(unittest.TestCase):
    """xrninsta had no version until 1.0.0: one constant, said by the README and `--version`."""

    def test_it_is_dotted_numbers(self):
        self.assertRegex(x.VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_the_readme_says_the_same(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md"), encoding="utf-8") as f:
            self.assertIn(f"Version {x.VERSION}:", f.read())

    def test_version_prints_it(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit) as stop:
            x.main(["--version"])
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"xrninsta {x.VERSION}")


class Page(unittest.TestCase):
    """The page's API, through the real server."""

    @classmethod
    def setUpClass(cls):
        import bcrypt
        cls.folder = tempfile.mkdtemp(prefix="xrninsta-http-")
        users = os.path.join(cls.folder, "users")
        with open(users, "w") as f:
            for name in ("owner", "guest"):
                f.write(f"{name}:{bcrypt.hashpw(b'pw', bcrypt.gensalt(4)).decode()}\n")
        web = os.path.join(cls.folder, "web")
        os.makedirs(web)
        with open(os.path.join(web, "index.html"), "w") as f:
            f.write("<!doctype html><title>xrninsta</title>")
        cls.store = x.Store(os.path.join(cls.folder, "data"))
        x.Handler.app = {"store": cls.store, "login": x.Login(users), "poller": None, "web": web}
        x.FAIL_DELAY_S = 0
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), x.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.folder)

    def setUp(self):
        x.Handler.app["login"].failures.clear()
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM songs")
            self.store.db.execute("DELETE FROM holds")

    def call(self, method, path, body=None, user="owner", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        h = dict(headers or {})
        if user:
            h["Authorization"] = "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out.decode()

    def test_only_the_owner_signs_in(self):
        self.assertEqual(self.call("GET", "/api/data", user=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/data", user="guest")[0], 403)
        status, doc = self.call("GET", "/xrninsta/api/data")
        self.assertEqual(status, 200)
        self.assertEqual(doc["genres"], x.GENRES)
        self.assertEqual(self.call("GET", "/health", user=None), (200, "ok"))

    def test_every_answer_names_the_version(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/health")
        r = c.getresponse()
        self.assertEqual(r.read(), b"ok")  # deploy.sh and the hub's monitor wait for exactly this
        c.close()
        self.assertEqual(r.getheader("Server").strip(), f"xrninsta/{x.VERSION}")

    def test_add_edit_and_delete_a_song(self):
        self.store.add_song("Existing", "VSTRA", "VPOP", 7)
        status, song = self.call("POST", "/api/songs", {"title": "  From  The Page ", "artists": "vstra", "genre": "j-pop"})
        self.assertEqual(status, 201)
        self.assertEqual((song["title"], song["artists"], song["genre"], song["day"]), ("From The Page", "VSTRA", "JPOP", 8))
        tag = {"If-Match": f'"{song["updated"]}"'}
        status, changed = self.call("PUT", f"/api/songs/{song['id']}", {"title": "Renamed", "artists": "VSTRA", "genre": "VPOP", "day": 9}, headers=tag)
        self.assertEqual((status, changed["title"], changed["day"]), (200, "Renamed", 9))
        status, doc = self.call("PUT", f"/api/songs/{song['id']}", {"title": "Again", "artists": "VSTRA", "genre": "VPOP", "day": 9}, headers=tag)
        self.assertEqual((status, doc["current"]["title"]), (412, "Renamed"))  # changed since that tag
        self.assertEqual(self.call("DELETE", f"/api/songs/{song['id']}", headers=tag)[0], 412)
        self.assertEqual(self.call("DELETE", f"/api/songs/{song['id']}", headers={"If-Match": f'"{changed["updated"]}"'})[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/songs/{song['id']}", headers={"If-Match": '"1"'})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/songs/1")[0], 428)

    def test_bad_input_is_refused(self):
        for body in ({"title": "", "artists": "A", "genre": "VPOP"}, {"title": "A", "artists": "A", "genre": "rock"},
                     {"title": "A", "artists": "A", "genre": "VPOP", "day": "3"}, {"title": "A\x07", "artists": "A", "genre": "VPOP"},
                     {"title": "x" * 201, "artists": "A", "genre": "VPOP"}, ["not", "an", "object"]):
            self.assertEqual(self.call("POST", "/api/songs", body)[0], 400, body)

    def raw(self, method, path, data):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path, body=data, headers={"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()})
        try:
            r = c.getresponse()
            r.read()
            return r.status
        except (http.client.HTTPException, OSError) as e:
            return type(e).__name__
        finally:
            c.close()

    def test_a_body_json_takes_but_the_server_cant_store_is_a_400(self):
        # A lone surrogate passed every check and broke in SQLite; a deep enough nesting broke the
        # parser with RecursionError. Both dropped the connection with a traceback in the log.
        self.assertEqual(self.raw("POST", "/api/holds", b'{"title": "\\ud800"}'), 400)
        self.assertEqual(self.raw("POST", "/api/songs", b'{"title": "A", "artists": "B\\udfff", "genre": "VPOP"}'), 400)
        self.assertEqual(self.raw("POST", "/api/holds", b"[" * 30000 + b"]" * 30000), 400)
        self.assertEqual((self.store.songs(), self.store.holds()), ([], []))

    def cut_upload(self, reset):
        """A POST whose body stops half way, then the connection closes, as the reverse proxy closes
        it when the caller's side drops: with [reset], hard, before any answer can come back."""
        import socket
        import struct
        body = json.dumps({"title": "Cut " * 500, "artists": "", "note": ""}).encode()
        login = base64.b64encode(b"owner:pw")
        head = (b"POST /api/holds HTTP/1.1\r\nHost: x\r\nAuthorization: Basic " + login +
                b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=10) as s:
                s.sendall(head + body[:len(body) // 2])
                if reset:
                    time.sleep(0.2)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    answer = b""
                else:
                    s.shutdown(socket.SHUT_WR)
                    answer = b"".join(iter(lambda: s.recv(4096), b""))
            for _ in range(300):  # the handler finishes on its own thread
                if any(word in log.getvalue() for word in ("dropped", "Traceback", '" 400 ')):
                    break
                time.sleep(0.01)
            time.sleep(0.1)
        return answer, log.getvalue()

    def test_an_upload_cut_short_is_answered_as_such_and_stores_nothing(self):
        # The half that came was parsed, and the 400 said the body must be JSON.
        answer, log = self.cut_upload(reset=False)
        self.assertRegex(answer, rb"^HTTP/1\.[01] 400 ")
        self.assertIn(b"the upload stopped short", answer)
        self.assertNotIn("Traceback", log)
        self.assertEqual(self.store.holds(), [])

    def test_a_caller_that_hangs_up_is_one_line_in_the_log_not_a_traceback(self):
        # 118 tracebacks on 15 and 16 Nov, a reset each, which xrnhub's log page counted as errors.
        _, log = self.cut_upload(reset=True)
        self.assertNotIn("Traceback", log)
        self.assertIn("connection dropped: ConnectionResetError", log)
        self.assertEqual(self.store.holds(), [])

    def idle_after(self, ask):
        """A kept-alive connection left idle past the handler's timeout, after one request if [ask]."""
        import socket
        login = base64.b64encode(b"owner:pw")
        with mock.patch.object(x.Handler, "timeout", 0.3), mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=10) as s:
                if ask:
                    s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nAuthorization: Basic " + login + b"\r\n\r\n")
                # Read the answer and then on: the server closing the connection once the timeout
                # passes ends this; a connection left open would raise socket.timeout instead.
                got = b""
                while chunk := s.recv(4096):
                    got += chunk
                closed = not ask or got.startswith(b"HTTP/1.1 200 ")
            time.sleep(0.1)
        return closed, log.getvalue()

    def test_an_idle_kept_alive_connection_closes_without_a_word(self):
        # 131 "Request timed out" lines on 18 Nov, one each time a connection the laptop kept open
        # was closed after the handler's timeout.
        closed, log = self.idle_after(ask=True)
        self.assertTrue(closed)
        self.assertNotIn("timed out", log)

    def test_a_connection_that_never_asks_is_still_logged_when_it_times_out(self):
        closed, log = self.idle_after(ask=False)
        self.assertTrue(closed)
        self.assertIn("Request timed out", log)

    def test_a_hold_becomes_a_song_in_one_go(self):
        status, hold = self.call("POST", "/api/holds", {"title": "Later", "artists": "", "note": "line one\nline two"})
        self.assertEqual((status, hold["note"]), (201, "line one\nline two"))
        status, song = self.call("POST", "/api/songs", {"title": "Later", "artists": "Someone", "genre": "MISC", "fromHold": hold["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(self.store.holds(), [])
        self.assertEqual(self.call("POST", "/api/songs", {"title": "L", "artists": "S", "genre": "MISC", "fromHold": hold["id"]})[0], 400)

    def guess_as(self, name, password, addr):
        auth = "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/api/data", headers={"Authorization": auth, "X-Forwarded-For": addr})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_a_right_login_clears_only_its_own_users_mistakes(self):
        # A right login wiped the whole address's count: guesses at another name from the same
        # address started over with every login of the first.
        login, addr = x.Handler.app["login"], "203.0.113.80"
        for _ in range(5):
            self.assertEqual(401, self.guess_as("admin", "nope", addr))
        for _ in range(4):
            self.assertEqual(401, self.guess_as("owner", "nope", addr))
        login.cache.clear()
        self.assertEqual(200, self.guess_as("owner", "pw", addr))  # owner's four go, admin's five stay
        for _ in range(x.FAIL_LIMIT - 5):
            self.assertEqual(401, self.guess_as("admin", "nope", addr))
        self.assertEqual(429, self.guess_as("admin", "nope", addr))

    def test_a_password_already_found_right_clears_nothing(self):
        # A browser or script at the same address as a guesser cleared the guesser's count with
        # every request. A password the server already knows is right (its cache) is a session.
        addr = "203.0.113.81"
        self.assertEqual(200, self.guess_as("owner", "pw", addr))
        for _ in range(x.FAIL_LIMIT - 1):
            self.assertEqual(401, self.guess_as("owner", "nope", addr))
            self.assertEqual(200, self.guess_as("owner", "pw", addr))
        self.assertEqual(401, self.guess_as("owner", "nope", addr))
        self.assertEqual(429, self.guess_as("owner", "nope", addr))

    def test_a_login_in_flight_is_not_a_wrong_guess(self):
        # Counted as wrong before it was checked, a right login being checked took a place: two
        # sign-ins at once, at an address with a few mistakes behind it, got a 429.
        login, addr = x.Handler.app["login"], "203.0.113.82"
        real = login.check_basic

        def slow(header):
            time.sleep(0.3)
            return real(header)

        with mock.patch.object(x, "FAIL_LIMIT", 3):
            self.assertEqual(401, self.guess_as("owner", "nope", addr))
            self.assertEqual(401, self.guess_as("owner", "nope", addr))  # one place left
            login.cache.clear()
            start = threading.Barrier(2)

            def sign_in(_):
                start.wait()
                return self.guess_as("owner", "pw", addr)

            with mock.patch.object(login, "check_basic", slow), concurrent.futures.ThreadPoolExecutor(2) as pool:
                self.assertEqual([200, 200], list(pool.map(sign_in, range(2))))

    def test_an_ipv6_guesser_is_its_64_not_each_address_in_it(self):
        # Each IPv6 address had a count of its own, so the owner of a /64 (every line comes with at
        # least one) had a new address for every guess and never reached the limit.
        login = x.Handler.app["login"]
        for i in range(x.FAIL_LIMIT):
            self.assertEqual(401, self.guess_as("owner", f"guess{i}", f"2001:db8:1:2::{i + 1:x}"))
        self.assertEqual(429, self.guess_as("owner", "guess10", "2001:db8:1:2:aaaa::1"))
        self.assertEqual(401, self.guess_as("owner", "nope", "2001:db8:1:3::1"))  # the /64 next door isn't held
        self.assertEqual(401, self.guess_as("owner", "nope", "::ffff:203.0.113.40"))  # an IPv4 caller written as IPv6
        self.assertIn("203.0.113.40", login.failures)  # ...is counted as that IPv4 address

    def test_addresses_whose_mistakes_are_old_are_forgotten(self):
        # Each address that ever got a password wrong stayed in memory for good.
        login = x.Handler.app["login"]
        long_ago = time.monotonic() - x.FAIL_WINDOW_S - 1
        for i in range(10001):
            login.failures[f"198.51.{i // 256}.{i % 256}"] = [(long_ago, "owner")]
        self.assertTrue(login.reserve_guess("203.0.113.90"))
        login.settle_guess("203.0.113.90", "owner", False)
        self.assertEqual(list(login.failures), ["203.0.113.90"])

    def test_a_write_from_another_sites_page_is_refused(self):
        status, _ = self.call("POST", "/api/holds", {"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.holds(), [])

    def test_the_cookie_signs_the_page_in(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", "/xrninsta/session", headers={"Authorization": "Basic " + base64.b64encode(b"owner:pw").decode()})
        r = c.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("Path=/", r.getheader("Set-Cookie"))
        c.request("GET", "/api/data", headers={"Cookie": cookie, "X-Xrn-Client": "xrninsta"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        c.request("GET", "/api/data", headers={"Cookie": cookie})  # no X-Xrn-Client: another site could send this
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        c.close()

    def a_cookie(self, expires, user="owner"):
        """A sign-in cookie as make_session makes it, running out at [expires]: two made in the same
        second are the same cookie."""
        login = x.Handler.app["login"]
        who = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        body = f"{who}.{expires}"
        return f"{body}.{login.mac(body, login.password_tag(user))}"

    def as_page(self, cookie):
        return {"Cookie": f"{x.SESSION_COOKIE}={cookie}", "X-Xrn-Client": "xrninsta"}

    def shared_list(self):
        """signed-out.json beside the users file: what every server that takes the cookie reads."""
        return os.path.join(self.folder, "signed-out.json")

    def read_list(self):
        try:
            with open(self.shared_list(), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def write_list(self, doc):
        """The shared list as another server (the hub) writes it: a new file in the old one's place."""
        with open(self.shared_list() + ".test", "w", encoding="utf-8") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        os.replace(self.shared_list() + ".test", self.shared_list())

    @staticmethod
    def hash_of(cookie):
        return hashlib.sha256(cookie.encode()).hexdigest()

    def test_a_cookie_signed_out_on_another_server_is_refused_here(self):
        # Signing out of xrnhub ended the cookie there only: a copy of it went on opening the songs
        # for the rest of its 30 days.
        expires = int(time.time()) + 8000
        cookie = self.a_cookie(expires)
        self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(cookie))[0], 200)
        self.write_list({**self.read_list(), self.hash_of(cookie): expires})
        self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(cookie))[0], 401)
        self.assertEqual(self.call("GET", "/api/data")[0], 200)  # a password still signs in

    def test_signing_out_ends_the_cookie_here_and_on_every_other_server(self):
        # DELETE /session only told the browser to drop its cookie: a copy of it (another browser,
        # a stolen one) went on signing in, here and on the hub, habits and blocks pages.
        expires = int(time.time()) + 8100
        mine, other = self.a_cookie(expires), self.a_cookie(expires + 1)
        self.assertEqual(self.call("DELETE", "/xrninsta/session", user=None, headers=self.as_page(mine))[0], 200)
        self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(mine))[0], 401)
        self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(other))[0], 200)
        self.assertEqual(self.read_list()[self.hash_of(mine)], expires)
        self.assertEqual(os.stat(self.shared_list()).st_mode & 0o777, 0o600)
        # A sign-out with no session of ours puts nothing on the list.
        before = self.read_list()
        for cookie in ("junk", self.a_cookie(int(time.time()) - 5), ""):
            self.assertEqual(self.call("DELETE", "/session", user=None, headers=self.as_page(cookie))[0], 200)
        self.assertEqual(self.read_list(), before)

    def test_a_missing_or_broken_list_breaks_no_sign_in(self):
        gone, live = self.a_cookie(int(time.time()) + 8200), self.a_cookie(int(time.time()) + 8300)
        x.Handler.app["login"].end_session(f"{x.SESSION_COOKIE}={gone}")
        self.write_list("{half a file")
        with mock.patch.object(sys, "stderr", io.StringIO()) as said:
            for _ in range(3):
                self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(live))[0], 200)
                # The list read before the file broke still holds.
                self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(gone))[0], 401)
            self.assertEqual(self.call("GET", "/api/data")[0], 200)
            self.assertEqual(self.call("POST", "/session")[0], 200)
        self.assertEqual(said.getvalue().count("signed-out.json"), 1, said.getvalue())  # one line, not one a request
        os.remove(self.shared_list())
        self.assertEqual(self.call("GET", "/api/data", user=None, headers=self.as_page(live))[0], 200)
        self.assertEqual(self.call("GET", "/api/data")[0], 200)
        self.assertEqual(self.call("POST", "/session")[0], 200)

    def test_a_new_session_is_never_one_signed_out(self):
        # A cookie is made from the name, the second it runs out and the password, so a sign-in in
        # the second of a sign-out (here or on another server) was handed the cookie just ended.
        login = x.Handler.app["login"]
        clock = mock.Mock()
        clock.time.return_value = 1_900_000_100.5
        with mock.patch.object(x, "time", clock):
            first = login.make_session("owner")
            self.assertEqual(login.end_session(f"{x.SESSION_COOKIE}={first}"), "owner")
            again = login.make_session("owner")
            self.assertEqual(int(again.split(".")[1]), int(first.split(".")[1]) + 1)
            self.assertEqual(login.check_session(f"{x.SESSION_COOKIE}={again}"), "owner")
            # Signed out by the hub instead, the next one is new again.
            self.write_list({**self.read_list(), self.hash_of(again): int(again.split(".")[1])})
            third = login.make_session("owner")
            self.assertNotIn(third, (first, again))
            self.assertEqual(login.check_session(f"{x.SESSION_COOKIE}={third}"), "owner")

    def test_servers_signing_out_at_once_keep_every_sign_out(self):
        # The servers share one file: written without a lock, of two signing out together, one
        # would undo the other.
        go = os.path.join(self.folder, "go")
        script = ("import os, sys, time\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "import xrninsta\n"
                  "out = xrninsta.SignedOut(sys.argv[2])\n"
                  "while not os.path.exists(sys.argv[4]): time.sleep(0.001)\n"
                  "for i in range(30): out.add(f'{sys.argv[3]}-{i}.4000000000.x')\n")
        here_dir = os.path.dirname(os.path.abspath(x.__file__))
        others = [subprocess.Popen([sys.executable, "-c", script, here_dir, self.shared_list(), f"server{n}", go])
                  for n in range(3)]
        time.sleep(0.3)
        open(go, "w").close()

        def here(n):
            for i in range(30):
                x.Handler.app["login"].signed_out.add(f"here{n}-{i}.4000000000.x")

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(here, range(2)))
        for other in others:
            self.assertEqual(other.wait(timeout=60), 0)
        os.remove(go)
        listed = self.read_list()
        for who in ("server0", "server1", "server2", "here0", "here1"):
            self.assertEqual(sum(self.hash_of(f"{who}-{i}.4000000000.x") in listed for i in range(30)), 30, who)
        self.write_list({k: v for k, v in listed.items() if v != 4_000_000_000})

    def test_a_sign_out_drops_the_ones_that_have_run_out(self):
        self.write_list({"a" * 64: int(time.time()) - 5, "b" * 64: int(time.time()) + 600})
        mine = self.a_cookie(int(time.time()) + 8400)
        x.Handler.app["login"].end_session(f"{x.SESSION_COOKIE}={mine}")
        listed = self.read_list()
        self.assertNotIn("a" * 64, listed)
        self.assertIn("b" * 64, listed)
        self.assertIn(self.hash_of(mine), listed)

    def test_the_status_says_how_the_cover_bot_is_doing(self):
        covers = x.Covers(self.store, FakeTelegram(), CHAT, os.path.join(self.folder, "covers"), SYDNEY)
        x.Handler.app["covers"] = (covers, x.covers_poller(self.store, covers))
        self.addCleanup(x.Handler.app.pop, "covers")
        covers.note("missing", "no Day_295.png")
        code, doc = self.call("GET", "/api/status")
        self.assertEqual((code, doc["state"]), (200, "off"))
        self.assertEqual((doc["covers"]["send"]["state"], doc["covers"]["bot"]["state"]), ("missing", "starting"))

    def test_the_page_and_nothing_outside_web(self):
        self.assertEqual(self.call("GET", "/xrninsta/", user=None)[0], 200)
        for path in ("/../xrninsta.py", "/xrninsta.py", "/.hidden.html"):
            self.assertEqual(self.call("GET", path, user=None)[0], 404, path)

    def test_the_page_gets_its_font(self):
        # MiSans comes with the page, as in every xrn app: served as a font, and let through the CSP.
        font = b"wOF2" + bytes(range(256))
        with open(os.path.join(self.folder, "web", "MiSansLatinVF.woff2"), "wb") as f:
            f.write(font)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/xrninsta/MiSansLatinVF.woff2")
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type"), r.read()), (200, "font/woff2", font))
        c.close()
        self.assertIn("font-src 'self'", r.getheader("Content-Security-Policy").split("; "))

    # ---- device logins (DeviceTokens) ---------------------------------------------------------

    def tokens_path(self):
        return os.path.join(os.path.dirname(x.Handler.app["login"].users_path), "device-tokens.json")

    def write_tokens(self, entries):
        """device-tokens.json as the server's owner writes it: a new file in the old one's place."""
        with open(self.tokens_path() + ".test", "w", encoding="utf-8") as f:
            f.write(entries if isinstance(entries, str) else json.dumps({"tokens": entries}))
        os.replace(self.tokens_path() + ".test", self.tokens_path())

    @staticmethod
    def a_token(services=("xrninsta",), user="owner"):
        """(a device token, its entry in the file)."""
        token_id = os.urandom(4).hex()
        secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        entry = {"id": token_id, "user": user, "device": f"device {token_id}", "services": list(services),
                 "sha256": hashlib.sha256(secret.encode()).hexdigest(), "created": 1764115200}
        return f"xrndev_{token_id}_{secret}", entry

    def device_call(self, auth, addr, method="GET", path="/api/data", body=b"", **headers):
        """(status, body, headers) of a request sent with Authorization: [auth], from [addr]."""
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers={"Authorization": auth, "X-Forwarded-For": addr, **headers})
        res = conn.getresponse()
        answer = res.status, res.read(), res
        conn.close()
        return answer

    def bearer(self, token, addr, **kw):
        return self.device_call(f"Bearer {token}", addr, **kw)[0]

    def with_password(self, addr, **kw):
        return self.device_call("Basic " + base64.b64encode(b"owner:pw").decode(), addr, **kw)[0]

    def test_a_device_token_signs_in_as_its_user(self):
        token, entry = self.a_token()
        self.write_tokens([entry])
        status, body, _ = self.device_call(f"Bearer {token}", "198.18.7.1", path="/xrninsta/api/data")
        self.assertEqual(200, status)
        self.assertEqual(self.device_call("Basic " + base64.b64encode(b"owner:pw").decode(), "198.18.7.1")[1], body, "owner's, as the password reads them")

    def test_a_token_for_other_services_is_refused_and_guessed_nothing(self):
        token, entry = self.a_token(services=("xrnrule", "xrnnote"))
        self.write_tokens([entry])
        addr = "198.18.7.2"
        for _ in range(x.FAIL_LIMIT + 2):
            status, body, res = self.device_call(f"Bearer {token}", addr)
            self.assertEqual(403, status)
            self.assertIn(b"xrninsta", body)
            self.assertIsNone(res.getheader("WWW-Authenticate"))
        self.assertEqual(200, self.with_password(addr), "not locked out: it guessed nothing")

    def test_a_wrong_token_is_a_wrong_login(self):
        token, entry = self.a_token()
        self.write_tokens([entry])
        addr = "198.18.7.3"
        wrong = [f"{token[:16]}{self.a_token()[0][16:]}",  # its id, another secret
                 "xrnhub_" + token[7:],  # another kind of token
                 "not-a-token"]
        wrong += [self.a_token()[0] for _ in range(x.FAIL_LIMIT)]  # ids that aren't there
        for guess in wrong[:x.FAIL_LIMIT]:  # each a new one, not one wrong login sent again
            self.assertEqual(401, self.bearer(guess, addr))
        self.assertEqual(429, self.bearer(token, addr), "locked out, the right token too, as with a password")
        self.assertEqual(200, self.bearer(token, "198.18.7.4"))

    def test_a_token_signs_in_only_the_owner_here_as_the_password_does(self):
        token, entry = self.a_token(user="guest")
        self.write_tokens([entry])
        self.assertEqual(403, self.bearer(token, "198.18.7.13"))

    def test_a_revoked_token_is_refused_from_its_next_request(self):
        token, entry = self.a_token()
        other, kept = self.a_token()
        self.write_tokens([entry, kept])
        self.assertEqual(200, self.bearer(token, "198.18.7.5"))
        self.write_tokens([kept])  # revoked, and nothing restarted
        self.assertEqual(401, self.bearer(token, "198.18.7.5"))
        self.assertEqual(200, self.bearer(other, "198.18.7.5"))

    def test_a_tokens_user_must_be_in_the_users_file(self):
        import bcrypt

        token, entry = self.a_token(user="sam")
        self.write_tokens([entry])
        self.assertEqual(401, self.bearer(token, "198.18.7.6"), "no sam in the users file")
        with open(x.Handler.app["login"].users_path, encoding="utf-8") as f:
            before = f.read()
        try:
            with open(x.Handler.app["login"].users_path, "a", encoding="utf-8") as f:
                f.write("sam:" + bcrypt.hashpw(b"other", bcrypt.gensalt(4)).decode() + "\n")
            self.assertEqual(403, self.bearer(token, "198.18.7.6"), "signed in, and not the owner, the only one let in here")
        finally:
            with open(x.Handler.app["login"].users_path + ".test", "w", encoding="utf-8") as f:
                f.write(before)
            os.replace(x.Handler.app["login"].users_path + ".test", x.Handler.app["login"].users_path)
        self.assertEqual(401, self.bearer(token, "198.18.7.6"), "taken out of it, sam's tokens end")

    def test_a_tokens_file_that_cant_be_read_lets_no_token_in_and_the_password_still_works(self):
        token, entry = self.a_token()
        addr = "198.18.7.7"
        broken = ("{not json", '{"tokens": {}}', "[1]")
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            for doc in broken:
                self.write_tokens(doc)
                self.assertEqual(401, self.bearer(token, addr))
                self.assertEqual(401, self.bearer(token, addr))
                self.assertEqual(200, self.with_password(addr))
        self.assertEqual(len(broken), log.getvalue().count("can't be read, so no device token signs in"),
                         "a line for each, not for each request")
        self.write_tokens([entry])
        self.assertEqual(200, self.bearer(token, "198.18.7.8"))
        os.remove(self.tokens_path())
        self.assertEqual(401, self.bearer(token, "198.18.7.8"), "no file, no tokens")

    def test_an_entry_not_as_it_should_be_signs_no_one_in(self):
        token, entry = self.a_token()
        for bad in (dict(entry, sha256="\u00e9" * 64), dict(entry, sha256=entry["sha256"].upper()),
                    dict(entry, user="../owner"), dict(entry, user=["owner"]), dict(entry, services="xrninsta"),
                    {k: v for k, v in entry.items() if k != "services"}):
            self.write_tokens([bad, "not an entry"])
            self.assertEqual(401, self.bearer(token, "198.18.7.9"), bad)

    def test_a_token_cant_become_a_sign_in_cookie(self):
        token, entry = self.a_token()
        self.write_tokens([entry])
        status, _, res = self.device_call(f"Bearer {token}", "198.18.7.10", method="POST", path="/session",
                                          **{"X-Xrn-Client": "web"})
        self.assertEqual(401, status)
        self.assertIsNone(res.getheader("Set-Cookie"))
        self.assertEqual(200, self.bearer(token, "198.18.7.10"), "and it still signs in itself")

    def test_when_a_device_was_last_here_is_written_down_now_and_then(self):
        token, entry = self.a_token()
        gone, revoked = self.a_token()
        self.write_tokens([entry, revoked])
        seen = os.path.join(os.path.dirname(x.Handler.app["login"].users_path), "device-seen", "xrninsta.json")

        def written():
            st = os.stat(seen)
            with open(seen, encoding="utf-8") as f:
                return (st.st_ino, st.st_mtime_ns), json.load(f), st.st_mode & 0o777

        start = int(time.time())
        self.assertEqual(200, self.bearer(gone, "198.18.7.11"))
        self.assertEqual(200, self.bearer(token, "198.18.7.11"))
        stamp, doc, mode = written()
        self.assertGreaterEqual(doc[entry["id"]], start)
        self.assertIn(revoked["id"], doc)
        self.assertEqual(0o600, mode)
        for _ in range(3):
            self.assertEqual(200, self.bearer(token, "198.18.7.11"))
        self.assertEqual(stamp, written()[0], "not written again within DEVICE_SEEN_S")
        self.write_tokens([entry])
        with unittest.mock.patch.object(x, "DEVICE_SEEN_S", 0):
            self.assertEqual(200, self.bearer(token, "198.18.7.11"))
        stamp2, doc, _ = written()
        self.assertNotEqual(stamp, stamp2)
        self.assertEqual([entry["id"]], list(doc), "a revoked token's time goes with it")
        with open(seen, encoding="utf-8") as f:
            self.assertNotIn(token[16:], f.read())

    def test_a_token_never_reaches_the_log(self):
        token, entry = self.a_token()
        self.write_tokens([entry])
        wrong = self.a_token()[0]
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertEqual(200, self.bearer(token, "198.18.7.12"))
            self.assertEqual(401, self.bearer(wrong, "198.18.7.12"))
            time.sleep(0.05)
        self.assertIn("/api/data", log.getvalue())
        for secret in (token[16:], wrong[16:]):
            self.assertNotIn(secret, log.getvalue())


if __name__ == "__main__":
    unittest.main()
