# Registre des hypothèses testées et falsifiées

> Trace des hypothèses formulées (utilisateur, littérature, `BACKLOG.md`, variante d'un edge prod) et **testées rigoureusement** par le pipeline gated, qu'elles aient été validées 🟢 ou rejetées 🔴.
>
> **Objectif** : éviter de re-tester sous une autre forme une hypothèse déjà invalidée. Capitaliser la connaissance accumulée → le hit-rate futur monte.
>
> **Écriture obligatoire** : à l'étape CAPITALISATION du pipeline, **toute** stratégie testée
> (🟢/🟡/🔴) ajoute 1 entrée ici. Consultation obligatoire à l'ÉTAPE 0 (ne pas re-tester un mort).

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

_(aucune entrée — baseline propre 2026-06-09)_

## 🟡 VEILLE

_(aucune entrée)_

## 🔴 REJET

_(aucune entrée)_
