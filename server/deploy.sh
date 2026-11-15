#!/bin/sh
# Installs or updates xrninsta on a home server reached over ssh (Termux with runit and Tailscale
# Funnel), next to the xrn sync services. Safe to run again: an update replaces the program and its
# page and restarts it, and leaves the songs and bot.json alone.
#
#     server/deploy.sh [ssh-host]      (default: homeserver)
set -eu

HOST=${1:-homeserver}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)

echo "Testing first..."
if ! (cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_xrninsta >/dev/null 2>&1); then
    echo "The tests failed, so nothing was changed. Run them for details:" >&2
    echo "    cd server && python3 -m unittest test_xrninsta" >&2
    exit 1
fi

echo "Copying it to $HOST..."
# The program and its page go together, never one without the other.
tar czf - --exclude=__pycache__ -C "$HERE" xrninsta.py -C "$REPO" web | ssh "$HOST" '
    set -e
    mkdir -p ~/xrninsta/data ~/.config/xrninsta
    chmod 700 ~/xrninsta ~/xrninsta/data ~/.config/xrninsta
    cd ~/xrninsta
    rm -rf web
    tar xzf -
    chmod 755 xrninsta.py
'

echo "Setting up the service..."
ssh "$HOST" '
    set -e
    S=$PREFIX/var/service
    export SVDIR=$S
    if [ -d "$S/xrninsta" ]; then
        sv restart xrninsta
        exit 0
    fi
    # Built under a dot name, which runit ignores, so it never starts half written.
    T=$S/.xrninsta.new
    rm -rf "$T"
    mkdir -p "$T/log"
    cat > "$T/run" <<EOS
#!$PREFIX/bin/sh
# xrninsta: the song bot and its page (see ~/xrninsta and the home-server README).
# Login is ~/.config/xrn-sync/users, shared with the sync services; the bot token is bot.json.
exec python $HOME/xrninsta/xrninsta.py \\
  --port 5239 --data $HOME/xrninsta/data \\
  --users $HOME/.config/xrn-sync/users --config $HOME/.config/xrninsta/bot.json 2>&1
EOS
    cp "$S/xrnwallet-sync/log/run" "$T/log/run"
    chmod 755 "$T/run" "$T/log/run"
    chmod 700 "$T" "$T/log"
    mv "$T" "$S/xrninsta"
'

echo "Waiting for it to answer..."
tries=0
until [ "$(ssh "$HOST" 'curl -s -m 3 http://127.0.0.1:5239/health' || true)" = ok ]; do
    tries=$((tries + 1))
    if [ "$tries" -ge 8 ]; then
        echo "It did not start. Its log:" >&2
        ssh "$HOST" 'tail -20 $PREFIX/var/log/sv/xrninsta/current' >&2 || true
        exit 1
    fi
    sleep 2
done

# `funnel`, never `serve`: serve rewrites the port as tailnet-only and takes the calendar offline.
echo "Publishing /xrninsta..."
ssh "$HOST" 'tailscale funnel --bg --set-path /xrninsta http://127.0.0.1:5239 >/dev/null; tailscale funnel status'

if ssh "$HOST" 'test -s ~/.config/xrninsta/bot.json'; then
    ssh "$HOST" 'python ~/xrninsta/xrninsta.py check --config ~/.config/xrninsta/bot.json' || true
else
    echo "No ~/.config/xrninsta/bot.json yet: the page runs, the bot waits for its token (see the README)."
fi

echo "Done."
