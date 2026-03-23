# RÉSUMÉ DES CHECKPOINTS — Projet Trading Intraday Topstep
## Dernière mise à jour : Mars 2026

---

## COMPARAISON RAPIDE

| Métrique | v1 | v2 (abandonné) | **v3 (actuel)** |
|---|---|---|---|
| Stratégie TP | RR2 fixe | Partial ½@1R ½@3R | **RR par actif, fixe** |
| Modification ordres | Non | Oui (SL→BE) | **Non** |
| Filtre zones | Non | Oui | **Oui** |
| Filtre pré-market | Non | Oui (MES/NQ) | **Oui (MES/NQ)** |
| **MES : WR / PF / P&L** | 40% / 1.24 / +$3,308 | 65% / 1.72 / +$2,064 | **58% / 1.92 / +$3,158** |
| **NQ : WR / PF / P&L** | 47% / 1.71 / +$8,185 | 71% / 2.36 / +$3,829 | **56% / 2.35 / +$5,761** |
| **YM : WR / PF / P&L** | 46% / 1.59 / +$7,828 | 67% / 1.55 / +$3,623 | **54% / 1.73 / +$6,634** |
| **Total 3 actifs** | +$19,321 | +$9,516 | **+$15,553** |
| **Pire Max DD** | -$1,095 | -$515 | **-$505** |

v3 = meilleur compromis : P&L 63% plus élevé que v2, PF supérieur partout, exécution simple.

---

## CHECKPOINT v3.0 — PRODUCTION

**Fichier** : `checkpoint_v3.py`
**Principe** : 1 entry, 1 SL, 1 TP. Aucune modification après placement.

### Paramètres par actif

| | MES1 | NQ1 | YM1 |
|---|---|---|---|
| $/point | $5 | $2 | $0.50 |
| SL minimum | 9 pts | 29 pts | 60 pts |
| **RR** | **1.5** | **2.0** | **1.5** |
| Qualité zone min | 70 | 60 | 40 |
| Distance zone min | 0.15% | 0.15% | 0.15% |
| Filtre PM | ON | ON | OFF |

### Paramètres communs
- Risque : $100/trade, 2 trades/jour max
- Ordres posés à 11h UTC (midi Paris)
- Session : 13h-21h UTC (fermeture auto si ni TP ni SL)
- SL buffer : 4 ticks sous/sur la zone
- Tendance : long interdit en BEAR, short interdit en BULL

### Filtres pré-market
| Actif | Condition | p-value |
|---|---|---|
| MES | prev_return < 0 OU prev_close_pos < 0.5 | 0.031 / 0.092 |
| NQ | ovn_path_eff > 0.10 | 0.040 |
| YM | aucun | — |

### Résultats vérifiés (déc 2024 → mars 2026)

| | MES1 | NQ1 | YM1 |
|---|---|---|---|
| Trades | 110 | 140 | 236 |
| WR | 58% | 56% | 54% |
| PF | 1.92 | 2.35 | 1.73 |
| P&L | +$3,158 | +$5,761 | +$6,634 |
| Max DD | -$253 | -$405 | -$505 |
| $/trade | +$28.7 | +$41.1 | +$28.1 |
| Mois négatifs | 4/15 | 1/15 | 2/16 |
