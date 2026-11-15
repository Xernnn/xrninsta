<p align="center">
  <img src="web/icon.svg" width="72" height="72" alt="">
</p>

<h1 align="center">xrninsta</h1>

The song-of-the-day list, kept by a Telegram bot. Send it a song (title, artist, genre, one per
line) and it becomes the next day's; it warns about a song that's already there, keeps an OnHold
list of songs for later, searches by title, artist, genre or "day 26", and shows leaderboards. A
page on the home server shows everything by artist, as the sheet's Dashboard did, and lets you fix
or delete anything.

It used to be a Google Apps Script bound to a Google Sheet. That script is kept in
[apps-script/code.gs](apps-script/code.gs) until the switch is done. This is the same bot, word for
word in Vietnamese, with the songs in an SQLite file on the home server and nothing at Google.

## How it differs from the script

- **The bot asks Telegram for messages** (long polling) instead of Telegram posting to a web app, so
  the bot needs no public address and no webhook key.
- **The page replaces the sheet.** By artist (the Dashboard, with its genre filter, sort and
  summary), Songs (every song, find, add, fix, delete) and OnHold (add, fix, delete, or add as a
  song). It's at `/xrninsta`, for the server's owner, signed in with the sync password.
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
     printf "{\"token\": \"%s\", \"chat\": \"%s\"}\n" "$t" "$c" > ~/.config/xrninsta/bot.json'
   ```

2. Install it: `server/deploy.sh`. It ends with `check`, which names the bot and says Telegram
   still sends its messages to the Apps Script. Nothing has moved yet.
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
```

They drive the bot against a fake Telegram, import a made-up sheet, and use the page's API over
HTTP. Every song in them is made up; never add real ones, or an export of the sheet, to this repo.
