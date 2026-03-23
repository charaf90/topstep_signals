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

---

## ÉTUDE : ZONES PURES + RSI MULTI-TIMEFRAME

### Objectif
Mesurer la valeur intrinsèque des zones S/R seules (sans tendance EMA ni pré-market),
puis évaluer si le RSI multi-TF apporte une valeur ajoutée mesurable.

### Baseline de référence : v4-fix (production actuelle)

| | MES1 | NQ1 | YM1 | **Total** |
|---|---|---|---|---|
| Trades | 79 | 127 | 147 | 353 |
| WR | 33% | 37% | 36% | — |
| PF | 1.28 | 1.44 | 0.97 | — |
| P&L | +$1,318 | +$2,013 | -$218 | **+$3,113** |
| Max DD | -$1,120 | -$778 | -$1,132 | — |
| $/trade | +$16.7 | +$15.9 | -$1.5 | — |

### Résultats : Zones pures (sans tendance, sans pré-market)

| | MES1 | NQ1 | YM1 | **Total** |
|---|---|---|---|---|
| Trades | 172 | 226 | 165 | 563 |
| WR | 35% | 32% | 39% | — |
| PF | 1.38 | 1.11 | 1.08 | — |
| P&L | +$3,730 | +$956 | +$752 | **+$5,438** |
| Max DD | -$1,538 | -$1,522 | -$1,182 | — |
| $/trade | +$21.7 | +$4.2 | +$4.6 | — |

#### Analyse LONG vs SHORT (zones pures)

| | MES1 LONG | MES1 SHORT | NQ1 LONG | NQ1 SHORT | YM1 LONG | YM1 SHORT |
|---|---|---|---|---|---|---|
| n | 88 | 84 | 115 | 111 | 83 | 82 |
| WR | 42% | 27% | 27% | 38% | 42% | 35% |
| PF | 1.75 | 1.06 | 0.90 | 1.37 | 1.23 | 0.95 |
| P&L | +$3,425 | +$305 | -$508 | +$1,464 | +$1,005 | -$254 |

#### Analyse par qualité de zone

| Qualité | MES1 (n/WR/P&L) | NQ1 (n/WR/P&L) | YM1 (n/WR/P&L) |
|---|---|---|---|
| Q<40 | — | — | 1 / 0% / -$90 |
| Q40-60 | — | 34 / 29% / +$58 | 11 / 27% / -$248 |
| Q60-80 | 149 / 32% / +$2,070 | 169 / 32% / +$668 | 130 / 39% / +$644 |
| Q80+ | 23 / 52% / +$1,660 | 23 / 39% / +$230 | 23 / 43% / +$446 |

### Résultats : Zones + RSI multi-TF

Mêmes trades que zones pures, enrichis du rsi_score (0-4 TFs RSI alignés).
Le RSI n'est PAS utilisé comme filtre — tous les signaux sont conservés.

#### Segmentation par RSI score — MES1

| RSI Score | Trades | WR | PF | P&L | $/trade |
|---|---|---|---|---|---|
| 0 | 17 | 29% | 0.97 | -$38 | -$2 |
| 1 | 33 | 33% | 1.31 | +$560 | +$17 |
| 2 | 43 | 33% | 1.22 | +$585 | +$14 |
| 3 | 43 | 35% | 1.37 | +$922 | +$21 |
| **4** | **36** | **42%** | **1.93** | **+$1,700** | **+$47** |

**MES1 : progression monotone claire.** RSI=4 surperforme nettement (PF 1.93 vs 1.38 global).

#### Segmentation par RSI score — NQ1

| RSI Score | Trades | WR | PF | P&L | $/trade |
|---|---|---|---|---|---|
| 0 | 31 | 45% | 1.85 | +$838 | +$27 |
| 1 | 37 | 35% | 1.29 | +$409 | +$11 |
| 2 | 55 | 31% | 1.10 | +$218 | +$4 |
| 3 | 58 | 21% | 0.58 | -$1,109 | -$19 |
| **4** | **45** | **38%** | **1.37** | **+$600** | **+$13** |

**NQ1 : pattern non monotone.** RSI=0 performe bien, RSI=3 est le pire segment.
Pas de relation linéaire entre RSI score et performance sur NQ1.

#### Segmentation par RSI score — YM1

| RSI Score | Trades | WR | PF | P&L | $/trade |
|---|---|---|---|---|---|
| 0 | 25 | 32% | 0.82 | -$270 | -$11 |
| 1 | 32 | 41% | 1.15 | +$246 | +$8 |
| 2 | 53 | 30% | 0.72 | -$920 | -$17 |
| 3 | 31 | 48% | 1.61 | +$885 | +$29 |
| **4** | **24** | **50%** | **1.75** | **+$810** | **+$34** |

**YM1 : RSI élevé (3-4) nettement supérieur.** RSI=4 est le meilleur segment (PF 1.75, WR 50%).
Mais pattern non monotone (RSI=2 est le pire).

### Tableau comparatif final

| Métrique | v4-fix baseline | Zones pures | Zones+RSI (tout) | Zones+RSI (RSI≥3) |
|---|---|---|---|---|
| MES1 Trades | 79 | 172 | 172 | 79 |
| MES1 P&L | +$1,318 | +$3,730 | +$3,730 | +$2,622 |
| MES1 PF | 1.28 | 1.38 | 1.38 | 1.59 |
| NQ1 Trades | 127 | 226 | 226 | 103 |
| NQ1 P&L | +$2,013 | +$956 | +$956 | -$509 |
| NQ1 PF | 1.44 | 1.11 | 1.11 | 0.87 |
| YM1 Trades | 147 | 165 | 165 | 55 |
| YM1 P&L | -$218 | +$752 | +$752 | +$1,695 |
| YM1 PF | 0.97 | 1.08 | 1.08 | 1.67 |
| **Total P&L** | **+$3,113** | **+$5,438** | **+$5,438** | **+$3,808** |
| **Pire Max DD** | **-$1,132** | **-$1,538** | **-$1,538** | — |

### Conclusions

**1. Les zones seules sont-elles viables sans tendance ni PM ?**

Oui. P&L total +$5,438 vs +$3,113 (v4-fix), soit +75%. Le PF est inférieur sur NQ1 et YM1
car l'absence de filtre tendance laisse passer des trades contre-tendance perdants, mais le
volume de trades double et la somme globale est supérieure. Les zones MES1 sont intrinsèquement
fortes (PF=1.38 sans aucun filtre). Le max drawdown est cependant plus élevé (-$1,538 vs -$1,132).

**2. Le RSI apporte-t-il une valeur ajoutée mesurable ?**

**Oui pour MES1 et YM1, non pour NQ1.**

- **MES1** : progression monotone du RSI score → RSI=4 (PF=1.93, WR=42%) surperforme RSI=0
  (PF=0.97, WR=29%). Le RSI est un excellent discriminant sur MES.
- **YM1** : RSI élevé (3-4) produit PF=1.61-1.75, WR=48-50%, nettement au-dessus du global.
  Cependant le pattern n'est pas monotone (RSI=2 est le pire segment).
- **NQ1** : aucune relation exploitable. RSI=0 performe mieux que RSI=3. Le RSI n'apporte
  pas d'information prédictive fiable sur NQ1.

**3. Recommandation : activer le filtre RSI ou non ?**

- **MES1** : filtre RSI≥3 recommandé (79 trades, PF=1.59, P&L=+$2,622). Réduit le nombre
  de trades de moitié mais améliore la qualité.
- **YM1** : filtre RSI≥3 recommandé (55 trades, PF=1.67, P&L=+$1,695). Transforme un actif
  marginal en actif profitable.
- **NQ1** : NE PAS filtrer par RSI. Le RSI n'est pas un discriminant fiable sur cet actif.

**Note** : ces résultats sont in-sample sur la période complète (déc 2024 → mars 2026).
Une validation out-of-sample est nécessaire avant mise en production.
