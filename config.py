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

# ==============================================================================
# GARDE-FOU TOPSTEP (challenge 50K)
# ==============================================================================
TOPSTEP_ACCOUNT_SIZE = 50_000
TOPSTEP_PROFIT_TARGET = 3_000
TOPSTEP_DAILY_LOSS_MAX = 1_000
TOPSTEP_TRAILING_DD = 2_000
TOPSTEP_SAFETY_MULT = 1.1

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
OPR_V5_1_LIVE_TICKERS = ["NQ1", "YM1"]  # MES1 reste sur v4 (config legacy)

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
