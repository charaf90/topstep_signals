"""
Configuration centrale du projet.
Tous les paramètres modifiables sont ici.
"""

# ==============================================================================
# TELEGRAM
# ==============================================================================

TELEGRAM_BOT_TOKEN = "7485374615:AAHejkAqaaH32eIHp4KzJ7PDcx3kPBYcNOk"
TELEGRAM_CHAT_ID = 1318401808  # Rempli automatiquement au premier /start

# ==============================================================================
# INSTRUMENTS (micro-contrats)
# ==============================================================================

INSTRUMENTS = {
    "MES1": {
        "dollar_per_point": 5.0,
        "tick_size": 0.25,
        "name": "Micro E-mini S&P 500",
        "tv_symbol": "MES1!",
        "tv_exchange": "CME_MINI",
    },
    "NQ1": {
        "dollar_per_point": 2.0,
        "tick_size": 0.25,
        "name": "Micro E-mini Nasdaq 100",
        "tv_symbol": "MNQ1!",
        "tv_exchange": "CME_MINI",
    },
    "YM1": {
        "dollar_per_point": 0.5,
        "tick_size": 1.0,
        "name": "Micro E-mini Dow Jones",
        "tv_symbol": "MYM1!",
        "tv_exchange": "CBOT_MINI",
    },
}

# ==============================================================================
# STRATÉGIE (checkpoint v3)
# ==============================================================================

# Stop loss minimum par actif (en points)
SL_MINIMUM = {"MES1": 9, "NQ1": 29, "YM1": 60}

# Risk/Reward par actif (optimisé par recherche granulaire)
RR_TARGET = {"MES1": 3.0, "NQ1": 2.5, "YM1": 1.75}

# Seuil qualité zone par actif
ZONE_QUALITY_MIN = {"MES1": 70, "NQ1": 40, "YM1": 30}

# Filtres pré-market par actif
USE_PM_FILTER = {"MES1": True, "NQ1": True, "YM1": False}

# Autoriser les trades en régime RANGE par actif
TRADE_RANGE = {"MES1": True, "NQ1": True, "YM1": False}

# ==============================================================================
# PARAMÈTRES GÉNÉRAUX
# ==============================================================================

RISK_PER_TRADE_USD = 100
MAX_TRADES_PER_DAY = 2
SL_BUFFER_TICKS = 2

# --- Nouvelles features de gestion du risque ---

# ATR dynamique pour le buffer SL
USE_ATR_BUFFER = False          # True = buffer dynamique basé sur ATR
ATR_PERIOD = 14                 # Période ATR (bougies 15m)
ATR_BUFFER_MULT = 0.5           # Multiplicateur ATR pour le buffer SL

# TP structurel (prochaine zone S/R)
USE_STRUCTURAL_TP = False       # True = TP ancré sur la prochaine zone S/R
STRUCTURAL_TP_MIN_RR = 1.5      # RR minimum pour accepter un TP structurel

# RR dynamique selon la force de la tendance
USE_DYNAMIC_RR = False          # True = RR ajusté selon la force de la tendance
DYNAMIC_RR_STRONG_MULT = 1.5    # |alignment| > 0.6 → RR × 1.5
DYNAMIC_RR_MODERATE_MULT = 1.0  # |alignment| > 0.33 → RR × 1.0
DYNAMIC_RR_RANGE_MULT = 0.75    # |alignment| <= 0.33 → RR × 0.75
DYNAMIC_RR_MIN = 1.5            # RR plancher après ajustement

# Entrée au POC (Point of Control / profil de volume)
USE_POC_ENTRY = False           # True = entrée au POC de la zone
POC_NUM_BINS = 20               # Nombre de bins pour le profil de volume

# Scale-in (entrées fractionnées)
USE_SCALE_IN = False            # True = 2 entrées fractionnées dans la zone

# Horaires (UTC)
CUTOFF_HOUR_UTC = 11        # Midi Paris = 11h UTC
US_SESSION_START_UTC = 13
US_SESSION_END_UTC = 21

# Historique minimum requis
MIN_BARS_HISTORY = 500
MIN_BARS_US_SESSION = 8

# ==============================================================================
# DÉTECTION DES PIVOTS
# ==============================================================================

PIVOT_CONFIGS = {
    "D1":  {"left": 3, "right": 3, "window": 200, "weight": 3.0},
    "H4":  {"left": 4, "right": 4, "window": 400, "weight": 2.0},
    "H1":  {"left": 7, "right": 7, "window": 400, "weight": 1.5},
    "15m": {"left": 8, "right": 8, "window": 400, "weight": 1.0},
}

# ==============================================================================
# ZONES S/R
# ==============================================================================

ZONE_TOLERANCE_PCT = 0.001      # optimisé
ZONE_MIN_TOUCHES = 2
ZONE_MIN_TF_OR_TOUCHES = (2, 3) # 2 TF minimum OU 3 touches sur 1 TF
ZONE_MAX_WIDTH_PCT = 0.004      # 0.4% max
ZONE_RECENCY_THRESHOLD = 0.66
ZONE_DISTANCE_MIN_PCT = 0.15
ZONE_DISTANCE_MAX_PCT = 2.0

# ==============================================================================
# TENDANCE
# ==============================================================================

TREND_EMA_PARAMS = {
    "D1": {"fast": 10, "slow": 30},
    "H4": {"fast": 15, "slow": 40},
    "H1": {"fast": 20, "slow": 50},
}
TREND_WEIGHTS = {"D1": 0.40, "H4": 0.35, "H1": 0.25}
TREND_BULL_THRESHOLD = 0.33
TREND_BEAR_THRESHOLD = -0.33

# Force de tendance minimale (|alignment_score|) par actif.
# Filtre appliqué par le score composite, même en régime BULL/BEAR.
# MES1 : valeur v5 conservée (0.25). L'optimizer IS proposait 0.15 mais
# l'OOS (PF=0.64) montrait un overfitting franc.
# NQ1  : 0.30 retenu après Phase C (OOS PF=1.75, P&L OOS +$870 validés).
# YM1  : conservé à 0.40 (ticker désactivé).
TREND_STRENGTH_MIN = {"MES1": 0.25, "NQ1": 0.3, "YM1": 0.40}

# ==============================================================================
# SCORE COMPOSITE (coeur du filtrage ultra-sélectif)
# ==============================================================================
# Pondération du score composite (somme = 1.0).
COMPOSITE_WEIGHTS = {
    "zone_quality":    0.40,
    "trend_alignment": 0.25,
    "pm_context":      0.20,
    "volatility":      0.15,
}

# Seuil composite minimum par actif (0-100). Plus élevé = plus sélectif.
# MES1 : valeur v5 conservée (60). Optimizer proposait 58 mais OOS négatif.
# NQ1  : 55 retenu après Phase C (OOS PF=1.75 validé).
# YM1  : 70 (ticker désactivé tant que YM1_ENABLED=False).
COMPOSITE_SCORE_MIN = {"MES1": 60, "NQ1": 55, "YM1": 70}

# YM1 : désactivation globale tant qu'aucune preuve OOS (PF ≥ 1.2).
# L'optimizer peut basculer à True après validation.
YM1_ENABLED = False

# ==============================================================================
# VOLATILITÉ PRÉ-MARCHÉ
# ==============================================================================
# Toutes les mesures sont normalisées par l'ATR journalier (atr_daily) —
# ainsi les seuils sont interprétables : 0.5 = 50% d'une journée typique.
ATR_OVN_PERIOD       = 14              # Période de l'ATR journalier (jours)
ATR30_LOOKBACK_DAYS  = 30              # Fenêtre 15m qui sert à reconstruire les bougies D1
VOL_SCORE_CENTER     = 0.55            # Nuit "moyenne" = 55% du range journalier
VOL_SCORE_TOL        = 0.30            # Tolérance de la courbe cloche

# atr_ratio = ovn_range / atr_daily. Bornes : nuit trop calme ou trop agitée.
ATR_RATIO_MIN = {"MES1": 0.20, "NQ1": 0.20, "YM1": 0.18}
ATR_RATIO_MAX = {"MES1": 1.40, "NQ1": 1.50, "YM1": 1.30}

# Gap (|open session - close J-1|) / atr_daily — rejet si gap violent.
GAP_ATR_MAX   = {"MES1": 0.80, "NQ1": 0.90, "YM1": 0.70}

# Range overnight / atr_daily — même mesure que atr_ratio, conservé pour lisibilité.
OVN_RANGE_MAX = {"MES1": 1.40, "NQ1": 1.50, "YM1": 1.30}

# ==============================================================================
# GARDE-FOU TOPSTEP (challenge 50K)
# ==============================================================================
TOPSTEP_ACCOUNT_SIZE   = 50_000
TOPSTEP_PROFIT_TARGET  = 3_000
TOPSTEP_DAILY_LOSS_MAX = 1_000         # Limite perte journalière (valeur absolue)
TOPSTEP_TRAILING_DD    = 2_000         # Trailing drawdown maximum

# Marge de sécurité : autorise le trade si slack > risk × mult.
TOPSTEP_SAFETY_MULT    = 1.1

# ==============================================================================
# CIRCUIT BREAKERS INTRA-JOUR (réduction du DD portefeuille)
# ==============================================================================
# Daily stop : après 1 SL dans la journée, les ordres restants sont annulés.
# Désactivé par défaut : combiné au consec-loss breaker, il coupait trop de
# trades profitables et faisait chuter le P&L total sous le target Topstep.
DAILY_STOP_AFTER_SL     = False

# Consecutive-loss breaker : après N jours perdants consécutifs, on saute 1 jour.
# 0 = désactivé. Le streak se réinitialise dès qu'un jour neutre/gagnant survient.
# 5 est le sweet spot empirique (bootstrap 99.9%, DD réduit vs v5 sans breaker).
CONSEC_LOSS_PAUSE_DAYS  = 5

# Daily lock-in : après un gain cumulé ≥ seuil sur la journée, plus de nouveau trade.
# 0 = désactivé. Un seuil trop bas (< 1.5× risque nominal) plafonne la capacité
# à atteindre le target Topstep dans le bootstrap.
DAILY_LOCKIN_THRESHOLD  = 0

# ==============================================================================
# GRAPHIQUES
# ==============================================================================

CHART_STYLE = {
    "figure.facecolor": "#131722",
    "axes.facecolor": "#131722",
    "axes.edgecolor": "#2a2e39",
    "axes.labelcolor": "#d1d4dc",
    "text.color": "#d1d4dc",
    "xtick.color": "#787b86",
    "ytick.color": "#787b86",
    "grid.color": "#1e222d",
    "grid.alpha": 0.8,
    "font.family": "sans-serif",
    "font.size": 9,
}
CHART_CANDLES = 200

# Backtest charts
BACKTEST_CHART_CONTEXT_BEFORE = 50   # Bougies avant le fill
BACKTEST_CHART_CONTEXT_AFTER = 20    # Bougies après la sortie
