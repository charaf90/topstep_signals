# Équipe d'agents — topstep_signals

Architecture sobre, performante et rigoureuse pour développer et exploiter des stratégies de
trading algorithmique sur le challenge Topstep 50K.

> **Objectif système** : maximiser le nombre de stratégies rentables, robustes et non-corrélées
> trouvées **par token dépensé**. Throughput ↑ · hit-rate ↑ · rigueur de validation INCHANGÉE.

- **Quotidien** : la FAST LANE (test rapide d'une idée) est faite **inline** par l'orchestrateur
  (zéro cold-start subagent). Les specialists ne servent qu'en DEEP LANE (validation lourde).
- **Exploration intensive** : agent teams (expérimental, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)
  pour parallélisme et débat contradictoire.

## Les 3 piliers

1. **SOBRIÉTÉ** — pipeline gated, dev inline, **zéro recalcul redondant** (`optimize.py` calcule
   déjà bootstrap/MC/PSR/Bonferroni/stress → `output/robustness_<id>.{json,md}` ; on lit, on ne refait pas).
2. **PERFORMANCE (hit-rate)** — sélection d'idée avant codage (backlog + red-flags), variantes
   d'edges prouvés, breadth via grille walk-forward, `@quant` pour repêcher les 🟡, fit portefeuille,
   capitalisation systématique.
3. **RIGUEUR** — ce qu'on allège = redondance + cérémonie, **jamais la validation** : live-equivalence
   BLOCANT, `@auditor`, seuils 🟢, Bonferroni, walk-forward fixe.

## Table des agents (6)

| Agent | Statut | Rôle | Invocation | Protection |
|---|---|---|---|---|
| **Orchestrateur** | cœur | FAST LANE inline (cadrage edge, scaffold, backtest, optimize, verdict) + routage + capitalisation. | Session principale | Règles `ask` sur `core/**` et `broker/**` |
| **@new-strategy** | dev (subagent) | Même pipeline gated, mais **isole le contexte** (dev long / parallèle). | `@new-strategy` ou `/new-strategy "..."` | Écriture `strategies/`, `config.py`, `output/` |
| **@auditor** | deep lane | Audit fidélité/look-ahead/frictions/live-eq/cohérence verdict. Peut rétrograder. | `@auditor` | Lecture seule stricte |
| **@quant** | on-demand | Mode `discover` : repêchage data-driven d'un 🟡 (RF/LogReg + Bonferroni). Moteur de hit-rate. | `@quant` (discover) | Écriture `output/<id>/` uniquement |
| **@athena** | on-demand | Conseil **one-shot** : priorisation backlog + fit portefeuille + go/no-go. | `@athena` | Lecture seule |
| **@argus** | live | Surveillance daemon (tmux, state, logs, Telegram). | `@argus` | Lecture seule absolue |
| **@forge** | promotion | Promotion prod : crée `core/<id>.py`, modifie `broker/live_runner.py`. | `@forge` + 🟢 confirmé + confirmation par fichier | Écriture `core/`/`broker/`, chaque écriture confirmée |

> Agents **supprimés** lors de la refonte : `researcher` (cadrage absorbé inline) et `chartist`
> (audit visuel = coup d'œil manuel `python -m core.explore_chart`, à la demande).

## Workflows

### 1. Développer une stratégie — pipeline gated (recommandé)

```
ÉTAPE 0 · SÉLECTION (inline)
  BACKLOG.md (P1>P2>P3) + REGISTRE_HYPOTHESES (ne pas re-tester un mort)
  + RED-FLAGS → rejet à coût zéro (indicateur arbitré, event-driven basse fréq, doublon prod, RR écrasé)
        ↓
FAST LANE (inline session principale — défaut)
  1. cadrer l'edge (qui paie ? falsifiable ?)   2. scaffold + config.py (variantes → PARAM_GRID)
  3. backtest.py + optimize.py (robustesse AUTO) 4. lire verdict + robustness_<id>.md
  5. GATE :  🔴 → STOP + 1 ligne REGISTRE + mémoire        🟡/🟢 → DEEP LANE
        ↓
DEEP LANE (survivants seulement)
  6. live-equivalence (BLOCANT si feature bougie de fill)
  7. [@quant discover si 🟡 + n_oos≥100]  → tenter 🟡→🟢 (rollback si Bonferroni fail)
  8. summary.json + rapport.md court  → 9. @auditor  → 10. @forge si 🟢 confirmé
        ↓
CAPITALISATION (tous cas)
  verdict → REGISTRE_HYPOTHESES.md + statut BACKLOG.md + mémoire si leçon transversale
```

Source de vérité du pipeline : `.claude/skills/new-strategy/SKILL.md`. Les subagents ne peuvent
pas spawn d'autres subagents — l'orchestrateur chaîne les invocations.

### 2. Conseil stratégique avant dev

```
Utilisateur : « ATHENA, quelle idée prioriser ? » / « ce concept vaut le coup ? »
```
`@athena` (one-shot) : priorise une idée du backlog, applique les red-flags, juge le fit portefeuille
(corr P&L daily < 0.5), rend un GO/NO-GO argumenté. Ne pilote pas le pipeline.

### 3. Repêcher un 🟡 (hit-rate)

```
Utilisateur (ou orchestrateur, baseline 🟡 + n_oos≥100) : « QUANT, discover sur <id> »
```
`@quant` produit `output/<id>/quant_patch.py`. Appliqué en vN+1 si verdict ≥ MEDIUM ; rollback si LOW.

### 4. Surveiller le live

```
Utilisateur : « ARGUS, état du live ? »
```
`@argus` : état tmux, compte Topstep (P&L, distances aux limites), stratégies actives, logs, alertes.

### 5. Promouvoir en production

```
Utilisateur (après 🟢 audité) : « FORGE, prépare la promotion de <id> »
```
`@forge` : vérifie préconditions → plan fichier par fichier → confirmation par fichier → vérifie imports
→ plan d'activation progressive (simulation → 1 contrat → sizing nominal).

### 6. Audit ponctuel

```
Utilisateur : « AUDITOR, vérifie strategies/<id>.py et son summary.json »
```

### 7. Exploration intensive — agent team

Pour un débat contradictoire (ex: « pourquoi OPR sous-performe le mardi ? »), l'orchestrateur peut
spawn une team de teammates basés sur les subagents restants (auditor, new-strategy, quant, +
éventuellement un "devil's advocate"). Voir la doc [agent-teams](https://code.claude.com/docs/en/agent-teams.md).

## Garanties de sécurité

1. **`core/**` et `broker/**` en mode `ask`** : tout `Edit`/`Write` déclenche un prompt — quel que soit l'agent.
2. **`state/**` et `logs/**` en `deny`** : aucun agent n'altère l'état du live runner.
3. **Bash destructeurs en `deny`** : `rm -rf`, `git push --force`, `git reset --hard` bloqués.
4. **Outils restreints par agent** : athena, auditor, argus n'ont pas d'écriture ; quant écrit
   uniquement `output/<id>/` ; new-strategy a Edit/Write mais interdit `core/`/`broker/`.
5. **FORGE** est le seul agent autorisé en écriture prod, chaque écriture confirmée individuellement.

## Capitalisation (Pilier 2)

- `strategie_futur/BACKLOG.md` — idées priorisées, source de vérité des candidats.
- `REGISTRE_HYPOTHESES.md` — hypothèses testées 🟢/🟡/🔴 + leçons (consulter avant, écrire après).
- Mémoire persistante (`MEMORY.md` + fichiers) — leçons transversales. Repart vide après reset baseline.

## Référence aux sources

- **Pipeline** : `.claude/skills/new-strategy/SKILL.md` (single-source-of-truth, gated).
- **Templates** : `.claude/skills/new-strategy/templates/{strategy_template,rapport_template}.md`.
- **Robustesse auto** : `core/optimizer.py` + `core/robustness.py::run_full_robustness`.
- **Exploration visuelle manuelle** : `core/explore_chart.py` (CLI, à la demande).
- **Permissions** : `.claude/settings.json` + `.claude/settings.local.json`.
- **Convention projet** : `CLAUDE.md`.
