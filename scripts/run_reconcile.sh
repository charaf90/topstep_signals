#!/usr/bin/env bash
# Wrapper cron pour scripts/reconcile_daily.py
#
# Cron ne charge PAS l'env du shell utilisateur (.env, miniconda dans PATH).
# Ce wrapper :
#   1. cd au répertoire projet
#   2. source le .env (PROJECTX_USERNAME, PROJECTX_API_KEY, etc.)
#   3. injecte miniconda dans PATH (cron a un PATH minimal)
#   4. lance reconcile_daily avec --notify (Telegram si mismatch)
#   5. log dans logs/reconcile.log (append, rotation manuelle si > 10 MB)
#
# Test manuel :
#   ./scripts/run_reconcile.sh
#
# Crontab :
#   30 22 * * 1-5 /home/charaf/topstep_signals/scripts/run_reconcile.sh

set -e

cd /home/charaf/topstep_signals

# Charger .env si présent (export automatique)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . .env
    set +a
fi

# miniconda dans PATH (chemin standard utilisateur charaf)
export PATH="/home/charaf/miniconda3/bin:$PATH"

LOG="logs/reconcile.log"
mkdir -p logs

# Rotation simple : si log > 10 MB, on rename et on repart sur un fichier neuf
if [ -f "$LOG" ]; then
    size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 10485760 ]; then
        mv "$LOG" "${LOG}.$(date +%Y%m%d)"
    fi
fi

{
    echo
    echo "════════════════════════════════════════════════════════════"
    echo "  RECONCILE $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "════════════════════════════════════════════════════════════"
    python scripts/reconcile_daily.py --notify
    rc=$?
    echo "  Exit code: $rc"
} >> "$LOG" 2>&1

# Propage l'exit code à cron (utile pour MAILTO si configuré)
exit "${rc:-0}"
