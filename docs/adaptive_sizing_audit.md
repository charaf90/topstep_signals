# Audit `core/adaptive_sizing.py` — PHASE 3.3 ROADMAP_SOLO

> **Statut** : audit en lecture seule, le module **reste désactivé** en prod.
> La réactivation est conditionnée à (a) un fix de la formule asymétrique
> identifié comme cause de désactivation 2026-05-21, et (b) une période
> minimum de 14 jours en **shadow mode** (cf. PHASE 3.2 framework) avec
> critère Go : PF shadow ≥ PF prod ET DD shadow ≤ DD prod.

## 1. Ce que fait le module

Sizing dynamique du risque par trade pour le challenge Topstep mensuel.
Trois fonctions pures (sans I/O) :

| Fonction | Rôle |
|---|---|
| `trading_days_until(today, reset_day)` | Nb jours ouvrés jusqu'au prochain reset mensuel (clamp 1+). |
| `compute_factors(rm_status, signal, today)` | Calcule tous les facteurs intermédiaires (slacks, edges, lockin, day_progress). |
| `adaptive_risk_usd(rm_status, signal, today)` | Formule finale, clampée. Renvoie `(risk, factors)`. |

**Formule** :

```
target_risk  = distance_target / (days_left^γ × n_per_day × e_strat)
raw          = min(target_risk × boost, dd_cap, daily_cap) × lockin × day_progress
risk         = clamp(raw, CHALLENGE_RISK_MIN_USD, CHALLENGE_RISK_MAX_USD)
```

**Paramètres** (config.py) :

- `CHALLENGE_RISK_MIN_USD` / `CHALLENGE_RISK_MAX_USD` : bornes [min, max]
- `CHALLENGE_STRAT_EDGE`, `CHALLENGE_STRAT_BOOST` : edge OOS par stratégie (OPR/Fib)
- `CHALLENGE_DD_GUARD_BUFFER` : marge de sécurité sur slack Topstep
- `CHALLENGE_TIME_PRESSURE_GAMMA` : courbure de la pression temporelle
- `CHALLENGE_LOCKIN_START_USD` : seuil au-delà duquel le risque diminue (lock-in)
- `CHALLENGE_DAY_PROFIT_SOFT_CAP_USD` / `HARD_CAP_USD` : damping intra-jour
  pour anticiper la règle de cohérence 50% Topstep
- `CHALLENGE_RESET_DAY` : jour du reset mensuel (typiquement 2)

## 2. État actuel — DÉSACTIVÉ

**Flag** : `CHALLENGE_ADAPTIVE_SIZING_ENABLED = False` dans `config.py` (depuis 2026-05-21).

**Comportement effectif** : le sizing tombe sur `RISK_PER_TRADE_USD = 200`
(fixe), bypassant `adaptive_risk_usd()`. Le module reste importable et
testable mais n'est pas appelé par le pipeline live.

## 3. Pourquoi désactivé (incident 2026-05-21)

Sources de la décision : `output/risk_comparison/`, `output/period_stats/`,
`output/losses_distribution/`, `output/sl_streaks/`.

**Cause racine** : formule **symétrique** alors que la situation Topstep
est **asymétrique** :

- Quand `cum_pnl < 0` proche du reset : la formule pousse le risque à la
  hausse pour "rattraper" le target — mais l'asymétrie Topstep est en
  réalité capée à la baisse (trailing DD $2k, daily $950 hard), et il n'y
  a **aucune urgence** à finir le mois en territoire positif (le compte
  reste actif tant qu'on n'a pas breaché).
- Quand `cum_pnl > 0` proche du target : la formule réduit correctement
  via `lockin`, mais le composé `target_risk` peut redescendre trop vite
  et étouffer un edge encore valide.

Bilan : à $200/trade fixe portfolio (hors OPR/MES1 v4), historique 21
mois → **100% mois positifs, 95% mois ≥ +$3000, 0 mois ≤ −$2000**.
Worst day = −$1,033 (1 jour sur 360). Pas besoin de sizing dynamique
pour ce niveau de performance.

## 4. Tests existants

`tests/test_adaptive_sizing.py` — 17 tests :

- ✅ `TestTradingDaysUntil` (5 tests) — passe, formule correcte
- ✅ `TestBornes` (2 tests) — clamp [MIN, MAX] respecté
- ✅ `TestMonotonie` (3 tests) — plus de temps → moins de risque,
  plus de progrès → moins de risque, boost Fib > OPR
- ✅ `TestCasDegeneres` (3 tests) — peak=3000/cum=1500, DD breached,
  hail-mary cum=−1200 / days=2
- ⚠️ `TestMonthlyReset` (3 tests) marqués `xfail` :
  - `test_reset_triggers_on_day_2` : attend reset au jour 2, n'a plus lieu
  - `test_no_reset_before_day_2` : idem
  - `test_reset_idempotent` : passe en `xpass` (cas bénin)
  - Cause : le reset mensuel a été retiré du `PortfolioRiskManager`
    avec la désactivation challenge mode. Le test n'a pas été mis à
    jour. À refixer ou supprimer lors de la réactivation.

## 5. Conditions de réactivation

Toute réactivation doit cocher les 5 cases suivantes :

1. **Fix asymétrie formule** dans `core/adaptive_sizing.py` :
   - Recovery activée uniquement si `cum_pnl < -SAFETY_BUFFER` (et non
     pas seulement `cum_pnl < target`).
   - Penaliser plus fortement les pertes proches du DLL (`slack_daily`
     conditionne le multiplicateur).
   - Documenter la nouvelle formule + tests Hypothesis dédiés.
2. **Audit + correction `TestMonthlyReset`** : soit le reset mensuel
   est ré-implémenté dans `PortfolioRiskManager`, soit les 3 tests
   sont supprimés.
3. **Shadow mode 14 jours** ininterrompus avec
   `FEATURE_VOL_TARGETING_ENABLED = True` côté shadow uniquement.
   Validation via `python tools/shadow_vs_live.py` quotidien.
4. **Critère Go** : PF shadow ≥ PF prod **ET** DD shadow ≤ DD prod
   sur la période 14j, sans divergence systématique sur les signaux
   communs (pas plus de 1 jour avec ≥3 divergences entry/sl/tp).
5. **Confirmation utilisateur explicite** pour basculer
   `CHALLENGE_ADAPTIVE_SIZING_ENABLED = True` en prod, dans une
   fenêtre de release samedi matin (PHASE 4).

## 6. Workflow de réactivation (post-fix)

```bash
# 1. Branche dédiée
git checkout -b feat/adaptive-sizing-v2

# 2. Fix formule + tests
# (édit core/adaptive_sizing.py + nouveaux tests Hypothesis)

# 3. Activation en SHADOW UNIQUEMENT
# Idée : ajouter un flag override côté shadow_runner pour forcer
#   FEATURE_VOL_TARGETING_ENABLED=True à la construction du runner,
#   indépendamment du config global qui reste False.

# 4. Lancement shadow daemon
./tools/launch_shadow.sh start
# Attendre 14 jours, surveiller :
python tools/shadow_vs_live.py --date $(date +%Y-%m-%d)

# 5. Si critère Go vert → PHASE 4 release coordonnée
```

## 7. Risques à surveiller pendant le shadow

- **Divergence systématique du n_ct** : si la nouvelle formule produit
  des n_ct très différents du fixe $200, le shadow vs live diff sera
  bruité. C'est attendu — l'important est que la **direction** des
  trades (long/short) et le **timing** (entry, sl, tp) restent identiques.
- **Lockin trop agressif** : si `cum_pnl` proche de $1450 (profit
  target Combine), le sizing peut tomber à `CHALLENGE_RISK_MIN_USD` —
  vérifier que ce minimum permet au moins 1 contrat sur chaque ticker.
- **Day-progress damping** : si le shadow gagne $1400 en milieu de
  séance, le damping descend le risque à 0.25× — observer si cela
  manque des opportunités late-session.

## 8. Liens

- Mémoire `project_risk_policy_2026-05-21.md` (décision désactivation)
- Mémoire `project_challenge_mode.md` (référence vers cette mémoire)
- `core/risk_portfolio.py` : check `tp_gain_usd` pour règle 50%
- `tests/test_adaptive_sizing.py` : 17 tests dont 3 xfail à refixer

---

*Audit ROADMAP_SOLO PHASE 3.3 — 2026-05-25. Lecture seule, aucune
modification du module. Le fichier `core/adaptive_sizing.py` reste intact.*
