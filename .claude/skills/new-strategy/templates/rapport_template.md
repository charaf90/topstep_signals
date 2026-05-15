# Template — output/rapport_<strategy_id>.md

```markdown
# Rapport — [STRATEGY_ID]

**Date :** [YYYY-MM-DD]
**Itérations :** [N] / 5
**Verdict :** 🟢 PRODUCTION / 🟡 VEILLE / 🔴 REJET

---

## 1. Concept

### Description
[Stratégie telle qu'implémentée — version finale, pas l'idée initiale]

### Edge théorique
[Pourquoi cet edge existe-t-il ? Quels acteurs paient ce P&L ?
 Exemples : retail FOMO, hedgers institutionnels en fin de session,
 stops mécaniques sur niveaux ronds, déséquilibres post-news…
 Si pas de réponse claire → drapeau rouge.]

### Hypothèse falsifiable
[Quelle observation invaliderait la stratégie en live ?
 Exemple : "PF tombe < 1.0 sur 60 trades consécutifs OOS"
 ou "DD > $1500 sur une fenêtre de 30 jours"]

**Fenêtre de trading :** [heures NY]
**Actifs tradés :** MES1 · NQ1 · YM1 (ou sous-ensemble + justification)

---

## 2. Indicateurs et paramètres

### Indicateurs

| Indicateur | Paramètres | Rôle | Justification |
|---|---|---|---|
| [ex: EMA] | [période] | [filtre tendance] | [pourquoi cette période] |
| [ex: ATR] | [période] | [calibrage SL/TP] | [pourquoi cette période] |
| ... | ... | ... | ... |

### Paramètres optimaux retenus

| Paramètre | Valeur | Justification | Stabilité |
|---|---|---|---|
| sl_mult | X.X | [pourquoi] | [plateau / pic isolé] |
| tp_mult | X.X | [pourquoi] | [plateau / pic isolé] |
| ... | ... | ... | ... |

---

## 3. Résultats — bruts ET nets de frais

> **Frais modélisés** : slippage [N] tick(s) entrée + sortie · commission $1.40 RT/contrat
> Toutes les colonnes `pnl` sont nettes. `pnl_gross` est dispo pour comparaison.

### Source des données

| Ticker | Source | Fenêtre disponible | N bars | IS / OOS appliqué |
|---|---|---|---|---|
| [ex: MES1] | CSV TradingView (data/MES1_data_m15.csv) | 2024-12-01 → 2026-03-02 | ~29k | IS=déc24→sept25, OOS=oct25→mars26 |
| [ex: MGC1] | data_fetcher ProjectX (fetché YYYY-MM-DD) | 2026-MM-DD → 2026-MM-DD | ~Nk | IS=…→…, OOS=…→… (adapté) |


### IS (déc 2024 → 2025-09-30)

| Asset | Trades | WR | PF brut | PF net | P&L brut | P&L net | DD max |
|---|---|---|---|---|---|---|---|
| MES1 | | | | | | | |
| NQ1 | | | | | | | |
| YM1 | | | | | | | |
| **Portfolio** | | | | | | | |

### OOS (2025-10-01 → mars 2026)

| Asset | Trades | WR | PF brut | PF net | P&L brut | P&L net | DD max |
|---|---|---|---|---|---|---|---|
| MES1 | | | | | | | |
| NQ1 | | | | | | | |
| YM1 | | | | | | | |
| **Portfolio** | | | | | | | |

### Métriques de robustesse

| Métrique | Valeur | Seuil 🟢 |
|---|---|---|
| Bootstrap **portfolio** OOS (block, 1000 itér.) | XX % | ≥ 80 % |
| Bootstrap par ticker (info, peut être bas) | MES1: XX% / NQ1: XX% / YM1: XX% | – |
| Probabilistic Sharpe Ratio PSR(0) | XX % | ≥ 95 % |
| Dégradation IS→OOS (PF net) | XX % | ≤ 30 % |
| Sharpe annualisé OOS | X.XX | ≥ 1.0 |
| Max conséqs perdants | XX | < limite Topstep / risque trade |
| Correction multiple testing | Bonferroni / SPA / RC | – |

---

## 4. Stress tests

### 4a. Performance par régime (OOS)

| Régime | Trades | PF net | P&L net | Verdict |
|---|---|---|---|---|
| Trending (ADX > 25) | | | | ✅ / ⚠️ / ❌ |
| Ranging (ADX < 20) | | | | ✅ / ⚠️ / ❌ |
| Volatilité haute (ATR P > 75) | | | | ✅ / ⚠️ / ❌ |
| Volatilité basse (ATR P < 25) | | | | ✅ / ⚠️ / ❌ |
| Jours macro (FOMC/CPI/NFP) | | | | ✅ / ⚠️ / ❌ |

### 4b. Monte Carlo (1000 permutations de l'ordre des trades OOS)

| Métrique | Valeur |
|---|---|
| DD median | -$XXX |
| DD P95 | -$XXX |
| DD P99 | -$XXX |
| P(DD > limite Topstep restante) | XX % |

### 4c. Worst-case clustering

[Les 20 pires trades OOS sont-ils concentrés ?]
- Période courte : [oui/non + plage]
- Actif unique : [oui/non + lequel]
- Type de jour : [oui/non + jour macro / jour de la semaine]

**Conclusion stress tests :** [synthèse]

---

## 5. Complémentarité avec le portefeuille existant

| Mesure | Valeur | Interprétation |
|---|---|---|
| Corrélation Spearman daily P&L vs OPR opr-v4 | 0.XX | [complémentaire / redondant] |
| Corrélation Spearman daily P&L vs Fib fib-v3 | 0.XX | [complémentaire / redondant] |
| Chevauchement temporel (heures NY) | XX % | [orthogonal / chevauchant] |
| Chevauchement actifs | [identique / différent] | – |

**P&L combiné simulé** (OPR + Fib + nouvelle stratégie) sur OOS :
- P&L net total : +$XX XXX (vs +$33 379 baseline OPR+Fib)
- DD max combiné : -$X XXX (vs -$822 baseline)
- Sharpe combiné : X.XX

---

## 6. Charts générés

- `output/equity_curve_<strategy_id>.png` — courbe d'equity cumulée IS+OOS
- `output/drawdown_underwater_<strategy_id>.png` — DD underwater
- `output/monthly_heatmap_<strategy_id>.png` — P&L mensuel par actif
- `output/hourly_distribution_<strategy_id>.png` — distribution horaire
- `output/day_*_<strategy_id>.png` — 5 jours gagnants + 5 jours perdants
- `output/correlation_rolling_<strategy_id>.png` — corrélation rolling 60j vs OPR/Fib

---

## 7. Verdict & recommandations

### [🟢 PRODUCTION / 🟡 VEILLE / 🔴 REJET]

### Justification
[Métriques clés + analyse qualitative.
 Pour un 🟢 : préciser ce qui a convaincu malgré les risques.
 Pour un 🟡 : préciser ce qui manque pour passer 🟢.
 Pour un 🔴 : préciser si fatal ou corrigeable.]

### Conditions d'upgrade / downgrade
- **Pour passer 🟡 → 🟢 :** [conditions précises et mesurables]
- **Pour rétrograder 🟢 → 🟡 :** [conditions précises et mesurables]
- **Pour rétrograder à 🔴 :** [conditions de kill-switch]

### Hypothèse falsifiable rappelée
[Reprise de la section 1 — ce qui invalide la stratégie en live]

### Workflow de promotion (si 🟢)

> **Demander confirmation explicite à l'utilisateur avant chaque étape touchant `core/` ou `broker/`.**

1. [ ] Créer `core/<strategy_id>.py` avec la fonction `get_<strategy>_live_signal()`
2. [ ] Mettre à jour `broker/live_runner.py` (imports + boucle de session)
3. [ ] Mettre à jour `core/signal_selector.py` si actifs spécifiques
4. [ ] Configurer `core/event_logger.py` pour tracer fills/closes/erreurs de la nouvelle stratégie
5. [ ] Tester en simulation (`PROJECTX_LIVE_MODE = False`) pendant 5 jours
6. [ ] Activation progressive en live : 1 contrat 1 semaine → sizing nominal
7. [ ] Surveillance post-promotion : seuils de kill-switch précisés ci-dessus

### Si 🟡 — actions
- [ ] Affiner [paramètre X]
- [ ] Attendre [N] trades OOS supplémentaires
- [ ] Surveiller en paper trading pendant [N] jours
- [ ] Revoir si [condition de réévaluation]

### Si 🔴 — actions
- [ ] Documenter la raison de rejet
- [ ] Préciser ce qui serait nécessaire pour reconsidérer (régime, indicateur, hypothèse)

---

## 8. Historique des itérations

### Itération 1 — [STRATEGY_ID]-v1
**Description :** [version initiale]
**Résultats clés :** PF OOS X.XX · Bootstrap XX % · DD -$XXX
**Verdict :** 🔴/🟡/🟢 — [raison]
**Modification décidée :** [ce qui change pour v2]

### Itération 2 — [STRATEGY_ID]-v2
**Description :** [ce qui a changé]
**Résultats clés :** PF OOS X.XX · Bootstrap XX % · DD -$XXX
**Verdict :** 🔴/🟡/🟢 — [raison]
**Modification décidée :** [ce qui change pour v3]

[...etc jusqu'à la version finale]

---

## 9. Annexes

### 9a. Configuration finale (à reporter dans `config.py`)

```python
# ==============================================================================
# STRATÉGIE <NOM>
# ==============================================================================
<STRATEGY_ID>_STRATEGY_VERSION       = "<strategy_id>"
<STRATEGY_ID>_SL_ATR_MULT_PER_TICKER = {"MES1": X.X, "NQ1": X.X, "YM1": X.X}
<STRATEGY_ID>_TP_ATR_MULT_PER_TICKER = {"MES1": X.X, "NQ1": X.X, "YM1": X.X}
<STRATEGY_ID>_HOUR_START_NY          = X
<STRATEGY_ID>_HOUR_END_NY            = X
<STRATEGY_ID>_ORDER_TIMEOUT_BARS     = X
# ...
```

### 9b. Limites connues
[Ce que ce backtest ne capture PAS et qui pourrait surprendre en live :
 - latence d'exécution réelle
 - profondeur du carnet en moments de stress
 - corrélation avec événements non listés dans MACRO_EVENT_DATES
 - changements structurels non observés dans la fenêtre IS+OOS
 - frais réels Topstep (peuvent évoluer)]
```
