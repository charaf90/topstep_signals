# Template — output/<strategy_id>/rapport.md

> **Writeup court survivant (~30 lignes).** Uniquement pour les 🟡/🟢 (deep lane). Les 🔴 ne
> produisent PAS de dossier — juste 1 ligne dans `REGISTRE_HYPOTHESES.md`. Détails structurés
> dans `summary.json` ; robustesse complète dans `full/robustness_<id>.{json,md}` (généré par `optimize.py`).

```markdown
# Rapport — [STRATEGY_ID] (v[N]) · [YYYY-MM-DD] · Verdict 🟢/🟡

## Concept
[1-2 phrases : signal, timing, tickers, edge — qui paie ce P&L.]
**Falsifiable :** [observation qui invalide en live]

## Métriques OOS (portfolio, net de frais — lues depuis robustness_<id>.md)
| | Valeur | Seuil 🟢 |
|---|---|---|
| Trades / WR | XXX / XX% | ≥ 50 |
| PF net | X.XX | ≥ 1.5 |
| P&L net | +$X XXX | > 0 |
| Bootstrap portfolio | XX% | ≥ 80% |
| Dégradation IS→OOS | XX% | ≤ 30% |
| MC P95 DD | -$XXX | < limite Topstep restante |

Stress régime (PF OOS) : trending=X.XX · ranging=X.XX · macro=X.XX · vol_h=X.XX · vol_b=X.XX

## Live-equivalence
applicable=[oui/non] · path=[M1Buffer/live_eq_script/n/a] · PF live-eq=[X.XX]

## Quant (si discover exécuté)
verdict=[HIGH/MEDIUM/LOW] · filtres=[<feature> <op> <seuil> (ΔPF +X.XX, p<X.XXX Bonferroni)] / rollback

## Fit portefeuille
corr daily P&L OOS vs OPR=0.XX · vs Fib=0.XX  (cible < 0.5)

## Verdict & next step
**[🟢/🟡]** — [justification 2 lignes]
Next : [promotion @forge / itération vN+1 / veille]
Limites connues : [latence réelle, profondeur carnet en stress, frais Topstep évolutifs]
```
