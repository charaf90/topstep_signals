#!/usr/bin/env bash
# Lance le dashboard v3 (Dash) en tmux always-on sur port 8502.
#
# Cohabite avec le v2 (port 8501) — on peut comparer les deux pendant
# la phase de validation.
#
# Usage :
#   ./tools/launch_dashboard_v3.sh         # démarre dans tmux session "dash_v3"
#   ./tools/launch_dashboard_v3.sh stop    # tue la session
#   ./tools/launch_dashboard_v3.sh status  # affiche l'état

set -e

cd "$(dirname "$0")/.."

SESSION="dash_v3"
PORT=8502

case "${1:-start}" in
    start)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' déjà active (port $PORT)."
            exit 0
        fi
        tmux new -d -s "$SESSION" "python -m tools.dashboard_v3.app"
        sleep 2
        echo "✓ Dashboard v3 démarré (tmux '$SESSION', port $PORT)"
        echo "  Local      : http://localhost:$PORT"
        echo "  Tailscale  : http://Katana17:$PORT  (iPhone)"
        echo "  Logs       : tmux attach -t $SESSION"
        echo "  Stop       : ./tools/launch_dashboard_v3.sh stop"
        ;;
    stop)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            tmux kill-session -t "$SESSION"
            echo "✓ Session '$SESSION' arrêtée."
        else
            echo "Aucune session '$SESSION' active."
        fi
        ;;
    status)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' active."
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
