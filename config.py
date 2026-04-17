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

ZONE_TOLERANCE_PCT = 0.001      # optimisé v4
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
