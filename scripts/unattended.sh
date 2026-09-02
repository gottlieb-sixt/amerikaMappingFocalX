#!/usr/bin/env bash
# Startet scripts/batch.py so, dass es ohne Aufsicht weiterläuft.
#
# Zwei verschiedene Probleme, zwei Werkzeuge:
#   tmux       — die Sitzung überlebt das Schließen des Terminals und den
#                Neustart von Cursor. Der gesperrte Bildschirm allein stoppt
#                ohnehin nichts.
#   caffeinate — verhindert den Ruhezustand. DER würde den Lauf anhalten
#                (Netzverbindungen brechen, FocalX-Wartezeiten laufen ins Leere).
#                Achtung: Gegen das ZUKLAPPEN des Deckels hilft auch caffeinate
#                nicht — Deckel offen lassen, Netzteil dran.
#
#   scripts/unattended.sh fl500 all --limit 25
#   tmux attach -t batch-fl500        # zusehen
#   Strg-B, dann D                    # verlassen, läuft weiter
#   tmux kill-session -t batch-fl500  # sauber abbrechen (nach dem laufenden Auto)
set -euo pipefail
cd "$(dirname "$0")/.."

RUN="${1:?Run-ID fehlt, z. B.: scripts/unattended.sh fl500 all --limit 25}"
shift
SESSION="batch-$RUN"
LOG="data/runs/$RUN/logs/batch-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Sitzung $SESSION läuft schon. Zusehen: tmux attach -t $SESSION"
  exit 1
fi

# Argumente einzeln quoten — tmux bekommt einen String, keine Argumentliste.
CMD="caffeinate -is python3 -u scripts/batch.py --run $(printf '%q' "$RUN")"
for a in "$@"; do CMD+=" $(printf '%q' "$a")"; done
CMD+=" 2>&1 | tee -a $(printf '%q' "$LOG")"

tmux new-session -d -s "$SESSION" "$CMD"
echo "Gestartet in tmux-Sitzung $SESSION"
echo "  Log:       $LOG"
echo "  Zusehen:   tmux attach -t $SESSION     (verlassen: Strg-B, dann D)"
echo "  Abbrechen: tmux kill-session -t $SESSION"
