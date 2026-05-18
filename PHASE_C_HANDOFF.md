# Phase C — Handoff Market Hub Streaming

**Pour la prochaine session Claude.** Document self-contained pour reprendre le chantier sans contexte préalable.

---

## 1. Contexte court

Projet `topstep_signals` : bot trading futures intraday automatisé sur ProjectX/TopstepX. Voir [CLAUDE.md](CLAUDE.md) pour l'architecture complète. **Lire CLAUDE.md AVANT toute action sur `core/` ou `broker/`** — protection automatique des écritures via `.claude/settings.json`.

État de la prod au 2026-05-18 :
- **OPR v4** (`core/opr.py`) en prod sur MES1, NQ1, YM1 depuis 2026-05-05
- **OPR v5.1 LIVE** (`core/opr_v5_1.py`) déployée sur NQ1+YM1 le 2026-05-18, **schéma A entrée différée** (commit `e043ab7`)
- **Phase B** (`broker/projectx_realtime.py`) SignalR User Hub mergée et **ACTIVE** (`PROJECTX_REALTIME_ENABLED=True`, commit `f31959f`) — fournit fill/close detection <1s
- Daemon tournant en tmux session `topstep`, mode `--daemon --execute`, compte 50K Challenge simulé (22530037)

---

## 2. Pourquoi Phase C

Le filtre F2 d'OPR v5.1 décide d'émettre le LIMIT seulement quand le push de cassure dépasse un seuil (en ATR daily). En M15 strict, on a mesuré **74 % de fidélité** vs le backtest post-fill (cf. `scripts/live_eq_v5_1.py`, `output/no_mes1/live_eq_v5_1.md`). La perte de 26 % vient des cas où F2 cross **dans la bougie M15 du fill**, non distinguable du backtest sans données plus fines.

**Phase A** (M1 polling REST) aurait pu améliorer ça mais on n'a pas pu **mesurer** son gain en backtest (les fill_time sont alignés sur l'index M15 dans la chaîne v5.1). Phase A reste possible mais peu attrayante.

**Phase C** = streaming temps réel des quotes/trades via Market Hub WebSocket. Permet :
- Monitoring **continu** de F2 (tick-by-tick ou agrégé M1 en RAM)
- Décision **intra-bar** : placer le LIMIT exactement quand F2 cross, pas au close M15 suivant
- Fidélité estimée **95-98 %** vs backtest
- Foundation pour stratégies futures à microstructure (scalp, momentum HFT-lite, news reactive)

---

## 3. Objectif livrable Phase C

Un module `broker/projectx_market_realtime.py` (nouveau) qui :
1. Maintient une connexion SignalR au **Market Hub** `rtc.topstepx.com/hubs/market`
2. S'abonne aux quotes/trades pour les contrats actifs (NQ1, YM1 minimum)
3. Reconstruit en RAM des bars M1 (et optionnellement M5/M15) au fil de l'eau
4. Expose une API simple `get_recent_m1_bars(contract_id, lookback_min)` côté `SessionRunner`
5. Permet à `core/opr_v5_1.py` de calculer running F2 sur **M1 intra-bar** au lieu de M15 strict

**Critère de succès** : re-faire tourner `scripts/live_eq_v5_1.py` adapté pour M1 et mesurer fidélité ≥ 90 %. Si oui → upgrade `core/opr_v5_1.py` pour utiliser le buffer M1. Sinon → analyser pourquoi.

---

## 4. Plan d'implémentation (ordre suggéré)

### Étape 1 — Smoke connexion Market Hub (2h)
- Adapter `scripts/realtime_smoke.py` (existe pour User Hub) en `scripts/realtime_smoke_market.py`
- URL hub : `https://rtc.topstepx.com/hubs/market`
- Subscribe methods : `SubscribeContractQuotes(contractId)`, `SubscribeContractTrades(contractId)`, optionnel `SubscribeContractMarketDepth(contractId)`
- Events à capturer : `GatewayQuote` (quote update), `GatewayTrade` (market trade, ≠ User Hub Trade !), `GatewayDepth`
- **Logger les payloads bruts** comme on l'a fait pour User Hub — la structure réelle peut différer de la doc, à confirmer empiriquement
- Doc référence : http://gateway.docs.projectx.com/docs/realtime/ (cf. WebFetch déjà fait en session précédente)

### Étape 2 — Module `broker/projectx_market_realtime.py` (4-6h)
- **Clone de pattern** depuis `broker/projectx_realtime.py` — réutiliser :
  - Structure dataclass + classe principale
  - `threading.RLock` (pas `Lock` — deadlock détecté en Phase B, cf. §6)
  - Pattern producer/consumer avec `queue.Queue(maxsize=)`
  - Supervisor thread (10s) avec reconnect + zombie detection
  - `_extract_payload` avec déballage de wrapper `{action, data}` si présent (à confirmer en smoke pour Market Hub — User Hub a wrapper sur Order, plat sur Position/Trade, donc à vérifier au cas par cas)
- **Spécificité Market Hub** : volume d'events potentiellement très élevé (centaines/s par contrat actif). Implications :
  - `queue_maxsize` plus grand (10k+) ou stratégie de drop différente
  - Ne PAS logger chaque event (debug-gated)
  - Probablement utile d'agréger côté thread WS (pas attendre que le runner consomme)

### Étape 3 — Buffer M1 in-memory (2-4h)
- Classe `M1Buffer(contract_id, max_minutes=120)` qui :
  - Reçoit les events `GatewayQuote` / `GatewayTrade`
  - Construit incrémentalement le bar M1 courant (open, high, low, close, volume)
  - À chaque passage de minute (UTC), close le bar et démarre le suivant
  - Stocke les N derniers bars dans un `collections.deque(maxlen=max_minutes)`
- API : `get_bars_since(timestamp_utc) -> List[M1Bar]` + `get_current_forming_bar() -> Optional[M1Bar]`
- Thread-safe (RLock) car alimenté par WS thread et lu par main thread

### Étape 4 — Intégration `core/opr_v5_1.py` (2-3h)
- Ajouter paramètre optionnel `m1_buffer` dans `get_opr_v5_1_live_signals(...)` :
  - Si fourni → utiliser le buffer M1 pour calcul de F2 running au lieu de `df_session` M15
  - Sinon → comportement actuel (M15)
- **Garder le pass-through MES1** intouché
- Ajouter une config `OPR_V5_1_USE_M1_BUFFER = False` (par défaut OFF, flip après validation)

### Étape 5 — Tests + smoke + validation empirique (3-4h)
- Tests unit pour `M1Buffer` (agrégation correcte, close à la minute, thread-safety)
- Tests unit pour `ProjectXMarketRealtimeClient` (mock SignalR comme Phase B)
- Smoke réel : 1h+ d'écoute sur NQ1 pendant session active, vérifier que les bars M1 reconstruits matchent les bars M1 fetchés via REST (`get_bars(unit=2, unit_number=1)`)
- **Validation fidélité** : adapter `scripts/live_eq_v5_1.py` pour utiliser le buffer M1 sur OOS récent — viser ≥ 90 % fidélité avant flip ON

### Étape 6 — Rollout (1-2h)
- `PROJECTX_MARKET_REALTIME_ENABLED = False` par défaut → ship et test sur sim
- Burn-in 1 session OPR complète : zéro crash thread, bars M1 cohérents, pas de gap >10s
- Si OK → `OPR_V5_1_USE_M1_BUFFER = True` + redémarrage daemon
- Auto-disable si > 10 erreurs/h (même pattern que Phase B)

---

## 5. Patterns à RÉUTILISER depuis Phase B

Le module `broker/projectx_realtime.py` est le template canonique. Patterns clés :

### 5.1 Producteur / consommateur strict
- WS thread ne mute QUE la queue
- Main thread consomme via `drain_events()`
- Aucun état partagé hors-queue

### 5.2 Idempotence par status check
- v5.1 et phase B reposent sur `placed_tags[tag].status` pour éviter doublons
- Pour Phase C (market data, pas d'orders) → idempotence pas critique mais garder le pattern pour les bars M1 (timestamp-keyed dict pour éviter doublons)

### 5.3 JWT refresh-aware
- `ProjectXClient.token` (property, `broker/projectx_client.py:72-87`) déclenche `_maybe_reauth` automatiquement
- Passer `lambda: client.token` (callable, pas string) au realtime client
- Force-rebuild périodique à 22 h pour rafraîchir le bearer

### 5.4 Reconnect dual-layer
- SignalR built-in : `with_automatic_reconnect({...})`
- Supervisor thread custom (10 s loop) :
  - Disconnect > 60 s → force rebuild avec backoff
  - Connected mais silencieux > `max_silence_s` (180 s pour User Hub, **à recalibrer plus bas** pour Market Hub car beaucoup plus bavard) → zombie → rebuild

### 5.5 Auto-disable sur erreurs
- Compteur `_rt_error_count_hour` dans `SessionRunner._drain_realtime`
- > 10 erreurs/h → `self.rt.stop(); self.rt = None`, Telegram critique, fallback REST seul
- Phase C devrait avoir le même pattern

---

## 6. Gotchas / leçons apprises Phase B

### 6.1 RLock obligatoire (sinon deadlock)
`_build_connection_and_start` tient `_lock` puis appelle `connection.start()` qui invoque synchroniquement `_on_open` → qui essaie de prendre `_lock` à nouveau pour `connection.send()` les subscriptions. Avec `threading.Lock()` (non-réentrant) → **deadlock**. Avec `threading.RLock()` → OK. Pattern à reproduire.

Voir `broker/projectx_realtime.py:127-130` :
```python
self._lock = threading.RLock()   # PAS threading.Lock()
```

### 6.2 Wrapper de payload ProjectX inconsistant
User Hub : `GatewayUserOrder` est wrappé `{"action": 1, "data": {...}}`, mais `GatewayUserPosition` et `GatewayUserTrade` sont **plats**. Le `_extract_payload` doit gérer les deux cas :

```python
@staticmethod
def _extract_payload(args) -> dict:
    if not args:
        return {}
    raw = args[0] if isinstance(args, list) else args
    if not isinstance(raw, dict):
        return {}
    if "data" in raw and isinstance(raw["data"], dict):
        return raw["data"]
    return raw   # fallback raw si pas d'enveloppe
```

Pour Market Hub : à confirmer en smoke. `GatewayQuote/Trade/Depth` peuvent être wrappés ou plats. Logger les payloads bruts d'abord, adapter le parser.

### 6.3 Mapping status codes ProjectX
Confirmé via smoke 2026-05-18 :
- `status=1` : actif/placed (entry order après placement)
- `status=2` : filled
- `status=3` : cancelled
- `status=6` : market in-flight (transitionnel, ~100 ms)
- `status=8` : bracket en attente
- `status=5` : rejected (supposé, non observé)

Pour Market Hub : pas de status code, mais structure des quotes à découvrir (bid/ask/lastPrice/volume/timestamp ?).

### 6.4 SignalR n'a PAS de replay
Les events émis pendant un outage WS sont définitivement perdus. C'est pourquoi le **polling REST 30 s reste autoritatif** dans Phase B. Pour Phase C (market data) :
- Si outage → on perd des ticks → bars M1 peuvent être incomplets pour la période d'outage
- Mitigation : au reconnect, fetch les M1 bars manquants via `get_bars(unit=2, unit_number=1)` REST sur l'intervalle [last_seen, now] pour combler le gap

### 6.5 Schema discovery via smoke obligatoire
Ne PAS coder le parser à partir de la doc seule. **Toujours faire un smoke** qui imprime les payloads bruts, puis adapter. Le User Hub a montré 3 inconsistances payload non documentées.

### 6.6 Threads daemon et stop propre
- `threading.Thread(daemon=True)` → kill auto à l'exit du process
- `stop()` doit set un `_stop_event`, puis `_connection.stop()`, puis `thread.join(timeout=)`
- Tester explicitement `stop_idempotent` (appeler stop 2× sans crash)

### 6.7 Drain en 2 points (Phase B)
- `broker/live_runner.py:run_tick()` au début (juste avant `_sync_broker`)
- `live.py:272` dans la micro-sync 30 s (juste avant `runner._sync_broker(now_utc_poll)`)
- Pour Phase C, le M1 buffer doit être lu par les stratégies au moment de leur décision → pas de drain global, mais accès on-demand depuis `core/opr_v5_1.py`

---

## 7. Validation strategy

### 7.1 Smoke parsing schemas
`scripts/realtime_smoke_market.py` doit imprimer tous les payloads bruts non tronqués pour mapper les champs réels. Faire tourner sur NQ1 pendant 5-10 min en session active.

### 7.2 Cohérence M1 buffer vs REST
Lancer le daemon avec Phase C ON pendant 30 min sur NQ1, puis comparer :
- Bars M1 reconstruits depuis le stream (mémoire)
- Bars M1 fetchés via `client.get_bars(NQ1, unit=2, unit_number=1, ...)` REST

Différence acceptable : ≤ 1 tick de slippage sur high/low (le stream voit chaque tick, le REST aggrège).

### 7.3 Re-mesure de fidélité v5.1
Adapter `scripts/live_eq_v5_1.py` :
- Au lieu de calculer F2 sur df_15m, le calculer sur le buffer M1 simulé (= bars M1 récupérés via REST historique sur OOS)
- Comparer la fidélité M1 vs M15 sur les MÊMES trades
- Critère : ≥ 90 % fidélité → Phase C utile, valider le flip

⚠️ Attention : pour cette validation backtest il FAUT du M1 historique. Les données actuelles dans `data/` sont M15 et M5. Soit :
- Fetch M1 historique via `client.get_bars(..., unit=2, unit_number=1)` sur la période OOS (oct 2025 → mai 2026, ~150 jours × ~390 minutes futures session = 60k bars/ticker, ~30 calls de 2000 bars chacun)
- Persister dans `data/NQ1_data_m1.csv` et `data/YM1_data_m1.csv`
- Réutiliser pour cette validation et toute future re-évaluation

### 7.4 Tests automatisés
Cible : ≥ 15 tests verts (12 unit + 3 intégration)
- M1Buffer : agrégation, close minute, thread-safety, lookback bounded
- ProjectXMarketRealtimeClient : subscribe, parse events, reconnect, queue overflow
- Intégration : v5.1 avec buffer M1 vs sans (deux résultats cohérents, M1 plus permissif)

---

## 8. Critères d'acceptation Phase C

Pour merger :
1. ✅ Smoke réel sur NQ1 : ≥ 100 events reçus sur 5 min en session active
2. ✅ M1 buffer cohérent avec REST sur 30 min (delta ≤ 1 tick high/low)
3. ✅ Fidélité v5.1 mesurée M1 ≥ 90 % (vs 74 % M15)
4. ✅ Tests : 15+ verts
5. ✅ Burn-in sim 1 session OPR complète sans crash thread
6. ✅ Zero régression : `PROJECTX_MARKET_REALTIME_ENABLED=False` par défaut, comportement v5.1 identique à actuel

Pour flip ON en prod :
- Critères 1-6 + 1 semaine de burn-in stable
- PR séparée pour le flip (1 ligne config)

---

## 9. Fichiers clés référence

| Fichier | Rôle | À modifier ? |
|---|---|---|
| `broker/projectx_realtime.py` | Template Phase B User Hub | Cloner pour Phase C |
| `broker/projectx_client.py:72-87` | Property `token` JWT refresh-aware | Réutiliser |
| `broker/live_runner.py:611-738` | `_sync_broker` + helpers idempotents | Probablement intouché |
| `core/event_logger.py:113-138` | Méthodes `realtime_*` | Cloner pour `market_*` |
| `core/opr_v5_1.py` | Wrapper v5.1 schéma A | Étendre pour buffer M1 optionnel |
| `config.py` | Bloc `PROJECTX_REALTIME_*` | Ajouter bloc `PROJECTX_MARKET_REALTIME_*` |
| `tests/test_projectx_realtime.py` | Template tests SignalR | Cloner pour Market Hub |
| `scripts/realtime_smoke.py` | Smoke User Hub | Cloner pour Market Hub |

---

## 10. Risques majeurs

1. **Volume d'events Market Hub** : peut-être 10-100× plus élevé que User Hub. Risque saturation queue + CPU. *Mitigation* : agrégation côté WS thread, queue grande, dropping policy testée.

2. **Disponibilité M1 historique** : si fetch via broker échoue ou rate-limit, on ne peut pas valider la fidélité. *Mitigation* : commencer par fetch petit échantillon (1 mois), si OK → fetch full OOS.

3. **Risque code prod** : `core/opr_v5_1.py` est en prod (commit e043ab7). Toute modif doit être additive (paramètre optionnel `m1_buffer=None`). *Mitigation* : config flag par défaut OFF, tests de non-régression.

4. **Connexion duale User Hub + Market Hub** : 2 WebSockets actifs en parallèle. Plus de RAM, plus de risque de crash dont 1 affecte l'autre. *Mitigation* : indépendance stricte des supervisors, auto-disable par hub séparément.

5. **Désynchronisation horloge bar M1** : si le stream donne des timestamps légèrement différents du REST, les bars peuvent avoir des boundaries différents. *Mitigation* : utiliser `pd.Timestamp.floor("1min")` consistent, comparer en UTC.

---

## 11. Si tu hésites

- **Demander à l'utilisateur avant de toucher `core/` ou `broker/`** (protection settings.json déclenche un prompt de toute façon)
- **Smoke test AVANT code** : valider les schemas en premier
- **Commit Phase B `f31959f`** est le template — relire le diff pour comprendre le pattern
- **Tests à chaque étape** : ne pas accumuler 500 lignes de code sans test entre

---

## 12. Cmd starter pour la prochaine session

```bash
# Vérifier l'état actuel
cd /home/charaf/topstep_signals
tmux ls                                  # daemon doit être actif (session "topstep")
git log --oneline -5                     # f31959f + e043ab7 visibles
python -m pytest tests/ -v               # baseline : 37/37 verts

# Récupérer le contexte précédent (smokes Phase B)
ls scripts/realtime_smoke.py scripts/place_test_order*.py

# Doc broker réelle
# WebFetch : http://gateway.docs.projectx.com/docs/realtime/

# Lire ce handoff
cat PHASE_C_HANDOFF.md
```

Bonne chance. La structure est posée, Phase C est essentiellement un clone+adapt+intégration de Phase B avec un buffer M1 par-dessus. ~2 jours focus si méthodique.
