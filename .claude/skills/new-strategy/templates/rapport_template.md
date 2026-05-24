# Template — output/<strategy_id>/rapport.md

> **Format compact 1 page (~80 lignes max).** Les détails complets vont dans
> `output/<strategy_id>/summary.json` (structuré) et `output/<strategy_id>/full/`
> (charts, robustness.json, audit_visuel.md, trades CSV). Ce rapport est la
> page d'archive humaine — lisible en 1 minute.

```markdown
# Rapport — [STRATEGY_ID] (v[N])

**Date :** [YYYY-MM-DD] · **Itérations :** [N]/5 · **Verdict :** 🟢/🟡/🔴

## Concept
[1-2 phrases : signal, timing, tickers, edge théorique.]
**Hypothèse falsifiable :** [1 phrase — observation qui invalide en live]

## Métriques OOS (portfolio, net de frais)

| Métrique | Valeur | Seuil 🟢 |
|---|---|---|
| Trades / WR | XXX / XX% | ≥ 50 / – |
| PF net | X.XX | ≥ 1.5 |
| P&L net | +$X XXX | > 0 |
| Bootstrap portfolio (block, 1000 iter) | XX% | ≥ 80% |
| Dégradation IS→OOS PF | XX% | ≤ 30% |
| DD max OOS | -$XXX | < limite Topstep restante |
| PSR(0) | XX% | ≥ 95% |

## Stress par régime (PF OOS)
trending=X.XX · ranging=X.XX · macro=X.XX · vol_h=X.XX · vol_b=X.XX
**MC P95 DD :** -$XXX (limite Topstep restante : -$XXX)

## Filtres data-driven (@quant — si PHASE 3.5)
[Si quant_used=true dans summary.json, lister top 3 filtres appliqués :]
1. <feature> <op> <seuil> — impact PF OOS +X.XX (p<X.XXX Bonferroni)
2. ...

## Complémentarité portefeuille (corrélation daily P&L OOS)
vs OPR opr-v4 : 0.XX · vs Fib fib-v3 : 0.XX · vs VPC vpc-v4 : 0.XX

## Verdict & next step

**[🟢 / 🟡 / 🔴]** — [Justification 2-3 lignes : ce qui convainc / ce qui manque.]

**Conditions d'upgrade/downgrade**
- Pour passer 🟡 → 🟢 : [conditions mesurables]
- Pour rétrograder 🟢 → 🟡 : [conditions kill-switch]

**Next step** : [promotion via @forge / itération vN+1 / rejet documenté]

## Itérations (résumé)
| v | Modif clé | PF OOS | Bootstrap | Verdict |
|---|---|---|---|---|
| v1 | baseline | X.XX | XX% | 🔴/🟡/🟢 |
| v2 | +<filtre quant ou param> | X.XX | XX% | 🔴/🟡/🟢 |
| vN | ... | X.XX | XX% | 🔴/🟡/🟢 |

## Workflow promotion (si 🟢, à confirmer par l'utilisateur étape par étape)
1. [ ] @forge crée `core/<strategy_id>.py` (logique live)
2. [ ] @forge update `broker/live_runner.py` + `core/signal_selector.py`
3. [ ] Test simulation `PROJECTX_LIVE_MODE = False` (5 jours)
4. [ ] Activation progressive : 1 contrat 1 semaine → sizing nominal

## Artefacts
- `summary.json` (verdict + métriques structurées, lu par auditor)
- `full/robustness.json` (Bootstrap, Bonferroni, PSR, MC complet)
- `full/audit_visuel.md` (si PHASE 6.5 exécutée — sinon skip 🟢 clair)
- `full/charts/` (10 jours + portfolio equity/DD/heatmap/hourly/corr)
- `quant_report.md` + `quant_patch.py` (si PHASE 3.5 exécutée)

## Limites connues
[2-3 lignes : ce que le backtest ne capture pas — latence réelle, profondeur
carnet en stress, changement structurel post-OOS, frais Topstep évolutifs.]
```
