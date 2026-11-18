<p align="center">
  <img src="web/icon.svg" width="72" height="72" alt="">
</p>

<h1 align="center">xrninsta</h1>

The song-of-the-day list, kept by a Telegram bot. Send it a song (title, artist, genre, one per
line) and it becomes the next day's; it warns about a song that's already there, keeps an OnHold
list of songs for later, searches by title, artist, genre or "day 26", and shows leaderboards. A
page on the home server looks like the sheet did, with its Dashboard, Songs and OnHold tabs, and
lets you fix or delete anything.

It used to be a Google Apps Script bound to a Google Sheet. That script is kept in
[apps-script/code.gs](apps-script/code.gs) until the switch is done. This is the same bot, word for
word in Vietnamese, with the songs in an SQLite file on the home server and nothing at Google.

## How it differs from the script

- **The bot asks Telegram for messages** (long polling) instead of Telegram posting to a web app, so
  the bot needs no public address and no webhook key.
- **The page replaces the sheet, and looks like it.** The same tabs along the bottom, the same
  fills (green headers, yellow cells to change, pink artist rows, each title in its genre's
  colour) and the same columns, full width, with the top rows frozen while the rest scrolls:
  - Dashboard: Genre Filter and Sort Day, each artist's songs under their name, and the Summary,
    Artist and Genre counts beside them. Double-click a song to fix it in Songs.
  - Songs: Title, Artist(s), Day No and Genre, with a genre filter and Sort Day (newest first, or
    oldest first as the sheet had them). The yellow row under the header adds a song;
    double-click a row (double-tap on a phone) to fix or delete it, or press its Fix.
  - OnHold: Title, Artist(s) and Note, with a row to add to, and Add as a song, Fix and Delete.

  One click on a row only picks it out, so reading down a list doesn't open a form in it. **Find**,
  at the top right (or `/`, or Ctrl+F), narrows whichever tab is shown: the Dashboard's artists and
  its counts, Songs by title or artist, and OnHold by title, artist or note, whatever the accents.

  It's at `/xrninsta`, for the server's owner, signed in with the sync password. On a phone the
  tables fit the screen: the rows wrap, the Note column folds under the title, and the rows to add
  to open from a button.
- **Records have ids, not rows.** An imported song keeps its sheet row as its id, so the buttons on
  the script's old messages (Undo, the OnHold list) still find the right song.
- **The matching is the script's, exactly.** Normalising, the duplicate checks and the search scores
  were run side by side with code.gs on the real list: 57,000 comparisons, none different.

## Moving over from the script

1. Put the bot's token and chat id on the server. They're in the Apps Script project's settings,
   under Script properties (`BOT_TOKEN`, `ALLOWED_CHAT_ID`):

   ```sh
   ssh -t homeserver 'mkdir -p ~/.config/xrninsta && umask 077 &&
     read -rsp "Bot token: " t && echo && read -rp "Chat id: " c &&
     case $c in ""|*[!0-9-]*) echo "The chat id is a number; nothing was written." >&2; exit 1;; esac &&
     printf "{\"token\": \"%s\", \"chat\": \"%s\"}\n" "$t" "$c" > ~/.config/xrninsta/bot.json'
   ```

   The bot answers only that chat. Without one it answers no one, and its log names each chat it
   ignored.

2. Install it: `server/deploy.sh`. It ends with `check`, which names the bot and says Telegram
   still sends its messages to the Apps Script. Nothing has moved yet. It says which branch and
   commit it sends, and refuses uncommitted changes to `server/xrninsta.py` or `web/`
   (`--force` sends them anyway).
3. Download the sheet as .xlsx (File, Download, Microsoft Excel) and import it:

   ```sh
   scp ~/Downloads/<the export>.xlsx homeserver:~/xrninsta/import.xlsx
   ssh homeserver 'python ~/xrninsta/xrninsta.py import ~/xrninsta/import.xlsx --data ~/xrninsta/data && rm ~/xrninsta/import.xlsx'
   ```

4. Switch: `ssh homeserver 'python ~/xrninsta/xrninsta.py webhook-off --config ~/.config/xrninsta/bot.json'`.
   Telegram stops posting to the script and the bot here takes the messages, those waiting
   included, within a minute.
5. In the Apps Script editor, delete the trigger that refreshes the Dashboard every 10 minutes, and
   don't run `setupBot` or `forceSetWebhook` again: either would point Telegram back at the script.

Add songs only through the bot between steps 3 and 4 if you're willing to import again; the import
won't run over songs that are already there without `--replace`.

## The daily cover

A second bot, the Daily Story Bot, used to be its own Apps Script
([apps-script/coverbot.gs](apps-script/coverbot.gs)) reading a Google Drive folder of covers. Each
cover is `Day_<n>.png`, Day 263 being 18 Oct 2026. xrninsta runs that bot too, with its own token,
and keeps the covers on the server, in `~/xrninsta/data/covers`.

- At **2am Sydney time**, daylight saving or not, it sends that day's cover to its chat with the
  caption "Day 295's cover". `/sendnow` sends it at once, and `/start` says hello, as before.
- It notes the day it last sent. A restart never sends a cover twice, and a server that was off at
  2am sends that day's cover once it's back (never an older one).
- A missing cover is said once in the chat, with its name.
- Covers stay on the server once sent. The script moved the last five to the bin.
- The covers are left out of the server's backups. A year of them is 38 MB, and backups are kept
  for 60 days. Keep the zip.

### Moving it over

1. Deploy (`server/deploy.sh`) so the server has the commands below.
2. Copy the covers. A zip of the Drive folder works as it is, and so does a folder of PNGs:

   ```sh
   scp output_stories.zip homeserver:~/xrninsta/covers.zip
   ssh homeserver 'python ~/xrninsta/xrninsta.py covers ~/xrninsta/covers.zip --data ~/xrninsta/data && rm ~/xrninsta/covers.zip'
   ```

   It says how far the covers go and which are missing. Add more the same way. A cover with a name
   already there replaces it.
3. Give it the bot. The token and chat are in the cover script's Project Settings, under Script
   properties (`BOT_TOKEN`, `ALLOWED_CHAT_ID`). The command asks for both, so the token never goes
   on a command line:

   ```sh
   ssh -t homeserver 'python ~/xrninsta/xrninsta.py covers-bot --config ~/.config/xrninsta/bot.json'
   ssh homeserver 'SVDIR=$PREFIX/var/service sv restart xrninsta'
   ```

   Started after 2am, it takes that day's cover as sent by the script, and starts with tomorrow's.
4. Switch: `ssh homeserver 'python ~/xrninsta/xrninsta.py webhook-off --covers --config ~/.config/xrninsta/bot.json'`.
   `check --covers` (add `--data ~/xrninsta/data` for the covers) shows where its messages go.
5. In the cover script's Triggers, delete the daily trigger for `sendDailyStory` before the next
   2am, or both send that night's cover. Don't run `forceSetWebhook` again.

## Running it

Python 3.11 or newer, plus `bcrypt`.

```sh
python3 server/xrninsta.py --port 5239 --data ./data --users <htpasswd file> --config bot.json
```

Open `http://127.0.0.1:5239/xrninsta/` and sign in as the users file's first user. Without a token
in bot.json the page runs and the bot doesn't.

## Tests

```sh
cd server && python3 -m unittest test_xrninsta
node --test test
```

The first drive the bot against a fake Telegram, import a made-up sheet, and use the page's API
over HTTP; the second check the page's own arithmetic (`web/model.js`: the Dashboard's groups and
counts, and the Songs tab's find and order). Every song in them is made up; never add real ones, or an export of the sheet, to this repo.
