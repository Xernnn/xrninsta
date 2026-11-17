"""xrninsta: the bot against a fake Telegram, the sheet import, and the page's API over HTTP.

Every song and name here is made up. Run: python3 -m unittest test_xrninsta (from server/).
"""
import base64
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest import mock

import xrninsta as x

CHAT, USER = "100", 7


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.updates = []

    def call(self, method, payload=None, timeout=15):
        self.calls.append((method, payload or {}))
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else {"ok": True, "result": []}
        return {"ok": True, "result": {}}

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
        self.say("/search@instasong_bot remember")
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

    def test_a_hold_becomes_a_song_in_one_go(self):
        status, hold = self.call("POST", "/api/holds", {"title": "Later", "artists": "", "note": "line one\nline two"})
        self.assertEqual((status, hold["note"]), (201, "line one\nline two"))
        status, song = self.call("POST", "/api/songs", {"title": "Later", "artists": "Someone", "genre": "MISC", "fromHold": hold["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(self.store.holds(), [])
        self.assertEqual(self.call("POST", "/api/songs", {"title": "L", "artists": "S", "genre": "MISC", "fromHold": hold["id"]})[0], 400)

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

    def test_the_page_and_nothing_outside_web(self):
        self.assertEqual(self.call("GET", "/xrninsta/", user=None)[0], 200)
        for path in ("/../xrninsta.py", "/xrninsta.py", "/.hidden.html"):
            self.assertEqual(self.call("GET", path, user=None)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
