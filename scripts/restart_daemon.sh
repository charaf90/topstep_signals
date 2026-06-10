#!/usr/bin/env bash
# ==============================================================================
# restart_daemon.sh — procédure CANONIQUE de gestion du daemon live.
#
# Créé le 2026-06-10 après une série de restarts artisanaux ratés :
# commande fausse dans le runbook (python live.py sans --daemon --execute),
# pane tmux = python direct (traceback perdu au crash), aucune vérification
# post-démarrage, confusion session tmux ↔ process (le verrou PID de live.py
# rend les deux indépendants).
#
# Usage :
#   scripts/restart_daemon.sh status            # health-check complet (sans risque)
#   scripts/restart_daemon.sh stop              # arrêt propre (SIGTERM + attente)
#   scripts/restart_daemon.sh restart [--force] # restart standard
#
# Garde-fou : restart REFUSÉ pendant la session NY (09:25-16:05 NY, lun-ven)
# sauf --force. Un restart se fait DÉLIBÉRÉMENT, hors session.
#
# Le pane tmux exécute un SHELL puis la commande : en cas de crash du daemon,
# le traceback reste visible dans le scrollback (tmux attach -t topstep).
# Le verrou PID de live.py évince toute instance précédente → jamais de doublon.
# ==============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="topstep"
PID_FILE="$ROOT/state/live_daemon.pid"
# SEULE commande valide (cf. mémoire projet). Le tee est INDISPENSABLE :
# live.py n'a pas de FileHandler — sans lui, aucun logs/daemon_*.log n'est
# écrit (panne de diagnostic constatée au restart du 2026-06-10 20:01).
# tee = fichier ET pane tmux (traceback visible des deux côtés).
CMD='python live.py --daemon --execute 2>&1 | tee -a "logs/daemon_$(date +%Y%m%d).log"'
START_TIMEOUT=60                          # s max pour valider le démarrage

# ── Helpers ───────────────────────────────────────────────────────────────────

daemon_pid() {
    # PID du daemon si vivant et cmdline cohérente, sinon vide.
    [ -f "$PID_FILE" ] || return 0
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 0
    if ps -p "$pid" -o cmd= 2>/dev/null | grep -q "live.py"; then
        echo "$pid"
    fi
}

newest_daemon_log() {
    ls -t "$ROOT"/logs/daemon_*.log 2>/dev/null | head -1
}

in_ny_session() {
    # 0 (vrai) si lun-ven 09:25-16:05 heure New York.
    local dow hm
    dow="$(TZ=America/New_York date +%u)"
    hm="$(TZ=America/New_York date +%H%M)"
    [ "$dow" -le 5 ] && [ "$hm" -ge 0925 ] && [ "$hm" -le 1605 ]
}

status() {
    echo "═══ STATUS daemon live — $(date '+%Y-%m-%d %H:%M:%S %Z') ═══"
    local pid log
    pid="$(daemon_pid)"
    if [ -n "$pid" ]; then
        echo "✅ Process : PID $pid vivant — $(ps -p "$pid" -o etime=,rss= | awk '{printf "uptime %s, RSS %.0f MB", $1, $2/1024}')"
    else
        echo "❌ Process : AUCUN daemon vivant (pid file : ${PID_FILE})"
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "✅ Tmux    : session '$SESSION' présente (informationnel — le process fait foi)"
    else
        echo "⚠️  Tmux    : pas de session '$SESSION' (le daemon peut tourner sans — vérifier le process ci-dessus)"
    fi
    log="$(newest_daemon_log)"
    if [ -n "$log" ]; then
        local age
        age=$(( $(date +%s) - $(stat -c %Y "$log") ))
        echo "📄 Log     : $(basename "$log") — dernière écriture il y a ${age}s"
        if [ "$age" -gt 1200 ] && [ -n "$pid" ]; then
            echo "⚠️           >20 min sans écriture avec un process vivant → daemon possiblement gelé"
        fi
        if [ -n "$pid" ] && [ -e "/proc/$pid" ]; then
            local proc_start log_mtime
            proc_start="$(stat -c %Y "/proc/$pid" 2>/dev/null || echo 0)"
            log_mtime="$(stat -c %Y "$log")"
            if [ "$log_mtime" -lt "$proc_start" ]; then
                echo "⚠️           Le daemon courant n'écrit AUCUN fichier log (lancé sans tee ?)"
                echo "             → sortie uniquement dans le pane tmux. Restart via ce script pour corriger."
            fi
        fi
        grep "Risk :" "$log" | tail -1 | sed 's/^/💰 /' || true
        echo "── 3 dernières lignes :"
        tail -3 "$log" | sed 's/^/   /'
    else
        echo "❌ Log     : aucun logs/daemon_*.log"
    fi
    echo "── 3 derniers événements trading :"
    tail -3 "$ROOT/logs/trading_events.log" 2>/dev/null | sed 's/^/   /' || echo "   (aucun)"
}

stop_daemon() {
    local pid
    pid="$(daemon_pid)"
    if [ -z "$pid" ]; then
        echo "Aucun daemon vivant — rien à arrêter."
        return 0
    fi
    echo "Arrêt du daemon PID $pid (SIGTERM)…"
    kill -TERM "$pid"
    for _ in $(seq 1 20); do
        sleep 0.5
        if ! ps -p "$pid" > /dev/null 2>&1; then
            echo "✅ Daemon arrêté proprement."
            return 0
        fi
    done
    echo "⚠️  Toujours vivant après 10 s — SIGKILL."
    kill -KILL "$pid" 2>/dev/null || true
}

restart() {
    local force="${1:-}"
    if in_ny_session && [ "$force" != "--force" ]; then
        echo "⛔ REFUS : session NY en cours ($(TZ=America/New_York date '+%H:%M NY'), lun-ven 09:25-16:05)."
        echo "   Un restart se fait DÉLIBÉRÉMENT hors session. Pour passer outre : restart --force"
        exit 1
    fi

    echo "▸ Restart daemon ($(TZ=America/New_York date '+%H:%M NY'))…"

    # Session tmux : on la recrée proprement. Tuer la session SIGHUP-era le
    # daemon courant ; le verrou PID de la nouvelle instance évincera de toute
    # façon tout survivant (SIGTERM→SIGKILL). Aucun doublon possible.
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION" -c "$ROOT"
    # Lancement via le shell du pane → en cas de crash, le traceback reste
    # lisible dans le scrollback (tmux attach -t topstep).
    tmux send-keys -t "$SESSION" "$CMD" Enter

    echo "▸ Attente du démarrage (max ${START_TIMEOUT}s)…"
    local old_pid new_pid waited=0
    old_pid="$(cat "$PID_FILE" 2>/dev/null || echo "")"
    while [ "$waited" -lt "$START_TIMEOUT" ]; do
        sleep 3; waited=$((waited + 3))
        new_pid="$(daemon_pid)"
        if [ -n "$new_pid" ] && [ "$new_pid" != "$old_pid" ]; then
            echo "✅ Nouveau daemon démarré (PID $new_pid) après ${waited}s."
            echo
            status
            return 0
        fi
    done
    echo "❌ ÉCHEC : pas de nouveau daemon après ${START_TIMEOUT}s. Inspection :"
    echo "   tmux attach -t $SESSION   (traceback dans le pane)"
    tmux capture-pane -t "$SESSION" -p | tail -15 | sed 's/^/   /'
    exit 1
}

# ── Main ─────────────────────────────────────────────────────────────────────

case "${1:-status}" in
    status)  status ;;
    stop)    stop_daemon ;;
    restart) restart "${2:-}" ;;
    *)       echo "Usage : $0 {status|stop|restart [--force]}" ; exit 1 ;;
esac
