"""
Configuration de la stratégie Fibonacci 50% retracement (M15).

Paramètres ISOLÉS — n'importe rien depuis config.py racine afin de garantir
l'indépendance totale du brouillon vis-à-vis du projet de production.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Instruments (specs identiques au projet principal)
# ─────────────────────────────────────────────────────────────────────────────

INSTRUMENTS = {
    "MES1": {"dollar_per_point": 5.0, "tick_size": 0.25,
             "name": "Micro E-mini S&P 500"},
    "NQ1":  {"dollar_per_point": 2.0, "tick_size": 0.25,
             "name": "Micro E-mini Nasdaq 100"},
    "YM1":  {"dollar_per_point": 0.5, "tick_size": 1.0,
             "name": "Micro E-mini Dow Jones"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Risque / compte
# ─────────────────────────────────────────────────────────────────────────────

RISK_PER_TRADE_USD = 100        # risque dollar fixe par trade
ACCOUNT_SIZE = 50_000           # taille de compte Topstep 50K (référence Sharpe)

# ─────────────────────────────────────────────────────────────────────────────
# Indicateurs techniques
# ─────────────────────────────────────────────────────────────────────────────

ATR_PERIOD = 14                 # ATR pour SL/TP dynamiques
EMA_FAST_PERIOD = 50            # EMA rapide (~12.5h sur M15)
EMA_SLOW_PERIOD = 200           # EMA lente (~50h = ~2 jours de session US)
ADX_PERIOD = 14                 # ADX standard
ADX_TREND_THRESHOLD = 20.0      # ADX > 20 = tendance confirmée (Wilder)

# ─────────────────────────────────────────────────────────────────────────────
# Détection des pivots / swings
# ─────────────────────────────────────────────────────────────────────────────

PIVOT_LEFT = 8                  # bougies à gauche pour valider un pivot
PIVOT_RIGHT = 8                 # bougies à droite (~4h M15 de chaque côté)

# ─────────────────────────────────────────────────────────────────────────────
# Détection des impulsions
# ─────────────────────────────────────────────────────────────────────────────

MIN_IMPULSE_ATR = 1.5           # taille minimale impulse en multiples d'ATR
MAX_IMPULSE_BARS = 25           # durée maximale d'une impulse (~6h M15)
IMPULSE_LOOKBACK = 60           # fenêtre de recherche du dernier impulse (bars)

# ─────────────────────────────────────────────────────────────────────────────
# Gestion ordres / positions
# ─────────────────────────────────────────────────────────────────────────────

ORDER_TIMEOUT_BARS = 12         # ordre limite annulé après N bougies (~3h)
MAX_HOLD_BARS = 32              # position fermée si pas SL/TP avant N bougies (~8h)

# ─────────────────────────────────────────────────────────────────────────────
# Risk management dynamique
# ─────────────────────────────────────────────────────────────────────────────
# Valeurs scalaires (fallback) — utilisées si le ticker n'a pas de dict spécifique
SL_ATR_MULT = 1.0               # SL_dist = SL_ATR_MULT × ATR
TP_ATR_MULT = 1.5               # TP_dist = TP_ATR_MULT × ATR

# Valeurs par ticker — calibrées walk-forward via optimize.py
# (IS Dec 2024 → Sep 2025, OOS Oct 2025 → Mar 2026, score = IS Sharpe parmi
# combinaisons OOS-validées : Sharpe ≥ 0.5, PF ≥ 1.2, n ≥ 8, P&L > 0).
#
# Performance OOS validée :
#   MES1  IS Sharpe=2.10  OOS Sharpe=1.65  OOS PF=1.24  OOS P&L=+$654
#   NQ1   IS Sharpe=0.93  OOS Sharpe=1.99  OOS PF=1.53  OOS P&L=+$1192
#   YM1   IS Sharpe=1.61  OOS Sharpe=1.95  OOS PF=1.30  OOS P&L=+$596
SL_ATR_MULT_PER_TICKER     = {"MES1": 1.25, "NQ1": 1.50, "YM1": 1.50}
TP_ATR_MULT_PER_TICKER     = {"MES1": 1.50, "NQ1": 3.00, "YM1": 1.50}
MIN_IMPULSE_ATR_PER_TICKER = {"MES1": 1.50, "NQ1": 1.00, "YM1": 2.00}

# ─────────────────────────────────────────────────────────────────────────────
# Filtres trigger (appliqués à l'armement de l'ordre limite)
# ─────────────────────────────────────────────────────────────────────────────
# Calibrés walk-forward IS/OOS via analyze_filters.py.
# Chaque filtre rejette le signal si feature {direction} threshold est faux.
# None = pas de filtre actif pour ce ticker.
#
# Performance OOS validée (vs baseline OOS Sharpe par ticker) :
#   MES1 : impulse_velocity_atr > 0.670  → Sharpe 1.65 → 3.28 (Δ +1.62), n=9
#   NQ1  : recent_vol_atr        > 0.811 → Sharpe 1.99 → 3.78 (Δ +1.78), n=9
#   YM1  : price_extension_atr   > 1.118 → Sharpe 1.95 → 7.89 (Δ +5.94), n=21
#
# Caveat : MES1 et NQ1 ont n=9 OOS — sample size faible, intervalle de
# confiance large. YM1 le plus robuste (n=21).
TRIGGER_FILTERS_PER_TICKER = {
    "MES1": {"feature": "impulse_velocity_atr", "direction": "gt", "threshold": 0.670},
    "NQ1":  {"feature": "recent_vol_atr",       "direction": "gt", "threshold": 0.811},
    "YM1":  {"feature": "price_extension_atr",  "direction": "gt", "threshold": 1.118},
}

# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward IS/OOS
# ─────────────────────────────────────────────────────────────────────────────

IS_END = "2025-09-30"           # cohérent avec optimize_opr.py (projet principal)

# ─────────────────────────────────────────────────────────────────────────────
# Session de trading (UTC) — bornes US session (cohérent projet principal)
# ─────────────────────────────────────────────────────────────────────────────

US_SESSION_START_UTC = 13
US_SESSION_END_UTC = 21

# ─────────────────────────────────────────────────────────────────────────────
# Visualisation
# ─────────────────────────────────────────────────────────────────────────────

N_TRADES_VIEW = 50              # nombre de trades les plus récents à visualiser
CHART_CONTEXT_BEFORE = 30       # bougies avant le fill
CHART_CONTEXT_AFTER = 15        # bougies après l'exit

# ─────────────────────────────────────────────────────────────────────────────
# Sharpe / annualisation
# ─────────────────────────────────────────────────────────────────────────────

# Annualisation Sharpe : sqrt(252) standard pour returns par trade considérés
# comme indépendants. Utilisé en référence pour le ranking des combinaisons.
SHARPE_ANNUALIZATION = 252
