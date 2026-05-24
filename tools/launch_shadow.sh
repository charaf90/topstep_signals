#!/usr/bin/env bash
# Lance le shadow runner en tmux always-on.
#
# Usage :
#   ./tools/launch_shadow.sh         # démarre dans tmux session "shadow"
#   ./tools/launch_shadow.sh stop    # tue la session
#   ./tools/launch_shadow.sh status  # affiche l'état
#
# Le shadow runner :
# - utilise le même WebSocket ProjectX que le live
# - applique la même logique de décision (OPR, Fib)
# - ne poste JAMAIS d'ordres réels (dry_run forcé)
# - écrit dans state/shadow_state.json et logs/shadow_events.log
# - utilise un PID lock séparé (state/shadow_daemon.pid)
#
# Comparaison ex-post avec le live :
#   python tools/shadow_vs_live.py

set -e

cd "$(dirname "$0")/.."

SESSION="shadow"

case "${1:-start}" in
    start)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' déjà active."
            echo "  Logs : tmux attach -t $SESSION   (Ctrl+B puis D pour détacher)"
            exit 0
        fi
        tmux new -d -s "$SESSION" "python shadow.py --daemon"
        sleep 2
        echo "✓ Shadow runner démarré (tmux session '$SESSION')"
        echo "  State    : state/shadow_state.json"
        echo "  Logs     : logs/shadow_events.log"
        echo "  Attach   : tmux attach -t $SESSION"
        echo "  Compare  : python tools/shadow_vs_live.py"
        echo "  Stop     : ./tools/launch_shadow.sh stop"
        ;;
    stop)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            tmux send-keys -t "$SESSION" C-c
            sleep 3
            tmux kill-session -t "$SESSION" 2>/dev/null || true
            echo "✓ Session '$SESSION' arrêtée."
        else
            echo "Aucune session '$SESSION' active."
        fi
        ;;
    status)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' active."
            echo
            tmux ls | grep "$SESSION"
        else
            echo "Aucune session '$SESSION' active."
        fi
        ;;
    *)
        echo "Usage: $0 [start|stop|status]" >&2
        exit 1
        ;;
esac
