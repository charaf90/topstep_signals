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
    # Ajoutés pour tests sur autres classes d'actifs (data_fetcher ProjectX).
    # USAGE RECHERCHE uniquement — pas de promotion live tant que pas validés OOS.
    "MGC1": {
        # Micro Gold : tickSize 0.10, tickValue $1.00 → 10 ticks/point × $1 = $10/point
        "dollar_per_point": 10.0,
        "tick_size": 0.10,
        "name": "Micro Gold",
    },
    "MCL1": {
        # Micro WTI Crude : tickSize 0.01, tickValue $1.00 → 100 ticks/point × $1 = $100/point
        "dollar_per_point": 100.0,
        "tick_size": 0.01,
        "name": "Micro WTI Crude Oil",
    },
}

# ==============================================================================
# PARAMÈTRES GLOBAUX
# ==============================================================================

# Risque dollar par trade — passé de $100 à $200 le 2026-05-21 après
# validation backtest extensive (output/period_stats/report.md,
# output/losses_distribution/report.md, output/sl_streaks/report.md) :
#   • 21 mois historiques à $200/trade fixe : 100% mois positifs,
#     95.2% mois ≥ +$3000 (challenge passé), 0 mois ≤ -$2000
#   • Worst day = -$1,033 (1 jour sur 360 = 0.3%, 8 jan 2025) —
#     reste sous DLL broker $950 fixée côté Topstep
#   • Décision : être fidèle au pipeline backtest plutôt que d'amplifier
#     le risque via recovery-mode adaptive (formule mal calibrée,
#     asymétrie négatif/positif identifiée — cf. décision séparée).
RISK_PER_TRADE_USD = 200
MAX_TRADES_PER_DAY = 2  # par actif (OPR)
SL_BUFFER_TICKS = 2

# ==============================================================================
# FRICTIONS DE MARCHÉ (slippage + commissions)
# ==============================================================================
# Slippage par actif (en ticks) — appliqué à l'entrée ET à la sortie côté
# core/robustness.py et templates strategy_template.md. Calibré sur l'observation
# des fills réels TopstepX :
#   • MES1 : tick 0.25, spread typique 1 tick → 1 tick de glissement
#   • NQ1  : tick 0.25, spread plus mince mais bougies M15 plus volatiles → 2 ticks
#   • YM1  : tick 1.0, spread serré → 1 tick
SLIPPAGE_TICKS_PER_TICKER = {
    "MES1": 1,
    "NQ1": 2,
    "YM1": 1,
    # Tests recherche — calibration prudente (par défaut 1 tick) :
    "MGC1": 1,  # Gold micro : spread typique 1 tick
    "MCL1": 2,  # Crude micro : ticks fins (0.01) + volatilité → 2 ticks
}

# Commission round-trip par contrat (entrée + sortie) — Topstep TopstepX micro
COMMISSION_RT_PER_CONTRACT = 1.40  # $/contrat aller-retour

# ── Slippage stochastique (PHASE 2.4 ROADMAP_SOLO) ────────────────────────────
# Feature flag — OFF par défaut. Si ON, core/slippage.apply_slippage_to_trades
# appliquera un slippage stochastique aux PnL des backtests, calibré sur
# SLIPPAGE_TICKS_PER_TICKER (base) + un extra ATR-dépendant.
#
# IMPORTANT : avec flag OFF, les pipelines de backtest restent strictement
# identiques au baseline figé en golden master. Activer ce flag rend les
# backtests "réalistes" mais NON COMPARABLES aux rapports historiques (PF/PnL
# OOS systématiquement plus faibles).
BACKTEST_REALISTIC_SLIPPAGE = False

# Échelle de la composante stochastique (exponentielle) appliquée en facteur
# multiplicatif × atr_ratio. Valeur conservatrice par défaut :
#   slip_extra = base_slip × Exp(λ=REALISTIC_SLIPPAGE_ATR_SCALE) × atr_ratio
# où atr_ratio = max(0, atr_actuel / atr_median - 1).
REALISTIC_SLIPPAGE_ATR_SCALE = 0.5

# Seed pour reproductibilité des tirages stochastiques (modifier pour Monte-Carlo).
REALISTIC_SLIPPAGE_SEED = 42

# ── Parallélisation walk-forward (PHASE 2.5 ROADMAP_SOLO) ─────────────────────
# Nombre de workers joblib pour core/optimizer.optimize.
#   -1 = tous les CPU disponibles (recommandé en local)
#    1 = séquentiel (utile pour debug / reproductibilité stricte trace par trace)
#    N = N workers (ex: 4 pour ne pas saturer la machine pendant le live)
OPTIMIZER_PARALLEL_N_JOBS = -1

# ── Shadow Mode (PHASE 3.2 ROADMAP_SOLO) ──────────────────────────────────────
# Le shadow runner tourne en parallèle du daemon live, dans un process tmux
# séparé, avec ses propres fichiers d'état et de logs. Il utilise la même
# logique que le SessionRunner mais avec dry_run=True forcé (jamais d'ordres
# réels postés). Sert à comparer les décisions du live vs des variations
# (ex: vol-targeting on) avant de les promouvoir.
SHADOW_STATE_FILE = "state/shadow_state.json"
SHADOW_LOG_FILE = "logs/shadow_events.log"
SHADOW_PID_FILE = "state/shadow_daemon.pid"

# Feature flag pour activer le vol-targeting via core/adaptive_sizing (PHASE 3.3).
# OFF par défaut — sera activé uniquement après 14j shadow OK + validation user.
FEATURE_VOL_TARGETING_ENABLED = False

# ==============================================================================
# CALENDRIER MACRO (jours sensibles : FOMC, CPI, NFP, JOLTS, PCE)
# ==============================================================================
# Format : ["YYYY-MM-DD", ...] — utilisé par core/robustness.py pour le stress
# test "macro_day" et par les wrappers `strategies/*.py` qui exposent la
# colonne `is_macro_day` (cf. strategy_template.md).
# À maintenir manuellement : pas d'ingestion automatisée pour l'instant.
MACRO_EVENT_DATES = [
    # 2025 ───────────────────────────────────────────────────────────────────
    "2025-10-08",  # FOMC minutes (sept)
    "2025-10-15",  # CPI sept
    "2025-10-29",  # FOMC
    "2025-11-07",  # NFP oct
    "2025-11-13",  # CPI oct
    "2025-12-05",  # NFP nov
    "2025-12-10",  # CPI nov
    "2025-12-17",  # FOMC
    # 2026 ───────────────────────────────────────────────────────────────────
    "2026-01-09",  # NFP déc
    "2026-01-14",  # CPI déc
    "2026-01-28",  # FOMC
    "2026-02-06",  # NFP janv
    "2026-02-11",  # CPI janv
    "2026-03-06",  # NFP fév
    "2026-03-12",  # CPI fév
    "2026-03-18",  # FOMC
    "2026-04-03",  # NFP mar
    "2026-04-10",  # CPI mar
    "2026-04-30",  # FOMC
    "2026-05-02",  # NFP avr
    "2026-05-13",  # CPI avr
    "2026-06-05",  # NFP mai
    "2026-06-10",  # CPI mai
    "2026-06-17",  # FOMC
]

# Horaires de session (UTC)
CUTOFF_HOUR_UTC = 11  # coupure analyse pré-marché
US_SESSION_START_UTC = 13
US_SESSION_END_UTC = 21

# Historique minimum requis
MIN_BARS_HISTORY = 500
MIN_BARS_US_SESSION = 8

# ==============================================================================
# LIMITES UTILISATEUR (live — plus strictes que Topstep)
# ==============================================================================
# Aligné à $950 le 2026-05-21 — match exact ta limite Topstep DLL côté broker
# (auto-flatten + lockout par Topstep à -$950). Le daemon bloque les nouveaux
# signaux au même seuil pour éviter le bruit "ordre rejeté broker" en logs.
# Précédente valeur $200 était trop serrée avec risk $200/trade (stoppait après 1 SL).
USER_DAILY_LOSS_MAX = 950  # $ perte journalière réalisée max — aligné Topstep DLL
USER_MAX_TRADES_PER_DAY = 0  # désactivé — Topstep DLL $950 gère le cap réel
USER_MAX_OPEN_POSITIONS = 0  # pas de limite (positions simultanées)
# Cap dur sur le risque ARMÉ total = pending + positions actives (ajout 2026-06-10).
# Borne le pire-cas "sweep de fills simultanés" (jour violent : les limites armées
# par 4 stratégies × 2-3 tickers fillent ensemble puis SL en cascade) — scénario
# invisible des checks slack, qui ne comptent que les positions actives depuis la
# décision 2026-05-21. Avec $130-200/trade, $600 autorise 3-4 ordres armés
# simultanés. 0 = désactivé. S'ajoute au garde-fou pire-cas DLL armé (toujours
# actif, cf. core/risk_portfolio.can_open check 5-bis).
USER_MAX_ARMED_RISK_USD = 600

# ==============================================================================
# GARDE-FOU TOPSTEP (challenge 50K)
# ==============================================================================
TOPSTEP_ACCOUNT_SIZE = 50_000
TOPSTEP_PROFIT_TARGET = 3_000
TOPSTEP_DAILY_LOSS_MAX = 1_000
TOPSTEP_TRAILING_DD = 2_000
TOPSTEP_SAFETY_MULT = 1.1

# ==============================================================================
# WALK-FORWARD GLOBAL (méthodologie de validation — toutes stratégies)
# ==============================================================================
# Dates du split standard. IS_START est appliqué explicitement (2026-06-10) —
# avant, le filtre IS n'avait pas de borne basse (= début des données, sept 2024
# pour le m15) ; 2024-09-01 rend le code honnête SANS changer le comportement.
WF_IS_START = "2024-09-01"
WF_IS_END = "2025-09-30"
WF_OOS_START = "2025-10-01"
# HOLD-OUT TERMINAL (ajout 2026-06-10) : les ~8 dernières semaines de données
# sont EXCLUES de l'OOS de sélection/robustesse par défaut. Motif : l'OOS fixe
# oct-2025→today a été consommé par des dizaines de tests d'hypothèses — les
# survivants sur-fittent le régime de cette fenêtre. Le hold-out n'est consulté
# qu'UNE fois, explicitement (optimize.py --holdout), juste avant promotion.
# À avancer chaque trimestre (rituel : nouvelles dates + re-calibration prod).
WF_HOLDOUT_START = "2026-04-15"
# Multi-folds ancrés (optimize.py --multifold) : K folds expanding-IS dont les
# OOS de WF_FOLD_MONTHS mois se suivent en remontant depuis WF_HOLDOUT_START.
# La stabilité inter-folds (params + PF par fold + OOS recousu) prédit mieux
# la tenue live qu'un split unique.
WF_N_FOLDS = 5
WF_FOLD_MONTHS = 2

# ==============================================================================
# CIRCUIT BREAKERS INTRA-JOUR
# ==============================================================================
DAILY_STOP_AFTER_SL = False  # stopper après 1 SL (désactivé)
CONSEC_LOSS_PAUSE_DAYS = 5  # pause 1 jour après N jours perdants consécutifs
DAILY_LOCKIN_THRESHOLD = 0  # lock-in après gain cumulé (0 = désactivé)

# ==============================================================================
# CHALLENGE — sizing adaptatif Topstep (mode mensuel)
# ==============================================================================
# Le risque par trade est calculé dynamiquement selon (a) distance au profit
# target, (b) slack DD, (c) jours restants avant le 2 du mois (réinit du compte),
# (d) edge OOS static par stratégie. Cf. core/adaptive_sizing.py.
#
# IMPORTANT : USER_DAILY_LOSS_MAX est bypassé quand ce mode est ON. Seules les
# limites Topstep dures restent actives. Toute prise de risque > seuil de
# notification déclenche un Telegram WARN.
#
# DÉSACTIVÉ le 2026-05-21 après backtest validation extensive : formule
# adaptive identifiée comme mal calibrée (asymétrie négatif/positif —
# amplifie le risque en recovery sans bénéfice attendu). Retour au sizing
# fixe RISK_PER_TRADE_USD = $200 pour rester fidèle au pipeline historique.
# Code adaptive conservé intact (réactivable en flipant ce flag à True).
CHALLENGE_ADAPTIVE_SIZING_ENABLED = False  # désactivé — sizing fixe $200
CHALLENGE_RESET_DAY = 2  # jour du mois (marge sécurité vs vrai 4)
CHALLENGE_TIME_PRESSURE_GAMMA = 0.7  # adoucit l'effet temps restant
CHALLENGE_DD_GUARD_BUFFER = 2.3  # ne jamais risquer plus que slack/buffer (calibré MC)
CHALLENGE_RISK_MIN_USD = 30  # plancher absolu par trade
CHALLENGE_RISK_MAX_USD = 350  # plafond absolu (calibré MC : P(bust)≤15%)
CHALLENGE_BYPASS_USER_DAILY_LIMIT = True  # ignore USER_DAILY_LOSS_MAX en mode challenge
CHALLENGE_NOTIFY_OVERRIDE_THRESHOLD_USD = 100  # alerte Telegram si risk_applied > ce seuil
CHALLENGE_EXPECTED_TRADES_PER_DAY_FALLBACK = 5  # observé en live (8 fills/j max, ~5 moy)
CHALLENGE_LOCKIN_START_USD = 1500  # lockin précoce — anticipe règle 50%

# Garde-fou règle de cohérence Topstep (50% : best_day ≤ profit_target × 0.5 = $1500).
# Soft cap : à partir de ce seuil de gain journalier, le risque est amorti
# linéairement. Hard cap : aucun nouveau trade dont le TP plein ferait
# dépasser ce seuil de réalisé journalier (cf. core/risk_portfolio.py).
CHALLENGE_DAY_PROFIT_SOFT_CAP_USD = 800  # damping progressif au-delà
CHALLENGE_DAY_PROFIT_HARD_CAP_USD = 1400  # plus de nouveaux trades au-delà
CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD = 1400  # marge 100$ vs $1500 (slippage)

# Edge OOS par stratégie : e = WR × R − (1 − WR), calculé depuis le walk-forward.
#   OPR opr-v4 (WR≈0.42, R≈2.2) → 0.34
#   Fib fib-v3 (PF≈1.7, WR≈0.45, R≈2.0) → 0.40
CHALLENGE_STRAT_EDGE = {"OPR": 0.34, "FIB": 0.40}
# Pondération multiplicative selon le PF OOS du portfolio (Fib > OPR).
CHALLENGE_STRAT_BOOST = {"OPR": 1.0, "FIB": 1.2}

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

OPR_ENABLED = True
YM1_ENABLED = True  # activé 2026-05-18 — promotion v5.1 (BS=100%, PF=1.87)

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
OPR_MIN_EXCURSION_ATR = {"MES1": None, "NQ1": None, "YM1": 0.17}
OPR_MAX_VOL_ZSCORE = {"MES1": -0.45, "NQ1": None, "YM1": None}
OPR_VOL_ZSCORE_WINDOW = 20  # bougies de session pour le z-score volume

# Tag de version OPR pour le dossier de graphiques d'analyse.
# opr-v3 : passage de SL/TP en distance fixe (points) à multiplicateur ATR.
# opr-v4 : ajout filtres trigger (excursion_atr YM1, vol_zscore MES1).
OPR_STRATEGY_VERSION = "opr-v4"

# ==============================================================================
# STRATÉGIE BOS_FVG (`bos-fvg-v1`) — ICT : Break of Structure + Fair Value Gap
# ==============================================================================
# Idée utilisateur (ICT). Setup M5 :
#   1. BOS : impulsion qui casse le dernier swing high/low (close au-delà).
#   2. FVG : imbalance 3 bougies dans l'impulsion (bullish : low[k] > high[k-2]).
#   3. Entrée LIMIT au Consequent Encroachment (50% du FVG), à condition que ce
#      point soit en DISCOUNT (sous le 50% Fib de l'impulsion pour un achat).
#   4. SL sous le swing origine de l'impulsion (∓ buf). TP = rr × risque.
#   5. Invalidation : clôture au-delà du FVG sans rebond → annulation pending.
# Entrée LIMIT = fill HONNÊTE (pas de look-ahead). M5 resamplé M1 (NQ1/MES1/YM1)
# → IS+OOS + test cross-instrument MES1. RED-FLAG ÉTAPE 0 : ICT ultra-canonique
# (cf. smc-v1 rejeté, leçon Ichimoku). Testé car spec précise/falsifiable.
BOS_FVG_STRATEGY_VERSION = "bos-fvg-v2"
BOS_FVG_TICKERS = ["NQ1", "MES1", "YM1"]
BOS_FVG_TIMEZONE = "America/New_York"
BOS_FVG_SESSION_OPEN = (9, 30)
BOS_FVG_SESSION_END = (16, 0)
BOS_FVG_PIVOT_W = 2  # fenêtre pivot (left=right) pour swings M5
BOS_FVG_ATR_PERIOD = 14
BOS_FVG_MIN_IMPULSE_ATR = 0.5  # impulsion ≥ 0.5×ATR M5 (mouvement significatif)
BOS_FVG_LOOKBACK_BARS = 30  # fenêtre max de recherche de l'impulsion/FVG (M5)
BOS_FVG_SL_BUF = 0.10  # SL = origine swing ∓ buf × range impulsion
BOS_FVG_RR = 2.0
BOS_FVG_ORDER_TIMEOUT_BARS = 24  # annule le limit non fillé (2h)
BOS_FVG_MAX_TRADES_PER_DAY = 3
BOS_FVG_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200) [recherche]

# ── Promotion LIVE (bos-fvg-v1, 2026-06-03) — config retenue NQ1+MES1 @ $150 ──
# Code live INERTE tant que le daemon n'est pas redémarré délibérément.
# Verdict 🟢 (audit look-ahead clean, stress régime robuste, MC DD P95 -$1031).
BOS_FVG_ENABLED = True  # actif au prochain RESTART du daemon (pas avant)
BOS_FVG_LIVE_TICKERS = ["NQ1", "MES1"]  # YM1 exclu (faible OOS + driver DD)
BOS_FVG_RISK_USD = 150  # sizing dédié (≠ RISK_PER_TRADE_USD global $200)
BOS_FVG_MAX_HOLD_BARS_M5 = 9  # time-stop bos-fvg-v2 : ferme la position après 9 barres M5 (~45min) depuis le fill, en plus de l'EOD (deep-lane 🟢 : OOS PF 2.26→2.93, MC DD P95 −22%)
BOS_FVG_RR_PER_TICKER = {"NQ1": 1.5, "MES1": 1.5}  # optimum walk-forward
BOS_FVG_SL_BUF_PER_TICKER = {"NQ1": 0.0, "MES1": 0.1}  # optimum walk-forward
BOS_FVG_LIVE_M5_WARMUP_BARS = 800  # barres M5 fetchées pour le warmup (REST dédié, aligné fib-fine)

# ==============================================================================
# STRATÉGIE LIQUIDITY_HOOK (`liq-hook-v1`) — 15:45 NY Liquidity Hook (fade open-drive)
# ==============================================================================
# Idée utilisateur. Setup M5 sur l'ouverture cash NY (09:30 = 15h30 CET) :
#   1. DRIVE : les 3 premières bougies M5 (09:30, 09:35, 09:40) poussent dans le
#      MÊME sens (3 vertes → SHORT / 3 rouges → LONG). Flux institutionnel d'open.
#   2. ANCRE : corps de la 2e bougie (09:35 = "velocity peak"), 0%=open, 100%=close.
#   3. ENTRÉE : ordre LIMIT à l'extension 127.2% du corps (projetée dans le sens du
#      drive) — on fade le dernier pic. Sell limit (drive haussier) / buy limit (baissier).
#   4. SL = extension 161.8% du corps. TP = 50% (midpoint) du corps. RR géométrique ≈ 2.23.
#   5. TIME-STOP : si fill mais TP non atteint à 10:15 NY (16h15 CET), close marché.
# Fade contre-tendance d'un mouvement d'ouverture. Entrée LIMIT = fill HONNÊTE.
# Niveaux fib FIXES (faithful) ; grille = filtre magnitude + heure du time-stop.
# RED-FLAG ÉTAPE 0 : niveaux fib arbitraires (127.2/161.8) MAIS mécanisme (fade de
# l'épuisement du drive d'open + reflux HFT) plausible et falsifiable. Daily-freq.
LIQ_HOOK_STRATEGY_VERSION = "liq-hook-v1"
LIQ_HOOK_TICKERS = ["NQ1", "MES1", "YM1"]  # micros indices actions (open cash 09:30)
LIQ_HOOK_TIMEZONE = "America/New_York"
LIQ_HOOK_SESSION_OPEN = (9, 30)  # ouverture cash NY = 15h30 CET
LIQ_HOOK_SIGNAL_CANDLE_IDX = 1  # 2e bougie M5 (09:35) = velocity peak
LIQ_HOOK_N_DRIVE_CANDLES = 3  # 3 bougies M5 consécutives même sens
LIQ_HOOK_ENTRY_EXT = 1.272  # entrée = extension 127.2% du corps 09:35
LIQ_HOOK_SL_EXT = 1.618  # SL = extension 161.8%
LIQ_HOOK_TP_LEVEL = 0.5  # TP1 = 50% (midpoint) du corps
LIQ_HOOK_TIMESTOP = (10, 15)  # time-stop NY = 16h15 CET (close marché si pas TP)
LIQ_HOOK_ATR_PERIOD = 14
LIQ_HOOK_MIN_DRIVE_ATR = 0.0  # filtre : span 3 bougies ≥ x×ATR (0 = off)
LIQ_HOOK_MAX_TRADES_PER_DAY = 1
LIQ_HOOK_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)
# "literal"  : ordre limite mécanique — si le prix est déjà au-delà de L au 1er bar
#              actif (drive très fort), fill au marché (entrée dégradée, souvent perdante
#              = honnête/conservateur). "rest" : skip si déjà au-delà (entrée propre @ L).
LIQ_HOOK_FILL_MODE = "literal"

# ==============================================================================
# STRATÉGIE EXPANSION_HUNT (`exp-hunt-v1`) — Turtle Soup : fade sweep Equal Highs
# ==============================================================================
# Idée utilisateur (ICT "expansion de chasse"). Setup M5 (NQ/MNQ), short :
#   1. DOUBLE TOP : deux swing highs quasi-identiques (|h1-h2| ≤ tol×ATR) séparés
#      par un swing low. EH = max(h1,h2). H (hauteur range) = EH − creux intermédiaire.
#   2. SWEEP : une bougie casse les deux sommets (high > EH) = chasse de liquidité.
#   3. ENTRÉE : sell limit @ EH + 0.272×H (extension externe, on fade l'overshoot).
#   4. SL = EH + 0.618×H. TP1 = creux intermédiaire (EH − H). RR géométrique ≈ 3.68.
# Buy limit symétrique (double bottom, extensions sous les lows). Entrée LIMIT =
# fill HONNÊTE. Niveaux fib FIXES (faithful) ; grille = strictness double-top + range.
# RED-FLAG ÉTAPE 0 : fade de sweep (cf. liq-hook/asian rejetés — « les sweeps
# continuent ») MAIS SL range-relatif + RR 3.68 + structure sélective + précédent
# bos-fvg (ICT survivant). Non-disqualifiable a priori → test fast lane.
EXP_HUNT_STRATEGY_VERSION = "exp-hunt-v1"
EXP_HUNT_TICKERS = ["NQ1", "MES1", "YM1"]  # NQ cible ; MES1/YM1 = juge de paix cross-instrument
EXP_HUNT_TIMEZONE = "America/New_York"
EXP_HUNT_SESSION_OPEN = (9, 30)
EXP_HUNT_SESSION_END = (16, 0)
EXP_HUNT_PIVOT_W = 2  # fenêtre pivot (swings M5), révélé avec délai (causal)
EXP_HUNT_ATR_PERIOD = 14
EXP_HUNT_EQ_TOL_ATR = 0.10  # |h1-h2| ≤ tol×ATR (tolérance "equal highs")
EXP_HUNT_MIN_H_ATR = 0.6  # hauteur range H ≥ x×ATR (structure significative)
EXP_HUNT_MAX_STRUCT_BARS = 40  # distance max entre les deux sommets (lookback structure)
EXP_HUNT_ENTRY_EXT = 0.272  # entrée = EH + ext × H
EXP_HUNT_SL_EXT = 0.618  # SL = EH + ext × H
EXP_HUNT_ORDER_TIMEOUT_BARS = 12  # annule le sell limit non fillé (~1h M5)
EXP_HUNT_MAX_TRADES_PER_DAY = 2
EXP_HUNT_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)
# "literal" : si le prix est déjà au-delà de L au moment de l'armement (sweep très
#             violent), fill au marché (honnête/conservateur). "rest" : skip si déjà au-delà.
EXP_HUNT_FILL_MODE = "literal"

# ==============================================================================
# STRATÉGIE RANGE_CARTO (`range-carto-v1`) — Cartographie du Range (fade Volume Profile)
# ==============================================================================
# Idée utilisateur 2026-06-08. Volume Profile causal (POC/VAH/VAL) sur la
# consolidation récente → fade des "poches d'aspiration" (sweep des stops 0.15×W
# au-delà de la Value Area), TP = POC (retour à la juste valeur), SL = 0.35×W.
# PRIOR DÉFAVORABLE (documenté) : doublon de DEUX familles abandonnées —
#   • fade-de-sweep (exp-hunt/liq-hook/asian-sweep, 🔴) et
#   • Volume Profile reconstruit depuis barres (vpc-v4, 🔴 « trop approximatif »).
# Testé « tel quel » sur demande explicite (constantes 0.15/0.35 FIXES = identité).
# Trio NQ1/MES1/YM1 = juge de paix cross-instrument (Gold/Oil impossibles en M5,
# pas de M1). Entrée LIMIT = fill honnête. Auto-contenu, prod intouchée.
RANGE_CARTO_STRATEGY_VERSION = "range-carto-v1"
RANGE_CARTO_TICKERS = ["NQ1", "MES1", "YM1"]
RANGE_CARTO_TIMEZONE = "America/New_York"
RANGE_CARTO_SESSION_OPEN = (9, 30)
RANGE_CARTO_SESSION_END = (16, 0)
RANGE_CARTO_ATR_PERIOD = 14
RANGE_CARTO_VP_LOOKBACK_BARS = 78  # profil sur ~1 session M5 (consolidation récente, causal)
RANGE_CARTO_BIN_TICKS = 10  # granularité du profil = bin_ticks × tick_size
RANGE_CARTO_VA_PCT = 0.70  # Value Area = 70% du volume autour du POC (standard)
RANGE_CARTO_ENTRY_EXT = 0.15  # entrée = VAH + ext×W (poche d'aspiration) — FIXE (spec user)
RANGE_CARTO_SL_EXT = 0.35  # SL = entry + sl_ext×W (cassure fondamentale) — FIXE (spec user)
RANGE_CARTO_MIN_W_ATR = 0.5  # sanity anti-dégénéré : largeur VA ≥ x×ATR
RANGE_CARTO_MAX_CONTRACTS = 30  # cap de sizing (évite n_ct absurde si SL micro)
RANGE_CARTO_ORDER_TIMEOUT_BARS = 12  # annule le limit non fillé (~1h M5)
RANGE_CARTO_MAX_TRADES_PER_DAY = 2
RANGE_CARTO_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)
RANGE_CARTO_FILL_MODE = "literal"  # cf. exp_hunt : fill au marché si ouverture déjà au-delà

# ==============================================================================
# STRATÉGIE ODC (`odc-v1`) — Open-Drive Continuation (CONTINUATION, pas fade)
# ==============================================================================
# Idée 2026-06-08 : MONÉTISER L'INVERSE de liq-hook (fade du drive d'open → NQ1
# 0% WR / 11 SL sur 11 = les drives d'open CONTINUENT). Au lieu de fader l'extension,
# on REJOINT le drive sur un pullback en discount.
#   1. DRIVE : 3 bougies M5 consécutives de même sens depuis 09:30 NY (cash open).
#      Jambe = origine (début du drive) → extrême. Filtre force : jambe ≥ k×ATR
#      (la SÉLECTION qui manquait aux géométries nues — cf. ib-retest « cassure franche »).
#   2. ENTRÉE : LIMIT au pullback (retrace `pullback_retr` de la jambe) DANS le sens
#      du drive. Fill HONNÊTE (pas de stop-entry look-ahead, cf. leçon asian-sweep).
#   3. SL : au-delà de l'ORIGINE du drive ∓ buf×jambe (jamais dans la trajectoire,
#      cf. leçon liq-hook). TP = rr×SL_dist. 1 trade/jour (setup 1-shot).
# CONTINUATION dans le sens du flux = aligné avec ce qui MARCHE (OPR/bos-fvg/ib-retest).
# GATE CLÉ : corrélation avec OPR (s'arme ~09:45 comme OPR → vérifier additivité).
# M5 resamplé M1, NQ1/MES1/YM1 (juge de paix). Auto-contenu, prod intouchée.
ODC_STRATEGY_VERSION = "odc-v1"
ODC_TICKERS = ["NQ1", "MES1", "YM1"]
ODC_TIMEZONE = "America/New_York"
ODC_SESSION_OPEN = (9, 30)
ODC_SESSION_END = (16, 0)
ODC_DRIVE_BARS = 3  # nb de bougies M5 du drive (09:30/09:35/09:40)
ODC_ATR_PERIOD = 14
ODC_MIN_DRIVE_ATR = 0.8  # filtre force : jambe du drive ≥ x×ATR (sélection)
ODC_PULLBACK_RETR = 0.382  # profondeur du pullback (retrace de la jambe) — entrée LIMIT
ODC_SL_BUF = 0.25  # SL = origine ∓ buf×jambe (au-delà de l'origine)
ODC_RR = 2.0  # TP = rr × SL_dist
ODC_ORDER_TIMEOUT_BARS = 12  # annule le pullback non fillé (~1h M5)
ODC_MAX_TRADES_PER_DAY = 1
ODC_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)
ODC_FILL_MODE = "literal"  # cf. exp_hunt : fill au marché si ouverture déjà au-delà du niveau

# ==============================================================================
# STRATÉGIE PHC (`phc-v1`) — Power-Hour Continuation (ib-retest sur range de midi)
# ==============================================================================
# Idée 2026-06-08 : appliquer la MÉCANIQUE PROUVÉE d'ib-retest (🟡 survivant —
# « la cassure FRANCHE ≥0.5×ATR EST l'edge d'un retest-continuation ») à une autre
# fenêtre de session = DÉCORRÉLÉ par construction du book matmassé sur l'open.
#   1. RANGE DE MIDI = [12:00, 14:00[ NY (consolidation déjeuner). Fixée à 14:00.
#   2. CASSURE FRANCHE : close ≥ 0.5×ATR au-delà de la borne, dans [14:00, cutoff[.
#   3. RETEST : LIMIT sur la borne cassée (flip support/résistance). Fill honnête.
#   4. SL = borne ∓ sl_buf×range, TP = rr×SL_dist. Invalidation : close borne opposée.
# CONTINUATION 2e jambe de l'après-midi (power hour). M5 resamplé M1, NQ1/MES1/YM1.
# 1 trade/jour, pending armé fin barre → fill j+1, SL-prio. Auto-contenu, prod intouchée.
PHC_STRATEGY_VERSION = "phc-v1"
PHC_TICKERS = ["NQ1", "MES1", "YM1"]
PHC_TIMEZONE = "America/New_York"
PHC_RANGE_START = (12, 0)  # début range de midi NY
PHC_RANGE_END = (14, 0)  # fin range de midi (causal au-delà)
PHC_ENTRY_CUTOFF = (15, 0)  # pas de nouvelle cassure armée après 15:00 NY
PHC_SESSION_END = (16, 0)  # close-all RTH
PHC_ATR_PERIOD = 14
PHC_BREAKOUT_BUF_ATR = 0.5  # cassure FRANCHE (= edge prouvé ib-retest)
PHC_MIN_RANGE_ATR = 0.0  # filtre : range_midi ≥ x×ATR (0 = off)
PHC_SL_BUF = 0.25  # SL = borne ∓ sl_buf × range
PHC_RR = 2.0
PHC_ORDER_TIMEOUT_BARS = 12  # annule le retest non fillé (~1h M5)
PHC_MAX_TRADES_PER_DAY = 1
PHC_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# ==============================================================================
# STRATÉGIE IB_RETEST (`ib-retest-v1`) — Initial Balance retest (variante OPR)
# ==============================================================================
# Backlog P3 (ib_retest_opr_variant, source Market Profile L3-4). Setup M5,
# CONTINUATION dans le sens du flux (pas un fade). Long :
#   1. IB (Initial Balance) = range de la 1ère heure RTH [09:30,10:30[ NY.
#      IB_high = plus haut, IB_low = plus bas. Fixé à 10:30.
#   2. CASSURE : une bougie CLÔTURE au-dessus de IB_high (+ buf×ATR) après 10:30.
#   3. RETEST : buy limit sur la borne IB_high (retour de la borne en support).
#   4. SL = IB_high − sl_buf × IB_range. TP = entrée + rr × sl_dist.
# Short symétrique (cassure sous IB_low, retest, sell limit). Entrée LIMIT =
# fill HONNÊTE (pullback sur la borne). DISTINCT d'OPR : fenêtre 09:30-10:30 (vs
# OPR 09:15-09:45) + entrée au RETEST (vs cassure). 1 trade/jour (1ère cassure).
# RED-FLAG ÉTAPE 0 : variante OPR (corrélation à mesurer si survivant) MAIS
# mécanisme distinct + dans le sens du flux (≠ fades rejetés liq-hook/exp-hunt).
IB_RETEST_STRATEGY_VERSION = "ib-retest-v2"  # v2 = filtre de cassure franche (v1 bo=0 = mort)
IB_RETEST_TICKERS = ["NQ1", "MES1", "YM1"]
IB_RETEST_TIMEZONE = "America/New_York"
IB_RETEST_IB_START = (9, 30)
IB_RETEST_IB_END = (10, 30)  # IB = 1ère heure RTH (Market Profile standard)
IB_RETEST_ENTRY_CUTOFF = (14, 0)  # pas de nouvelle cassure armée après 14:00 NY
IB_RETEST_SESSION_END = (16, 0)  # close-all RTH
IB_RETEST_ATR_PERIOD = 14
# v2 : la cassure doit être FRANCHE (close ≥ buf×ATR au-delà de la borne) avant
# d'armer le retest — sinon retests dans le chop = mort (v1 bo=0 : IS+OOS PF 0.95).
# Relation MONOTONE 0→0.25→0.5→1.0 : PORTF OOS PF 0.92→1.14→1.29→1.33.
IB_RETEST_BREAKOUT_BUF_ATR = 0.5
IB_RETEST_MIN_IB_ATR = 0.0  # filtre : IB_range ≥ x×ATR (0 = off)
IB_RETEST_SL_BUF = 0.25  # SL = borne ∓ sl_buf × IB_range
IB_RETEST_RR = 2.0
IB_RETEST_ORDER_TIMEOUT_BARS = 24  # annule le limit de retest non fillé (~2h M5)
IB_RETEST_MAX_TRADES_PER_DAY = 1  # 1ère cassure du jour seulement
IB_RETEST_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# ==============================================================================
# STRATÉGIE GOLDEN_POCKET (`gp-v1`) — Fib 0.618 sur la 1ère impulsion du jour
# ==============================================================================
# Backlog P3 (golden_pocket_first_impulse). DISTINCT de fib-v4/fib-fine (qui
# détectent des impulsions par pivots N'IMPORTE OÙ et tradent 0.382/0.5) :
#   • Ancré sur LA 1ère impulsion du jour RTH (1 setup / jour).
#   • Niveau golden pocket 0.618.
# Logique :
#   1. Impulsion = plus grand leg directionnel dans [9h30, GP_IMPULSE_DEADLINE[ NY
#      (swing_low→swing_high pour up ; symétrique down). Requiert range ≥
#      GP_MIN_IMPULSE_ATR × ATR journalier.
#   2. Entrée LIMIT au retracement GP_ENTRY (0.618) du leg, valide jusqu'à la cloche.
#      Long : buy limit @ swing_high - 0.618×range. Short symétrique.
#   3. SL = origine du swing ∓ GP_SL_BUF × range ; TP = entrée ± GP_RR × sl_dist.
#   4. 1 position/jour ; close-all GP_SESSION_END NY (intraday).
# Entrée LIMIT = fill honnête (pas de look-ahead stop-entry). Fill intra-bar
# SL-prioritaire. Données M15 natives full history. Check doublon : corrélation
# des trades avec fib-v4 à vérifier avant tout verdict 🟡+.
GP_STRATEGY_VERSION = "gp-v1"
GP_TICKERS = ["NQ1", "MES1", "YM1", "MGC1"]
GP_TIMEZONE = "America/New_York"
GP_SESSION_OPEN = (9, 30)
GP_IMPULSE_DEADLINE = (11, 0)  # impulsion figée à 11h00 NY
GP_SESSION_END = (16, 0)  # close-all RTH NY
GP_ATR_PERIOD = 14
GP_MIN_IMPULSE_ATR = 0.5  # taille min de l'impulsion (× ATR journalier)
GP_ENTRY = 0.618  # niveau golden pocket (retracement)
GP_SL_BUF = 0.10  # SL = origine swing ∓ buf × range
GP_RR = 2.0
GP_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# ==============================================================================
# STRATÉGIE ASIAN_SWEEP (`asian-sweep-v1`) — sweep range asiatique → reversal Londres
# ==============================================================================
# Backlog P3 (asian_range_sweep). Edge : à l'ouverture de Londres, le marché
# balaie souvent les stops posés au-delà du range de la session asiatique (faux
# breakout / liquidity sweep), puis revient dans le range. On FADE ce sweep.
#   • Range asiatique = [ASIAN_RANGE_START_UTC, ASIAN_RANGE_END_UTC[ UTC
#     (asian_high / asian_low / range).
#   • Fenêtre de trade = [ASIAN_RANGE_END_UTC, ASIAN_TRADE_END_UTC[ UTC (Londres,
#     avant l'open US 13h30) : si une bougie pique au-dessus de asian_high PUIS
#     une bougie CLÔTURE de retour sous asian_high → SHORT (fade). Symétrique LONG.
#   • SL = extrême du sweep + ASIAN_SWEEP_SL_BUF × range. TP = entrée ∓
#     ASIAN_SWEEP_TP_MULT × range (réversion vers l'autre bord). Close-all à
#     ASIAN_TRADE_END_UTC. Intraday only, 1 position à la fois.
# Marchés : MGC1 (gold, très London-driven) + MCL1 (oil). NON corrélé à la prod
# (OPR/Fib indices). Données M15 natives full history (sept 2024 → mai 2026).
# Validation cross-instrument gold vs oil = juge de paix.
ASIAN_SWEEP_STRATEGY_VERSION = "asian-sweep-v1"
ASIAN_SWEEP_TICKERS = ["MGC1", "MCL1"]
ASIAN_RANGE_START_UTC = 0  # [00:00 UTC
ASIAN_RANGE_END_UTC = 7  # 07:00 UTC) — fin range asiatique = ~open Londres
ASIAN_TRADE_END_UTC = 13  # 13:00 UTC) — close-all avant open cash US (13h30)
ASIAN_SWEEP_SL_BUF = 0.10  # SL = extrême sweep + buf × range
ASIAN_SWEEP_TP_MULT = 1.0  # TP = entrée ∓ tp_mult × range (réversion)
ASIAN_SWEEP_MIN_RANGE_TICKS = {"MGC1": 0, "MCL1": 0}  # filtre range min (off baseline)
ASIAN_SWEEP_MAX_TRADES_PER_DAY = 1
ASIAN_SWEEP_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# ==============================================================================
# STRATÉGIE OPR_BASE (`opr-base-v1`) — OPR PUR (rebond après cassure), M5 & M15
# ==============================================================================
# Demande utilisateur : repartir de la BASE de l'OPR, SANS AUCUN FILTRE, pour ne
# PAS être biaisé par opr-v4. Une seule règle de signal : rebond après cassure
# de l'opening range, retest du niveau cassé.
#
#   • OPR = opening range des 15 premières minutes du cash NY (9h30-9h45 NY) :
#       - M15 : 1 bougie (9h30).      - M5 : 3 bougies (9h30, 9h35, 9h40).
#     opr_high = max(high) ; opr_low = min(low) ; opr_mid = (high+low)/2.
#   • CASSURE : bougie post-OPR qui CLÔTURE hors de l'OPR (open dedans, close
#       dehors) → LONG si close > opr_high, SHORT si close < opr_low.
#   • ENTRÉE : ordre LIMIT au niveau OPR cassé (retest), fillé au retouch
#       (low <= niveau <= high).  → "rebond après cassure".
#   • SL : MILIEU de l'OPR. Généralisé : sl_dist = sl_frac × range_OPR
#       (sl_frac = 0.5 ⇒ milieu exact).
#   • TP : rr × sl_dist (rr = 2 au départ).
#
# AUCUN FILTRE : pas d'ATR, pas de cap trades/jour, pas de timeout d'ordre, pas
# de z-score volume, pas d'excursion minimale, pas de range minimal. Une seule
# position à la fois (mécanique, pas un filtre) ; close-all à la cloche RTH
# (intraday only) ; l'ordre limite reste valide jusqu'à la cloche.
#
# DONNÉES : M5 ET M15 RESAMPLÉS depuis le M1 DATA_BACKTEST (fév 2025 → mars 2026)
# → même période et même back-adjustment pour les deux TF → comparaison M5 vs
# M15 à périmètre égal + IS (fév-sep 2025) / OOS (oct 2025-mars 2026) propre.
#
# Les FEATURES (amélioration du winrate) sont explorées séparément à partir du
# M1 (scripts/), puis ajoutées dans une version `opr-feat-*` distincte. La base
# reste volontairement nue.
OPR_BASE_STRATEGY_VERSION = "opr-base-v1"
OPR_BASE_TICKERS = ["NQ1"]  # focus Nasdaq (demande utilisateur)
OPR_BASE_TIMEZONE = "America/New_York"
OPR_BASE_SESSION_OPEN = (9, 30)  # ouverture cash NY (heure NY, DST-aware)
OPR_BASE_OPR_END = (9, 45)  # fin de l'opening range (15 min)
OPR_BASE_SESSION_END = (16, 0)  # close-all RTH (heure NY)
OPR_BASE_SL_FRAC = 0.5  # SL = milieu OPR (fraction du range depuis l'entrée)
OPR_BASE_RR = 2.0  # RR initial (TP = rr × sl_dist)
OPR_BASE_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# ── Version FEATURED (`opr-feat-v1`) — base + qualité de formation OPR ────────
# Découverte cross-TF (scripts/opr_m1_features.py) : le rebond post-cassure marche
# nettement mieux quand l'opening range s'est formé comme un mouvement DIRECTIONNEL
# PROPRE (body_ratio élevé) AVEC accélération de volume (vol_accel). Deux features
# DIRECTION-AGNOSTIQUES (≠ biais long), mesurées en M1 sur [9h30,9h45[ (causal,
# OPR clôturé avant tout trigger). Seuils FIXES pré-spécifiés (NON optimisés) :
#   body_ratio = Σ|c-o|/Σ(h-l) ≥ 0.50  ;  vol_accel = vol(2e moitié)/vol(1re) ≥ 0.65
# Effet (RR=2, SL=milieu) : WR ~38% → ~52%, OOS PF M5 1.08→1.70 / M15 1.01→1.58.
# SL/RR optimisés séparément en walk-forward (PARAM_GRID des wrappers featured).
OPR_FEAT_STRATEGY_VERSION = "opr-feat-v1"
OPR_FEAT_BODY_RATIO_MIN = 0.50
OPR_FEAT_VOL_ACCEL_MIN = 0.65

# ==============================================================================
# STRATÉGIE OPR_H4 (`opr_h4-v1`) — recherche, variante d'opr-v4
# ==============================================================================
# Hypothèse H4 (chartist mode idea, NQ1 weeklies) : les setups OPR déclenchés
# alors que le prix est DANS le cloud Ichimoku 15m sont des faux signaux
# fréquents (zone de fair value contestée). Les filtrer doit améliorer la
# qualité au prix d'un volume de trades réduit.
#
# Filtre appliqué AVANT armement de l'ordre limite OPR, à la bougie i :
#   close[i-1] vs cloud Ichimoku [senkou_a[i-1], senkou_b[i-1]] (déjà shift +26)
#   buffer = OPR_H4_BUFFER_ATR × ATR_15m_Wilder(14)[i-1]
#   LONG  autorisé si close[i-1] > max(s_a, s_b) + buffer
#   SHORT autorisé si close[i-1] < min(s_a, s_b) - buffer
#
# Voir strategies/opr_h4.py.

OPR_H4_STRATEGY_VERSION = "opr_h4-v1"
OPR_H4_TICKERS = ["MES1", "NQ1", "YM1"]

# Filtre cloud — paramètre principal (1 dimension, calibré walk-forward)
OPR_H4_BUFFER_ATR = 0.3  # défaut. Grille testée : [0.0, 0.3, 0.5, 0.8]

# Ichimoku 15m (mêmes constantes que core.explore_chart.compute_ichimoku)
OPR_H4_ICHIMOKU_TENKAN = 9
OPR_H4_ICHIMOKU_KIJUN = 26
OPR_H4_ICHIMOKU_SENKOU_B = 52
OPR_H4_ICHIMOKU_SHIFT = 26  # Senkou A/B portent déjà shift(+26)

# ATR intraday 15m pour le buffer (Wilder)
OPR_H4_INTRADAY_ATR_PERIOD = 14

# ==============================================================================
# STRATÉGIE OPR v5 (`opr-v5`) — recherche, variante d'opr-v4 + 3 features
# ==============================================================================
# OPR v4 augmenté de 3 features de filtrage causal (post-traitement walk-forward) :
#   F1 : nb de bougies M15 entre fin OPR (label OPR + 15min = ~9h45 NY) et la
#        bougie de cassure (trigger_time). Filtre les cassures trop rapides
#        (réaction émotionnelle non digérée) ou trop tardives (post-momentum).
#   F2 : excursion max (normalisée par ATR daily) dans le sens de la cassure
#        entre trigger_time et fill_time. Filtre les "longs mouvements" suggérant
#        retournement plutôt qu'un pullback propre vers le niveau OPR.
#   F3 : nb de bougies M15 entre trigger_time et fill_time. Filtre pullbacks
#        trop tardifs (la mécanique aller-retour n'est plus réactive).
#
# Approche : wrapper post-traitement de core/opr.run_opr_day() — équivalent
# causal au filtre pré-fill. Sémantique de rejet :
#   F1 rejet → trade marqué NOT_FILLED, F2/F3 perdus (non évaluables)
#   F2/F3 rejet → trade marqué NOT_FILLED (uniquement sur trades filled natifs)
#
# Causalité (zero look-ahead) :
#   F1 utilise bougies sur (opr_ts_ny + 15min, trigger_time]   → tous ≤ trigger
#   F2/F3 utilisent bougies sur [trigger_time, fill_time]      → tous ≤ fill
#   Asserté algorithmiquement dans _compute_features_for_signal.
#
# Note frictions : core/opr.py retourne du P&L brut (pas de slippage/commission
# appliqué). Pour comparaison apples-to-apples avec opr-v4 baseline en
# production, opr-v5 reste sur le même régime gross. Cf. note opr_h4-v1
# strategies/opr_h4.py L41-42 pour la même décision.
#
# Voir strategies/opr_v5.py.

OPR_V5_STRATEGY_VERSION = "opr-v5"
OPR_V5_TICKERS = ["MES1", "NQ1", "YM1"]

# ── Filtres (None = filtre désactivé) ────────────────────────────────────────
# Bornes par défaut à None pour test de non-régression critique : avec tous
# les filtres désactivés, opr-v5 doit produire exactement les mêmes trades
# qu'opr-v4. Les bornes finales sont écrites après walk-forward (PHASE 4).
#
# Valeurs renseignées : optimum walk-forward IS=déc2024→sept2025 / OOS=oct2025→mars2026
# (cf. output/robustness_opr-v5.json) :
#   MES1 : filtres tous None → équivalent opr-v4 (pas d'edge marginal)
#   NQ1  : f2_max_atr=0.5    → OOS PF=1.55, P&L=+$5,560, n=221, BS=100 %
#   YM1  : f1_max=10, f2_max_atr=1.0 → OOS PF=3.76, P&L=+$3,032, n=53, BS=100 %
# Portfolio OOS : PF=2.17, P&L=+$9,267, BS=66.6 % → 🟡 VEILLE
OPR_V5_F1_MIN = {"MES1": None, "NQ1": None, "YM1": None}  # borne basse F1
OPR_V5_F1_MAX = {"MES1": None, "NQ1": None, "YM1": 10}  # borne haute F1
OPR_V5_F2_MAX_ATR = {"MES1": None, "NQ1": 0.5, "YM1": 1.0}  # borne haute F2
OPR_V5_F3_MAX = {"MES1": None, "NQ1": None, "YM1": None}  # borne haute F3

# ==============================================================================
# STRATÉGIE OPR v5.1 (recherche — extension v5 avec filtre data-driven f2_min_atr)
# ==============================================================================
# v5.1 ajoute UN filtre INFÉRIEUR sur F2 (`f2_min_atr`) au-dessus de la grille
# v5 existante. Motivation : l'analyse data science approfondie
# (output/data_science_opr_v5.md) a confirmé via 5 méthodes ML indépendantes
# (Random Forest, Logistic Regression, Decision Tree, grid search, permutation
# test 10 000 itér) qu'un seuil ~0.15 ATR sur F2 sépare significativement les
# trades gagnants/perdants — pattern non testé par la grille v5 (qui n'avait
# que des bornes SUPÉRIEURES).
#
# Nouveau motif de rejet : F2_excursion_too_narrow.
#
# Causalité : F2 reste calculé sur [trigger_time, fill_time] → causal au moment
# du fill, comme v5. Pas de look-ahead.
#
# Note frictions : héritage v4/v5 — P&L brut (gross) pour comparaison
# apples-to-apples avec la baseline.
#
# Voir strategies/opr_v5_1.py.

OPR_V5_1_STRATEGY_VERSION = "opr-v5.1"
OPR_V5_1_TICKERS = ["MES1", "NQ1", "YM1"]

# ── Filtres (None = filtre désactivé) ────────────────────────────────────────
# IMPORTANT : f1_max, f2_max_atr, f3_max sont FIGÉS sur les optima v5
# walk-forward déjà validés (cf rapport_opr-v5.md PHASE 4) pour isoler
# strictement le test de la nouveauté v5.1 = f2_min_atr.
#
# La grille d'optimisation v5.1 (cf strategies/opr_v5_1.py PARAM_GRID) ne fait
# varier QUE f2_min_atr. Les autres bornes sont héritées de la config v5 par
# ticker, recopiées ici pour autonomie de la stratégie v5.1.
#
# Valeurs renseignées : optimum walk-forward IS=déc2024→sept2025 / OOS=oct2025→mai2026
# (cf output/robustness_opr-v5.1.json) :
#   MES1 : f2_min_atr=0.15  → OOS PF=1.23, P&L=+$288, n=29, BS=0% (caveat ML p=0.23)
#   NQ1  : f2_min_atr=0.10  → OOS PF=2.15, P&L=+$7211, n=161, BS=100%
#   YM1  : f2_min_atr=0.15  → OOS PF=5.75, P&L=+$3216, n=45, BS=100%
# Portfolio OOS : PF=3.04, P&L=+$10715, n=235, BS=66.7% (moyenne Topstep per-ticker)
# Block-bootstrap stationnaire P(PF>1)=100% (cf robustness_opr-v5.1.md)
OPR_V5_1_F1_MIN = {"MES1": None, "NQ1": None, "YM1": None}  # borne basse F1
OPR_V5_1_F1_MAX = {"MES1": None, "NQ1": None, "YM1": 10}  # ← optimum v5
OPR_V5_1_F2_MIN_ATR = {"MES1": 0.15, "NQ1": 0.10, "YM1": 0.15}  # ← optimum v5.1
OPR_V5_1_F2_MAX_ATR = {"MES1": None, "NQ1": 0.5, "YM1": 1.0}  # ← optimum v5
OPR_V5_1_F3_MAX = {"MES1": None, "NQ1": None, "YM1": None}  # ← optimum v5

# ──────────────────────────────────────────────────────────────────────────────
# DÉPLOIEMENT LIVE v5.1 — schéma A (entrée différée)
# ──────────────────────────────────────────────────────────────────────────────
# Tickers où la stratégie v5.1 est ACTIVE en live. Les tickers absents de cette
# liste utilisent v4 (core.opr.run_opr_day) en pass-through.
#
# MES1 exclu car edge ML F2 non significatif (p=0.23 sur 10 000 permutations,
# BS Topstep OOS = 0 % sur n=29). NQ1 et YM1 inclus (p<0.0001, BS=100 % chacun).
# Cf. rapport opr-v5.1 §7 et output/no_mes1/robustness_opr-v5.1.{json,md}.
#
# Fidélité attendue (mesurée par scripts/live_eq_v5_1.py) :
#   - 74 % avec granularité M15 strict
#   - 26 % des trades backtest sont rejetés en live (F2 cross dans la M15
#     de fill, non distinguable avant le fill)
# Phase A (M1 polling) pourrait améliorer cette fidélité — à évaluer après
# le burn-in.
# NQ1 mis EN PAUSE le 2026-06-01 (edge non robuste, double confirmation) :
#   • Post-fix « bougie ambiguë → SL » : PF OOS 2.29 → 1.23 (94 % des trades
#     se résolvaient dans la bougie de fill = artefact de résolution optimiste).
#   • Analyse MFE/MAE sur M1 (scripts/opr_sltp_mfe_mae.py) : excursions
#     symétriques (MFE≈MAE), 0 cellule SL/TP à stops actifs tient PF≥1.3
#     cohérent IS↔OOS → pas d'edge de gestion. Cf. project_opr_sltp_mfe_mae_2026-06-01.
#   • NQ1 reste tradé par fib-v4 (honnête, robuste). YM1 OPR conservé (PF OOS 4.42).
# Pour réactiver NQ1 : remettre "NQ1" dans la liste. Code v5.1 conservé intact.
OPR_V5_1_LIVE_TICKERS = ["YM1"]  # MES1 sur v4 (legacy) ; NQ1 en pause (cf. ci-dessus)

# Tickers tradés via OPR v4 fallback (legacy). Vidé le 2026-05-21 :
# OPR/MES1 (v4) en veille — PF 1.16 vs portfolio 2.67, drag confirmé sur 20 mois.
#   • Apporte +$1,946 PnL à risk $100 (~+$97/mois marginal) pour DD individuel
#     -$1,217 (= -$3,652 à risk $300)
#   • Représente 20% des trades (213/1029) pour 3.6% du PnL
#   • Retrait → PF +19%, WR +5.1pp, DD -12% sur le portefeuille
# Pour réactiver MES1 : remettre ["MES1"]. Code v4 conservé intact.
OPR_V4_LIVE_TICKERS = []

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

# NOTE 2026-05-19 : fib-v3 a été supprimée du code prod, mais les constantes
# ci-dessous restent partagées par fib-v4 (helpers core/fib_helpers.py +
# core/strategy_fib_v4.py) — c'est pourquoi la section "STRATÉGIE FIB" est
# renommée en "CONSTANTES FIB partagées". Cf docs/strategies_abandoned.md.

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
FIB_ORDER_TIMEOUT_BARS = 12  # ordre limite annulé après ~3h
FIB_MAX_HOLD_BARS = 32  # fermeture forcée après ~8h

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
FIB_LEVEL_PER_TICKER = {
    "MES1": 0.382,
    "NQ1": 0.382,
    "YM1": 0.382,
    # fib-v4 — extension recherche multi-actifs / multi-niveaux. Valeurs par
    # défaut posées à 0.382 (référence) ; les niveaux 0.5 / 0.618 sont testés
    # par scripts/baseline_fib_v4.py via override `fib_level` du wrapper.
    "MGC1": 0.382,
    "MCL1": 0.382,
}

# SL/TP/IMP per-ticker — calibrés walk-forward via draft_fibo_50/optimize_fib_levels.py
# Performance OOS validée pour le niveau 38.2 % (sans filtres trigger) :
#   MES1  IS Sharpe=2.99  OOS Sharpe=1.66  OOS PF=1.27  OOS P&L=+$1 156  (n=77)
#   NQ1   IS Sharpe=5.52  OOS Sharpe=4.52  OOS PF=1.79  OOS P&L=+$765   (n=35)
#   YM1   IS Sharpe=1.04  OOS Sharpe=1.44  OOS PF=1.22  OOS P&L=+$640   (n=57)
# MGC1/MCL1 : valeurs préliminaires fib-v4 (à raffiner phase 4 walk-forward).
FIB_SL_ATR_MULT_PER_TICKER = {"MES1": 0.75, "NQ1": 1.50, "YM1": 1.00, "MGC1": 1.00, "MCL1": 1.50}
FIB_TP_ATR_MULT_PER_TICKER = {
    "MES1": 1.50,
    "NQ1": 1.50,
    "YM1": 2.00,  # MES1 : 2.0→1.5 (fib-v3)
    "MGC1": 1.50,
    "MCL1": 2.00,
}
FIB_MIN_IMPULSE_ATR_PER_TICKER = {
    "MES1": 1.00,
    "NQ1": 1.00,
    "YM1": 2.00,  # MES1 : 2.0→1.0 (fib-v3)
    "MGC1": 1.00,
    "MCL1": 1.50,
}

# Fenêtre de session horaire par ticker (heures UTC).
# Clés de SESSION_WINDOWS dans core/strategy_fib.py.
# MES1 : "no_nuit" (0h–21h UTC) validé 🟢 fib-v3 — OOS PF=1.82, BS=100%
# NQ1/YM1 : "us_session" inchangé
# MGC1/MCL1 : "us_session" par défaut fib-v4 (peut être révisé phase 4).
FIB_SESSION_PER_TICKER = {
    "MES1": "no_nuit",  # 0h–21h UTC
    "NQ1": "us_session",  # 13h–21h UTC
    "YM1": "us_session",  # 13h–21h UTC
    "MGC1": "us_session",  # 13h–21h UTC (fib-v4 préliminaire)
    "MCL1": "us_session",  # 13h–21h UTC (fib-v4 préliminaire)
}

# ──────────────────────────────────────────────────────────────────────────────
# STRATÉGIE FIB v4 — extension recherche multi-actifs / multi-niveaux
# avec 2 conditions d'invalidation data-driven (Phase 4 walk-forward) :
#   1. Pivot break : annule le pending si le prix casse le pivot d'impulse
#      au-delà du buffer ATR pendant la phase pending.
#   2. Wick excess : annule le fill si la barre de fill perce le niveau
#      Fibonacci de plus de `wick_max` ATR (= washout — edge négatif).
#
# Configuration calibrée walk-forward IS/OOS sur historique sept 2024 →
# mai 2026 (cf. output/rapport_fib-v4_optimize.md).
# ──────────────────────────────────────────────────────────────────────────────
FIB_V4_STRATEGY_VERSION = "fib-v4"
FIB_V4_ENABLED = True  # PROMU EN PRODUCTION 2026-05-19 — MES1+NQ1+MGC1

# Univers de production retenu après Phase 4 (verdict 🟢) :
#   MES1, NQ1 (M15 fib=0.382) ; MGC1 (M15 fib=0.5).
# YM1 et MCL1 EXCLUS : YM1 a n_oos < 20 → 🟡 VEILLE ; MCL1 = REJET structurel
# (Fib inadapté au crude, cf. note historique stratégie ARF).
FIB_V4_TICKERS = ["MES1", "NQ1", "MGC1"]

# Niveau Fibonacci retenu par ticker — issu de Phase 4 (PF OOS le plus haut).
FIB_V4_LEVEL_PER_TICKER = {
    "MES1": 0.382,  # PF OOS = 6.01 (Phase 4, wick<0.05)
    "NQ1": 0.382,  # PF OOS = 6.47 (Phase 4, wick<0.80)
    "MGC1": 0.500,  # PF OOS = 2.53 (Phase 4, wick<0.40)
}

# Buffer pivot break en ATR : annule pending si LONG close < swing_low − buffer
# (symétrique SHORT). 0.0 = strict (toute cassure invalide).
FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER = {
    "MES1": 0.0,
    "NQ1": 0.0,
    "MGC1": 0.0,
}

# Seuil maximum wick_through_atr : annule le fill si la mèche du fill perce
# au-delà du niveau Fib de plus de `wick_max` × ATR (signal de washout).
# Découvert par walk-forward Phase 4 (selection IS, validation OOS).
FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER = {
    "MES1": 0.05,  # MES1 très strict — fills propres uniquement
    "NQ1": 0.80,  # NQ1 plus tolérant — vol naturellement plus large
    "MGC1": 0.40,
}

# Skip macro days par ticker — découvert Phase 6 stress sur MGC1 :
# `is_macro_day=True` → PF=0.49 (n=4 ; statistiquement faible mais signal
# directionnel négatif). Filtre prophylactique appliqué AVANT promotion live.
# True = on évite les jours macro pour ce ticker.
FIB_V4_SKIP_MACRO_PER_TICKER = {
    "MES1": False,  # 1.7% des fills sur macro days, signal neutre
    "NQ1": False,  # 0 trade en macro days OOS, pas de signal
    "MGC1": True,  # ⚠️ PF macro=0.49 (n=4) — filtre prudentiel actif
}

# ==============================================================================
# STRATÉGIE ARES — Asian Range European Session Breakout
# ==============================================================================
# Concept : la session asiatique (20h NY veille → 02h NY courant) définit un
# range. En session européenne (02h → 07h NY), on trade le breakout pur de ce
# range dans le sens du biais directionnel (position du dernier close asiatique
# vs midpoint du range), confirmé par la première bougie 02h-02h15 NY.
#
# SL = extrémité opposée du range ± buffer (full range).
# TP calculé DEPUIS le high/low asiatique (pas depuis entry) :
#   TP LONG  = asian_high + buffer + asian_range × TP_MULT
#   TP SHORT = asian_low  - buffer - asian_range × TP_MULT
# RR attendu ≈ 0.45-0.55 (asian_range × tp_mult / (asian_range + 2 × buffer)).
#
# Voir strategies/ares.py — RECHERCHE pure.

ARES_STRATEGY_VERSION = "ares-v1"

# Buffer de cassure en points au-delà des extrêmes asiatiques.
# Calibré par ticker en fonction du tick et de la volatilité habituelle.
ARES_BUFFER_PTS = {"NQ1": 4, "MES1": 1, "YM1": 4}

# Multiplicateur du range asiatique pour le calcul du TP.
# TP_MULT = 0.5 → TP à 50 % du range au-delà du point d'entrée.
ARES_TP_MULT = {"NQ1": 0.5, "MES1": 0.6, "YM1": 0.5}

# Range minimum en points pour valider le setup (élimine les jours trop calmes).
ARES_MIN_RANGE = {"NQ1": 79, "MES1": 16, "YM1": 95}

# Coupure horaire en heure NY : tout break ≥ ENTRY_CUTOFF_HOUR est ignoré.
ARES_ENTRY_CUTOFF_HOUR = 7

# Fenêtres de session (heures NY, DST-aware via zoneinfo).
ARES_ASIAN_START_HOUR = 20  # début session asiatique (soirée veille NY)
ARES_ASIAN_END_HOUR = 2  # fin session asiatique (exclusive, matin NY)
ARES_EURO_START_HOUR = 2  # début fenêtre d'entrée européenne (NY)

# ==============================================================================
# BROKER PROJECTX / TOPSTEPX
# ==============================================================================
# Mapping tickers internes → symboles ProjectX (recherche de contrats).
# Utilisé par broker/projectx_client.py et broker/live_runner.py.
PROJECTX_BASE_URL = "https://api.topstepx.com"
PROJECTX_SYMBOLS = {"MES1": "MES", "NQ1": "MNQ", "YM1": "MYM", "MGC1": "MGC"}

# Note : le flag `live` envoyé à l'API ProjectX (search_contract, get_bars,
# place_order) est déterminé automatiquement à partir du type de compte
# renvoyé par l'API (`account.simulated`) — voir live.py:_build_runner.
# Quand Topstep promeut Combine → Funded, `simulated=False` côté API et le
# daemon passe automatiquement en mode live au prochain restart.

# ==============================================================================
# TELEGRAM
# ==============================================================================
# Credentials chargés depuis .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
# Laisser vide pour désactiver complètement.

# Activation globale (False → aucun message envoyé, toutes les méthodes no-op)
TELEGRAM_ENABLED = True

# Niveaux d'alerte activables indépendamment
TELEGRAM_LEVEL_TRADES = True  # fills, clôtures, signaux, ordres placés
TELEGRAM_LEVEL_RISK = True  # blocages RM, limites approchantes, breach Topstep
TELEGRAM_LEVEL_SYSTEM = True  # erreurs API, perte de connexion
TELEGRAM_LEVEL_REPORT = True  # bilan de session (Niveau 2)
TELEGRAM_LEVEL_COMMANDS = True  # /status bidirectionnel (Niveau 3)

# Nombre de barres 15m à fetcher pour garantir le warmup des indicateurs
# (ATR journalier OPR = 14 jours × 80 bougies = 1120 ; Fib warmup = 250).
# 2000 barres ≈ 25 jours de trading → confortable.
PROJECTX_BARS_WARMUP = 2000

# Fichier d'état persistant du live runner
LIVE_STATE_FILE = "state/live_state.json"

# ==============================================================================
# REALTIME (SignalR User Hub) — fast path event-driven
# ==============================================================================
# Connexion WebSocket SignalR au User Hub ProjectX (rtc.topstepx.com/hubs/user).
# Reçoit en temps réel les events GatewayUserOrder/Position/Trade/Account →
# détection fill/close < 1 s au lieu de 30 s (polling REST). Le polling REST 30 s
# reste actif comme filet de sécurité — idempotence garantie par
# placed_tags[tag].status (helpers _handle_*_transition vérifient l'état avant
# tout register_fill/close/cancel_open sur le RM).
#
# SignalR n'a pas de replay — les events émis pendant un outage WS sont perdus.
# C'est exactement pour ça que le polling REST reste autoritatif.
#
# Désactivé par défaut, à flipper après burn-in sur compte sim.

PROJECTX_REALTIME_ENABLED = True  # flip ON 2026-05-18 (burn-in WS)
PROJECTX_REALTIME_HUB_URL = "https://rtc.topstepx.com/hubs/user"
PROJECTX_REALTIME_QUEUE_MAXSIZE = 2048  # ~3 min de full-speed à 10 evt/s
PROJECTX_REALTIME_RECONNECT_DELAYS = (0, 2, 5, 10, 30, 60, 120)  # secondes
PROJECTX_REALTIME_MAX_SILENCE_S = 180  # > 3 min sans event → rebuild
PROJECTX_REALTIME_FORCE_REAUTH_S = 22 * 3600  # rebuild forcé pour JWT frais
PROJECTX_REALTIME_ALERT_OUTAGE_S = 600  # alerte Telegram si WS down > 10 min
PROJECTX_REALTIME_DEBUG_EVENTS = False  # logger chaque event (debug)

# ==============================================================================
# PROJECTX MARKET HUB STREAMING (Phase C — running F2 sur bars M1 intra-bar)
# ==============================================================================
# Connexion WebSocket au Market Hub ProjectX (rtc.topstepx.com/hubs/market) pour
# recevoir les trades exécutés (GatewayTrade) et reconstruire en RAM des bars M1
# OHLCV. Permet à OPR v5.1 de mesurer le running F2 intra-bar M15 au lieu de
# n'avoir l'info qu'à la close M15 (fidélité backtest 74 % → cible 90 % +).
#
# Architecture (cf. broker/projectx_market_realtime.py + broker/m1_buffer.py) :
#   - Volume événements ~30-200 evt/s par contract en RTH → queue 10k+
#   - max_silence_s plus court (60 s) car Market Hub est très bavard
#   - Bars M1 = trades only (quotes ignorées pour OHLCV)
#   - SignalR sans replay → fallback M15 si buffer indispo ou bars manquants
#
# Désactivé par défaut. Le code est additif : si OFF, OPR v5.1 reste en M15
# strict (comportement actuel). Flip ON après burn-in sim 1 session OPR
# complète sans crash thread.

PROJECTX_MARKET_REALTIME_ENABLED = True  # flip ON 2026-05-18 (burn-in Phase C)
PROJECTX_MARKET_REALTIME_HUB_URL = "https://rtc.topstepx.com/hubs/market"
PROJECTX_MARKET_REALTIME_QUEUE_MAXSIZE = 20_000  # ~1-2 min full-speed à 200 evt/s
PROJECTX_MARKET_REALTIME_RECONNECT_DELAYS = (0, 2, 5, 10, 30, 60, 120)
PROJECTX_MARKET_REALTIME_MAX_SILENCE_S = 60  # > 60s sans event → rebuild
PROJECTX_MARKET_REALTIME_FORCE_REAUTH_S = 22 * 3600
PROJECTX_MARKET_REALTIME_ALERT_OUTAGE_S = 600
PROJECTX_MARKET_REALTIME_BUFFER_MINUTES = 120  # bars M1 historisés en RAM
PROJECTX_MARKET_REALTIME_DEBUG_EVENTS = False

# Activation du buffer M1 dans le calcul F2 running d'OPR v5.1.
# Si False : v5.1 reste en M15 strict (fallback). Pour passer True, le buffer
# doit avoir été démarré (PROJECTX_MARKET_REALTIME_ENABLED=True).
OPR_V5_1_USE_M1_BUFFER = True  # flip ON 2026-05-18 — F2 intra-bar M15

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
BACKTEST_CHART_CONTEXT_BEFORE = 50  # Bougies avant le fill
BACKTEST_CHART_CONTEXT_AFTER = 20  # Bougies après la sortie

# ==============================================================================
# GRAPHIQUES D'ANALYSE JOURNALIERS (1 PNG / jour tradé / ticker)
# ==============================================================================
# Voir CLAUDE.md → "Graphiques d'analyse journaliers (consigne pérenne)" :
# toute nouvelle stratégie doit produire ces graphiques en backtest pour
# permettre une revue visuelle rapide. Les fichiers sont stockés sous
# output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/{YYYY-MM-DD}.png.
STRATEGY_VERSION = "v5.2"  # tag de la stratégie courante
ANALYSIS_CHARTS_ENABLED = True  # générer ces graphiques par défaut en backtest
ANALYSIS_CHART_CONTEXT_BEFORE = 200  # bougies 15m avant cutoff (cf. spec utilisateur)

# ==============================================================================
# ==============================================================================
# STRATÉGIE OPR_GOLD (évolution OPR v4)
# ==============================================================================
# Améliorations vs OPR v4 :
#   F1 — Time-based exit à 15h45 NY
#   F2 — Profit lock à +0.5R → breakeven
#   F3 — Filtre tendance J-1 via MA20 daily (long si veille > MA20)
#   F4 — Sizing adaptatif : demi-lot si ATR(5j) > 1.5 × ATR(14j)
#   F5 — Pullback qualifié (≤ 4 barres M15, retracement max 50 % range OPR)
#   F6 — TP multi-niveau : TP1 = 1.2R (50 % position), TP2 = 2.5R (50 % restants)
#   Bonus — Filtre macro (skip si jour dans MACRO_EVENT_DATES)
#
# v2 — corrections post-diagnostic PHASE 3/4 :
#   • SL_ATR_MULT augmenté (MES1 0.15→0.25, NQ1 0.05→0.30) pour garantir
#     n_ct >= 2 et activer le split TP1/TP2 (sinon n_tp2=0 → F6 inopérant)
#   • YM1 exclu (PF OOS systématiquement < 0.40 sur toutes combinaisons,
#     dérive persistante oct2025→fév2026 — cohérence avec OPR_v4 YM1_ENABLED=False)
#   • tp1_rr 1.0→1.2, tp2_rr 2.0→2.5 (paramètres retenus walk-forward v2)
OPR_GOLD_STRATEGY_VERSION = "opr_gold-v2"
OPR_GOLD_TICKERS = ["MES1", "NQ1"]  # YM1 exclu jusqu'à preuve OOS
OPR_GOLD_ATR_PERIOD = 14  # période ATR journalier
OPR_GOLD_TREND_MA_PERIOD = 20  # MA daily pour filtre tendance J-1
# sl_mult augmenté vs v1 : garantit n_ct >= 2 sur MES1/NQ1 → split TP1/TP2 actif
OPR_GOLD_SL_ATR_MULT = {"MES1": 0.25, "NQ1": 0.30, "YM1": 0.12}
OPR_GOLD_TP1_RR = 1.2  # TP1 en multiple du SL_dist (v2: 1.0→1.2)
OPR_GOLD_TP2_RR = 2.5  # TP2 en multiple du SL_dist (v2: 2.0→2.5)
OPR_GOLD_PROFIT_LOCK_R = 0.5  # déclencheur profit lock (en R)
OPR_GOLD_PULLBACK_MAX_BARS = 4  # max barres M15 pour le retest
OPR_GOLD_PULLBACK_MAX_RETRACE = 0.5  # max retracement (fraction de la range OPR)
OPR_GOLD_ATR_FILTER_MULT = 1.5  # filtre jour trop volatil (ATR_court > MULT × ATR_long)
OPR_GOLD_TIME_EXIT_HOUR = 15  # fermeture forcée heure NY
OPR_GOLD_TIME_EXIT_MINUTE = 45  # fermeture forcée minute NY
OPR_GOLD_SKIP_MACRO = True  # skip si jour macro (MACRO_EVENT_DATES)
OPR_GOLD_SESSION_END = (16, 30)  # clôture session US (heure NY)

# ==============================================================================
# STRATÉGIE KIJUN_PB — Kijun pullback bidirectionnel (kijun_pb-v1)
# ==============================================================================
# Concept :
#   En régime trending (prix nettement hors Cloud Ichimoku 15m), la Kijun(26)
#   sert de fair value à moyen terme. Les retracements vers la Kijun en
#   alignement avec un cross StochRSI (depuis survente pour LONG, depuis
#   surachat pour SHORT) offrent une entrée limit à fort R:R.
#
# Edge théorique :
#   Beaucoup de stratégies retail/quant utilisent Ichimoku → confluence
#   d'ordres limit autour de la Kijun. Le filtre Cloud écarte les régimes
#   plats où la Kijun n'a pas de "défense" significative. Le cross StochRSI
#   filtre les pullbacks sans rebond effectif.
#
# Risque :
#   La Kijun est un indicateur ultra-populaire ; si l'edge subsiste, c'est
#   probablement parce que le filtre StochRSI extreme cross + Cloud breakout
#   sélectionne un sous-ensemble très étroit de setups (vs les implémentations
#   "Ichimoku simple" massivement arbitrées). À surveiller en live.
#
# Falsification :
#   - Bootstrap portfolio OOS < 50 %, OU
#   - PF OOS < 1.0 sur 50 trades consécutifs en live.
# ──────────────────────────────────────────────────────────────────────────────

KIJUN_PB_STRATEGY_VERSION = "kijun_pb-v1"

# V1 : pilote NQ1. Extension MES1/YM1 conditionnée à OOS NQ1 ≥ 🟡.
KIJUN_PB_TICKERS = ["NQ1"]

# Fenêtre NY (DST-aware via zoneinfo) : skip 09:30 (open noise) et fin de journée
KIJUN_PB_TIMEZONE = "America/New_York"
KIJUN_PB_SESSION_START = (9, 45)
KIJUN_PB_SESSION_END = (14, 0)

# Paramètres Ichimoku (standards) — utilisés via core.explore_chart.compute_ichimoku
KIJUN_PB_TENKAN_PERIOD = 9
KIJUN_PB_KIJUN_PERIOD = 26
KIJUN_PB_SENKOU_B_PERIOD = 52
KIJUN_PB_KUMO_SHIFT = 26

# Paramètres StochRSI (standards) — utilisés via core.explore_chart.compute_stochrsi
KIJUN_PB_RSI_PERIOD = 14
KIJUN_PB_STOCH_PERIOD = 14
KIJUN_PB_STOCH_K = 3
KIJUN_PB_STOCH_D = 3
KIJUN_PB_STOCHRSI_OVERSOLD = 30.0  # K[i-2] < 30 pour LONG
KIJUN_PB_STOCHRSI_OVERBOUGHT = 70.0  # K[i-2] > 70 pour SHORT

# Pente Kijun (lookback en barres M15 pour évaluer la tendance moyen terme)
KIJUN_PB_SLOPE_LOOKBACK = 5

# ATR pour SL et buffer Cloud (Wilder 14)
KIJUN_PB_ATR_PERIOD = 14

# Grille d'optimisation (24 combinaisons → Bonferroni n_tests=24)
#   BUFFER_ATR     : marge d'extension hors Cloud (avant Kijun) → trending fort
#   LOOKBACK       : profondeur du pullback récent (en barres M15)
#   SL_BUFFER_ATR  : marge sous le low du pullback pour le SL
#   TP_ATR_MULT    : objectif en multiples d'ATR (entry ± mult × ATR)
KIJUN_PB_BUFFER_ATR_DEFAULT = 0.5
KIJUN_PB_LOOKBACK_DEFAULT = 5
KIJUN_PB_SL_BUFFER_ATR_DEFAULT = 0.5
KIJUN_PB_TP_ATR_MULT_DEFAULT = 2.0

# Sécurité : floor en ticks au cas où l'ATR serait très faible
KIJUN_PB_SL_BUFFER_TICKS_FLOOR = 2

# Max trades/jour/ticker
KIJUN_PB_MAX_TRADES_PER_DAY = 2

# Vie de l'ordre limit (en barres M15) — 1 barre = fill à la barre i si touché,
# sinon NOT_FILLED (conservateur : pas de courir après le marché)
KIJUN_PB_ORDER_TIMEOUT_BARS = 1

# Durée max de hold (en barres M15) — fermeture forcée au close
# 16 barres = 4h, couvre la session 09:45→14:00 même pour entrée tardive
KIJUN_PB_MAX_HOLD_BARS = 16

# Exclusion jours macro US (FOMC/CPI/NFP perturbent la structure Ichimoku)
KIJUN_PB_EXCLUDE_MACRO_DAYS = False  # V1 : on garde tout pour mesurer l'impact

# ==============================================================================
# STRATÉGIE SMC (Smart Money Concepts - LuxAlgo) — smc-v1
# ==============================================================================
# Concept :
#   Multi-zone Smart Money Concepts : 5 types de zones détectées en temps réel
#   sur M15 (causal) :
#     - OB_internal  : Order Block sur pivots length=5  (latence +5 barres)
#     - OB_swing     : Order Block sur pivots length=50 (latence +50 barres)
#     - FVG bull/bear: Fair Value Gap (gap 3-bougies)   (latence +0, à la close)
#     - EQH bearish  : 2 pivots high égaux (length=3)   (latence +3 barres)
#     - EQL bullish  : 2 pivots low égaux  (length=3)   (latence +3 barres)
#
#   Entry : LIMIT au bord PROCHE de la zone (variante V_A).
#   Filtre dur Premium/Discount : long autorisé uniquement si fill_price ≤ mid
#   du dernier swing range confirmé (Discount), short si ≥ mid (Premium).
#
#   Exits :
#     SL = bord opposé de la zone ± SMC_SL_BUFFER_TICKS
#     TP = prochain swing HH (long) / LL (short) confirmé APRÈS fill_bar
#          (scan à partir de fill_bar + SMC_SWING_FILTER_LENGTH)
#     TP fallback : entry ± SMC_TP_FALLBACK_RR × sl_dist si pas de pivot
#                   dans SMC_TP_FALLBACK_BARS barres.
#
#   Priorité multi-zone : OB_swing > OB_internal > FVG > EQH/EQL
#   Max SMC_MAX_TRADES_PER_DAY trades/jour/ticker.
#   Macro days : SKIP via MACRO_EVENT_DATES.
#
# Edge théorique :
#   Confluence d'ordres institutionnels (entry liquidity zones) renforcée par
#   le filtre Premium/Discount. Les pivots length=50 + filtre directionnel
#   sélectionnent un sous-ensemble étroit de setups à fort R:R asymétrique
#   (SL serré au bord opposé de la zone, TP au prochain swing).
#
# Falsification :
#   Bootstrap portfolio OOS < 50 %, OU PF OOS < 1.0 sur 60 trades live.
# ──────────────────────────────────────────────────────────────────────────────

SMC_STRATEGY_VERSION = "smc-v1"

# Tickers (M15) — actifs standards
SMC_TICKERS = ["MES1", "NQ1", "YM1"]

# Détection des pivots (LuxAlgo style)
SMC_PIVOT_LENGTH_INTERNAL = 5  # OB_internal
SMC_PIVOT_LENGTH_SWING = 50  # OB_swing
SMC_PIVOT_LENGTH_EQHL = 3  # EQH/EQL

# FVG
SMC_FVG_MIN_GAP_TICKS = 1  # gap minimum (≥1 tick) pour valider FVG

# EQH/EQL : seuil de "quasi-égalité" entre 2 pivots, normalisé par ATR
SMC_EQH_EQL_THRESHOLD = 0.10  # |h2 - h1| ≤ 0.10 × ATR

# Swing filter length : utilisé pour scan TP (next swing HH/LL) après fill
SMC_SWING_FILTER_LENGTH = 10

# Âge max d'une zone avant qu'elle soit considérée obsolète (en barres M15)
SMC_ZONE_MAX_AGE_BARS = 96  # ~4 jours de session NY M15

# SL : buffer en ticks au-delà du bord opposé de la zone
SMC_SL_BUFFER_TICKS = 2

# TP fallback (si aucun swing HH/LL trouvé)
SMC_TP_FALLBACK_RR = 1.5  # entry ± 1.5 × sl_dist
SMC_TP_FALLBACK_BARS = 40  # bornes max du scan post-fill

# Sizing
SMC_MAX_CONTRACTS_PER_TRADE = 3  # plafond contrats (sécurité Topstep)
SMC_MAX_TRADES_PER_DAY = 2  # par ticker

# Fenêtre NY (DST-aware via zoneinfo) — session cash
SMC_SESSION_START_NY = (9, 30)
SMC_SESSION_END_NY = (12, 0)

# Priorité multi-zones (du plus fort au plus faible)
SMC_ZONE_PRIORITY = ["OB_swing", "OB_internal", "FVG", "EQH", "EQL"]

# ATR période pour calculs zone_width_atr et adx_at_entry
SMC_ATR_PERIOD = 14
SMC_ADX_PERIOD = 14

# Exclusion jours macro (FOMC/CPI/NFP)
SMC_EXCLUDE_MACRO_DAYS = True

# ==============================================================================
# STRATÉGIE PIVOT-REV (mean-reversion ML MGC1 M5)
# ==============================================================================
# Concept : "wash out" volumétrique sur MGC1 M5 + filtre ML (RandomForest
# entraîné sur IS uniquement, label oracle argrelextrema(order=20)).
# Détection : proba_RF ≥ p10%_IS AND vol_rel ≥ VOL_REL_MIN AND
#             range_atr_ratio ≥ 1.5 AND hour_ny ≥ 6 AND NOT macro_day.
# Trigger   : stop entry au high/low de la bougie candidate ± 1 tick,
#             valide CONFIRM_BARS barres M5 (15 min si =3).
# Exit      : triple barrier — SL = max(1×ATR14, low/high candidate ± 0.5 tick),
#             TP = entrée ± RR_TARGET × SL_dist, time barrier 20 barres M5,
#             clôture forcée 15:50 NY.
# Validation : HOLDOUT PUR (PAS de walk-forward standard projet).
#   IS  = 2025-10-19 → 2026-01-31 (entraînement RF + grid search)
#   OOS = 2026-02-01 → 2026-05-15 (mesure unique, jamais ré-optimisé)
#   PURGE 20 barres M5 en frontière (label leakage argrelextrema order=20)
# ─────────────────────────────────────────────────────────────────────────────

PIVOT_REV_STRATEGY_VERSION = "pivot-rev-v1"
PIVOT_REV_TICKERS = ["MGC1"]

# ── Schéma holdout pur (PAS le walk-forward standard) ────────────────────────
PIVOT_REV_IS_START = "2025-10-19"
PIVOT_REV_IS_END = "2026-01-31"
PIVOT_REV_OOS_START = "2026-02-01"
PIVOT_REV_OOS_END = "2026-05-15"
PIVOT_REV_PURGE_BARS = 20  # barres M5 purgées en frontière IS/OOS

# ── ML — RandomForest (cf. scripts/archive/research_pivot/research_pivot_nq1.py) ─
PIVOT_REV_PIVOT_ORDER = 20  # argrelextrema(close, order=20) → pivots stables
PIVOT_REV_RF_N_ESTIMATORS = 300
PIVOT_REV_RF_MAX_DEPTH = 8
PIVOT_REV_RF_MIN_SAMPLES_LEAF = 20
PIVOT_REV_RF_RANDOM_STATE = 42
PIVOT_REV_RF_QUANTILE_IS = 0.90  # seuil p10% IS = quantile 90% de proba_RF sur IS

# ── Filtres fixes (validés par étude amont, NON optimisés) ───────────────────
PIVOT_REV_RANGE_ATR_RATIO_MIN = 1.5
PIVOT_REV_HOUR_NY_MIN = 6  # 06:00 NY → 16:00 NY (capture US session complète)
PIVOT_REV_HOUR_NY_MAX = 16

# ── Filtres optimisables (3³ = 27 combos) ────────────────────────────────────
PIVOT_REV_SL_MULT = 1.0  # plancher ATR14 (grille : [0.75, 1.0, 1.25])
PIVOT_REV_RR_TARGET = 3.0  # break-even RR = 2.73 (grille : [2.5, 3.0, 3.5])
PIVOT_REV_VOL_REL_MIN = 2.5  # ratio volume vs SMA20 (grille : [2.0, 2.5, 3.0])

# ── Gestion du trade ─────────────────────────────────────────────────────────
PIVOT_REV_CONFIRM_BARS = 3  # barres M5 max pour trigger d'entrée (15 min)
PIVOT_REV_TIME_BARRIER_BARS = 20  # clôture forcée après 20 barres M5 (100 min)
PIVOT_REV_FORCE_CLOSE_HOUR_NY = 15
PIVOT_REV_FORCE_CLOSE_MIN_NY = 50  # clôture forcée à 15:50 NY
PIVOT_REV_MAX_TRADES_PER_DAY = 1  # 1 seul trade MGC1 / jour (anti-cluster)
PIVOT_REV_SKIP_MACRO_DAYS = True

# ── Sécurité fill ────────────────────────────────────────────────────────────
PIVOT_REV_MIN_SL_DIST_TICKS = 2  # SL_dist ≥ 2 ticks (plancher sécurité)
PIVOT_REV_DOJI_BODY_RATIO_MAX = 0.10  # body/range < 10% → ignoré (doji ambigu)


# ==============================================================================
# STRATÉGIE EIA-INV (event-driven mean-reversion sur EIA Weekly Petroleum)
# ==============================================================================
# Concept : sur-réaction HFT au headline EIA (mercredi 10:30 NY) → entrée
# LIMIT contrarian à l'extremum de la barre EIA M15, TP = close 10:15 NY (ancre).
# Edge : Lazar/Maneescu/Manzoni 2017 — mean-reversion < 25 min documentée.
# Ticker unique : MCL1 (Micro WTI Crude).
#
# IMPORTANT — Biais backtest M15 : le fill LIMIT au high/low de la barre EIA est
# assumé garanti dès atteinte (~10-15 % d'optimisme PF vs live). Live-équivalence
# via M5Buffer obligatoire avant toute promotion en prod
# (cf. feedback_live_eq_pattern.md).
# ==============================================================================
EIA_INV_STRATEGY_VERSION = "eia-inv-v1"

# ── Tickers + timeframe ──────────────────────────────────────────────────────
EIA_INV_TICKERS = ["MCL1"]

# ── Filtre magnitude (FILTRE BASELINE v1) ────────────────────────────────────
EIA_INV_SPIKE_MIN_PCT = 0.40  # spike_pct = spike_range / ANCHOR × 100 (%)

# ── Gestion du SL ─────────────────────────────────────────────────────────────
EIA_INV_SL_MULT = 1.5  # extension SL au-delà du spike (× spike_range)

# ── Frictions event-driven (slippage spécifique) ─────────────────────────────
EIA_INV_SLIP_ENTRY_TICKS = 3  # spread élargi post-release (override MCL1 = 2)
EIA_INV_SLIP_EXIT_TICKS = 1  # sortie standard

# ── Time barrier ──────────────────────────────────────────────────────────────
EIA_INV_TIME_BARRIER_MIN = 60  # fermeture forcée après N min post-EIA (→ 11:30 NY)

# ── Heures NY (DST-aware via zoneinfo) ───────────────────────────────────────
EIA_INV_ANCHOR_TIME_NY = (10, 15)  # barre M15 fermée à 10:15 NY = ANCHOR close
EIA_INV_EVENT_TIME_NY = (10, 30)  # barre M15 EIA (ouvre 10:30, ferme 10:45 NY)

# ── Trades max ───────────────────────────────────────────────────────────────
EIA_INV_MAX_TRADES_PER_DAY = 1  # 1 event EIA / mercredi

# ── Calendrier EIA — dates SKIPPÉES (cf. concept.md Q6) ──────────────────────
# Source officielle EIA : mercredis fériés US qui décalent l'event au jeudi
# (ou décalage exceptionnel). En v1, on skip ces mercredis purement et
# simplement (perte ~5 events/an, gain en simplicité).
EIA_INV_SKIP_DATES = [
    # 2025 — fériés US qui décalent l'EIA au jeudi
    "2025-01-22",  # MLK Jr. Day + Inauguration → jeudi 23/01
    "2025-02-19",  # Presidents Day → jeudi 20/02
    "2025-05-28",  # Memorial Day → jeudi 29/05
    "2025-09-03",  # Labor Day → jeudi 04/09
    "2025-10-15",  # Columbus Day → jeudi 16/10
    "2025-11-12",  # Veterans Day → jeudi 13/11
    "2025-12-24",  # Christmas → lundi 29/12 17:00 ET (cas unique)
    # 2026
    "2026-01-21",  # MLK Jr. Day → jeudi 22/01
    "2026-02-18",  # Presidents Day → jeudi 19/02
]

# ── Walk-forward (standard projet) ───────────────────────────────────────────
EIA_INV_IS_END = "2025-09-30"
EIA_INV_OOS_START = "2025-10-01"

# ── Grille d'optimisation (PHASE 4 — non exécutée en v1 baseline) ────────────
# Documentée pour traçabilité ; le scaffold v1 utilise uniquement les valeurs
# par défaut ci-dessus (filtre spike_pct seul).
EIA_INV_PARAM_GRID = {
    "spike_min_pct": [0.30, 0.40, 0.50],
    "sl_mult": [1.0, 1.5, 2.0],
    "time_barrier_min": [45, 60, 90],
}

# ==============================================================================
# STRATÉGIE VWAP-PB (vwap-pb-v1) — VWAP pullback intraday
# ==============================================================================
# Concept : entrée LIMIT @ VWAP(j-1) après stretch ≥ 1.0×ATR contre tendance
# courte (3 barres M15 du même côté de VWAP), TP/SL = ±1×ATR (RR brut 1:1).
# Edge : VWAP = benchmark institutionnel ; pression de rappel mécanique en
#        tendance intraday. Mais second-ordre sur micros (volume faible vs
#        contrat standard) — surveillance d'érosion nécessaire.
# Falsification : PF OOS < 1.2 OU taux reversion < 40% en 8 barres M15.
# Tickers : NQ1 (primaire), MGC1 (secondaire). MCL1 EXCLU v1 (frictions 31% SL).
# Concept source : output/vwap-pb-v1/concept.md (sortie @researcher 2026-05-26)
#
# IMPORTANT — VWAP causale stricte : groupby('date_ny').cumsum().shift(1).
# Le shift(1) est OBLIGATOIRE sinon la VWAP barre i inclut la close(i) → look-ahead.
# Test unitaire dédié dans strategies/vwap_pb.py (auto-exécution si __main__).
# ==============================================================================
VWAP_PB_STRATEGY_VERSION = "vwap-pb-v1"

# ── Tickers + timeframe ──────────────────────────────────────────────────────
VWAP_PB_TICKERS = ["NQ1", "MGC1"]  # MCL1 réservé V3 (frictions destructrices)

# ── Filtres signal (4 paramètres optimisables max — anti-curve-fitting) ──────
VWAP_PB_STRETCH_MIN = 1.0  # stretch minimum en × ATR (close - VWAP)
VWAP_PB_STRETCH_MAX = 2.5  # plafond anti-momentum (trade pas un fort breakout)
VWAP_PB_TREND_BARS = 3  # nombre barres M15 consécutives même côté VWAP

# ── ROLLBACK v1.1 → v1 (2026-05-26) ──────────────────────────────────────────
# Le filtre SKIP_VOL_H (seuil ATR p80 figé IS) testé en v1.1 a été ROLLBACK.
# Cause racine : drift de volatilité +148% sur MGC1 entre IS et OOS (Gold ATH
# crise géopolitique 2025-2026). Le seuil IS p80=8.11 dépassé par 94% des barres
# OOS MGC1 → filtre élimine 100% des trades MGC1 OOS (53→0). P&L OOS portfolio
# chute de $3,542 → $1,086 (-69%). Verdict v1.1 = 🔴.
# Leçon : un seuil quantile IS-only n'est PAS robuste si drift régime structurel.
# Patch archivé : output/archive/vwap-pb-v1.1/.
# ─────────────────────────────────────────────────────────────────────────────

# ── Gestion du SL/TP (RR brut 1:1, RR net ~0.89 après frictions) ─────────────
VWAP_PB_SL_ATR_MULT = 1.0  # multiplicateur SL × ATR14
# TP = même multiplicateur que SL (RR brut 1:1)

# ── TTL ordre LIMIT (max 2h avant cancel) ────────────────────────────────────
VWAP_PB_TTL_BARS = 8  # 8 barres M15 = 2h max avant cancel LIMIT

# ── Heures NY (DST-aware via zoneinfo) — fenêtres par actif ──────────────────
# NQ1 : exclut 09:30-09:59 (VWAP instable peu de barres + collision OPR)
# MGC1: exclut <09:30 pré-market + >13:00 post-pit (volume COMEX décline)
VWAP_PB_SESSION_WINDOW_NY = {
    "NQ1": ((10, 0), (15, 0)),  # [10:00, 15:00) NY
    "MGC1": ((9, 30), (13, 0)),  # [09:30, 13:00) NY
}

# ── Reset VWAP par actif (volume significatif) ───────────────────────────────
# NQ1 : 09:30 NY = open cash session standard
# MGC1: 09:00 NY = volume COMEX significatif (open pit RTH)
VWAP_PB_RESET_HOUR_NY = {
    "NQ1": (9, 30),
    "MGC1": (9, 0),
}

# ── Trades max par actif par jour ────────────────────────────────────────────
VWAP_PB_MAX_TRADES_PER_DAY = 1  # 1 trade max par actif par jour (anti-overtrade)

# ── ROLLBACK v2 → v1 (2026-05-26) ────────────────────────────────────────────
# Le filtre @quant MEDIUM `VWAP_PB_MIN_MINUTES_AFTER_RESET=90` testé en v2 a
# été ROLLBACK. Gain marginal (+0.05 PF OOS) au prix de -$1,202 P&L absolu, et
# finding Bonferroni-fail (p=0.006 vs seuil 0.0038 sur N=13 features).
# v1 plus simple et plus rentable. Patch archivé : output/archive/vwap-pb-v2/.
# ─────────────────────────────────────────────────────────────────────────────

# ── Walk-forward (standard projet — actifs avec historique long) ─────────────
VWAP_PB_IS_END = "2025-09-30"
VWAP_PB_OOS_START = "2025-10-01"

# ── Grille d'optimisation (PHASE 4 — 4 dimensions × 3 = 81 combos max) ───────
# Justification grille (cf. concept.md §Paramètres) :
#   - STRETCH_MIN : seuil de signification (< 0.8 = bruit, > 1.2 = restrictif)
#   - STRETCH_MAX : plafond anti-momentum (> 3.0 = on chase un breakout)
#   - SL_ATR_MULT : tradeoff WR/n_trades (< 0.8 = SL trop serré, > 1.5 = trop large)
#   - TREND_BARS  : robustesse signal tendance (2 = bruyant, 4 = restrictif)
VWAP_PB_PARAM_GRID = {
    "stretch_min": [0.8, 1.0, 1.2],
    "stretch_max": [2.0, 2.5, 3.0],
    "sl_atr_mult": [0.8, 1.0, 1.5],
    "trend_bars": [2, 3, 4],
}

# ==============================================================================
# STRATEGIE GAP FILL (gap-fill-v1) — Gap Fill Open NY
# ==============================================================================
# Concept : le marche tend a combler les gaps entre le close RTH J-1 (PSC =
#   Prior Session Close) et l'open RTH J (OPEN_RTH = open barre 09:30 ET).
#   Edge statistique (~60-70% des gaps < 0.5% se comblent dans la matinee NY).
#   Mecanisme : rebalancement des algos VWAP/TWAP + liquidation des positions
#   overnight ouvertes dans le gap.
# Falsification : PF OOS < 1.2 OU bootstrap portfolio < 50%.
#
# PIÈGES CRITIGUES (implementes dans strategies/gap_fill.py) :
#   1. PSC = close de la DERNIERE barre M15 RTH (09:30-16:00 ET) de J-1.
#      Ne jamais utiliser df.iloc[-1] sans filtre RTH (inclut barres overnight).
#   2. ATR14 = rolling 14 sur barres RTH uniquement (pas le DF brut overnight).
#   3. DST-aware via zoneinfo("America/New_York").
#   4. No look-ahead : decision 09:30 ET sur barres J-1. Confirmation = close
#      barre 09:30 (connue uniquement a 09:45). Entree = open barre 09:45 ET.
#   5. C4 : OPEN_RTH dans range RTH J-1 uniquement (low/high RTH 09:30-16:00).
#   6. YM1 inclus : meme classe d'actif (equities), meme phenomene de gap RTH.
#      Absent du backlog original mais ajoute ici apres validation concept.
#
# Walk-forward standard : IS -> 2025-09-30 | OOS -> 2025-10-01
# Voir strategies/gap_fill.py.
# ==============================================================================

GAP_STRATEGY_VERSION = "gap-fill-v1"

# ── Tickers ──────────────────────────────────────────────────────────────────
GAP_TICKERS = ["MES1", "NQ1", "YM1"]  # equities micro futures US

# ── Seuils signal (filtres C2 et C3) ─────────────────────────────────────────
# GAP_MIN_PCT  : gap minimum en % du PSC. < 0.03% = bruit de marche.
#                Defaut 0.05% : compromis volume / qualite signal.
# GAP_MAX_ATR  : gap maximum en multiple ATR14 RTH. > 0.30 ATR = gap de rupture
#                (news/earnings). On evite les gaps violents non recouvrables.
GAP_MIN_PCT = 0.05  # % du PSC — grille : [0.03, 0.05, 0.08]
GAP_MAX_ATR = 0.30  # multiple ATR14 RTH — grille : [0.20, 0.30, 0.40]

# ── Gestion du risque ─────────────────────────────────────────────────────────
# SL = OPEN_RTH + sl_mult × ATR14 (pour SHORT, inverse pour LONG)
# TP = PSC (comblage complet du gap)
# TE = 11:00 ET (Time Exit fixe, non optimise)
GAP_SL_ATR_MULT = {
    "MES1": 1.0,  # grille : [0.75, 1.0, 1.5]
    "NQ1": 1.0,
    "YM1": 1.0,
}
GAP_TE_HOUR = 11  # Time Exit 11:00 ET
GAP_TE_MINUTE = 0

# ── Filtres contextuels ───────────────────────────────────────────────────────
# FILTER_MONDAY : exclure les lundis (gap weekend = reouverte apres 2j hors
#   marche, dynamique differente). Defaut True.
GAP_FILTER_MONDAY = True  # grille : [True, False]

# ── Walk-forward (dates standard projet) ──────────────────────────────────────
GAP_IS_END = "2025-09-30"
GAP_OOS_START = "2025-10-01"

# ── Grille d'optimisation (PHASE 4) ──────────────────────────────────────────
# 4 dimensions : 3 × 3 × 3 × 2 = 54 combos.
# Bonferroni : p_seuil = 0.05 / 54 = 0.0009.
# Justification :
#   gap_min_pct  : [0.03, 0.05, 0.08] — bornes autour du defaut 0.05
#   gap_max_atr  : [0.20, 0.30, 0.40] — bornes autour du defaut 0.30
#   sl_atr_mult  : [0.75, 1.0, 1.5]   — tradeoff WR / n_trades
#   filter_monday: [True, False]       — test causal lundi vs reste semaine
GAP_PARAM_GRID = {
    "gap_min_pct": [0.03, 0.05, 0.08],
    "gap_max_atr": [0.20, 0.30, 0.40],
    "sl_atr_mult": [0.75, 1.0, 1.5],
    "filter_monday": [True, False],
}

# ==============================================================================
# STRATÉGIE PDH/PDL RETEST
# ==============================================================================
PDH_PDL_STRATEGY_VERSION = "pdh-pdl-retest-v1"
PDH_PDL_TICKERS = ["MES1", "NQ1", "YM1", "MGC1"]

# Paramètres fixes (non optimisés)
PDH_PDL_MIN_BREAK_ATR = 0.05  # cassure min en fraction ATR14
PDH_PDL_RETEST_ZONE_ATR_HIGH = 0.10  # zone haute retest = PDH + 10% ATR
PDH_PDL_RETEST_ZONE_ATR_LOW = 0.15  # zone basse retest = PDH - 15% ATR
PDH_PDL_RETEST_CLOSE_BUFFER = 0.10  # tolérance clôture barre retest
PDH_PDL_GAP_EXCLUSION_ATR = 0.30  # exclure setup si gap overnight > 30% ATR
PDH_PDL_MAX_TRADES_PER_DAY = 2  # max 1 long + 1 short par jour

# Paramètres optimisables (valeurs baseline v1)
PDH_PDL_BREAK_BUFFER_TICKS = 2  # grille : [0, 1, 2] — optimisé MES1
PDH_PDL_SL_ATR_MULT = 0.5  # grille : [0.3, 0.5, 0.7] — optimisé MES1
PDH_PDL_TP_RR = 1.5  # grille : [1.5, 2.0, 2.5] — optimisé MES1
PDH_PDL_MAX_RETEST_BARS = 16  # grille : [4, 8, 16] — optimisé MES1

# Walk-forward (dates standard projet)
PDH_PDL_IS_END = "2025-09-30"
PDH_PDL_OOS_START = "2025-10-01"

# Grille d'optimisation (PHASE 4)
# 4 dimensions : 3 × 3 × 3 × 3 = 81 combos
# Bonferroni : p_seuil = 0.05 / 81 = 0.000617
PDH_PDL_PARAM_GRID = {
    "break_buffer_ticks": [0, 1, 2],
    "sl_atr_mult": [0.3, 0.5, 0.7],
    "tp_rr": [1.5, 2.0, 2.5],
    "max_retest_bars": [4, 8, 16],
}

# ==============================================================================
# STRATÉGIE XMKT-RV (dislocation inter-marché NQ/ES/YM — relative-value M1)
# ==============================================================================
# Concept : NQ1, MES1 (ES) et YM1 partagent un facteur macro commun → leurs
# rendements log M1 sont très corrélés (corr observée : NQ-ES 0.95, ES-YM 0.89,
# NQ-YM 0.79). Le bruit de microstructure crée des désynchronisations
# transitoires (lead-lag). Quand la jambe exécutée s'écarte de son marché de
# référence au-delà d'une bande (Z-score d'un spread résiduel beta-ajusté), on
# parie sur le réalignement (index-arbitrage / réversion) en tradant la jambe
# laggard dans le sens du catch-up.
#
# Edge : qui paie ? Les liquidity-takers qui poussent une jambe au-delà de son
# fair-value relatif (impulsion momentum mono-instrument). Le market-making
# cross-asset et l'index-arbitrage HFT réalignent en secondes/minutes ; on
# capte la queue lente de ce réalignement à l'échelle M1.
#
# Falsification : si en OOS les entrées |Z|>seuil ne reversent pas (Z continue
# à s'étendre plus souvent qu'il ne revient), l'edge n'existe pas → rejet.
#
# DATA : M1 dans DATA_BACKTEST/{NQ1,MES1,YM1}_data_m1.csv,
#        couverture 2025-02-16 → 2026-03-02 (~12,5 mois, historique RÉDUIT).
#        Inner-join sur timestamp. RTH NY uniquement, aucun overnight.
# ------------------------------------------------------------------------------
XMKT_STRATEGY_VERSION = "xmkt-rv-v1"
XMKT_TICKERS = ["NQ1", "MES1", "YM1"]

# ── Marché de référence par jambe exécutée (régression r_leg ~ r_ref) ─────────
# Choix ancré sur la corrélation observée (cf. probe data quality) :
#   NQ1 régressé sur ES (la plus corrélée, 0.948)
#   MES1 régressé sur NQ (0.948)
#   YM1  régressé sur ES (0.894)
XMKT_REF_PAIR = {
    "NQ1": "MES1",
    "MES1": "NQ1",
    "YM1": "MES1",
}

# ── Fenêtres roulantes (en barres M1) ─────────────────────────────────────────
# XMKT_BETA_WINDOW : fenêtre de la régression rolling r_leg ~ r_ref (beta lead-lag)
# XMKT_Z_WINDOW    : fenêtre du Z-score du spread résiduel cumulé
XMKT_BETA_WINDOW = 60  # 60 min — beta stable intraday
XMKT_Z_WINDOW = 30  # 30 min — bande de réversion courte

# ── Seuils de décision (Z-score) ──────────────────────────────────────────────
XMKT_Z_ENTRY = 2.0  # |Z| > entrée → dislocation tradable
XMKT_Z_EXIT = 0.5  # |Z| <= sortie → réversion atteinte (take profit logique)
XMKT_Z_STOP = 3.5  # |Z| >= stop → continuation (la dislocation s'aggrave) → SL

# ── Gestion du temps ──────────────────────────────────────────────────────────
XMKT_TIME_STOP_MIN = 30  # time-stop en minutes (réversion intraday rapide)
XMKT_HOUR_START_NY = 9.5  # 09:30 NY (RTH open)
XMKT_HOUR_END_NY = 15.5  # 15:30 NY — pas de nouvelle entrée après (EOD flat 16:00)
XMKT_EOD_FLAT_HOUR_NY = 16.0  # clôture forcée RTH — aucun overnight

XMKT_MAX_TRADES_PER_DAY = 4  # par actif (M1 → fréquence plus élevée tolérée)

# ── Neutralisation des artefacts de roll / overnight ──────────────────────────
# Les contrats continus back-adjustés créent de faux spikes de rendement aux
# frontières de roll trimestriel (mars/juin/sept/déc) et aux coupures de session.
# Traitement :
#   1. Tout rendement traversant une coupure de session (barre M1 non
#      consécutive à la précédente, gap > XMKT_SESSION_GAP_SEC) est mis à 0.
#   2. Winsorisation des rendements log à ±XMKT_WINSOR_SIGMA × σ_rolling.
# Un edge qui dépend de ces spikes serait un artefact → éliminé par construction.
XMKT_SESSION_GAP_SEC = 60  # barres M1 espacées de >60s = coupure de session
XMKT_WINSOR_SIGMA = 6.0  # cap des rendements à 6σ rolling
XMKT_WINSOR_WINDOW = 120  # fenêtre du σ pour la winsorisation (min)

# ── Sizing ────────────────────────────────────────────────────────────────────
# Risque dollar par trade : RISK_PER_TRADE_USD (global). La distance de stop
# en relative-value n'est pas un prix fixe (sortie sur Z), donc on borne le SL
# physique via XMKT_HARD_SL_ATR (ATR M1 multiple) pour le sizing et un garde-fou
# catastrophe (gap brutal du sous-jacent).
XMKT_HARD_SL_ATR = 4.0  # SL physique de secours = 4 × ATR(14) M1 de la jambe
XMKT_ATR_PERIOD = 14  # période ATR M1

# ── Walk-forward (historique M1 réduit : IS fév→sept 2025, OOS oct 2025→mars 2026)
XMKT_IS_END = "2025-09-30"
XMKT_OOS_START = "2025-10-01"

# ── Grille d'optimisation (PHASE 4) ───────────────────────────────────────────
# 4 dimensions : 3 × 3 × 3 × 3 = 81 combos.
# Bonferroni : p_seuil = 0.05 / 81 = 0.000617.
# Justification des bornes :
#   z_entry  : [1.75, 2.0, 2.5]  — bande de dislocation (sous 1.75 = bruit)
#   z_exit   : [0.0, 0.5, 1.0]   — cible de réversion (0 = retour fair-value)
#   z_window : [20, 30, 45]      — horizon de réversion (min)
#   time_stop: [20, 30, 45]      — durée max de tenue (min)
XMKT_PARAM_GRID = {
    "z_entry": [1.75, 2.0, 2.5],
    "z_exit": [0.0, 0.5, 1.0],
    "z_window": [20, 30, 45],
    "time_stop": [20, 30, 45],
}

# ==============================================================================
# STRATÉGIE FIB-FINE — retracement Fibonacci NATIF sur timeframe fin (M1 / M5)
# ==============================================================================
# Mirror data-driven de fib-v4 (continuation sur retracement Fib), mais TOUTE la
# structure (impulsion, pivots, niveaux, entrée, invalidation, sortie) est
# détectée sur un timeframe FIN. Le timeframe est un PARAMÈTRE (`m1`/`m5`) afin
# de lancer la MÊME logique sur les deux résolutions et comparer l'edge NET.
#
# Objectif central : déterminer si une Fib détectée nativement sur TF fin bat /
# complète le fib-v4 M15 prod, et laquelle des deux résolutions (M1 vs M5) donne
# le meilleur edge NET (les frictions par trade pèsent proportionnellement plus
# lourd sur des targets plus petits).
#
# Caveats données (cf. rapport) :
#   • M1 : DATA_BACKTEST/{NQ1,MES1,YM1}_data_m1.csv — 2025-02-16 → 2026-03-02.
#   • M5 natif : data/{TICKER}_data_m5.csv — 2025-10-19 → 2026-05-15 (OOS-only).
#   • Pour la comparaison à PÉRIMÈTRE ÉGAL, le M5 est RESAMPLÉ depuis le M1
#     (même back-adjustment, même fenêtre). Le M5 natif sert au bonus robustesse.
#   • Rolls trimestriels : un saut de prix bar-to-bar > FIB_FINE_ROLL_GAP_PCT
#     neutralise le swing chevauchant (faux swing d'impulsion).
# ==============================================================================
FIB_FINE_STRATEGY_VERSION = "fib-fine-v2"

# Timeframe par défaut du backtest (surchargeable via params["timeframe"]).
# v2 : M5 UNIQUEMENT (les *_m1 sont tués par les frictions, PF NET <= 0.76 —
# abandon confirmé par @quant discover 2026-06-01). Le runner backtest.py
# utilise FIB_FINE_DEFAULT_TF.
FIB_FINE_DEFAULT_TF = "m5"

# Univers v2 (réduit, justifié @quant) : NQ1 + MES1 + MGC1 en M5.
#   • YM1 RETIRÉ : son edge v1 était un MIRAGE look-ahead (wick complet de la
#     barre de fill) ; après filtre causal, OOS reste plat (1.01 -> 1.08) → pas
#     d'edge récupérable. Ne PAS le réintroduire sans preuve OOS causale.
#   • M1 RETIRÉ : frictions par trade trop lourdes sur targets fins.
# NQ1/MES1 : M5 resamplé depuis le M1 DATA_BACKTEST (a un IS fév-sep 2025 + un
# OOS oct 2025-mars 2026 → walk-forward propre).
# MGC1 : M5 NATIF (data/MGC1_data_m5.csv) couverture oct 2025 → mai 2026 =
# OOS-PUR (aucun IS) → cellule de confiance MOINDRE (cf. caveat rapport).
FIB_FINE_TICKERS = ["NQ1", "MES1", "MGC1"]

# Source de données PAR ticker pour le chargement du M5 fin (cf. caveats data).
#   resampled : M5 reconstruit depuis le M1 DATA_BACKTEST (a un IS).
#   native    : CSV M5 natif data/{TICKER}_data_m5.csv (OOS-only pour MGC1).
FIB_FINE_SOURCE_PER_TICKER = {
    "NQ1": "resampled",
    "MES1": "resampled",
    "MGC1": "native",  # pas de M1 MGC1 → seul M5 natif disponible (OOS-only)
}

# ── Structure de détection (cohérent avec fib-v4, adapté au nombre de barres) ──
# En TF fin, on garde la même logique de pivots/impulse mais les périodes en
# BARRES doivent couvrir une durée comparable. On exprime donc les périodes
# structurelles PAR timeframe (m1 ≈ 5× plus de barres que m5 pour une même durée).
FIB_FINE_ATR_PERIOD = 14
FIB_FINE_EMA_FAST_PERIOD = 50
FIB_FINE_EMA_SLOW_PERIOD = 200
FIB_FINE_ADX_PERIOD = 14
FIB_FINE_ADX_TREND_THRESHOLD = 20.0

# Pivots left/right, impulse lookback/max-bars, timeouts — PAR timeframe.
# Choisis pour couvrir ~ la même durée physique qu'en M15 (pivot 8 barres M15 =
# 120 min ; en m5 = 24 barres, en m1 = 120 barres → on borne pour rester sobre).
FIB_FINE_STRUCT_PER_TF = {
    "m5": {
        "pivot_left": 6,
        "pivot_right": 6,
        "impulse_lookback": 60,  # ~5 h
        "max_impulse_bars": 40,  # ~3,3 h
        "order_timeout_bars": 24,  # ~2 h pour fill
        "max_hold_bars": 60,  # ~5 h (intraday, time-stop)
    },
    "m1": {
        "pivot_left": 15,
        "pivot_right": 15,
        "impulse_lookback": 240,  # ~4 h
        "max_impulse_bars": 150,  # ~2,5 h
        "order_timeout_bars": 90,  # ~1,5 h pour fill
        "max_hold_bars": 240,  # ~4 h (intraday, time-stop)
    },
}

# ── Paramètres data-driven PER-TICKER (mirror fib-v4) ──────────────────────────
# Niveau Fib retenu par ticker (data-driven en PHASE 4 ; défaut 0.382 = réf prod).
FIB_FINE_LEVEL_PER_TICKER = {"NQ1": 0.382, "MES1": 0.382, "YM1": 0.382, "MGC1": 0.500}

# SL/TP en multiples d'ATR (du TF fin) — défaut hérité de fib-v4, raffiné PHASE 4.
FIB_FINE_SL_ATR_MULT_PER_TICKER = {"NQ1": 1.50, "MES1": 0.75, "YM1": 1.00, "MGC1": 1.00}
FIB_FINE_TP_ATR_MULT_PER_TICKER = {"NQ1": 1.50, "MES1": 1.50, "YM1": 2.00, "MGC1": 1.50}

# Taille minimale de l'impulse en ATR (filtre faux swings). Plus élevé en TF fin
# pour exiger une impulse significative et limiter le bruit.
FIB_FINE_MIN_IMPULSE_ATR_PER_TICKER = {"NQ1": 1.50, "MES1": 1.50, "YM1": 2.00, "MGC1": 1.50}

# ── v2 : filtre wick NEUTRALISÉ (était LOOK-AHEAD) ─────────────────────────────
# Le filtre wick_through_atr v1 lisait la mèche COMPLÈTE de la barre de FILL
# (low/high finaux) = information NON observable en live à l'instant où le broker
# fille l'ordre limite intra-bar. C'était le blocant #1 de l'audit. Il est
# NEUTRALISÉ (1e9 = inactif) et remplacé par le filtre causal d'expansion de
# volatilité ci-dessous. La colonne wick reste écrite (NaN) pour compat schéma.
FIB_FINE_WICK_THROUGH_MAX_ATR_PER_TICKER = {"NQ1": 1e9, "MES1": 1e9, "YM1": 1e9, "MGC1": 1e9}

# ── v2 : filtre CAUSAL d'expansion de volatilité (remplace le wick look-ahead) ─
# atr_ratio_sl = compute_atr(df, 5).iloc[fill_i-1] / atr_a_l_armement.
# On ne garde l'entrée que si ce ratio >= seuil. Le seuil est FIXE (1.0), PRÉ-
# SPÉCIFIÉ par @quant (1 test parmi 3 hypothèses pré-déclarées, Bonferroni 0.0167,
# perm-test pooled m5 indices p=0.002 PASS, stable IS->OOS). Aucun degré de
# liberté de calibrage — l'optimiser serait du p-hacking : NE PAS le mettre dans
# la PARAM_GRID. Causalité : volatilité court terme >= volatilité d'armement →
# l'impulsion garde de l'énergie → continuation atteint le TP avant un stop pris
# dans un range mou. Lecture sur barre i-1 (clôturée AVANT le fill) → no look-ahead.
# 0.0 / None = filtre inactif (YM1 absent = volontairement exclu de l'univers v2).
FIB_FINE_ATR_EXPANSION_PERIOD = 5  # ATR court terme (barres clôturées)
FIB_FINE_ATR_EXPANSION_MIN_PER_TICKER = {
    "NQ1": 1.0,  # robuste m5 (gain IS + OOS)
    "MES1": 1.0,  # récupération partielle m5 (causale)
    "MGC1": 1.0,  # boost m5 (mais data OOS-only → confiance moindre)
}

# Buffer pivot break en ATR : annule le pending si le prix casse le pivot
# d'impulse au-delà du buffer. 0.0 = strict.
FIB_FINE_PIVOT_BREAK_BUFFER_ATR_PER_TICKER = {"NQ1": 0.0, "MES1": 0.0, "YM1": 0.0, "MGC1": 0.0}

# Skip jours macro (FOMC/CPI/NFP/JOLTS) par ticker. True pour Gold (parité v4).
FIB_FINE_SKIP_MACRO_PER_TICKER = {"NQ1": False, "MES1": False, "YM1": False, "MGC1": True}

# ── v2 : Session horaire DST-AWARE (heures NY, America/New_York) ───────────────
# CORRECTION AUDIT : la session v1 était filtrée en heures UTC FIXES → glisse 1h
# entre hiver (EST) et été (EDT). v2 convertit l'index en heure NY via
# zoneinfo("America/New_York") (pattern projet, cf. core/opr.py) et filtre sur
# l'heure NY. Les fenêtres sont exprimées en (heure_début_NY, heure_fin_NY).
# us_session 9h-17h NY = RTH 9:30-16:00 + marge ; no_nuit = journée NY hors nuit.
FIB_FINE_SESSION_WINDOWS_NY = {
    "us_session": (9, 17),  # 9h-17h NY (couvre RTH 9:30-16:00)
    "no_nuit": (4, 17),  # 4h-17h NY (pré-marché US + RTH)
    "europe_us": (3, 13),  # ~Europe + ouverture US (heure NY)
    "all": (0, 24),
}
FIB_FINE_SESSION_PER_TICKER = {
    "NQ1": "us_session",  # 9h-17h NY
    "MES1": "no_nuit",  # 4h-17h NY
    "YM1": "us_session",  # (hors univers v2, conservé pour compat)
    "MGC1": "us_session",  # 9h-17h NY
}

# Neutralisation des rolls : saut de prix close-to-close > ce % entre deux barres
# consécutives → tout swing dont l'intervalle contient ce gap est rejeté (faux
# swing d'impulsion fabriqué par le roll/back-adjust). 1,5 % = bien au-dessus du
# bruit intraday normal mais en dessous des journées extrêmes (avr 2025 ~4 %).
FIB_FINE_ROLL_GAP_PCT = 0.015

# ── v2 : Sizing risk-per-trade dédié (correction Topstep MC DD) ────────────────
# CORRECTION AUDIT : au sizing nominal RISK_PER_TRADE_USD = $200, le MC P95 DD de
# l'univers v2 dépassait la limite trailing Topstep (-$2000). On dimensionne donc
# fib-fine-v2 avec un risk-per-trade RÉDUIT, calibré pour que le MC P95 DD reste
# sous la limite. None = utilise le nominal global RISK_PER_TRADE_USD ($200).
# Valeur calibrée en PHASE 5 (Monte-Carlo) ci-dessous.
FIB_FINE_RISK_PER_TRADE_USD = 130  # calibré PHASE 5 : MC P95 DD < -$2000 Topstep

# ── Walk-forward (dates fixes projet) ──────────────────────────────────────────
# NQ1/MES1 : M5 resamplé depuis M1 (fév 2025 → mars 2026) → IS + OOS propres.
# MGC1     : M5 natif (oct 2025 → mai 2026) = OOS-PUR (aucune barre avant IS_END)
#            → la stabilité IS→OOS de MGC1 ne peut PAS être validée (caveat).
FIB_FINE_IS_END = "2025-09-30"
FIB_FINE_OOS_START = "2025-10-01"

# ── Grille d'optimisation (PHASE 4) — v2 ──────────────────────────────────────
# 2 dimensions data-driven (≤ 4 → conforme). fib_level × sl_mult.
#   • wick_max RETIRÉ de la grille : le filtre wick est neutralisé (look-ahead).
#   • atr_expansion_min VOLONTAIREMENT ABSENT : seuil FIXE pré-spécifié (1.0),
#     0 degré de liberté ; l'inclure dans la grille = p-hacking (interdit).
# Bonferroni appliqué sur le nombre de configs effectivement testées.
#   fib_level : [0.382, 0.5, 0.618]  — niveaux Fib canoniques
#   sl_mult   : [0.75, 1.0, 1.5]     — serrage du stop (sensible aux frictions)
FIB_FINE_PARAM_GRID = {
    "fib_level": [0.382, 0.5, 0.618],
    "sl_mult": [0.75, 1.0, 1.5],
}

# ── PROMOTION LIVE (fib-fine-v2) — ajout @forge 2026-06-02 ─────────────────────
# Feature flag MAÎTRE. ACTIVÉ 2026-06-02 (cœur NQ1+MES1, $130) — activation directe
# sans phase simulation, surveillance utilisateur. Le code live (core/fib_fine_v2.py
# + blocs flag-gated de broker/live_runner.py) devient ACTIF au prochain restart
# DÉLIBÉRÉ du daemon (hors session NY). Pour désactiver : repasser à False + restart.
FIB_FINE_ENABLED = True

# Univers LIVE initial (config-driven, SÉPARÉ de FIB_FINE_TICKERS de recherche).
# Vague 1 = NQ1 + MES1 UNIQUEMENT (cœur 🟢, sizing $130). MGC1 (bonus 🟡 OOS-only),
# YM1 (mirage look-ahead) et tout M1 EXCLUS. Ajout futur de MGC1 = éditer cette
# liste (aucune refonte de code nécessaire — les params per-ticker existent déjà).
FIB_FINE_LIVE_TICKERS = ["NQ1", "MES1"]

# Sizing DÉDIÉ fib-fine ($130). N'altère PAS RISK_PER_TRADE_USD global ($200).
# Réutilise la valeur de recherche déjà calibrée PHASE 5 (MC p95 DD < $2000 Topstep).
FIB_FINE_RISK_USD = FIB_FINE_RISK_PER_TRADE_USD  # = 130

# Timeframe de décision live (barres CLÔTURÉES). Doit rester "m5" (cohérence backtest).
FIB_FINE_LIVE_TF = FIB_FINE_DEFAULT_TF  # = "m5"

# Source des barres M5 en live : "rest" → fetch REST M5 dédié (unit=2,
# unit_number=5) via SessionRunner._fetch_bars_m5. Pas de dépendance M1Buffer
# (le filtre causal atr_ratio_sl ne lit que des barres M5 closes → no look-ahead).
FIB_FINE_LIVE_BARS_SOURCE = "rest"

# Nb de barres M5 d'historique à fournir au détecteur live (warmup EMA200 + pivots).
# EMA_SLOW=200 + marge → ~2 jours de M5 (12*23*2 ≈ 552) ; on prend large.
FIB_FINE_LIVE_M5_WARMUP_BARS = 800


# ==============================================================================
# STRATÉGIE FIB-ARGREL (variante Fibonacci — pivots scipy.argrelextrema)
# ==============================================================================
# Variante data-géométrique du retracement Fib (famille edge prod prouvée), avec
# des RÈGLES SPÉCIFIÉES PAR L'UTILISATEUR (2026-06-07) :
#   1. Pivots détectés par scipy.signal.argrelextrema sur les colonnes brutes
#      `low` (creux) et `high` (sommets), pour PLUSIEURS ordres (≥ 5).
#   2. Filtre d'incohérence : l'amplitude de l'impulsion (|high−low|) doit être
#      STRICTEMENT SUPÉRIEURE à la moyenne (causale) des amplitudes détectées.
#   3. Niveaux Fib calculés sur cette impulsion ; on teste les 3 principaux.
#   4. Entrée = ordre LIMIT légèrement EN DESSOUS du niveau (long) / au-dessus
#      (short, mirroir).
#   5. SL = origine de l'impulsion (le « 0 ») → SL_dist = (1−f)·R. Lecture
#      canonique : invalidation = origine cassée. (choix utilisateur explicite)
#   6. TP = rr × SL_dist, rr ≥ 2.
#
# Look-ahead : ZÉRO par construction — pivots confirmés à idx+ordre (offset droit
# de argrelextrema), moyenne causale (legs antérieurs uniquement), entrée LIMIT
# (fill honnête, pas de stop-entry), résolution intra-bar SL-prioritaire. Aucune
# feature lue sur la barre de fill → live-équivalent (pattern bos-fvg / fib-fine).
FIB_ARGREL_STRATEGY_VERSION = "fib-argrel-v1"

# Univers de recherche walk-forward : NQ1/MES1/YM1 (M5 RESAMPLÉ depuis M1
# DATA_BACKTEST → IS fév-sep 2025 + OOS oct 2025+ propre). MGC1 n'a pas de M1 →
# bonus OOS-only natif, ajouté manuellement en deep lane si le cœur survit.
FIB_ARGREL_TICKERS = ["NQ1", "MES1", "YM1"]
FIB_ARGREL_DEFAULT_TF = "m5"
FIB_ARGREL_SOURCE_PER_TICKER = {
    "NQ1": "resampled",
    "MES1": "resampled",
    "YM1": "resampled",
    "MGC1": "native",
}

# ── Walk-forward (dates fixes projet) ──────────────────────────────────────────
FIB_ARGREL_IS_END = "2025-09-30"
FIB_ARGREL_OOS_START = "2025-10-01"

# ── Détection des pivots / impulsion ──────────────────────────────────────────
# `order` argrelextrema : nb de barres voisines comparées de CHAQUE côté. Un
# pivot à l'index p n'est CONNU qu'à p+order (offset de confirmation droit) →
# c'est la garantie no-look-ahead. Défaut hors grille = 8.
FIB_ARGREL_ORDER = 8
# Niveau Fib par défaut (hors grille). 0.5 = milieu.
FIB_ARGREL_DEFAULT_LEVEL = 0.5
# RR par défaut (hors grille). ≥ 2 (règle utilisateur).
FIB_ARGREL_RR = 2.0
# Décalage de l'ordre LIMIT vs le niveau Fib (« légèrement en dessous »), en ticks.
FIB_ARGREL_ENTRY_OFFSET_TICKS = 1
# Filtre d'écart : amplitude impulsion > FIB_ARGREL_MEAN_MULT × moyenne causale.
# 1.0 = strictement « plus que la moyenne » (règle utilisateur, seuil FIXE 0-DoF).
FIB_ARGREL_MEAN_MULT = 1.0
# Nb minimal de legs observés avant d'activer le filtre de moyenne (warmup stat).
FIB_ARGREL_MIN_LEGS_FOR_MEAN = 10
# Récence : le pivot confirmant l'impulsion doit être à ≤ N barres de la barre
# courante (on ne trade pas une structure ancienne). En barres M5.
FIB_ARGREL_IMPULSE_LOOKBACK = 60
# Durée max de l'impulsion elle-même (origine→extrême), en barres M5.
FIB_ARGREL_MAX_IMPULSE_BARS = 80
# Délai max d'attente d'un fill (ordre LIMIT pending), en barres M5.
FIB_ARGREL_ORDER_TIMEOUT_BARS = 24
# Durée de détention max d'une position (time-stop intraday), en barres M5.
FIB_ARGREL_MAX_HOLD_BARS = 48
# Fenêtre de session NY (DST-aware) pour la GÉNÉRATION d'entrées [h_début, h_fin).
# Couvre pré-NY + cash NY. Pas d'overnight (cancel pending + close à la frontière jour).
FIB_ARGREL_SESSION_NY = (8, 16)
# Saut close-to-close au-delà duquel une barre est marquée « gap de roll » → une
# impulsion chevauchant un gap est neutralisée (faux swing back-adjust).
FIB_ARGREL_ROLL_GAP_PCT = 0.02
# Skip des jours macro (FOMC/CPI/NFP…) ? Défaut False (règle utilisateur pure).
FIB_ARGREL_SKIP_MACRO = False
# Sizing dédié. None → RISK_PER_TRADE_USD global ($200) [recherche].
FIB_ARGREL_RISK_PER_TRADE_USD = None

# ── Grille d'optimisation (PHASE 4) ───────────────────────────────────────────
# 3 dimensions = règles utilisateur explicites :
#   fib_level : 3 niveaux principaux (testés tous, exigence utilisateur)
#   order     : argrelextrema ≥ 5 (« différents ordres minimum 5 »)
#   rr        : ratio risque/récompense ≥ 2 (« RR 2 minimum »)
# Le filtre de moyenne (1.0) et l'offset (1 tick) sont FIXES (pré-spécifiés,
# 0 degré de liberté) → pas dans la grille (anti p-hacking). Bonferroni appliqué
# par l'optimiseur sur le nombre de configs effectivement testées.
FIB_ARGREL_PARAM_GRID = {
    "fib_level": [0.382, 0.5, 0.618],
    "order": [5, 8, 12, 18, 25],
    "rr": [2.0, 2.5, 3.0],
}


# ==============================================================================
# STRATÉGIE WICK_FILL (`wick-fill-v1`) — Rejet de mèche (pin-bar fade)
# ==============================================================================
# Source : utilisateur (liste 2026-06-08, idée #1). Setup M15 natif.
# Sur une bougie M15 CLÔTURÉE en RTH : corps faible (< body_max×range) + longue
# mèche (≥ wick_min×range) du côté du rejet → on FADE l'extrême.
#   • Rejet baissier (mèche HAUTE, close<open) → SELL LIMIT au HIGH (rest au-dessus
#     du marché), TP = LOW, SL = HIGH + buf ticks.
#   • Rejet haussier (mèche BASSE, close>open) → BUY LIMIT au LOW, TP = HIGH,
#     SL = LOW − buf ticks.
# Ordre valide UNIQUEMENT sur la bougie suivante (timeout=1). Entrée LIMIT au reste
# = fill honnête (le prix doit revenir TOUCHER l'extrême). Résolution intra-bar
# SL-prioritaire. Close-all à la cloche RTH.
# ⚠️ SL razor-thin (buf ticks au-delà de l'extrême) → sizing plafonné
# (WICK_FILL_MAX_CONTRACTS) sinon n_ct explose. PF invariant au plafond (linéaire) ;
# le plafond ne fixe que la magnitude P&L absolue (Topstep-réaliste).
WICK_FILL_STRATEGY_VERSION = "wick-fill-v1"
WICK_FILL_TICKERS = ["NQ1", "MES1", "YM1", "MGC1", "MCL1"]  # YM/NQ/MES/Or/Pétrole
WICK_FILL_TIMEZONE = "America/New_York"
WICK_FILL_SESSION_START = (9, 30)  # 1ère bougie de signal éligible (RTH open)
WICK_FILL_SIGNAL_CUTOFF = (15, 30)  # dernière bougie de signal commence avant (=21:30 Paris)
WICK_FILL_SESSION_END = (16, 0)  # close-all RTH (TE)
WICK_FILL_BODY_MAX_FRAC = 0.4  # corps < body_max × range
WICK_FILL_WICK_MIN_FRAC = 0.6  # mèche du côté rejet ≥ wick_min × range
WICK_FILL_SL_BUFFER_TICKS = 2  # SL = extrême ± buf ticks (spec : 2 ticks / 2 pts YM)
WICK_FILL_ATR_PERIOD = 14  # régime ADX uniquement
WICK_FILL_ORDER_TIMEOUT_BARS = 1  # ordre valide jusqu'à fin de la bougie suivante
WICK_FILL_MAX_CONTRACTS = 30  # plafond sizing (SL razor → évite explosion n_ct)
WICK_FILL_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# Grille walk-forward (breadth) : on grid la STRICTESSE de la définition du pin-bar
# (corps/mèche). SL=buf ticks, TP=extrême opposé, timeout=1 = identité figée (spec,
# 0 DoF). 9 cellules = Bonferroni honnête, pré-spécifié AVANT run (anti p-hacking).
WICK_FILL_PARAM_GRID = {
    "body_max": [0.3, 0.4, 0.5],
    "wick_min": [0.5, 0.6, 0.7],
}


# ==============================================================================
# STRATÉGIE FAKEOUT_FIRST (`fakeout-first-v1`) — Fausse cassure de la 1ère bougie M15
# ==============================================================================
# Source : utilisateur (liste 2026-06-08, idée #2). Turtle-soup sur l'opening range.
# 1ère bougie RTH [09:30,09:45) NY → high1/low1. On surveille les 3 bougies suivantes
# [09:45,10:30) : une FAUSSE CASSURE = une bougie qui dépasse l'extrême de ≥ break_ticks
# (intra-bar) PUIS CLÔTURE de retour à l'intérieur.
#   • Fakeout HAUSSIER (high≥high1+break_t, close<high1) → SHORT : SELL LIMIT @ high1
#     (rest au-dessus du marché), TP = low1, SL = fakeout_high + sl_buf_t.
#   • Fakeout BAISSIER (low≤low1−break_t, close>low1) → LONG (symétrique).
# Ordre armé à la clôture de la bougie de fausse cassure, valide 4 bougies (1h).
# Entrée LIMIT au reste = fill HONNÊTE (fill bar j+1 au plus tôt, zéro same-bar).
# SL-prio intra-bar, close-all RTH (TE). 1 trade/jour (1ère fausse cassure).
FAKEOUT_FIRST_STRATEGY_VERSION = "fakeout-first-v1"
FAKEOUT_FIRST_TICKERS = ["NQ1", "MES1", "YM1", "MGC1", "MCL1"]  # YM/NQ/MES/Or/Pétrole
FAKEOUT_FIRST_TIMEZONE = "America/New_York"
FAKEOUT_FIRST_SESSION_OPEN = (9, 30)  # début 1ère bougie RTH
FAKEOUT_FIRST_FIRST_BAR_END = (9, 45)  # 1ère bougie = [09:30,09:45)
FAKEOUT_FIRST_WATCH_END = (10, 30)  # surveille 3 bougies [09:45,10:30)
FAKEOUT_FIRST_SESSION_END = (16, 0)  # close-all RTH (TE)
FAKEOUT_FIRST_BREAK_TICKS = 5  # cassure ≥ break_ticks au-delà de l'extrême
FAKEOUT_FIRST_SL_BUFFER_TICKS = 5  # SL = extrême fakeout ± sl_buf ticks
FAKEOUT_FIRST_ATR_PERIOD = 14  # régime ADX uniquement
FAKEOUT_FIRST_ORDER_TIMEOUT_BARS = 4  # ordre valide 4 bougies (1h) après armement
FAKEOUT_FIRST_MAX_CONTRACTS = 40  # plafond sizing (hygiène ; PF invariant)
FAKEOUT_FIRST_RISK_PER_TRADE_USD = None  # None → RISK_PER_TRADE_USD global ($200)

# Grille walk-forward (breadth) : strictesse de la fausse cassure (break) × distance SL.
# TP=extrême opposé, timeout=4 = identité figée (spec, 0 DoF). 9 cellules pré-spécifiées
# AVANT run (anti p-hacking).
FAKEOUT_FIRST_PARAM_GRID = {
    "break_ticks": [3, 5, 8],
    "sl_buffer_ticks": [3, 5, 8],
}


# ==============================================================================
# STRATÉGIE ODC_V2 (`odc-v2`) — Open-Drive Continuation, EXIT re-spécifié
# ==============================================================================
# Re-spec du mécanisme RÉEL d'odc-v1 (audit 2026-06-08) : l'edge est à 90% TE-driven
# (pari directionnel tenu jusqu'à l'EOD), le TP=rr×SL est cosmétique (WR_TP 7%), et
# les trades TE atteignent MFE +1.2R PUIS RENDENT. Entrée INCHANGÉE (validée :
# pullback 38.2%, SL au-delà de l'origine). Seul l'EXIT change.
#   • exit_mode "tp_rr"    : baseline odc-v1 (TP=rr×SL) — référence.
#   • exit_mode "eod"      : pas de TP, hold-to-close (SL ou TE) — mécanisme honnête nu.
#   • exit_mode "be_trail" : DÉCISION pré-engagée — hold-to-EOD + lock SL au breakeven
#     dès MFE ≥ be_trigger×SL_dist (cible la give-back diagnostiquée).
# Constantes be_trigger=1.0R / lock=breakeven : PRÉ-SPÉCIFIÉES (prior mécaniste du
# « +1.2R MFE puis give-back », 0 DoF) — pas optimisées.
ODC_V2_STRATEGY_VERSION = "odc-v2"
ODC_V2_EXIT_MODE = "be_trail"  # variante de DÉCISION
ODC_V2_BE_TRIGGER_R = 1.0  # SL → breakeven dès MFE ≥ 1R (audit : MFE +1.2R puis give-back)
ODC_V2_SL_BUF = 0.5  # SL = origine ∓ sl_buf×jambe (centre de grille)
# Reste hérité d'ODC_* (drive, pullback 38.2%, ATR, session, timeout, tickers, risk).


# ==============================================================================
# STRATÉGIE MOM_FADE (`mom-fade-v1`) — Momentum Fade 61.8% (liste user #3, M5)
# ==============================================================================
# Bougie M5 de momentum épuisé (range > range_mult×ATR, close à l'extrême) → fade au
# retracement 61.8% interne. Bull exhaust → sell limit @ high−0.618×range, TP=low,
# SL=high+buf. ⚠️ fill ambigu (niveau SOUS le marché = liq-hook literal/rest) : on
# teste les 2 lectures. literal = fill ≈ marché (≈close, razor SL) ; rest = fill au
# niveau 0.618 (RR≈0.62), skip si déjà au-delà.
MOM_FADE_STRATEGY_VERSION = "mom-fade-v1"
MOM_FADE_TICKERS = ["NQ1", "MES1", "YM1", "MGC1", "MCL1"]
MOM_FADE_TIMEZONE = "America/New_York"
MOM_FADE_SESSION_START = (9, 30)
MOM_FADE_SIGNAL_CUTOFF = (15, 0)  # 21:00 Paris
MOM_FADE_SESSION_END = (16, 0)
MOM_FADE_ATR_PERIOD = 14
MOM_FADE_RANGE_MULT = 1.5  # range > x×ATR
MOM_FADE_CLOSE_EXTREME = 0.15  # close dans les x% extrêmes du range
MOM_FADE_RETR = 0.618  # niveau de fade (interne à la bougie)
MOM_FADE_SL_BUFFER_TICKS = 2
MOM_FADE_FILL_MODE = "literal"  # "literal" (substantif = fade extreme) | "rest" (= 0 trade)
MOM_FADE_ORDER_TIMEOUT_BARS = 3  # 3 bougies M5
MOM_FADE_MAX_CONTRACTS = 40
MOM_FADE_RISK_PER_TRADE_USD = None
MOM_FADE_PARAM_GRID = {"range_mult": [1.2, 1.5, 2.0], "close_extreme": [0.10, 0.15, 0.20]}

# ==============================================================================
# STRATÉGIE SPIKE_1M (`spike-1m-v1`) — Initial Micro Spike (liste user #4, M1, MES/YM)
# ==============================================================================
# Bougie M1 de 09:31 NY (1ère minute cash) : range > range_mult×ATR_étendu, close dans
# les close_extreme% extrêmes → fade. SELL LIMIT @ high (rest au-dessus), TP=low,
# SL=high+buf. 1 trade/jour. ⚠️ DOUBLON opr_v10 (spike M1 rejeté AUC 0.50) + frictions M1.
SPIKE_1M_STRATEGY_VERSION = "spike-1m-v1"
SPIKE_1M_TICKERS = ["MES1", "YM1"]
SPIKE_1M_TIMEZONE = "America/New_York"
SPIKE_1M_OPEN = (9, 30)  # 1ère minute = [09:30,09:31)
SPIKE_1M_ATR_BARS = 20  # ATR « étendu » = moyenne range des 20 M1 précédentes
SPIKE_1M_SESSION_END = (16, 0)
SPIKE_1M_RANGE_MULT = 1.8
SPIKE_1M_CLOSE_EXTREME = 0.10
SPIKE_1M_SL_BUFFER_TICKS = 2
SPIKE_1M_ORDER_TIMEOUT_BARS = 3  # 3 minutes
SPIKE_1M_MAX_CONTRACTS = 40
SPIKE_1M_RISK_PER_TRADE_USD = None
SPIKE_1M_PARAM_GRID = {"range_mult": [1.5, 1.8, 2.2], "close_extreme": [0.05, 0.10, 0.15]}

# ==============================================================================
# STRATÉGIE LONDON_REV (`london-rev-v1`) — London Close Reversal (liste user #5, M15, Or/Pétrole)
# ==============================================================================
# À 11:30 ET (fin fixing Londres), si le prix s'est éloigné de l'open US (1ère M15 RTH)
# de ≥ move_atr×ATR sur [09:30,11:30) → fade vers l'open US. Monté → SELL LIMIT @ plus
# haut de la fenêtre, TP=open US, SL=high+buf. 1 trade/jour. ⚠️ fade gold/oil session
# (asian-sweep déjà mort) + event-driven + SL razor à l'extrême.
LONDON_REV_STRATEGY_VERSION = "london-rev-v1"
LONDON_REV_TICKERS = ["MGC1", "MCL1"]
LONDON_REV_TIMEZONE = "America/New_York"
LONDON_REV_US_OPEN = (9, 30)
LONDON_REV_FIX_TIME = (11, 30)  # fin fixing Londres (= 17:30 Paris)
LONDON_REV_ENTRY_END = (14, 30)  # ordre valide ~3h (18:30 Paris ≈ 12:30 ET ; on laisse 3h)
LONDON_REV_SESSION_END = (16, 0)
LONDON_REV_ATR_PERIOD = 14
LONDON_REV_MOVE_ATR = 0.6  # déplacement min depuis l'open US
LONDON_REV_SL_BUFFER_TICKS = 3
LONDON_REV_ORDER_TIMEOUT_BARS = 12  # ~3h M15
LONDON_REV_MAX_CONTRACTS = 40
LONDON_REV_RISK_PER_TRADE_USD = None
LONDON_REV_PARAM_GRID = {"move_atr": [0.4, 0.6, 0.8], "sl_buffer_ticks": [3, 5, 8]}

# ==============================================================================
# STRATÉGIE YMNQ_DIV (`ymnq-div-v1`) — Divergence YM/NQ extrema (liste user #6, M5, trade YM)
# ==============================================================================
# Ratio R=YM_close/NQ_close, médiane+σ glissantes (lookback bars). |R−med| > sigma×σ ET
# ranges YM&NQ > 0.3×ATR → fade sur YM. R>med+σ (YM trop haut) → SELL LIMIT @ high YM,
# TP=low YM, SL=high+sl_ticks. ⚠️ DOUBLON xmkt-rv-v1 (RV inter-marché rejeté, OOS 0.77,
# HFT capte l'index-arb sub-seconde).
YMNQ_DIV_STRATEGY_VERSION = "ymnq-div-v1"
YMNQ_DIV_TICKERS = ["YM1"]  # trade YM uniquement (NQ = référence)
YMNQ_DIV_REF_TICKER = "NQ1"
YMNQ_DIV_TIMEZONE = "America/New_York"
YMNQ_DIV_SESSION_START = (9, 30)
YMNQ_DIV_SIGNAL_CUTOFF = (15, 30)
YMNQ_DIV_SESSION_END = (16, 0)
YMNQ_DIV_ATR_PERIOD = 14
YMNQ_DIV_LOOKBACK = 30  # barres M5 pour médiane/σ du ratio
YMNQ_DIV_SIGMA = 2.5
YMNQ_DIV_MIN_RANGE_ATR = 0.3  # ranges YM & NQ ≥ 0.3×ATR (anti-compression)
YMNQ_DIV_SL_TICKS = 10
YMNQ_DIV_ORDER_TIMEOUT_BARS = 2  # 2 bougies M5
YMNQ_DIV_MAX_CONTRACTS = 60
YMNQ_DIV_RISK_PER_TRADE_USD = None
YMNQ_DIV_PARAM_GRID = {"sigma": [2.0, 2.5, 3.0], "sl_ticks": [10, 15, 20]}
