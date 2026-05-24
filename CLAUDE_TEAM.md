# Équipe d'agents — topstep_signals

Architecture hybride pour le développement et l'exploitation de stratégies de trading algorithmique sur le challenge Topstep 50K.

- **Quotidien (90 %)** : pipeline séquentiel via 8 subagents — isolation de contexte, sécurité, sobriété tokens.
- **Exploration intensive (10 %)** : agent teams (expérimental, déjà activé dans `.claude/settings.json`) pour parallélisme et débat contradictoire.

## Table des agents

| Agent | Rôle | Comment l'invoquer | Modèle | Protection |
|---|---|---|---|---|
| **Orchestrateur** | Route les demandes, synthétise. **Ne fait pas le travail de fond.** | Session principale (par défaut) | Opus 4.7 | Suit les règles `ask` sur `core/**` et `broker/**` |
| **@athena** | Cheffe d'orchestre stratégique. Émet un PLAN d'orchestration que l'orchestrateur exécute. | `@athena` ou « ATHENA, ... » | Inherit (Opus) | Lecture seule (Read, Grep, Glob, Bash, WebSearch) |
| **@researcher** | Recherche web, formalisation algorithmique de concepts trading, pièges classiques. | `@researcher` ou « Recherche / formalise ... » | Sonnet | Lecture seule (+ WebFetch/WebSearch) |
| **@new-strategy** | Pipeline complet (PHASES 1-8) — implémentation, backtest, optimisation, stress tests. | `@new-strategy` ou `/new-strategy "..."` | Inherit (Opus) | Écriture limitée à `strategies/`, `config.py`, `output/` |
| **@auditor** | Audit indépendant de la fidélité concept↔code. Peut rétrograder le verdict. | `@auditor` ou « Audite ... » | Inherit (Opus) | Lecture seule stricte |
| **@chartist** | Expert en analyse technique multimodale. Mode `idea` (idéation visuelle PHASE 0.5) ou `audit` (audit visuel PHASE 6.5 — **CONDITIONNEL** : skip si verdict 🟢 clair). Lit les PNG (mono-TF ou trio 15m+H1+D1). Ne décide pas du verdict. | `@chartist MODE: idea` ou `@chartist MODE: audit` | Inherit (Opus, multimodal) | Lecture seule absolue |
| **@quant** | Data scientist trading senior. Mode `catalog` (PHASE 2.5 — propose features candidates) ou `discover` (PHASE 3.5 — sklearn RF + permutation test sur trades baseline, produit un patch Python). Ne modifie jamais de fichier de stratégie directement. | `@quant MODE: catalog` ou `@quant MODE: discover` | Inherit (Opus) | Écriture limitée à `output/<strategy_id>/` |
| **@argus** | Surveillance du daemon live (tmux, state, logs, Telegram). | `@argus` ou « État du live ? » | Sonnet | Lecture seule absolue (interdiction `kill`, `rm`, `tmux send-keys`, etc.) |
| **@forge** | Promotion en production : crée `core/<id>.py`, modifie `broker/live_runner.py`. | `@forge` + verdict 🟢 confirmé + confirmation utilisateur **par fichier** | Inherit (Opus) | Modification autorisée de `core/` et `broker/`, mais chaque écriture déclenche un prompt utilisateur |

## Workflows

### 1. Développer une nouvelle stratégie (workflow recommandé — pipeline orchestré)

```
Utilisateur : « ATHENA, développe une stratégie ICT order block sur NQ1 »
```

Flow exécuté par l'orchestrateur (étapes chartist + quant conditionnelles selon décision d'Athena) :

```
[Orchestrateur] → @athena                    (Tour 1 — émet PLAN ATHENA)
              ↓
[Optionnel — concept ouvert, mono-/multi-TF]
[Orchestrateur] → python -m core.explore_chart --ticker <X> --n 10 [--multi-tf]
[Orchestrateur] → @chartist MODE: idea       (Tour 2a — hypothèses d'edge)
              ↓
[Orchestrateur] → @researcher                (Tour 2 — formalisation + sources)
              ↓
[Orchestrateur] → @athena                    (Tour 3 — transition, décide [2.5] et [3.5])
              ↓
[Optionnel — concept conditionnel ou neuf]
[Orchestrateur] → @quant MODE: catalog       (Tour 3a — propose features candidates)
              ↓
[Orchestrateur] → @new-strategy              (Tour 4 — PHASES 2-3 : scaffold + baseline)
              ↓
[Conditionnel — baseline 🟡 ou 🟢 borderline avec n_oos ≥ 100]
[Orchestrateur] → @quant MODE: discover      (Tour 4a — sklearn + patch)
[Orchestrateur] → @new-strategy              (Tour 4b — applique patch en vN+1, PHASES 4-8)
              ↓
[Orchestrateur] → @athena                    (Tour 5 — lit summary.json, décide [4.5])
              ↓
[CONDITIONNEL — skip si verdict 🟢 clair (bootstrap≥80% AND PF≥1.5) OU 🔴]
[Orchestrateur] → @chartist MODE: audit      (Tour 5a — warnings visuels)
              ↓
[Orchestrateur] → @auditor                   (Tour 6 — lit summary.json + audit quant_patch.py)
              ↓
[Orchestrateur] → @athena                    (Tour 7 — VERDICT FINAL)
              ↓
[Orchestrateur] → Utilisateur                (synthèse + suggestion : @forge si 🟢)
```

**Décision d'invoquer le chartist** :
- Mode `idea` (avant @researcher) : oui si concept ouvert / non formalisé / exploration multi-marché. Non si concept clair (ex: "ORB pullback NQ 09:30").
- Mode `audit` (après @new-strategy) : **SKIP si verdict 🟢 clair (bootstrap ≥ 80% AND PF OOS ≥ 1.5)** — gain ~80-100k tokens. Exécuter sur 🟢 borderline et 🟡. Skip sur 🔴.

**Décision d'invoquer le quant** :
- Mode `catalog` (avant @new-strategy) : oui si concept implique du filtrage conditionnel (régime, timing, microstructure) ou concept neuf sans héritage prod. Non si simple variant d'une strat prod (ex: opr-v6).
- Mode `discover` (après baseline) : **recommandé si baseline = 🟡** (PF OOS 1.2-1.5) — un filtre data-driven peut basculer en 🟢. Skip si baseline 🔴 dur (problème structurel) ou si n_oos < 100 (sample insuffisant pour ML).

### 2. Développer une stratégie (workflow rapide — skill direct)

```
Utilisateur : /new-strategy "ORB inverse sur YM1, fenêtre 9h30-10h30 NY"
```

Le skill `/new-strategy` exécute le pipeline en autonomie (PHASES 1-8 du skill). Pas de phase researcher/auditor explicite. Les PHASES 2.5 et 3.5 (`@quant`) peuvent être invoquées par le skill s'il tourne en session principale (`agent: general-purpose`). À privilégier quand l'idée est déjà claire et formalisée.

### 3. Surveiller le live

```
Utilisateur : « ARGUS, état du live ? »
            ou « Comment va le compte ce matin ? »
```

`@argus` produit un rapport structuré avec :
- État du daemon tmux
- Compte Topstep (P&L, distances aux limites)
- Stratégies actives et trades du jour
- Logs (erreurs, NOT_FILLED, anomalies)
- Alertes (🚨 critique / ⚠️ attention / ℹ️ info)

### 4. Promouvoir en production

```
Utilisateur : (après verdict 🟢 confirmé)
              « FORGE, prépare la promotion de <strategy_id> »
```

`@forge` :
1. Vérifie toutes les **préconditions** (verdict 🟢 audité, rapports présents, hors session live).
2. Émet un **plan de promotion** fichier par fichier.
3. Demande **confirmation pour chaque fichier** avant écriture.
4. Vérifie les imports après chaque modification.
5. Affiche un récap final avec plan d'activation progressive (simulation 1 semaine → live 1 contrat → sizing nominal).

### 5. Audit ponctuel d'un code existant

```
Utilisateur : « AUDITOR, vérifie strategies/concept-v3.py et son rapport »
```

`@auditor` produit un rapport critique sans écrire ; signale les look-ahead, biais, incohérences verdict.

### 6. Exploration intensive — agent team (10 % des cas)

Pour les questions difficiles qui méritent un débat contradictoire (ex: « pourquoi notre OPR sous-performe sur les mardis ? »), l'orchestrateur peut proposer la création d'une **agent team** :

```
Utilisateur : « Crée une équipe pour investiguer le pattern de pertes du mardi sur OPR. »
```

L'orchestrateur (lead de la team) spawn 3-4 teammates :
- Un teammate de type `auditor` (vérifie data + code)
- Un teammate de type `researcher` (cherche des explications structurelles)
- Un teammate de type `new-strategy` (teste un fix éventuel)
- Optionnellement un teammate "devil's advocate" qui challenge les hypothèses

Les teammates communiquent entre eux via la mailbox d'agent teams. Voir la doc [agent-teams](https://code.claude.com/docs/en/agent-teams.md) pour les détails opérationnels (Shift+Down pour cycler, `TeamCreate`, etc.).

## Garanties de sécurité

1. **`core/**` et `broker/**` sont en mode `ask`** dans `.claude/settings.json` : tout `Edit` ou `Write` y déclenche un prompt utilisateur — quel que soit l'agent.
2. **`state/**` et `logs/**` sont en `deny`** : aucun agent ne peut altérer l'état du live runner.
3. **Bash destructeurs en `deny`** : `rm -rf`, `git push --force`, `git reset --hard` sont bloqués globalement.
4. **Outils restreints par agent** : athena, researcher, auditor, argus n'ont **pas** d'outils d'écriture. new-strategy a Edit/Write mais son system prompt interdit `core/` et `broker/`.
5. **FORGE** est le seul agent autorisé en écriture sur la prod, et chaque écriture est confirmée individuellement par l'utilisateur.

## Référence aux sources

- **Pipeline `new-strategy`** : `.claude/skills/new-strategy/SKILL.md` (source de vérité unique, PHASES 0.5 → 8 dont 0.5 et 6.5 optionnelles via chartist)
- **Templates** : `.claude/skills/new-strategy/templates/strategy_template.md`, `rapport_template.md`
- **Outil idéation visuelle** : `core/explore_chart.py` (génère N jours stratifiés par régime, flag `--multi-tf` pour trio 15m+H1+D1)
- **Permissions** : `.claude/settings.json` (équipe + protection) et `.claude/settings.local.json` (allowlist Bash personnelle)
- **Convention projet** : `CLAUDE.md` (architecture, paramètres, performances prod)
