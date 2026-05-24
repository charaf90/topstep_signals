# Détecteur de pivots ML — Étude de recherche

> **Statut** : recherche clôturée le 2026-05-18 — résultat robuste identifié sur MGC1, en attente d'exploitation dans une stratégie.
>
> **Ce fichier** : référence auto-contenue pour reprendre le travail dans une session ultérieure sans avoir à recharger le contexte.

---

## 1. Hypothèse initiale

Concept proposé : sur les contrats à terme M5,
1. Générer des **points pivots** (hauts/bas locaux) via `scipy.signal.argrelextrema(Close, order=N)`.
2. Identifier les zones de prix où les pivots se concentrent (proxy "S/R").
3. Enrichir avec des features techniques.
4. Entraîner un classificateur pour détecter ces pivots **avec une haute précision** (peu importe le recall).

**Hypothèse de travail principale** : les zones de clustering de pivots passés sont prédictives de futurs pivots (concept S/R classique).

**Status** : ❌ **HYPOTHÈSE FALSIFIÉE**. La densité de pivots historiques (`past_pivot_density_2atr`) reste en queue d'importance pour les 25 expériences (5 actifs × 5 orders), avec même une légère **anti-corrélation** observée (plus de pivots passés ⇒ légèrement moins de pivots actuels).

---

## 2. Méthodologie

### Données
- **Source** : `data/<TICKER>_data_m5.csv` (OHLCV M5)
- **Période** : 2025-10-19 → 2026-05-15 (~7 mois, ~40 000 barres par actif)
- **Actifs testés** : NQ1, MES1, YM1, MGC1 (Gold), MCL1 (Crude)

### Label (oracle non-causal)
```python
is_pivot_high = argrelextrema(close.values, np.greater, order=N)
is_pivot_low  = argrelextrema(close.values, np.less,    order=N)
is_pivot_any  = is_pivot_high OR is_pivot_low
```
- `order` testés : 2, 5, 10, 15, 20
- **Caveat** : ce label utilise le futur (un pivot à `t` n'est confirmé qu'à `t+N`). En pratique trading, un signal serait actionnable à `t+N+1` au plus tôt. C'est accepté car on évalue la **prédictibilité** pas la tradabilité directe.

### Features (31 baseline, toutes strictement causales)
- **Momentum** : EMA9/21/50 slopes, ROC 5/20/50, ADX(14)
- **Volatilité** : ATR(14), ATR ratio short/long, BB width, range/ATR, dist(close, EMA21)
- **Microstructure** : body/range, upper/lower wick, vol_rel
- **Position relative** : `dist_to_max20_atr`, `dist_to_min20_atr`, densité de pivots passés (1×ATR et 2×ATR)
- **Séquence** : returns lag 1/2/3/5/10, up_bars_last_10
- **Timing** : hour_ny (DST-aware), minute_ny, dow, bars_since_open, is_macro_day

### Modèles
- Logistic regression (baseline linéaire)
- Random Forest (500 arbres, max_depth=8, class_weight=balanced)
- HistGradientBoosting (300 iters, lr=0.05)

### Évaluation
- **Métrique principale** : précision à recall = 10 % (la cible étant la détection à haute précision)
- **Secondaire** : PR-AUC, lift vs base rate
- **Validation temporelle** : walk-forward 4 splits expanding window (OOS ~2 mois chacun)
  - Split 1 : IS → 2025-12-31 / OOS jan-fév 2026
  - Split 2 : IS → 2026-01-31 / OOS fév-mar
  - Split 3 : IS → 2026-02-28 / OOS mar-avr
  - Split 4 : IS → 2026-03-31 / OOS avr-mai

---

## 3. Trajectoire des expériences

| # | Étape | Résultat |
|---|---|---|
| 1 | Baseline M5 (5 actifs × 5 orders) | ✅ Lift monte avec order. `dist_to_min20_atr` #1 universel pour les 5 actifs à order=20. |
| 2 | Walk-forward 4 splits | ✅ Validation : YM1 ×7.25 démasqué comme artefact (WF mean ×5.6). MGC1 et MCL1 confirmés. |
| 3 | Multi-TF H1 (labels + features H1) | ⛔ Pas de gain net vs M5 à lift comparable. |
| 4 | Divergences v1 (binaires RSI/Stoch/CCI) | ⛔ Dégrade légèrement (-0.4 à -1.6 pp). |
| 5 | Divergences v2 (continues + OBV/MFI) | ⛔ Dégrade aussi. Toutes nouvelles features en queue de classement. |
| 6 | Diagnostic high-confidence errors | ✅ `hour_ny` discriminant pour MCL1, `vol_rel` pour MGC1. |
| 7 | Grid search combos | ✅ Précision agrégée 27-29 %. |
| 8 | **Validation split-by-split** | ✅ **MGC1 ultra-stable (std 0.5 %)**, ⚠ MCL1 partiellement overfit. |

---

## 4. Résultat principal — Filtres robustes identifiés

### 🏆 MGC1 (Gold) — Le résultat le plus solide de l'étude

**Filtre** :
```
proba_RF ≥ p10% (seuil dynamique) 
AND vol_rel ≥ 2.5
AND range_atr_ratio ≥ 1.5
AND hour_ny ≥ 6
```

**Performance walk-forward (4 splits chronologiquement distincts)** :

| Split | n signaux OOS | Précision |
|:-:|:-:|:-:|
| 1 | 41 | 27.78 % |
| 2 | 44 | 29.27 % |
| 3 | 40 | 28.79 % |
| 4 | 41 | 28.13 % |
| **Mean ± std** | — | **28.39 % ± 0.54 %** |

- Spread max-min = 1.5 pp seulement
- **Lift vs base rate (3.23 %) : ×8.79**
- **Lift vs baseline (~19 %) : ×1.49**

**Interprétation trading** :
- `vol_rel ≥ 2.5` : barre courante avec un volume **2.5× au-dessus** de la moyenne 20 barres
- `range_atr_ratio ≥ 1.5` : barre **plus large que 1.5× l'ATR moyen** → mouvement marqué
- `hour_ny ≥ 6` : session européenne ou après → on évite les heures asiatiques de faible activité

→ Cohérent avec l'analyse technique classique : les vrais pivots sur l'or s'accompagnent d'un "wash out" volumétrique avec une bougie wide en session active.

### MCL1 (Crude) — Signal stable mais simple

**Filtre robuste** : `proba_RF ≥ p10% AND hour_ny ≥ 13`

| Split | Précision |
|:-:|:-:|
| Min | 23.6 % |
| Max | 33.8 % |
| **Mean ± std** | **26.7 % ± 4.1 %** |

- **Lift vs base rate (2.96 %) : ×9.0**
- Plus simple que MGC1, moins de filtres → moins d'overfit
- Recall conservé : 59 %

**Combo enrichi (NON RECOMMANDÉ tel quel)** : `hour_ny ≥ 14 AND vol_rel ≥ 1.5`
- Mean 29.64 %, **mais std 7.08 % et spread 20 pp** → instable
- Le grid search a exploité du bruit sur 1-2 splits chanceux
- À traiter avec prudence si exploité plus tard

### Pourquoi pas les indices ?

NQ1, MES1, YM1 montrent aussi un edge baseline (PR-AUC OOS ≈ 0.13-0.14 à order=20, P@R10% ≈ 15-17 %), mais :
- Aucune feature aussi discriminante que `hour_ny` ou `vol_rel` n'a été identifiée
- Le diagnostic n'a pas été poussé sur eux (étape #6 et #7 limitées à MCL1/MGC1)
- **Piste à creuser** dans une session future

---

## 5. Apprentissages négatifs (à NE PAS re-tester sans nouvelle hypothèse)

| Hypothèse testée | Résultat | Raison probable |
|---|---|---|
| Zones de pivots passés (densité ±X ATR) | Falsifiée | Anti-corrélation faible avec is_pivot |
| Divergences prix vs oscillateurs (binaires RSI/Stoch/CCI) | Dégrade | Toute info dérivée d'OHLCV est mécaniquement corrélée à `dist_to_min/max20_atr` + `ret_lag` |
| Divergences continues z-score normalisées | Dégrade | Idem — la formulation continue ne révèle pas plus d'info |
| Features volume orthogonales (OBV, MFI) | Dégrade aussi (curse of dimensionality) | Pas d'info supplémentaire vs `vol_rel` |
| Multi-TF H1 (labels + features H1) | Pas de gain | À sample size équivalent, M5 reste meilleur en lift |

**Méta-leçon** : pour réellement débloquer un edge supplémentaire, il faudrait des données **vraiment orthogonales** à l'OHLCV agrégé : orderflow (footprint, CVD, delta), tick data, profil de volume intraday, news/sentiment. Tout ce qui dérive d'OHLCV par formule classique est mécaniquement contenu dans les features baseline.

---

## 6. Limites de l'étude

1. **Période courte** : 7 mois de données. Les filtres trouvés tiennent sur 4 splits chevauchants, mais une fenêtre plus longue (1-2 ans) renforcerait la confiance.
2. **Label non-causal** : un pivot à `t` n'est confirmé qu'à `t+N`. Le passage à la production demandera de redéfinir un label causal (ex: "pivot confirmé prédit à t+N+1") et de re-tester.
3. **Précision < 30 %** : reste 🟠 selon notre règle automatique. Pas un signal autonome, mais utilisable comme **filtre** ou **feature** dans un système plus large.
4. **Pas de PnL backtest** : on a mesuré la précision ML, pas la rentabilité après slippage/commissions. Étape nécessaire avant toute stratégie live.
5. **Pas testé sur autres actifs** : tout est validé sur les contrats à terme micro-CME (MGC, MCL, MES, NQ, YM). Pas de FX, indices ETF, actions, crypto.

---

## 7. Prochaines étapes — pistes pour une session future

### Pistes prioritaires (validation/robustesse)

1. **Holdout pur** : refaire le pipeline COMPLET (incluant grid search) sur 2025-10 → 2026-01, puis tester la rule fixe sur 2026-02 → 2026-05. Mesure le vrai pouvoir de généralisation, sans data leakage du grid search.
2. **Étendre le diagnostic + combo aux 3 indices** (NQ1, MES1, YM1) — voir si MGC1 est exceptionnel ou si le pattern est plus large.
3. **Backtest PnL** : transformer le détecteur en stratégie naïve (long si pivot_low confirmé, short si pivot_high), mesurer rentabilité nette après slippage MCL1/MGC1.

### Pistes pour pousser le signal

4. **Label causal** : redéfinir `is_pivot` comme "confirmation à t" prédit à `t-N` (causal stricte). Comparer si le modèle perd beaucoup en précision.
5. **Orderflow** : si on a accès à du tick data, tester features CVD, delta, volume profil intraday — probablement la seule voie pour passer 🟡 → 🟢.
6. **Ensemble multi-actifs** : combiner les signaux MGC1 et MCL1 (corrélation commodities) → peut-être un signal portfolio plus robuste.

### Pistes annexes (low priority)

7. Tester sur d'autres horizons de pivot (order=25, 30, 50) — mais sample size devient critique.
8. Tester d'autres modèles (LightGBM, XGBoost si installés) — marginal vu que RF/HGB donnent déjà la même chose.
9. Calibration de probabilités (Platt scaling, isotonic) — pour avoir une vraie probabilité interprétable.

---

## 8. Outputs et scripts (reproductibilité)

### Scripts (tous dans `scripts/research_pivot_*.py`)

| Fichier | Rôle |
|---|---|
| `research_pivot_nq1.py` | Pipeline complet single-split (label + features + 3 modèles + rapport) |
| `research_pivot_wf.py` | Walk-forward 4 splits |
| `research_pivot_h1.py` | Multi-TF H1 |
| `research_pivot_divergence.py` | Test divergences v1 binaires (RSI/Stoch/CCI) |
| `research_pivot_divergence_v2.py` | Test divergences v2 continues + OBV/MFI |
| `research_pivot_diagnostic.py` | Diagnostic FP vs TP, recherche features discriminantes |
| `research_pivot_combo.py` | Grid search combos atomic filters |
| `research_pivot_validate.py` | Validation split-by-split des combos |
| `run_pivot_grid.sh` / `run_pivot_wf_grid.sh` / `run_pivot_h1_grid.sh` | Launchers bash pour parallélisation par ticker |

### Outputs (tous dans `output/pivot_research*/`)

| Dossier | Contenu |
|---|---|
| `pivot_research/<TICKER>/order_N/` | Baseline single-split par cellule |
| `pivot_research_wf/<TICKER>/order_N/` | Walk-forward par cellule |
| `pivot_research_h1/<TICKER>/order_N/` | Multi-TF H1 par cellule |
| `pivot_research_div/<TICKER>/` | Divergences v1 |
| `pivot_research_div_v2/<TICKER>/` | Divergences v2 |
| `pivot_research_diag/<TICKER>/` | Diagnostic FP vs TP |
| `pivot_research_combo/<TICKER>/` | Grid search combos |
| `pivot_research_combo/validation/` | **Validation split-by-split (résultat final)** |

### Synthèses agrégées

- `output/pivot_research/SYNTHESE_global.md` — Baseline M5 single-split (25 expériences)
- `output/pivot_research_wf/SYNTHESE_global_WF.md` — Walk-forward (25 expériences)
- `output/pivot_research_h1/SYNTHESE_h1.md` — H1 multi-TF
- `output/pivot_research_combo/SYNTHESE_FINALE.md` — Synthèse pré-validation
- `output/pivot_research_combo/validation/{MCL1,MGC1}_validation.md` — Validation split-by-split

---

## 9. Commandes pour reprendre

```bash
# Rejouer le pipeline complet pour MGC1 (l'actif "winner")
python scripts/research_pivot_validate.py

# Inspecter le rapport final
cat output/pivot_research_combo/validation/MGC1_validation.md

# Refaire la cellule baseline MGC1 order=20 single-split
python scripts/research_pivot_nq1.py --ticker MGC1 --order 20

# Refaire le walk-forward MGC1 order=20
python scripts/research_pivot_wf.py --ticker MGC1 --order 20
```

---

## 10. Verdict pour une stratégie future

**MGC1 order=20 + filtre `vol_rel ≥ 2.5 AND range_atr_ratio ≥ 1.5 AND hour_ny ≥ 6`** est le **candidat principal** pour le développement d'une stratégie de retournement sur Gold M5. Le filtre :
- Sort un signal **toutes les ~3-4 jours** environ (~40 par split de 2 mois)
- Avec une précision **≈ 28 % stable** (vs base rate 3 %)
- Donne ~10 trades/mois avec 3 vrais retournements sur 10 → potentiellement exploitable si RR ≥ 2.5

**Prochaine étape concrète** : backtest PnL avec RR=2, RR=3, SL serré (1×ATR ?), pour mesurer si la précision 28 % suffit à être profitable net de frais.

Aussi à creuser avant d'écrire la stratégie : la nature exacte des **6 splits skip 4 sur 40-44** (i.e. les TP : quelle est leur ampleur de retournement post-entry ? On peut détecter le pivot mais si le swing est trop court ça ne paye pas).
