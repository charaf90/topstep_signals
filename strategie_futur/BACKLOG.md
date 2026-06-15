# Backlog des stratégies à explorer

> **Source de vérité** pour les idées de stratégies en attente de développement.
> Mis à jour manuellement quand une idée est ajoutée, sortie en dev, ou abandonnée.
>
> Pour développer une idée → la passer en `@athena` ou `/new-strategy` puis mettre à jour la colonne **Statut**.

---

## Légende

- **Priorité** : P1 (à pousser dans les prochaines semaines) / P2 (à creuser après P1) / P3 (idée intéressante, pas urgente)
- **Statut** : `Idée` (brut) / `À formaliser` (règles à préciser avant @athena) / `Fiche prête` (fichier dédié dans `strategie_futur/`) / `En dev` (passé en @athena ou @new-strategy) / `Verdict 🟢🟡🔴` (résultat)

---

## Backlog actif

| Priorité | ID | Idée | Marché cible | Hypothèse / Edge | Source | Statut | Note |
|---|---|---|---|---|---|---|---|
| P2 | fib-v4-tight | fib-v4 région « tight » (sl≈0.45, tp≈0.9-1.3, wick≈0.4) ± gate anti-trend MGC1 | MES1, MGC1 (NQ1 fragile) | P&L recousu ×2-4 vs prod à PF comparable (multifold 5/5 MES1) ; MAIS pic isolé + sl au bord d'espace → re-valider sur grille grossière, borne sl < 0.4 | cycle-recalib-2026Q2 (🟡 2026-06-12) | Idée | Deep-lane complète exigée (plateau honnête, @quant/@auditor, live-eq du gate = annulation pending au close M15) ; budget cumulé fib-v4 ≈ 1175 tests à déclarer |
| P2 | zones-v1-retest | Zones d'intérêt multi-échelle M15 + confluence déclin volume — re-test mécanique params figés sur OOS frais | NQ1, MES1, YM1 (MGC1 faible) | OOS pool PF 1.50/n=86/BS 96 % mais DSR 18.5 % @175 tests (OOS court) ; multifold NQ1+YM1 5/5 — il faut juste plus d'OOS, 0 nouveau test | idée utilisateur (Verdict 🟡 2026-06-13, audité) | Verdict 🟡 | Re-test au rituel trimestriel (params gelés config ZONES_*) ; @quant si n_oos≥100 ; sizing $100-130 si jamais live ; M5/M1 🔴 fermés (ne pas re-tuner) |
| **P1** | bos-fvg-fill-bias | Quantifier le biais « annulation avant fill » du backtest bos_fvg (PROD) — re-run fill-prioritaire sur copie brouillon | NQ1, MES1 (live) | **MAGNITUDE MESURÉE 2026-06-13** (dev sfp-fvg) : A/B sur clone honnête, code identique, ordre seul varié → legacy (=prod) OOS PF **2.51** (≈ publié 2.26-2.93) vs honnête **1.14** (NQ1 0.82 négatif ; MES1 1.49 mais IS breakeven=régime) → edge bos-fvg **largement artefactuel** | audit zones-v1 + dev sfp-fvg (confirmé) | **A/B FAIT — décision LIVE requise** | ⚠️ bos-fvg-v2 **EN LIVE depuis 2026-06-09** (net **−$206** / 8 trades). ✅ A/B sur copie LITTÉRALE du code prod (`bos_fvg_ab.py`, 2026-06-13) : legacy OOS PF **2.58-2.61** (=publié) → honnête **1.17-1.22** ; **NQ1 honnête 0.86-1.06 (🔴), MES1 1.39-1.58 mais IS breakeven (régime)** → bos-fvg n'aurait pas passé 🟢 honnêtement. → décision utilisateur : **pause NQ1 live** / re-validation honnête MES1 (pipeline @forge) / fix backtest. Drivers : `brouillon/scripts/run_bos_ab.py` |
| **P1** | fib-v4-wick-revalidation | Re-valider fib-v4 honnêtement : le filtre wick-at-fill (edge dominant) lit la mèche COMPLÈTE de la bougie de fill = look-ahead, NON remédié (fib-fine-v2 l'a remplacé par un filtre causal i-1, fib-v4 non) | MES1, NQ1, MGC1 (live) | Audit 2026-06-13 : wick PROD vs OFF → MES1 OOS 7.65→1.87, MGC1 1.95→**1.00**, NQ1 robuste 2.48→2.22. **Live +$658/17 (POSITIF)** → PF surévalué (borne haute), PAS un raté type bos-fvg ; MGC1 honnête breakeven | audit prod (revérif utilisateur) | **Deep-lane 🟢 — @forge PARKÉ (rituel trimestriel)** | **Causal FAIT 2026-06-13** (filtre atr-exp i-1 façon fib-fine, sélection IS-only) : MES1 OOS **2.42** / NQ1 **3.30** (🟢 robustes, IS<OOS) / MGC1 **1.13** (🟡-🔴, le 1.95 prod = look-ahead). → le LA inflait mais n'inventait pas l'edge. Suite : deep-lane complète (robustesse/bootstrap/@auditor) sur le filtre causal → @forge pour remplacer Invalidation #2 dans `core/strategy_fib_v4.py` (MES1+NQ1). **MGC1 ✅ RETIRÉ du live 2026-06-13** (`FIB_V4_TICKERS=[MES1,NQ1]`, daemon+dash restartés ; broker flat). **DEEP-LANE 🟢 FAITE 2026-06-13** : OOS pool 2.22, hold-out terminal **3.19** (one-shot), DSR 95.8%, multifold NQ1 5/5 + MES1 4/5 (params stables), plateau ✓. @auditor = **🟡 conditionnel** : (1) sizing $200 → MC DD worst −$2127 > trailing (sizing dédié ~$130 + portfolio_replay combiné requis), (2) ✅ artefacts faits. Pivot-break Inval.#1 = **immatériel** (0% fillés, clos). ⚠️ **fib-v4 LIVE déjà honnête (M1Buffer)** → @forge = unification backtest/live, PAS urgent → **PARKÉ au rituel trimestriel** (faire avec la recalib WF : sizing + skip_macro MES1/NQ1 + bump version). Matériel prêt : `output/fib-v4-causal/` (summary/rapport/full/) + `brouillon/strategies/fib_v4_causal.py` + drivers `run_fib_v4_{wick,causal}.py` |

_(**ib-retest-v1 : Verdict 🟢 AUDITÉ 2026-06-13** — contribution Claude, MES1+NQ1, IB break franche 0.5×ATR + retest LIMITE ; OOS sél 2.69 / hold-out 1.95 / multifold 10/10 / DSR 95.1 % ; M5 prouve M15 conservateur. **Pré-promotion @forge** : conditions portfolio_replay combiné + sizing → voir REGISTRE 🟢 + output/ib-retest-v1/)_
_(squeeze-v1 · gmr-v1 · onr-v1 : Verdict 🔴 2026-06-13 — essais Claude (breakout vol / gold mean-reversion / overnight break-retest) sur le chemin vers ib-retest ; voir REGISTRE)_
_(sfp-fvg-v1 : Verdict 🔴 2026-06-13 — idée ICT SFP+FVG, 4e rejet de la famille fade-de-sweep ; le dev a quantifié l'artefact fill-bias de bos-fvg → voir REGISTRE + ligne P1 bos-fvg-fill-bias)_
_(opr-nq1-v1 : Verdict 🔴 2026-06-12 — voir REGISTRE)_
_(opr-nq1-trend [ex-P3] : FERMÉE 2026-06-12 — falsifiée sur OOS frais par cycle-recalib-2026Q2 : gates pro-trend jamais retenus/perdants en OOS, opr-v5.1-t1 argmax = gate OFF — voir REGISTRE)_
