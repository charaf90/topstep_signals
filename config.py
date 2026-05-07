"""
Configuration centrale du projet.
Tous les paramètres modifiables sont ici.
"""

# ==============================================================================
# INSTRUMENTS (micro-contrats)
# ==============================================================================

INSTRUMENTS = {
    "MES1": {
        "dollar_per_point": 5.0,
        "tick_size": 0.25,
        "name": "Micro E-mini S&P 500",
    },
    "NQ1": {
        "dollar_per_point": 2.0,
        "tick_size": 0.25,
        "name": "Micro E-mini Nasdaq 100",
    },
    "YM1": {
        "dollar_per_point": 0.5,
        "tick_size": 1.0,
        "name": "Micro E-mini Dow Jones",
    },
}

# ==============================================================================
# PARAMÈTRES GLOBAUX
# ==============================================================================

RISK_PER_TRADE_USD   = 100
MAX_TRADES_PER_DAY   = 2       # par actif (OPR)
SL_BUFFER_TICKS      = 2

# Horaires de session (UTC)
CUTOFF_HOUR_UTC      = 11      # coupure analyse pré-marché
US_SESSION_START_UTC = 13
US_SESSION_END_UTC   = 21

# Historique minimum requis
MIN_BARS_HISTORY     = 500
MIN_BARS_US_SESSION  = 8

# ==============================================================================
# LIMITES UTILISATEUR (live — plus strictes que Topstep)
# ==============================================================================
USER_DAILY_LOSS_MAX    = 200   # $ perte journalière réalisée max
USER_MAX_TRADES_PER_DAY = 3   # fills global portefeuille
USER_MAX_OPEN_POSITIONS = 0

# ==============================================================================
# GARDE-FOU TOPSTEP (challenge 50K)
# ==============================================================================
TOPSTEP_ACCOUNT_SIZE   = 50_000
TOPSTEP_PROFIT_TARGET  = 3_000
TOPSTEP_DAILY_LOSS_MAX = 1_000
TOPSTEP_TRAILING_DD    = 2_000
TOPSTEP_SAFETY_MULT    = 1.1

# ==============================================================================
# CIRCUIT BREAKERS INTRA-JOUR
# ==============================================================================
DAILY_STOP_AFTER_SL    = False  # stopper après 1 SL (désactivé)
CONSEC_LOSS_PAUSE_DAYS = 5      # pause 1 jour après N jours perdants consécutifs
DAILY_LOCKIN_THRESHOLD = 0      # lock-in après gain cumulé (0 = désactivé)

# ==============================================================================
# STRATÉGIE OPR (Opening Range Breakout — pullback PineScript) — opr-v3
# ==============================================================================
# Réécriture fidèle au PineScript fourni par l'utilisateur (avr. 2026) :
#   • Zone OPR = 1ère bougie 15min de la session US ouvrant à 9h30 NY.
#     L'heure est fixée en heure NY (America/New_York) afin que la stratégie
#     soit invariante au passage été/hiver côté Paris.
#   • Trigger pullback : une bougie qui OUVRE dans la zone OPR et CLÔTURE
#     hors zone arme un ordre LIMIT au niveau OPR du sens de sortie
#     (limit BUY @ OPR_high si close > OPR_high, limit SELL @ OPR_low
#     si close < OPR_low).
#   • Une seule position ouverte à la fois (pyramiding=0 dans le PineScript).
#   • SL / TP en multiplicateur de l'**ATR journalier** (14 jours), calculé
#     strictement sur les jours achevés AVANT la session courante (pas de
#     leak temporel). Floor minimum en points pour éviter un sizing absurde
#     en régime ultra-calme.
#   • Toutes les positions sont fermées à 16h30 NY.
#
# Voir core/opr.py.

OPR_ENABLED  = True
YM1_ENABLED  = False  # désactivé jusqu'à preuve OOS (PF ≥ 1.2)

# Fuseau horaire de référence pour la zone OPR et l'horaire de session.
# `America/New_York` gère automatiquement DST (EST/EDT) — l'OPR reste à
# 9h30 NY toute l'année, ce qui se traduit en 14h30 UTC (hiver) ou 13h30
# UTC (été). C'est une exigence explicite : on ne hard-code plus l'heure UTC.
OPR_TIMEZONE = "America/New_York"

# Fenêtre de RECHERCHE de la bougie OPR par pic de volume — heures NY (h, m).
# Depuis opr-v3.1 la bougie OPR n'est plus prise en strict matching à 9h30
# NY mais identifiée comme la bougie de volume max dans `[WINDOW_START,
# WINDOW_END[` NY. Ça gère DST (heure été/hiver) et les éventuelles dérives
# de timestamp côté data provider, tout en restant verrouillé à l'ouverture
# cash NY (qui se traduit par une explosion de volume systématique).
#
# Largeur 30min ([9h15, 9h45[) = ±15min de tolérance autour de 9h30 NY.
# Sur ~390 jours testés (MES1 Dec 2024 → Mar 2026), 320/320 jours actifs
# placent leur pic de volume sur la bougie 9h30 NY, et la fenêtre couvre
# proprement les rares cas de drift broker ou de session écourtée.
OPR_WINDOW_START = (9, 15)
OPR_WINDOW_END = (9, 45)

# Heure NY de fermeture forcée des positions (clôture session US).
OPR_SESSION_END = (16, 30)

# Période de l'ATR journalier utilisé comme référence pour SL/TP. 14 jours
# est le standard de la profession — assez réactif sans être bruité.
OPR_ATR_PERIOD = 14

# Multiplicateurs ATR par actif. SL_dist = mult × atr_daily, TP_dist idem.
# Valeurs calibrées en walk-forward (IS Dec 2024 → Sep 2025, OOS Oct 2025
# → Mar 2026) via optimize_opr.py avec filtres trigger actifs (opr-v4).
# Critère : IS PF ≥ 1.35 ET OOS PF ≥ 1.2 ET P&L OOS > 0.
# Score optimizer = OOS_PF × OOS_P&L.
#
# IS / OOS validés (opr-v4, avec filtres trigger) :
#   MES1  IS PF=1.41  OOS PF=1.51  OOS P&L=+$1000  OOS DD=-$508   (RR=3.33)
#   NQ1   IS PF=1.65  OOS PF=1.65  OOS P&L=+$4230  OOS DD=-$804   (RR=2.00, inchangé)
#   YM1   IS PF=1.69  OOS PF=2.59  OOS P&L=+$2639  OOS DD=-$264   (RR=1.25)
OPR_SL_ATR_MULT = {"MES1": 0.15, "NQ1": 0.05, "YM1": 0.12}
OPR_TP_ATR_MULT = {"MES1": 0.50, "NQ1": 0.10, "YM1": 0.15}

# Floor minimum SL en points par actif. Empêche les SL d'être trop serrés
# en régime ultra-calme (atr_daily * mult < bruit du tick) — protège contre
# le noise stop-out. Fixé à ~2× le tick_size minimum trade-able.
OPR_SL_MIN_POINTS = {"MES1": 3.0, "NQ1": 8.0, "YM1": 15.0}

# Plafond de fills par jour (sécurité même si la logique "1 position à la
# fois" rend ce plafond rarement atteint). Conservé pour homogénéité.
OPR_MAX_TRADES_PER_DAY = 4

# Filtres au moment du trigger (avant armement de l'ordre limite).
# Calibrés en walk-forward IS/OOS via analyse/03_filter_backtest.py.
# None = filtre désactivé pour cet actif.
#
# OPR_MIN_EXCURSION_ATR : excursion minimale du prix dans le sens du trigger
#   depuis la bougie OPR (exclu) jusqu'au trigger (inclus), normalisée par
#   atr_daily. Rejette les triggers où le prix n'a pas encore "voyagé" vers
#   la zone — protège contre les tests chaotiques immédiats post-OPR.
#   YM1 : 0.17 → IS PF=1.76 / OOS PF=2.04 (+0.53 vs baseline 1.51)
#
# OPR_MAX_VOL_ZSCORE : z-score du volume de la bougie trigger vs les
#   OPR_VOL_ZSCORE_WINDOW bougies précédentes de la session. Rejette les
#   triggers sur volume anormalement élevé (spike = move émotionnel/news).
#   MES1 : -0.45 → IS PF=1.89 / OOS PF=1.79 (+0.46 vs baseline 1.33)
OPR_MIN_EXCURSION_ATR  = {"MES1": None,  "NQ1": None, "YM1": 0.17}
OPR_MAX_VOL_ZSCORE     = {"MES1": -0.45, "NQ1": None, "YM1": None}
OPR_VOL_ZSCORE_WINDOW  = 20   # bougies de session pour le z-score volume

# Tag de version OPR pour le dossier de graphiques d'analyse.
# opr-v3 : passage de SL/TP en distance fixe (points) à multiplicateur ATR.
# opr-v4 : ajout filtres trigger (excursion_atr YM1, vol_zscore MES1).
OPR_STRATEGY_VERSION = "opr-v4"

# ==============================================================================
# STRATÉGIE FIBONACCI 50% RETRACEMENT (`fib-v1`)
# ==============================================================================
# Promotion depuis draft_fibo_50/ après validation walk-forward IS/OOS :
#   • Tendance multi-critères : EMA50/EMA200 stack + ADX(14) > 20
#   • Impulse = pivot_low → pivot_high (ou inverse), validé par taille ATR + durée
#   • Entrée LIMIT à fib_50 = swing_low + 0.5 × (swing_high − swing_low)
#   • SL/TP en multiplicateurs ATR per-ticker
#   • Filtre trigger walk-forward per-ticker (analyse Sharpe)
#   • Position fermée au timeout (MAX_HOLD_BARS) si SL/TP non atteint
#
# Voir core/strategy_fib.py.

FIB_ENABLED = True

# Indicateurs (cohérents avec draft validé)
FIB_ATR_PERIOD = 14
FIB_EMA_FAST_PERIOD = 50
FIB_EMA_SLOW_PERIOD = 200
FIB_ADX_PERIOD = 14
FIB_ADX_TREND_THRESHOLD = 20.0

# Pivots (= 4h M15 de chaque côté — sweep validé robuste)
FIB_PIVOT_LEFT = 8
FIB_PIVOT_RIGHT = 8

# Impulse — défauts globaux (peuvent être surchargés per-ticker)
FIB_MIN_IMPULSE_ATR = 1.5
FIB_MAX_IMPULSE_BARS = 25
FIB_IMPULSE_LOOKBACK = 60

# Vie d'ordre / position
FIB_ORDER_TIMEOUT_BARS = 12         # ordre limite annulé après ~3h
FIB_MAX_HOLD_BARS = 32              # fermeture forcée après ~8h

# Niveau Fibonacci utilisé pour le calcul du prix d'entrée par ticker.
# Calibré walk-forward via draft_fibo_50/optimize_fib_levels.py + comparaison
# portefeuille via compare_fib_levels.py.
#
# Choix retenu : 0.382 (uniforme sur les 3 actifs).
# Justification (Dec 2024 → Mar 2026, sans filtres trigger) :
#   38.2 seul        : 483 trades, P&L=+$8 718, DD=-$1 141, Sharpe=3.00, BS=93.1 %
#   50  seul         : 488 trades, P&L=+$4 662, DD=-$1 212, Sharpe=2.24, BS=96.7 %
#   61.8 seul        : 574 trades, P&L=+$956,   DD=-$4 496, Sharpe=0.37, BS=0.0 %  (rejeté)
#   38.2 + 50        : 971 trades, P&L=+$13 380, DD=-$2 218, BS=41.6 %  (DD trop lourd)
#   Triplet 38.2+50+61.8 : 1 545 trades, P&L=+$14 336, BS=4.6 %   (inacceptable Topstep)
FIB_LEVEL_PER_TICKER = {"MES1": 0.382, "NQ1": 0.382, "YM1": 0.382}

# SL/TP/IMP per-ticker — calibrés walk-forward via draft_fibo_50/optimize_fib_levels.py
# Performance OOS validée pour le niveau 38.2 % (sans filtres trigger) :
#   MES1  IS Sharpe=2.99  OOS Sharpe=1.66  OOS PF=1.27  OOS P&L=+$1 156  (n=77)
#   NQ1   IS Sharpe=5.52  OOS Sharpe=4.52  OOS PF=1.79  OOS P&L=+$765   (n=35)
#   YM1   IS Sharpe=1.04  OOS Sharpe=1.44  OOS PF=1.22  OOS P&L=+$640   (n=57)
FIB_SL_ATR_MULT_PER_TICKER     = {"MES1": 0.75, "NQ1": 1.50, "YM1": 1.00}
FIB_TP_ATR_MULT_PER_TICKER     = {"MES1": 1.50, "NQ1": 1.50, "YM1": 2.00}   # MES1 : 2.0→1.5 (fib-v3)
FIB_MIN_IMPULSE_ATR_PER_TICKER = {"MES1": 1.00, "NQ1": 1.00, "YM1": 2.00}   # MES1 : 2.0→1.0 (fib-v3)

# Fenêtre de session horaire par ticker (heures UTC).
# Clés de SESSION_WINDOWS dans core/strategy_fib.py.
# MES1 : "no_nuit" (0h–21h UTC) validé 🟢 fib-v3 — OOS PF=1.82, BS=100%
# NQ1/YM1 : "us_session" inchangé
FIB_SESSION_PER_TICKER = {
    "MES1": "no_nuit",     # 0h–21h UTC
    "NQ1":  "us_session",  # 13h–21h UTC
    "YM1":  "us_session",  # 13h–21h UTC
}

# Filtres trigger walk-forward (calibrés pour fib-v2, niveau 38.2 %)
# via draft_fibo_50/analyze_filters_v2.py.
# Format : {"feature": <nom>, "direction": "gt"|"lt", "threshold": <float>}
# None = pas de filtre actif.
#
# Sélection : compromis Sharpe / robustesse (OOS n).
#   MES1 : bars_since_confirm < 10 (OOS Sharpe 4.51 vs baseline 1.66, n=44 robuste)
#   NQ1  : adx_at_arm > 44.035     (OOS Sharpe 18.67 vs baseline 4.52, n=10 limite)
#   YM1  : bars_since_confirm < 2  (OOS Sharpe 7.11 vs baseline 1.44, n=12 limite)
#
# Caveat : NQ1 et YM1 ont des samples OOS faibles (n=10/12) → IC large.
# À re-valider sur 2026-Q2/Q3 dès données disponibles.
FIB_TRIGGER_FILTERS_PER_TICKER = {
    "MES1": {"feature": "bars_since_confirm", "direction": "lt", "threshold": 10.0},
    "NQ1":  {"feature": "adx_at_arm",         "direction": "gt", "threshold": 44.035},
    "YM1":  {"feature": "bars_since_confirm", "direction": "lt", "threshold": 2.0},
}

FIB_STRATEGY_VERSION = "fib-v3"

# ==============================================================================
# STRATÉGIE SMC (Smart Money Concepts) — smc-v1
# ==============================================================================
# Inspirée de LuxAlgo SMC. Logique :
#   1. Pivot swing (right-side, size configurable)
#   2. CHoCH = retournement de structure (tendance inverse → cassure swing)
#   3. Order Block = barre avec min parsedLow (long) ou max parsedHigh (short)
#      dans la plage [pivot source → bar CHoCH], filtré volatilité
#   4. Ordre LIMIT à entry_pct dans l'OB, SL structurel, TP = ATR × mult
#   5. Killzone NY Open uniquement (9h30–11h00 NY)

SMC_TIMEZONE = "America/New_York"

# Killzone de production (défaut)
SMC_KILLZONE_NY = (9, 30, 11, 0)

# Toutes les killzones disponibles — (h_start, m_start, h_end, m_end) heure NY
SMC_ALL_KILLZONES = {
    "london_open":  (2,  0,  5,  0),   # ouverture session européenne
    "ny_premarket": (7,  0,  9, 30),   # pré-marché NY
    "ny_open":      (9, 30, 11,  0),   # cash open NY
    "london_close": (10, 0, 12,  0),   # chevauchement clôture London
    "ny_midday":    (11, 0, 13,  0),   # déjeuner NY
    "ny_afternoon": (13, 0, 16,  0),   # après-midi NY
}

# Taille des fenêtres pivot (barres)
SMC_SWING_SIZE    = 50   # structure swing (LuxAlgo défaut)
SMC_INTERNAL_SIZE = 5    # structure interne

# Entrée dans l'OB : 0.0 = bord extérieur, 0.5 = milieu, 1.0 = bord intérieur
SMC_ENTRY_PCT = 0.5

# SL : au-delà de l'OB extreme + sl_atr_mult × ATR(14)
# Le buffer ATR remplace le buffer fixe en ticks — s'adapte à la volatilité
SMC_SL_ATR_MULT_PER_TICKER = {"MES1": 0.5, "NQ1": 0.5, "YM1": 0.5}

# TP : multiplicateur ATR par ticker (calibré walk-forward)
SMC_TP_ATR_MULT_PER_TICKER = {"MES1": 2.0, "NQ1": 2.0, "YM1": 2.0}

# ATR pour le TP
SMC_ATR_PERIOD = 14

# Filtre volatilité OB (méthode LuxAlgo) :
# bougie avec range ≥ SMC_OB_VOL_MULT × ATR(SMC_OB_VOL_ATR_PERIOD) → inversée
SMC_OB_VOL_ATR_PERIOD = 200
SMC_OB_VOL_MULT       = 2.0

# Vie de l'ordre
SMC_ORDER_TIMEOUT_BARS = 8     # annulé après 8 barres = 2h sans fill
SMC_SESSION_END_NY     = (16, 30)
SMC_MAX_TRADES_PER_DAY = 2

# Type de signal : "choch" | "bos" | "both"
SMC_SIGNAL_TYPE = "both"

# Niveau de structure : "swing" | "internal" | "both"
# Analyse exploratoire → swing inutilisable (0 TP), internal uniquement
SMC_STRUCTURE_LEVEL = "internal"

# Durée de validité d'un CHoCH (barres max depuis l'événement)
SMC_CHOCH_LOOKBACK_BARS = 30

SMC_STRATEGY_VERSION = "smc-v1"

# ==============================================================================
# BROKER PROJECTX / TOPSTEPX
# ==============================================================================
# Mapping tickers internes → symboles ProjectX (recherche de contrats).
# Utilisé par broker/projectx_client.py et broker/live_runner.py.
PROJECTX_BASE_URL  = "https://api.topstepx.com"
PROJECTX_SYMBOLS   = {"MES1": "MES", "NQ1": "MNQ", "YM1": "MYM"}

# live=False : compte de simulation (challenge Topstep).
# live=True  : compte financé (après validation du challenge).
# Tous les appels API (search_contract, get_bars, place_order) utilisent cette valeur.
PROJECTX_LIVE_MODE = False

# ==============================================================================
# TELEGRAM
# ==============================================================================
# Credentials chargés depuis .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
# Laisser vide pour désactiver complètement.

# Activation globale (False → aucun message envoyé, toutes les méthodes no-op)
TELEGRAM_ENABLED = True

# Niveaux d'alerte activables indépendamment
TELEGRAM_LEVEL_TRADES   = True   # fills, clôtures, signaux, ordres placés
TELEGRAM_LEVEL_RISK     = True   # blocages RM, limites approchantes, breach Topstep
TELEGRAM_LEVEL_SYSTEM   = True   # erreurs API, perte de connexion
TELEGRAM_LEVEL_REPORT   = True   # bilan de session (Niveau 2)
TELEGRAM_LEVEL_COMMANDS = True   # /status bidirectionnel (Niveau 3)

# Nombre de barres 15m à fetcher pour garantir le warmup des indicateurs
# (ATR journalier OPR = 14 jours × 80 bougies = 1120 ; Fib warmup = 250).
# 2000 barres ≈ 25 jours de trading → confortable.
PROJECTX_BARS_WARMUP = 2000

# Fichier d'état persistant du live runner
LIVE_STATE_FILE = "state/live_state.json"

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

# Backtest charts (par trade)
BACKTEST_CHART_CONTEXT_BEFORE = 50   # Bougies avant le fill
BACKTEST_CHART_CONTEXT_AFTER = 20    # Bougies après la sortie

# ==============================================================================
# GRAPHIQUES D'ANALYSE JOURNALIERS (1 PNG / jour tradé / ticker)
# ==============================================================================
# Voir CLAUDE.md → "Graphiques d'analyse journaliers (consigne pérenne)" :
# toute nouvelle stratégie doit produire ces graphiques en backtest pour
# permettre une revue visuelle rapide. Les fichiers sont stockés sous
# output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/{YYYY-MM-DD}.png.
STRATEGY_VERSION = "v5.2"             # tag de la stratégie courante
ANALYSIS_CHARTS_ENABLED = True        # générer ces graphiques par défaut en backtest
ANALYSIS_CHART_CONTEXT_BEFORE = 200   # bougies 15m avant cutoff (cf. spec utilisateur)
