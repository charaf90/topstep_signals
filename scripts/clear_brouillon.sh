#!/usr/bin/env bash
# =============================================================================
#  clear_brouillon.sh — Vide le travail d'essai et ramène le projet à l'ÉTAT DE BASE.
#
#  Modèle : tout travail jetable vit dans brouillon/ (gitignoré). La base
#  (live + outillage + docs) n'est jamais touchée par les essais. « Vider » =
#  pure opération filesystem, AUCUNE commande git → aucun risque de casser la base.
#
#  Purge (choix utilisateur) :
#    1. brouillon/  → contenu d'essai supprimé, scaffold conservé
#                     (README.md, __init__.py, strategies/__init__.py, scripts/, notes/)
#    2. output/     → contenu purgé (résultats de backtest régénérables)
#    3. mémoire     → fichiers supprimés, MEMORY.md remis à un index vide
#
#  NE TOUCHE JAMAIS : strategies/ (wrappers live), core/, broker/, config.py,
#  state/, logs/, data/, .env — c'est la BASE.
#
#  Usage :
#    bash scripts/clear_brouillon.sh             # vide
#    bash scripts/clear_brouillon.sh --dry-run   # prévisualise sans rien supprimer
# =============================================================================
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]] && DRY_RUN=1
FIND_ACTION="-delete"
[[ $DRY_RUN -eq 1 ]] && FIND_ACTION="-print"

# --- Garde-fou : racine du dépôt attendue ---
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" || ! -f "$REPO_ROOT/live.py" || ! -d "$REPO_ROOT/broker" ]]; then
  echo "❌ Racine de dépôt inattendue (live.py/broker introuvables). Abandon." >&2
  exit 1
fi
cd "$REPO_ROOT"

MEMDIR="$HOME/.claude/projects/-home-charaf-topstep-signals/memory"

echo "════════════════════════════════════════════════════════════"
echo "  VIDAGE BROUILLON → état de base — racine : $REPO_ROOT"
[[ $DRY_RUN -eq 1 ]] && echo "  MODE DRY-RUN (rien n'est supprimé ; liste ci-dessous)"
echo "════════════════════════════════════════════════════════════"

# --- 1. brouillon/ : contenu d'essai (préserve le scaffold) ---
echo "1. brouillon/ — fichiers d'essai supprimés (scaffold conservé) :"
if [[ -d brouillon ]]; then
  find brouillon -type f \
    ! -path 'brouillon/README.md' \
    ! -path 'brouillon/__init__.py' \
    ! -path 'brouillon/strategies/__init__.py' \
    ! -path 'brouillon/scripts/.gitkeep' \
    ! -path 'brouillon/notes/.gitkeep' \
    $FIND_ACTION | sed 's/^/   /' || true
  if [[ $DRY_RUN -eq 0 ]]; then
    find brouillon -mindepth 1 -type d -empty -delete 2>/dev/null || true
  fi
else
  echo "   (brouillon/ absent — rien à faire)"
fi

# --- 2. output/ : contenu régénérable ---
echo "2. output/ — contenu purgé :"
if [[ -d output ]]; then
  find output -mindepth 1 $FIND_ACTION 2>/dev/null | sed 's/^/   /' | head -20 || true
  [[ $DRY_RUN -eq 0 ]] && true
else
  echo "   (output/ absent)"
fi

# --- 3. mémoire persistante : reset à un index vide ---
echo "3. mémoire ($MEMDIR) — reset à un index vide :"
if [[ -d "$MEMDIR" ]]; then
  find "$MEMDIR" -mindepth 1 -name '*.md' ! -name 'MEMORY.md' $FIND_ACTION 2>/dev/null | sed 's/^/   /' || true
  if [[ $DRY_RUN -eq 0 ]]; then
    cat > "$MEMDIR/MEMORY.md" <<'EOF'
# MEMORY INDEX

<!-- Index de la mémoire persistante. Une ligne par fait : `- [Titre](fichier.md) — accroche`.
     État de base : aucun historique. À remplir au fil des essais. -->
EOF
  else
    echo "   [dry-run] MEMORY.md serait réécrit (index vide)"
  fi
else
  echo "   (dossier mémoire absent)"
fi

echo "════════════════════════════════════════════════════════════"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  DRY-RUN terminé — relancer sans --dry-run pour appliquer."
else
  echo "  ✅ Brouillon vidé. Base intacte (strategies/ core/ broker/ config.py state/ logs/ data/)."
fi
echo "════════════════════════════════════════════════════════════"
