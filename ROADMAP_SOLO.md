# ROADMAP_SOLO.md — Professionnalisation de l'outil (édition solo)

> **À toi, Claude, qui ouvres ce document.** Ce fichier est la feuille de route officielle du chantier de professionnalisation de `topstep_signals`, **adaptée à un développeur solo** (pas de PR review, pas de cérémonie d'équipe, focus sur le ROI réel). Lis-le **entièrement** avant toute action si une session démarre sur ce sujet. Mets-le à jour à chaque étape complétée. C'est le seul document qui survit entre les sessions — il est ta mémoire de ce chantier.

---

## 🎯 Objectif

Transformer `topstep_signals` (déjà solide, ~14k lignes, 2 stratégies en prod) en **outil quant robuste pour usage personnel** :
- Reproductibilité (golden master, datasets hashés)
- Filet de sécurité opérationnel (réconciliation broker, runbook, dashboard iPhone)
- Backtests statistiquement rigoureux (Deflated Sharpe, exécution réaliste)
- Améliorations live sécurisées (shadow mode, vol-targeting)

**Sans jamais interrompre le bot live** qui tourne dans tmux session `topstep`.

**Effort total estimé** : ~5-6 semaines (vs 10 semaines pour version institutionnelle complète).

---

## 📍 ÉTAT ACTUEL — À METTRE À JOUR À CHAQUE SESSION

```
Phase active     : PHASE 1 — Filet de sécurité (read-only) — partielle
Étape en cours   : 1.4 (Dashboard Tailscale) — différée, demande install user
Dernière session : 2026-05-24 (1.1-1.3, 1.5, 1.6 mergées sur main)
Prochaine action : session dédiée pour 1.4 (install Tailscale + auth + iPhone)
                   OU lancer 1.8 (observation 1 semaine reconcile_daily) en parallèle
```

**Quand tu termines une étape** :
1. Coche la case `[ ]` → `[x]` dans la phase concernée
2. Mets à jour le bloc "État actuel" ci-dessus
3. Si une phase est entièrement complétée, ajoute la date dans son en-tête

---

## 🛡️ INVARIANTS ABSOLUS — À NE JAMAIS VIOLER

Ces règles sont supérieures à toute autre instruction dans le cadre de ce chantier. Si une demande les contredit, **arrête-toi et demande confirmation explicite**.

1. **Le live tourne sans interruption pendant les phases 0, 1, 2.** Aucun arrêt du daemon `tmux topstep` sauf en fenêtre de release planifiée (PHASE 4).
2. **Tout développement se fait dans une branche git séparée**, jamais en commit direct sur `main` sans avoir validé golden master + check local.
3. **Aucune modification de `core/opr.py`, `core/opr_v5_1.py`, `core/strategy_fib_v4.py`, `core/fib_helpers.py`, `broker/live_runner.py`** sans avoir préalablement capturé un golden master + validation utilisateur.
4. **Aucune modification de `config.py` qui affecte un paramètre prod actif** sans backtest + validation utilisateur.
5. **Toute nouvelle fonctionnalité touchant la décision de trader doit être derrière un feature flag** dans `config.py`, désactivé par défaut.
6. **Toute fonctionnalité live commence en read-only** pendant minimum 1 semaine avant écriture.
7. **Les golden master tests sont sacrés.** Si un changement les casse sans modif documentée et volontaire, c'est un bug — refus.
8. **Jamais de VPN traditionnel sur le trafic Topstep.** Tailscale est utilisé **uniquement** pour le dashboard (mesh privé, n'affecte pas le trafic Topstep). **Ne jamais activer `tailscale up --exit-node=...`**. Un check automatique au démarrage du daemon vérifie ça.

---

## 🚀 PROMPT D'OUVERTURE DE SESSION (à coller en début de session)

Quand l'utilisateur démarre une session sur ce chantier, exécute :

```bash
# 1. Vérifier que le live tourne toujours (sanctuaire)
tmux ls
tail -n 5 logs/trading_events.log

# 2. Lire l'état du chantier
cat ROADMAP_SOLO.md | head -50

# 3. Vérifier la branche de dev courante
git status
git log --oneline -5
```

Puis répondre :
- État du live (OK / anomalie)
- Phase + étape en cours d'après ROADMAP_SOLO.md
- Prochaine action concrète à proposer

---

## 📐 PHASES DU CHANTIER

### PHASE 0 — Fondations (3-4 jours) — RISQUE ZÉRO — ✅ terminée le 2026-05-24

**Objectif** : infrastructure de sécurité (tests local, golden master) **sans toucher au code métier**.

**Pré-requis** : aucun.

**Étapes** :

- [x] **0.1 Créer la branche de dev**
  ```bash
  cd /home/charaf/topstep_signals
  git checkout -b infra/foundation
  ```
  Travail dans cette branche. Le live tourne sur `main` (mais le daemon python charge ses fichiers depuis le disque — donc tant que la branche checkout est en cours, le live actuel ne voit pas les changements jusqu'à son prochain redémarrage). **Tu peux travailler en sécurité.**

- [x] **0.2 `pyproject.toml`** avec config ruff + black + pytest
  - ruff : règles E, W, F, I, B, UP, SIM
  - black : line-length 100
  - **mypy en mode permissif** (pas strict — tu typeras au fur et à mesure des modifs)
  - pytest : `testpaths = ["tests"]`

- [x] **0.3 `.pre-commit-config.yaml`** : ruff + black + check trailing whitespace + check gros fichiers
  ```bash
  pre-commit install
  pre-commit run --all-files  # première passe peut révéler des issues — corriger ou whitelister
  ```

- [x] **0.4 Script local `check.sh`** (à la place d'un CI GitHub Actions)
  ```bash
  #!/usr/bin/env bash
  set -e
  ruff check .
  black --check .
  pytest -x
  echo "✅ All checks passed"
  ```
  Lancé manuellement avant chaque merge sur `main` (et automatiquement par un hook git pre-push si tu veux).

- [x] **0.5 Capturer les golden masters**
  - Créer `tests/golden/` (dossier versionné)
  - Script `tests/golden/regenerate.sh` qui produit :
    - `tests/golden/opr_baseline.json` (output déterministe de `backtest.py --strategy opr`)
    - `tests/golden/fib_baseline.json`
  - Créer `tests/test_golden_master.py` : re-exécute les backtests et compare au cent près (PnL, nombre de trades, fills)
  - Lock des versions pandas/numpy dans `pyproject.toml` pour reproductibilité

- [x] **0.6 Mesurer coverage baseline** (informatif, non bloquant)
  ```bash
  pytest --cov=core --cov=broker --cov=strategies --cov-report=term-missing
  ```
  Documenter dans `docs/test_coverage_baseline.md`.

- [x] **0.7 Merge sur `main`**
  - `./check.sh` doit passer
  - Golden master doit passer
  - `git checkout main && git merge infra/foundation`
  - Aucune perturbation du live (le daemon en cours d'exécution n'est pas affecté tant qu'on ne le redémarre pas)

**Critères de validation PHASE 0** :
- ✅ `./check.sh` passe
- ✅ Golden masters capturés et test passe
- ✅ Coverage baseline mesurée
- ✅ Aucune modif de `core/`, `broker/`, `strategies/`, `config.py` (à part `pyproject.toml`)
- ✅ Live toujours intact

**Livrable** : branche mergée sur `main`.

---

### PHASE 1 — Filet de sécurité (1 semaine) — RISQUE ZÉRO (read-only)

**Objectif** : composants de sécurité opérationnelle qui **lisent** l'état du live mais ne le modifient jamais. Inclut le **dashboard iPhone via Tailscale**.

**Pré-requis** : PHASE 0 mergée.

**Étapes** :

- [x] **1.1 Branche `infra/safety-net`**
  ```bash
  git checkout main && git pull
  git checkout -b infra/safety-net
  ```

- [x] **1.2 `scripts/reconcile_daily.py`** — réconciliation broker
  - Lit `state/live_state.json` (positions, P&L journalier)
  - Appelle l'API ProjectX en mode read-only (via `broker/projectx_client.py`)
  - Compare positions, P&L, ordres
  - Tolérance : 1 tick prix, $1 P&L
  - Mismatch → log structuré + alerte Telegram
  - **Tests** : mocker l'API, vérifier détection mismatch + état conforme passe
  - **Activation** : cron quotidien 19h30 UTC post-session

- [x] **1.3 `docs/runbook.md`** — playbook d'incident (8 scénarios)
  1. Bot poste ordres en boucle
  2. WebSocket déconnecté > 5 min
  3. État interne désynchronisé du broker
  4. Limite Topstep approchée < $200
  5. Arrêt propre du daemon en urgence
  6. Flatten toutes positions immédiatement
  7. Isoler un ticker problématique
  8. Redémarrer après crash
  + **Encart sécurité Tailscale** : règles + commandes de vérification (voir 1.4)

  Format : pour chaque scénario, "Symptômes → Action exacte (commandes copier-collables) → Vérification → Récupération".

- [ ] **1.4 Dashboard Streamlit + accès iPhone via Tailscale**  *(différée — demande intervention user)*

  **Sous-étape 1.4.a — Installer Tailscale** (10 min)
  ```bash
  # Sur le PC (WSL2)
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up
  # Suivre l'URL d'auth (Google/GitHub)
  # Noter le hostname assigné (ex: topstep-pc)
  ```

  **Sous-étape 1.4.b — VÉRIFIER que Tailscale n'affecte pas Topstep** (5 min — CRITIQUE)
  ```bash
  # Route par défaut doit pointer vers ton interface normale, PAS tailscale0
  ip route | grep default
  # Attendu : default via 192.168.X.1 dev eth0

  # IP publique vue de l'extérieur = IP ISP réelle
  curl -s ifconfig.me
  # Doit être ton IP ISP, identique à avant Tailscale

  # Traceroute vers Topstep : premier hop = gateway local
  traceroute api.topstepx.com 2>/dev/null | head -3
  # Premier hop attendu : 192.168.X.1 (ta box), JAMAIS 100.X.X.X
  ```
  Si l'un des 3 checks échoue → **STOP**, désinstaller Tailscale et investiguer.

  **Sous-étape 1.4.c — Sur iPhone** (5 min)
  - App Store → installer **Tailscale**
  - Login avec le même compte qu'au point 1.4.a
  - Le PC apparaît dans la liste des devices

  **Sous-étape 1.4.d — `tools/dashboard.py`** (Streamlit mobile-first)
  ```python
  import streamlit as st
  st.set_page_config(
      page_title="Topstep Live",
      page_icon="📊",
      layout="centered",
      initial_sidebar_state="collapsed",
  )
  # Auto-refresh 30s
  # Lecture seule : state/live_state.json + logs/trading_events.log
  # Affichage : équité jour, équité cumulée, distance limites Topstep (DLL/trailing/consistency),
  #             positions ouvertes, derniers fills, latence WS
  ```
  Thème dark mode dans `.streamlit/config.toml`.

  **Sous-étape 1.4.e — Lancement always-on**
  ```bash
  # tools/launch_dashboard.sh
  tmux new -d -s dashboard "streamlit run tools/dashboard.py \
      --server.port 8501 \
      --server.address 0.0.0.0 \
      --server.headless true"
  ```

  **Sous-étape 1.4.f — Accès iPhone**
  - Safari → `http://topstep-pc:8501` (hostname Tailscale) ou `http://100.X.X.X:8501` (IP Tailscale)
  - Bouton Partager → **"Sur l'écran d'accueil"** → icône comme une vraie app

  **Sous-étape 1.4.g — Garde-fou anti-VPN au démarrage du daemon**

  Modifier `broker/live_runner.py` (ou ajouter un check dans `live.py`) :
  ```python
  def _check_no_vpn_on_topstep():
      """Refuse de démarrer si Tailscale exit-node ou VPN détecté.
      Compare l'IP publique à une liste de plages Tailscale (100.64.0.0/10).
      Si match → refuse démarrage + alerte Telegram."""
      import requests, ipaddress
      try:
          public_ip = requests.get("https://ifconfig.me", timeout=5).text.strip()
          ts_range = ipaddress.ip_network("100.64.0.0/10")
          if ipaddress.ip_address(public_ip) in ts_range:
              raise RuntimeError(
                  f"❌ IP publique {public_ip} dans plage Tailscale. "
                  "Désactive l'exit-node Tailscale avant de démarrer le live."
              )
      except requests.RequestException:
          pass  # Pas de réseau ? on laisse le runner gérer
  ```
  Appelé en début de `SessionRunner.run()`. **Ce check est l'invariant #8 de cette roadmap.**

- [x] **1.5 Tests Hypothesis sur les garde-fous**
  - `tests/test_risk_topstep_properties.py` : property-based tests sur les invariants de `core/risk_topstep.py`
  - `tests/test_risk_portfolio_properties.py` : idem pour `core/risk_portfolio.py`
  - Coverage cible : 90%+ sur ces deux fichiers
  - **Important** : ces tests ne modifient pas `risk_topstep.py` ni `risk_portfolio.py`. Si un test révèle un bug, isoler pour traitement séparé avec validation utilisateur.

- [x] **1.6 Snapshot SHA256 datasets dans `summary.json`**
  - Modifier le générateur de `summary.json` (probablement dans `core/optimizer.py` ou `core/metrics.py`)
  - Format : `"datasets": {"NQ1": "sha256:abc...", "MES1": "sha256:def..."}`

- [~] **1.7 Merge sur `main`** *(partiel — 1.4 différée)*
  - `./check.sh` + golden master verts
  - `git checkout main && git merge infra/safety-net`

- [ ] **1.8 Observation 1 semaine**
  - Cron reconcile_daily actif
  - Dashboard ouvert régulièrement
  - Critère Go pour PHASE 2 : **zéro faux positif Telegram pendant 7 jours**

**Critères de validation PHASE 1** :
- ✅ Réconciliation tourne 7 jours sans faux alerte
- ✅ Dashboard accessible iPhone depuis 4G et WiFi
- ✅ Check anti-VPN intégré et testé
- ✅ Runbook relu, kill switch testé en simulation
- ✅ Tests Hypothesis verts, coverage 90%+ sur risk_*
- ✅ Live toujours intact

**Livrable** : branche mergée + 1 semaine d'observation validée.

---

### PHASE 2 — Backtest professionnel (1.5 semaine) — RISQUE ZÉRO (offline)

**Objectif** : améliorations statistiques et perf du moteur de backtest. Offline, le live n'est pas concerné.

**Pré-requis** : PHASE 1 validée + golden masters intacts.

**Étapes** :

- [ ] **2.1 Branche `infra/backtest-pro`**

- [ ] **2.2 Audit de `core/robustness.py` existant** (639 lignes — voir ce qui est déjà en place avant d'ajouter)

- [ ] **2.3 Métriques rigoureuses à ajouter si absentes**
  - **Deflated Sharpe Ratio** (Bailey-López de Prado, 2014) — corrige le data snooping
  - **Probabilistic Sharpe Ratio**
  - Intégrer dans le verdict de `core/metrics.py` — seuil informationnel (pas bloquant initialement)

- [ ] **2.4 Modèle d'exécution réaliste** dans `core/backtester.py`
  - Slippage stochastique corrélé à l'ATR du moment du fill
  - Distribution paramétrable par ticker, calibrée sur les fills live disponibles dans `logs/trading_events.log`
  - Feature flag `BACKTEST_REALISTIC_SLIPPAGE` dans `config.py` (off par défaut)
  - **Garde-fou** : avec flag off → golden master identique. Avec flag on → backtest "réaliste", non comparable au golden.

- [ ] **2.5 Parallélisation walk-forward** dans `core/optimizer.py`
  - `joblib.Parallel` sur les cellules du grid
  - Reproductibilité préservée (seed fixé par cellule)
  - Bench avant/après documenté

- [ ] **2.6 Merge sur `main`**
  - **OBLIGATOIRE** : golden master passe au cent près sur OPR + Fib (avec flag realistic_slippage off)
  - Régénérer `rapport_opr-v5.1.md` et `rapport_fib-v4.md` → identiques aux archivés

**Critères de validation PHASE 2** :
- ✅ Golden masters intacts
- ✅ Walk-forward au moins 4× plus rapide
- ✅ Métriques rigoureuses lisibles dans les rapports
- ✅ Live toujours intact

**Livrable** : branche mergée + rapports régénérés identiques.

> **Items DROPPÉS de la version institutionnelle (non utiles solo)** : CPCV complet, feature store parquet, tracking `experiments.jsonl` (le dossier `output/` actuel suffit).

---

### PHASE 3 — Améliorations live (2 semaines) — RISQUE CONTRÔLÉ

**Objectif** : modifier le comportement live (sizing dynamique principalement). Tout derrière feature flag + shadow mode.

**Pré-requis** : PHASES 0-2 validées + 1 semaine d'observation silencieuse.

**Étapes** :

- [ ] **3.1 Branche `feat/live-upgrades`**

- [ ] **3.2 Framework Shadow Mode** (`broker/shadow_runner.py`)
  - Tourne en parallèle du `live_runner.py` (process séparé, tmux dédié)
  - Écoute le même WebSocket sans poster d'ordres
  - Applique la logique de décision (incluant nouvelles features candidates)
  - Sauvegarde dans `state/shadow_state.json`
  - Script `tools/shadow_vs_live.py` : rapport quotidien "shadow vs live"
  - **Isolé** du live : process, fichier d'état, logs séparés

- [ ] **3.3 Vol-targeting** (réactivation `core/adaptive_sizing.py`)
  - Auditer le code existant (désactivé depuis 2026-05-21 — voir mémoire `project_challenge_mode`)
  - Tourner en shadow mode **2 semaines minimum**
  - Critère Go : PF shadow ≥ PF prod ET DD shadow ≤ DD prod
  - Feature flag `FEATURE_VOL_TARGETING_ENABLED` (OFF par défaut)

- [ ] **3.4 (Optionnel) Nouvelle stratégie diversifiante**
  - Pipeline `/new-strategy` complet (verdict 🟢 obligatoire)
  - Shadow 4 semaines avant promotion
  - **Reporté** si tu préfères d'abord stabiliser

- [ ] **3.5 Merge sur `main`** avec **flags OFF par défaut**
  - L'activation est l'objet de la PHASE 4

**Critères de validation PHASE 3** :
- ✅ Shadow runner tourne 14 jours sans impact sur le live
- ✅ Comparaisons shadow vs live documentées
- ✅ Feature flags testés (on/off → comportement attendu)

**Livrable** : code mergé, flags OFF.

> **Items DROPPÉS** : audit trail cryptographiquement signé (probabilité litige Topstep ≈ 0 pour compte 50K), mkdocs API documentation.

---

### PHASE 4 — Release coordonnée (1 jour) — FENÊTRE DE MAINTENANCE

**Objectif** : activer les feature flags progressivement.

**Pré-requis** : PHASES 0-3 toutes mergées + verdicts shadow Go.

**Fenêtre cible** : **un samedi matin** (marché fermé, pas de positions ouvertes).

#### Checklist PRÉ-RELEASE (vendredi soir)

- [ ] Session live a clôturé proprement (16h00 NY / 21h00 UTC)
- [ ] `cat state/live_state.json` : 0 position ouverte
- [ ] Backup : `cp state/live_state.json state/live_state.json.pre_release_$(date +%Y%m%d)`
- [ ] Backup logs : `tar czf logs_backup_$(date +%Y%m%d).tar.gz logs/`
- [ ] Tag git : `git tag -a v$(date +%Y%m%d)-pre-release -m "État avant release pro"`
- [ ] `./check.sh` passe sur `main`
- [ ] Golden master passe
- [ ] Verdicts shadow Go documentés
- [ ] Feature flags positionnés : safe ON, risqué OFF (vol-targeting activé progressivement)
- [ ] Runbook relu, kill switch testé sur branche de dev
- [ ] Confirmation utilisateur explicite : "GO RELEASE samedi"

#### Procédure RELEASE (samedi matin)

```bash
# 1. Arrêt propre du daemon
tmux send-keys -t topstep C-c
sleep 5
tmux kill-session -t topstep
ps aux | grep live_runner  # vérifier aucun process zombie

# 2. Pull final
cd /home/charaf/topstep_signals
git checkout main
git pull
git tag -a v$(date +%Y%m%d)-released -m "Release pro"

# 3. Redémarrage en dry-run (mode à implémenter en PHASE 3 si pas déjà présent)
tmux new -d -s topstep_dryrun "python live.py --dry-run"
tail -f logs/trading_events.log  # observer 30 min

# 4. Si dry-run OK : bascule en live réel
tmux kill-session -t topstep_dryrun
tmux new -d -s topstep "python live.py"

# 5. Vérifier dashboard iPhone accessible
# 6. Premier ordre test à 1 contrat sur MES1
```

#### Checklist POST-RELEASE

- [ ] **Lundi (J+1)** : session live normale, dashboard ouvert toute la journée
- [ ] **Lundi soir** : reconcile_daily passe → état conforme
- [ ] **Lundi soir** : `tools/shadow_vs_live.py` → écart < 5%
- [ ] **Mardi (J+2)** : revue logs pour anomalies
- [ ] **Mercredi (J+3)** : si tout OK, activer `FEATURE_VOL_TARGETING=ON`
- [ ] **Vendredi (J+5)** : revue hebdo
- [ ] **Vendredi (J+7)** : déclaration "release stabilisée"

#### 🔥 ROLLBACK D'URGENCE — En 60 secondes

```bash
# 1. Couper le live
tmux kill-session -t topstep

# 2. Rollback git + état
cd /home/charaf/topstep_signals
git checkout v$(date +%Y%m%d)-pre-release  # tag du vendredi
cp state/live_state.json.pre_release_YYYYMMDD state/live_state.json

# 3. Redémarrer
tmux new -d -s topstep "python live.py"

# 4. Vérifier
tail -f logs/trading_events.log
```

Notifier par Telegram + créer note rapide dans `docs/incidents/YYYY-MM-DD.md` (1 paragraphe suffit, pas de post-mortem formel).

---

## 📊 SUIVI GLOBAL

```
PHASE 0 — Fondations         (3-4 jours) : [ ] non démarrée  / [ ] en cours  / [x] terminée le 2026-05-24
PHASE 1 — Filet sécurité     (1 semaine) : [ ] non démarrée  / [ ] en cours  / [ ] terminée le YYYY-MM-DD
PHASE 2 — Backtest pro       (1.5 sem.)  : [ ] non démarrée  / [ ] en cours  / [ ] terminée le YYYY-MM-DD
PHASE 3 — Améliorations live (2 sem.)    : [ ] non démarrée  / [ ] en cours  / [ ] terminée le YYYY-MM-DD
PHASE 4 — Release            (1 jour)    : [ ] non démarrée  / [ ] en cours  / [ ] terminée le YYYY-MM-DD

Total estimé : 5-6 semaines, bot live jamais interrompu sauf samedi PHASE 4
```

---

## 🧭 Règles de conduite pour Claude

1. **Ne saute jamais d'étape.** Si l'utilisateur veut accélérer, rappelle le risque (le golden master existe pour une raison).
2. **Demande confirmation à chaque transition de phase** (avant merge sur `main`, avant activation d'un flag).
3. **Tiens le journal de bord** : à chaque session, mets à jour "État actuel" et coche les cases.
4. **Si un invariant est menacé, arrête-toi.** Préfère perdre 10 min à demander qu'à casser le live.
5. **Pour les tâches de PHASE 2 et 3 qui touchent les stratégies, invoque le bon agent** : `@auditor` pour valider, `@new-strategy` pour développer, `@forge` pour la promotion live.
6. **N'efface jamais les tags git ou les backups d'état sans confirmer**.
7. **Vérifie le check anti-VPN** chaque fois que tu touches au démarrage du daemon — c'est l'invariant Topstep #8.

---

## 📚 Documents associés

- `CLAUDE.md` — guide général du projet
- `CLAUDE_TEAM.md` — table des agents
- `docs/strategies_abandoned.md` — registre des stratégies rejetées
- `docs/runbook.md` — (à créer en PHASE 1.3) playbook d'incident + section Tailscale
- `tests/golden/` — (à créer en PHASE 0.5) golden master tests

---

*Dernière mise à jour : 2026-05-24 (PHASE 1 partielle mergée — commit 88c1af9 ; 1.4 et 1.8 restantes).*
