# Registre des hypothèses testées et falsifiées

> Trace des hypothèses formulées (utilisateur, littérature, `BACKLOG.md`, variante d'un edge prod) et **testées rigoureusement** par le pipeline gated, qu'elles aient été validées 🟢 ou rejetées 🔴.
>
> **Objectif** : éviter de re-tester sous une autre forme une hypothèse déjà invalidée. Capitaliser la connaissance accumulée → le hit-rate futur monte.
>
> **Écriture obligatoire** : à l'étape CAPITALISATION du pipeline, **toute** stratégie testée
> (🟢/🟡/🔴) ajoute 1 entrée ici. Consultation obligatoire à l'ÉTAPE 0 (ne pas re-tester un mort).
>
> _Repeuplé le 2026-06-10 depuis l'historique git (docs/strategies_abandoned.md supprimé au
> nettoyage baseline + ancien registre + commentaires config.py). Détails complets :
> `git show 10c6ec3~1:docs/strategies_abandoned.md`._

---

## Format

Chaque entrée doit contenir :
- `ID` : identifiant court (H4, ICT-OB-v1, etc.)
- `Date` : YYYY-MM-DD du test
- `Source` : utilisateur / littérature / `BACKLOG.md` / variante edge prod (OPR, Fib)
- `Verdict` : 🟢 PRODUCTION / 🟡 VEILLE / 🔴 REJET
- `Spec` : description courte de l'hypothèse
- `Métriques` : PF OOS, n_trades, bootstrap (ce qui a justifié le verdict)
- `Leçon` : ce qu'on retient, **surtout si rejet**

---

## 🟢 PRODUCTION

- **opr-v5.1** · 2026-05-18 · variante edge prod OPR · Schéma A entrée différée + filtre F2 data-driven (running excursion ≥ seuil ATR). Live YM1 (NQ1 promu puis pausé 2026-06-01, edge non robuste ; MES1 reste opr-v4 pass-through, edge ML F2 p=0.23 non significatif). Métriques YM1 : OOS PF 4.5+, BS 100 %. Multifold 2026-06-10 : 5/5 folds positifs, params stables, recousu PF 3.43, hold-out PF 3.62. **Leçon** : un filtre data-driven validé per-ticker (p<0.0001) peut être significatif sur certains tickers seulement — router per-ticker plutôt que tout-ou-rien.
- **fib-v4** · 2026-05-19 · variante fib-v3 · Retracement Fibonacci + filtre wick-excess data-driven + invalidation pivot break intra-bar (M1Buffer). Univers MES1+NQ1+MGC1 (3 cellules 🟢) ; YM1 exclu (n_oos<20 → 🟡), MCL1 rejet structurel. **Leçon** : l'edge dominant était sur la **qualité du fill** (wick excess, p<0.001) — à chercher systématiquement sur tout setup limite. ⚠️ Verdict initial basé sur un PF naïf (feature lue sur la bougie de fill) → live-equivalence désormais BLOQUANTE.
- **fib-fine-v2** · 2026-06-02 · variante fib-v4 sur TF fin · Fibonacci natif M5, filtre causal d'expansion de volatilité (atr_ratio_sl ≥ 1.0 lu sur barre i-1, remplace le filtre wick look-ahead de v1). NQ1+MES1, sizing $130 (calibré MC P95 DD). PF OOS 1.63, BS 99.6 %, Bonferroni OK, PSR≈100 %. Replay portefeuille : corr daily 0.03 vs OPR+fib-v4. **Leçon** : la v1 avait un look-ahead (wick de la bougie de fill) détecté à l'audit — le remplacement par un filtre causal a coûté du PF mais rendu la stratégie honnête.
- **bos-fvg-v2** · 2026-06-09 · idée utilisateur (ICT) · Break of Structure + FVG natif M5, entrée LIMIT au Consequent Encroachment en discount, time-stop 9 barres M5. NQ1+MES1, $150. OOS PF 2.26→2.93 avec time-stop, MC DD P95 -22 %. **Leçon** : malgré le red-flag « ICT canonique » (cf. smc-v1 🔴), une spec précise/falsifiable avec fill honnête peut passer — c'est la précision mécanique qui fait la différence, pas le label.

## 🟡 VEILLE

- **pdh-pdl-mes1-v1** · 2026-05-27 · variante du rejet pdh-pdl-retest-v1 · PDH/PDL breaker MES1 SEUL (macro filter actif) : OOS PF 1.75, n=60, BS 99.7 %, DD -$626. **Re-évaluer août 2026 (n_oos ~90)**. Code : `git show 10c6ec3~1:strategies/pdh_pdl_retest.py` (--ticker MES1).
- **ib-retest** · 2026-06 · littérature (Initial Balance) · Cassure FRANCHE ≥0.5×ATR de l'IB puis retest-continuation sur la borne cassée. 🟡 survivant — l'edge est la **franchise de la cassure**, pas le retest seul. A inspiré phc-v1 (même mécanique, range de midi).
- **opr-v5 (portfolio)** · 2026-05 · variante OPR · Filtres F1/F2 post-fill : portfolio OOS PF 2.17, BS 66.6 % → 🟡. **Remplacé par opr-v5.1** (schéma A live-faisable) — ne pas re-tester tel quel : le filtre post-fill n'est pas exécutable en live (on ne dé-fille pas une position).

## 🔴 REJET

- **kijun-pb-v1** · 2026-05-15 · @chartist (H1+H1bis) · Kijun pullback + StochRSI bidirectionnel NQ1. PF OOS 1.49 sur n=26 MAIS bootstrap 74.7 % vs Bonferroni 99.79 % (24 tests) → FAIL ; IS breakeven (PF 1.00) ; P&L concentré sur 2 mois. **Leçons** : PF OOS ≠ edge si bootstrap fail Bonferroni · edge OOS sans edge IS = régime favorable, pas edge · concentration mensuelle = fragilité · Ichimoku canonique = arbitré (2/2 échecs).
- **opr-h4 / H4 (filtre cloud Ichimoku sur OPR)** · 2026-05-15 · @chartist · OPR seulement si prix hors cloud : PF 1.53→1.46 (−4.6 %), sweep monotone descendant. **Leçons** : voir un pattern ≠ edge non exploité (déjà capturé par les filtres existants) · sweep monotone = paramètre nuisible · ne pas re-tester en variantes (cloud thickness, alignment…).
- **smc-v1 (Smart Money Concepts LuxAlgo)** · 2026-05-17 · littérature · Multi-zones OB/FVG/Breaker M15, entrée à la mitigation. PF OOS 0.95. **Leçon** : empiler des zones dilue le signal au lieu de le concentrer — valider chaque setup séparément (PF ≥ 1.5 OOS) avant d'en superposer.
- **fib-v3** · 2026-05-19 (remplacée) · prédécesseur fib-v4 · PF OOS 1.56, mais ordre limite restait actif après cassure du pivot d'impulsion (thèse morte → trades subis). **Leçon** : toute stratégie de retracement DOIT invalider le pending si la zone source casse.
- **vpc-v4 (Volume Profile Confluence)** · 2026-05-19 · littérature · POC/VAH/VAL reconstruits depuis M15, 3 setups empilés. PF OOS < 1.0. **Leçons** : VP reconstruit depuis M15 trop approximatif (il faut du tick) · 3 setups sans signal régime = comportement opportuniste.
- **arf-v4 (Asian Range Failure)** · 2026-05 · littérature · Fausse cassure du range asiatique pendant Londres. PF 0.55 sur MCL1, 1 signal/49j sur MGC1. **Leçon** : Gold/Crude sont continuation à la cassure (news-driven), pas mean-reversion — vérifier la dynamique du marché cible avant de présumer le « failure ».
- **pivot-rev-v1 (+ étude pivot ML)** · 2026-05-26 · ML interne · Détecteur pivots RF (lift ×8.8 robuste) traduit en stratégie reversal Gold M5 : PF IS 0.97, 70 % des signaux expirent sans fill, taux TP réel 7 %. **Leçons** : edge de CLASSIFICATION ML ≠ edge de TRADING (la traduction signal→exécution coûte tout l'edge) · confirmation par cassure = sélection adverse sur les reversals. Abandon définitif sans nouvelle donnée causale (orderflow/tick).
- **eia-inv-v1 (EIA Inventory Retest MCL1)** · 2026-05-26 · event-driven · Fade du spike post-release EIA. IS PF 0.98 (arrêt précoce). **Leçons** : ⚠️ **biais transversal découvert** — sur tout backtest M15 avec event intra-bar, le fill doit être autorisé strictement APRÈS la fermeture de la barre événement (premier run : PF artificiel 31) · event-driven hebdo (~3 trades/mois) incompatible avec les seuils statistiques du pipeline (n=100 fills ≈ 5 ans).
- **gap-fill-v1** · 2026-05-26 · littérature · Fade du gap d'ouverture NY vers le PSC. IS PF 0.54. **Leçon** : entrer POST-confirmation sur un gap = erreur d'architecture (gap déjà partiellement comblé → RR médian 0.5) ; un TP à 50 % du gap serait plus réaliste que le PSC complet.
- **pdh-pdl-retest-v1 (portfolio 4 tickers)** · 2026-05-27 · ICT Breaker · MES1 PF 1.61/BS 99.7 % MAIS YM1 0.74 et NQ1 1.05/BS<1 % → portfolio BS 24.8 %. **Leçons** : la dilution multi-ticker peut MASQUER un edge réel (tester single-ticker d'abord) · 3/4 params aux bords de grille = sur-ajustement (DSR 35.9 %).
- **vwap-pb v1.1 (filtre SKIP_VOL_H)** · 2026-05-26 (rollback) · variante vwap-pb · Seuil ATR p80 figé IS : drift vol +148 % sur MGC1 (Gold ATH) → filtre élimine 100 % des trades OOS MGC1, P&L portfolio -69 %. **Leçon** : un seuil quantile calibré IS-only n'est PAS robuste à un drift de régime structurel — préférer des seuils relatifs/glissants.
- **liq-hook-v1, exp-hunt-v1, asian-sweep-v1 (famille fade-de-sweep)** · 2026-06 · littérature · Fades de sweep de liquidité (open drive, expansion hunt, asian sweep) : 3/3 rejetés. **Leçon** : « les sweeps sur indices US continuent plus souvent qu'ils ne reversent » — red-flag ÉTAPE 0 pour toute nouvelle idée fade-de-sweep.
- **xmkt-rv-v1 (relative value YM/NQ)** · 2026-06 · idée utilisateur · Divergence inter-marché : OOS PF 0.77. **Leçon** : la RV inter-indices micros est arbitrée — doublon à bloquer si reproposé (cf. ymnq_div).
- **spike-1m / opr_v10** · 2026-06 · variante OPR M1 · Spike M1 : AUC 0.50 (aucun pouvoir prédictif) + frictions M1 dominantes. **Leçon** : en M1 les frictions (slippage+commission par trade court) absorbent les micro-edges.
- **fib-argrel-v1** · 2026-06 · variante fib · Pivots argrelextrema : niveau 61.8 seul = BS 0.0 %, DD -$4 496. Rejeté.

---

## Leçons transversales (à consulter avant tout nouveau test)

1. **Patterns Ichimoku canoniques = arbitrés** (2/2 échecs : H4, kijun-pb).
2. **n < 30 OOS** → exiger PSR fiable + bootstrap > Bonferroni + P&L réparti sur ≥ 4-5 mois, sinon rejet par sécurité.
3. **IS breakeven + OOS positif = artefact de régime**, pas un edge structurel.
4. **Multi-ticker après échec = data dredging** ; à l'inverse, la dilution multi-ticker peut masquer un edge single-ticker réel.
5. **Fade-de-sweep sur indices US : 3/3 rejetés** — les sweeps continuent plus qu'ils ne reversent.
6. **Event intra-bar M15** : fill autorisé strictement APRÈS la fermeture de la barre événement.
7. **Edge ML de classification ≠ edge de trading** — toujours backtester le PnL net.
8. **Seuils quantiles figés IS** cassent sur drift de régime (vwap-pb v1.1, -69 %).
9. **« Pourquoi cet edge existerait-il encore ? »** — si la réponse honnête est « pas clair », rétrograder.
10. **L'edge est souvent dans la qualité du fill** (wick excess fib-v4, cassure franche ib-retest) — à tester systématiquement sur les setups limite.
