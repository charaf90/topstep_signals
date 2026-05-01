# README — Logique de la stratégie Fibonacci 50% (M15)

> Brouillon de recherche **isolé** dans `draft_fibo_50/`. Aucune dépendance vers
> les modules de production (`core/`, `config.py` racine). Tout est self-contained
> pour permettre de tester, casser et itérer librement.

---

## 1. Pourquoi cette stratégie

Le retracement Fibonacci 50 % est un point d'entrée bien documenté en analyse
technique : après une impulsion (mouvement directionnel fort), le prix revient
souvent corriger 50 % de l'amplitude avant de reprendre la direction principale
(*"buy the dip" / "sell the rally"*).

L'enjeu est triple :

1. **Filtrer le contexte** — ne tenter le pullback que si la tendance est
   confirmée (sinon l'impulse n'est qu'un faux mouvement dans un range).
2. **Quantifier l'impulsion** — éviter les pseudo-impulses (mouvements
   marginaux) qui ne créent pas de vraie zone de retest.
3. **Doser le risque** — adapter SL/TP à la volatilité du jour pour
   maintenir un risque dollar fixe.

Sur Topstep 50K, la priorité n'est pas le Net Profit brut mais le **risk-adjusted
return** (Sharpe) : un drawdown trop fort fait perdre le compte avant de
toucher la cible. C'est pourquoi l'optimiseur cible le Sharpe, pas le P&L.

---

## 2. Choix des indicateurs

### EMA 50 / EMA 200 (tendance)

- Calcul standard `ewm(span=N, adjust=False)` — formule TradingView/MT5
  identique.
- **Sur M15 :** EMA50 ≈ 12.5h (≈ 1.5 sessions US), EMA200 ≈ 50h
  (≈ 6 sessions). Couvre simultanément la tendance intra-jour et la
  tendance multi-jours.
- Combinaison utilisée : `close > EMA50 > EMA200` (BULL) ou inverse (BEAR).
  Ce pattern est appelé *EMA stack* — il garantit l'alignement de plusieurs
  horizons temporels.

### ADX (14) (force de tendance)

- ADX = `rolling_mean(DX, 14)` avec `DX = 100·|+DI − −DI|/(+DI + −DI)`.
- Seuil retenu : **20**. C'est la valeur de référence Wilder pour distinguer
  un marché *trending* d'un marché *choppy*. En-dessous, le prix oscille
  sans direction nette → on classe en RANGE.
- **Justification du combo EMA + ADX :** l'EMA seule peut donner de fausses
  alertes en range (les EMAs s'alignent brièvement). L'ADX corrige ce biais
  en confirmant qu'il y a bien un *flux directionnel*.

### ATR (14) (volatilité)

- ATR = `rolling_mean(TR, 14)`. Cohérent avec `core/opr.py` du projet
  principal. Pas la version Wilder *smoothed* (RMA) parce que :
  1. Le projet utilise déjà SMA dans `core/scoring.py` et `core/opr.py`.
  2. Les écarts entre RMA et SMA sont marginaux (< 5 %) sur 14 périodes.
- L'ATR sert à 3 choses :
  - Filtrer les impulses (taille minimale = `MIN_IMPULSE_ATR × ATR`).
  - Calibrer SL/TP (`SL_dist = SL_ATR_MULT × ATR`).
  - Calculer le sizing (`n_ct = $100 / (sl_dist × $/pt)`).

---

## 3. Définition mathématique de l'impulsion

L'impulse n'est **pas** définie par la simple variation de prix sur N bougies
(approche fragile aux mèches). On la définit par les **pivots de structure**
+ filtres ATR/durée/tendance.

### 3.1 Détection des pivots

Méthode left/right classique : un point est pivot si c'est l'extrême sur
`[i-LEFT, i+RIGHT]`. Avec `LEFT = RIGHT = 8` (= 4 h M15 de chaque côté), un
pivot doit être plus extrême que ses voisins immédiats ET être l'extrême
absolu sur 17 bougies. C'est rigoureux et ça filtre les micro-pivots.

**Conséquence importante :** un pivot à l'index `i` n'est CONFIRMÉ qu'à l'index
`i + RIGHT`. Pas de leak temporel — toute exploitation d'un pivot dans la
boucle de backtest se fait au plus tôt à `i + RIGHT + 1`.

### 3.2 Validation de l'impulse

À chaque bougie, on cherche le **dernier pivot final confirmé** dans la
direction de la tendance détectée :

- BULL → dernier pivot_high → on remonte au pivot_low précédent
- BEAR → dernier pivot_low → on remonte au pivot_high précédent

L'impulse `(pivot_low, pivot_high)` (ou inverse) est validée si :

| Critère | Seuil | Raison |
|---|---|---|
| Distance ≥ `MIN_IMPULSE_ATR × ATR(@pivot final)` | défaut 1.5× | pas de pseudo-impulse |
| Durée ≤ `MAX_IMPULSE_BARS` | 25 (~6h) | impulse propre, pas un drift |
| Direction = tendance courante | hard | filtre contre-tendance |
| `current_idx − pivot_final ≤ IMPULSE_LOOKBACK` | 60 (~15h) | impulse récente uniquement |

### 3.3 Pourquoi cette définition

- **Pivots vs ROC/momentum :** les pivots saisissent la *structure* du
  mouvement (les "vraies" zones de retournement), pas juste sa vitesse.
- **Filtre ATR :** une distance < 1.5× ATR n'est qu'une fluctuation normale,
  pas un mouvement directionnel exploitable.
- **Filtre durée :** un mouvement étalé sur 50+ bougies n'est pas une
  *impulsion* mais une *tendance lente* — le pullback n'a pas la même
  signification.

---

## 4. Logique d'entrée Fibonacci 50%

```
Pour BULL impulse :
    fib_50 = swing_low + 0.5 × (swing_high − swing_low)
    → ordre LIMIT BUY @ fib_50

Pour BEAR impulse :
    fib_50 = swing_high − 0.5 × (swing_high − swing_low)
    → ordre LIMIT SELL @ fib_50
```

### Conditions d'armement

1. Aucune position ouverte ni ordre en attente.
2. Tendance ≠ RANGE.
3. Impulse valide trouvée (cf. § 3.2).
4. Impulse pas déjà tradée (clé d'unicité = `(direction, pivot_low_idx, pivot_high_idx)`).
5. Prix actuel n'a pas déjà dépassé `fib_50` dans le sens du trade
   (sinon le pullback est consommé — armer un ordre limite serait
   incohérent avec la logique pullback).
6. En session US (13h-21h UTC), pour cohérence avec OPR/composite.

### Vie de l'ordre

- L'ordre devient actif sur la **bougie suivante** son armement (pas de
  look-ahead — on ne checke pas le fill sur la bougie d'armement).
- Il fille dès que `bar.low ≤ fib_50 ≤ bar.high`.
- Il expire après `ORDER_TIMEOUT_BARS = 12` bougies (~3h) sans fill.

---

## 5. Gestion du risque (SL/TP/timeout)

### Formules

```
sl_dist = SL_ATR_MULT × ATR(@armement)
tp_dist = TP_ATR_MULT × ATR(@armement)

LONG  : SL = fib_50 − sl_dist  ;  TP = fib_50 + tp_dist
SHORT : SL = fib_50 + sl_dist  ;  TP = fib_50 − tp_dist

n_ct = floor($100 / (sl_dist × $/pt))
```

### Choix des paramètres

- **SL/TP basés ATR** plutôt que distances fixes : adaptation automatique au
  régime de volatilité (jour calme → SL serré + plus de contrats ;
  jour volatil → SL large + moins de contrats). C'est la même philosophie
  que `core/opr.py` opr-v3/v4.
- **Sizing risque dollar fixe** ($100) : standard du projet. Permet de
  comparer directement les performances entre stratégies.
- **MAX_HOLD_BARS = 32** (~8h) : si SL/TP n'a pas été touché en une
  session, on ferme au close (TE). Évite de laisser dormir une position
  overnight (slippage/gap) ou pendant une consolidation prolongée.

---

## 6. Optimisation : pourquoi le Sharpe ratio

Trois fonctions objectif possibles : Net Profit, Profit Factor, Sharpe ratio.

| Critère | Avantages | Inconvénients |
|---|---|---|
| Net Profit | Simple, cible Topstep ($3K) | Ignore la régularité — un gros gain isolé masque 10 pertes |
| Profit Factor | Mesure le ratio gains/pertes | Pondère mal les outliers (un trade énorme peut faire monter le PF artificiellement) |
| **Sharpe ratio** | Pénalise la variance — exactement ce qu'on veut sur Topstep | Plus volatil sur petit échantillon |

**Sharpe annualisé** = `mean(returns) / std(returns) × √252`, où
`returns = pnl / ACCOUNT_SIZE`. L'annualisation `√252` est conventionnelle
pour des trades considérés comme indépendants à fréquence quotidienne.
Elle sert au ranking, pas à un objectif absolu.

### Critères de validation OOS (filtre dur)

Une combinaison n'est **acceptée** que si :

```
OOS Sharpe ≥ 0.5
OOS PF ≥ 1.2
OOS n ≥ 8
OOS P&L > 0
IS n ≥ 10
```

Parmi les combos validés, on retient celle qui **maximise IS Sharpe** —
c'est cohérent avec `optimize_opr.py` du projet (calibration sur IS, validation
en aveugle sur OOS).

---

## 7. Hypothèses & limites

| Hypothèse | Réalisme | Impact si fausse |
|---|---|---|
| Fill garanti à fib_50 si touché | ⚠ Assume slippage = 0 | Surestime PF de quelques % |
| Pas de frais brokers | ✅ Topstep absorbe les commissions | Aucun |
| Pivots calculés en avance (pré-boucle) | ✅ Mais usage différé via `confirm_idx` | Aucun (cf. § 3.1) |
| ATR de la bougie courante utilisé pour SL/TP | ✅ Disponible en live | Aucun |
| Indicators recalculés sur l'historique entier | ⚠ Léger biais en pratique vs streaming live | Marginal sur EMA/ATR (convergent rapidement) |
| Sessions US uniquement (13-21 UTC) | ✅ Liquidité maximale | Manque les setups Asie/EU mais réduit le bruit |

### Limites identifiées

1. **Sample size OOS faible** : ~50-80 trades par ticker sur 6 mois OOS — le
   Sharpe OOS reste bruité.
2. **Sensibilité aux pivots** : changer `PIVOT_LEFT/RIGHT` redéfinit la
   structure ; un pivot 8/8 vs 5/5 donne des résultats très différents.
3. **Dépendance régime** : la stratégie suppose une tendance + un pullback ;
   en marché purement choppy (post-FOMC, news), le filtre tendance la met
   en pause mais peut quand même prendre des faux signaux.

---

## 8. Workflow d'utilisation

```bash
cd draft_fibo_50/

# 1. Backtest paramètres par défaut (sanity check)
python backtest.py --csv-dir ../data

# 2. Optimisation walk-forward (~15-20 min sur 3 tickers)
python optimize.py --csv-dir ../data

# 3. Reporter les meilleurs paramètres dans config.py manuellement
#    (le script ne modifie pas config.py — cohérent avec optimize_opr.py)

# 4. Re-run backtest avec params optimisés
python backtest.py --csv-dir ../data

# 5. Générer les visualisations des 50 derniers trades par ticker
python visualize.py --csv-dir ../data
#    → output/charts/{TICKER}/{NN}_{result}_{pnl}.png
```

### Fichiers de sortie

```
output/
├── trades_{TICKER}.csv            # détail des trades clos
├── optimization_{TICKER}.csv      # grille complète SL × TP × IMP
├── performance.md                 # rapport final exhaustif
└── charts/{TICKER}/               # 50 PNGs par ticker
```

---

## 9. Comment auditer / itérer

1. **Lire `output/performance.md`** pour la vue macro.
2. **Inspecter visuellement les 50 derniers PNGs** : chercher des patterns
   bizarres (entrée trop tôt, SL trop serré, swing mal détecté).
3. **Si un pattern aberrant ressort**, modifier les paramètres dans
   `config.py` et re-lancer (1) → (2).
4. **Si la stratégie est viable mais imparfaite**, lancer une optimisation
   plus fine (`optimize.py` avec grille élargie ou ajout de nouveaux
   paramètres : `PIVOT_LEFT/RIGHT`, `IMPULSE_LOOKBACK`, etc.).

Le brouillon est conçu pour être **jeté** ou **promu** au statut de
production : si les résultats OOS tiennent (Sharpe ≥ 0.5, PF ≥ 1.2),
on peut envisager de migrer le code dans `core/strategy_fib.py` et de
brancher sur `backtest.py` racine.
