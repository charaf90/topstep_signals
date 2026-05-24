#!/usr/bin/env bash
# Lance le dashboard Streamlit en tmux always-on.
#
# Usage :
#   ./tools/launch_dashboard.sh         # démarre dans tmux session "dashboard"
#   ./tools/launch_dashboard.sh stop    # tue la session
#   ./tools/launch_dashboard.sh status  # affiche l'état
#
# Accès iPhone via Tailscale :
#   http://topstep-pc:8501  (hostname Tailscale)
#   http://100.X.X.X:8501   (IP Tailscale du PC)

set -e

cd "$(dirname "$0")/.."

SESSION="dashboard"
PORT=8501

case "${1:-start}" in
    start)
        if tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "✓ Session '$SESSION' déjà active (port $PORT)."
            echo "  Pour voir les logs : tmux attach -t $SESSION"
            exit 0
        fi
        tmux new -d -s "$SESSION" "streamlit run tools/dashboard.py \
            --server.port $PORT \
            --server.address 0.0.0.0 \
            --server.headless true"
        sleep 1
        echo "✓ Dashboard démarré dans tmux session '$SESSION' (port $PORT)"
        echo "  Local         : http://localhost:$PORT"
        echo "  Tailscale     : http://topstep-pc:$PORT (à confirmer hostname)"
        echo "  Logs          : tmux attach -t $SESSION"
        echo "  Stop          : ./tools/launch_dashboard.sh stop"
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
