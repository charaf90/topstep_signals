#!/usr/bin/env bash
# Lance le dashboard v4 (Dash) en tmux always-on sur port 8503.
#
# Cohabite avec le v3 (port 8502) pendant la phase de validation —
# une session de marché en double-écran avant de basculer.
#
# Usage :
#   ./tools/launch_dashboard_v4.sh         # démarre dans tmux session "dash_v4"
#   ./tools/launch_dashboard_v4.sh stop    # tue la session
#   ./tools/launch_dashboard_v4.sh status  # affiche l'état

set -e

cd "$(dirname "$0")/.."

SESSION="dash_v4"
PORT=8503

case "${1:-start}" in
    start)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' déjà active (port $PORT)."
            exit 0
        fi
        tmux new -d -s "$SESSION" "python -m tools.dashboard_v4.app"
        sleep 2
        echo "✓ Dashboard v4 démarré (tmux '$SESSION', port $PORT)"
        echo "  Local      : http://localhost:$PORT"
        echo "  Tailscale  : http://Katana17:$PORT  (iPhone)"
        echo "  Logs       : tmux attach -t $SESSION"
        echo "  Stop       : ./tools/launch_dashboard_v4.sh stop"
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
            echo "✓ Session '$SESSION' active (port $PORT)."
        else
            echo "✗ Session '$SESSION' inactive."
        fi
        ;;
    *)
        echo "Usage : $0 {start|stop|status}"
        exit 1
        ;;
esac
