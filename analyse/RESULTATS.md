# Analyse exploratoire — Filtrage OPR

**Date :** 2026-04-29  
**Stratégie :** OPR opr-v3  
**Période :** IS = Dec 2024 → Sep 2025 / OOS = Oct 2025 → Mar 2026

---

## Méthode

Extraction de ~20 features au moment du trigger (bougie qui casse l'OPR, avant placement
de l'ordre limite), pour tous les triggers générés (filled + NOT_FILLED). Analyse
univariée Mann-Whitney U + corrélation point-biserial sur IS. Walk-forward test des
meilleurs filtres : seuil calibré sur IS uniquement, évalué en aveugle sur OOS.

Critère de validation OOS (identique à `optimize_opr.py`) : PF ≥ 1.2, n ≥ 8, P&L > 0.

---

## Données brutes (triggers extraits)

| Ticker | Triggers total | TP | SL | TE | NOT_FILLED | Win rate fills |
|--------|---------------|----|----|-----|------------|----------------|
| MES1 | 534 | 196 | 192 | 36 | 110 | 46.2% |
| NQ1 | 608 | 216 | 260 | 3 | 129 | 45.1% |
| YM1 | 613 | 201 | 267 | 17 | 128 | 41.4% |

---

## Résultats par ticker

### MES1 — baseline OOS PF=1.33

**Features les plus discriminantes (IS) :**
| Feature | MWU p-value | PBC | Interprétation |
|---------|-------------|-----|----------------|
| `close_beyond_opr_atr` | 0.136 | +0.097 | Plus la bougie clôture loin de l'OPR → meilleur |
| `trigger_vol_zscore` | 0.176 | -0.101 | Volume élevé au trigger → moins bon |
| `max_excursion_atr` | 0.210 | +0.089 | Grande excursion vers OPR avant trigger → meilleur |

**Filtres validés OOS :**
| Filtre | Seuil | OOS n | OOS PF | Δ PF | OOS P&L |
|--------|-------|-------|--------|------|---------|
| `trigger_vol_zscore < -0.45` | -0.454 | 41 | **1.794** | +0.460 | +$870 |
| `max_excursion_atr > 0.22` | 0.224 | 46 | 1.365 | +0.031 | +$570 |
| `ovn_path_eff > 0.27` | 0.273 | 16 | 1.381 | +0.047 | +$232 |

**Meilleur filtre MES1 : `trigger_vol_zscore < -0.45`**
- Conserve 41/148 fills OOS (28%), PF passe de 1.33 → 1.79
- Interprétation : rejeter les triggers sur bougie à volume anormalement élevé vs la session.
  Un spike de volume au moment de la cassure OPR = move émotionnel/news, pas de pullback propre.

---

### NQ1 — baseline OOS PF=1.62

**Features les plus discriminantes (IS) :** faibles (p > 0.35 pour tout)

**Filtres validés OOS :**
| Filtre | Seuil | OOS n | OOS PF | Δ PF | OOS P&L |
|--------|-------|-------|--------|------|---------|
| `opr_range_atr_ratio < 0.33` | 0.327 | 62 | 1.498 | **-0.120** | +$1,409 |

> ⚠️ Le filtre "validé" dégrade le PF vs baseline (1.498 < 1.62). NQ1 est déjà très
> sélectif nativement — aucun filtre trigger n'améliore la stratégie OOS.
>
> **Conclusion NQ1 : conserver la stratégie sans filtre.**

---

### YM1 — baseline OOS PF=1.51

**Features les plus discriminantes (IS) :**
| Feature | MWU p-value | PBC | Interprétation |
|---------|-------------|-----|----------------|
| `ovn_path_eff` | **0.020** | -0.125 | Overnight chaotique → meilleur signal OPR |
| `max_excursion_atr` | **0.037** | +0.060 | Grande excursion vers OPR avant trigger → meilleur |
| `is_trend_aligned` | 0.101 | +0.091 | Trade dans le sens de la tendance → meilleur |

**Filtres validés OOS :**
| Filtre | Seuil | OOS n | OOS PF | Δ PF | OOS P&L |
|--------|-------|-------|--------|------|---------|
| `ovn_path_eff < 0.034` | 0.034 | 18 | **2.875** | +1.369 | +$1,200 |
| `max_excursion_atr > 0.17` | 0.166 | 65 | **2.039** | +0.533 | +$2,761 |
| `time_since_opr_mins < 30` | 30 min | 31 | 1.387 | -0.119 | +$618 |
| `is_trend_aligned > 0` | 1 | 58 | 1.262 | -0.244 | +$781 |

**Meilleur filtre YM1 : `max_excursion_atr > 0.17`**
- Conserve 65/155 fills OOS (42%), PF passe de 1.51 → 2.04 (+0.53)
- Interprétation : exiger que le prix soit déjà "en chemin" vers l'OPR avant la bougie trigger.
  Une excursion > 16.6% de l'ATR journalier depuis 9h30 NY indique un pullback ordonné
  plutôt qu'un test immédiat et chaotique.

**Alternative YM1 : `ovn_path_eff < 0.034`**
- PF très élevé (2.875) mais n=18 OOS seulement — à surveiller sur plus de data.
- Interprétation : overnight très chaotique (efficacité directionnelle ≈ 0) →
  le marché n'a pas de biais, l'OPR devient le pivot de référence de la journée.

---

## Synthèse inter-actifs

### Feature `max_excursion_atr` — signal robuste

Présente sur MES1 (OOS PF=1.365, n=46) et YM1 (OOS PF=2.039, n=65) avec le même sens.

**Définition :** max excursion du prix dans le sens du trigger depuis la bougie OPR (incluse)
jusqu'au trigger (inclu), normalisée par ATR journalier.

Exemple pour un LONG : si `opr_high = 5200`, `trigger_ts = 10h15 NY`, et `max(high)` des
bougies entre 9h30 et 10h15 = 5204, alors `max_excursion_atr = (5204 - 5200) / atr_daily`.

**Pourquoi ça marche :** un trigger sans excursion préalable (le prix n'a pas encore
approché l'OPR) indique que le marché "tombe directement" sur la zone — setup plus risqué.
Une excursion progressive indique un "voyage" en douceur vers le niveau.

### Feature `trigger_vol_zscore` (MES1 uniquement)

Rejeter les triggers avec volume anormalement élevé vs la session en cours. Cohérent avec
la théorie : un spike volume = catalyseur externe (nouvelles, macro), pas un retour propre
vers une zone technique.

### NQ1 : ne pas filtrer

NQ1 baseline OOS PF=1.62 est déjà le meilleur du portefeuille. Appliquer un filtre ne fait
que réduire le nombre de trades sans amélioration cohérente.

---

## Recommandations pour opr-v4

### Filtre à implémenter en priorité

```python
# Dans core/opr.py, juste après _check_trigger() — avant _make_signal()

# YM1 : max_excursion_atr > 0.17 (seuil OOS-validé)
# MES1 : trigger_vol_zscore < -0.45 (seuil OOS-validé, à re-tester sur données futures)
# NQ1 : aucun filtre
```

**Seuils OOS-validés à paramétrer dans `config.py` :**

| Param | MES1 | NQ1 | YM1 |
|-------|------|-----|-----|
| `OPR_MIN_EXCURSION_ATR` | 0.22 (PF +0.03) | None | **0.17 (PF +0.53)** |
| `OPR_MAX_VOL_ZSCORE` | **-0.45 (PF +0.46)** | None | None |

### Priorité d'implémentation

1. **YM1 `max_excursion_atr > 0.17`** — impact le plus fort (+0.53 PF OOS), n=65 robuste.
2. **MES1 `trigger_vol_zscore < -0.45`** — fort impact (+0.46 PF OOS), mais n=41 OOS à surveiller.
3. **YM1 `ovn_path_eff < 0.034`** — PF extraordinaire mais n=18 OOS, risque d'overfitting. Attendre plus de données ou combiner avec le filtre excursion.

### Précaution

Ces filtres ont été calibrés sur IS et validés sur OOS unique. Avant de merger dans
`core/opr.py`, il faudra :
1. Bumper `OPR_STRATEGY_VERSION = "opr-v4"` dans `config.py`
2. Re-calibrer `OPR_SL_ATR_MULT` / `OPR_TP_ATR_MULT` via `optimize_opr.py` avec les filtres
   actifs (la population de trades filtrée peut avoir un RR optimal différent)
3. Valider visuellement les charts d'analyse journaliers sur les jours OOS

---

## Fichiers générés

```
analyse/
├── data/
│   ├── opr_triggers_MES1.csv      (534 triggers, 24 features + résultat)
│   ├── opr_triggers_NQ1.csv       (608 triggers)
│   ├── opr_triggers_YM1.csv       (613 triggers)
│   ├── feature_ranking_MES1.csv   (features triées par p-value IS)
│   ├── feature_ranking_NQ1.csv
│   ├── feature_ranking_YM1.csv
│   ├── filter_results_MES1.csv    (walk-forward test tous filtres)
│   ├── filter_results_NQ1.csv
│   └── filter_results_YM1.csv
└── charts/
    ├── univariate/               (66 charts : 22 features × 3 tickers)
    └── filters/                  (24 charts : filtres IS/OOS comparatifs)
```
