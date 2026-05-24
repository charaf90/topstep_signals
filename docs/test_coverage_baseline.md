# Coverage baseline — PHASE 0 (ROADMAP_SOLO)

Mesure initiale du coverage de la suite de tests, capturée le 2026-05-24
sur la branche `infra/foundation` à la fin de PHASE 0.

```bash
pytest --cov=core --cov=broker --cov=strategies --cov-report=term
```

**Global : 29 %** (6574 stmts, 4686 missing).

Le chiffre brut est tiré vers le bas par :
- les stratégies archivées (`strategies/archive/`) qui ne servent plus mais
  restent dans le scope du report,
- les modules de charting / data-fetcher (chart.py, analysis_chart.py,
  data_fetcher.py, explore_chart.py) jamais testés en unitaire mais
  exercés implicitement par les backtests,
- `core/optimizer.py` et `core/robustness.py` (research) non testés
  unitairement.

## Coverage par fichier prod critique

| Fichier | Coverage | Commentaire |
|---|---|---|
| `core/risk_topstep.py` | **100 %** | Garde-fou Topstep par trade |
| `core/data.py` | **100 %** | Chargement CSV |
| `broker/m1_buffer.py` | **96 %** | Buffer M1 intra-bar |
| `core/fib_helpers.py` | **94 %** | Pivot / impulse / wick |
| `core/opr.py` | **92 %** | Stratégie prod OPR v4 |
| `core/adaptive_sizing.py` | **90 %** | Sizing adaptatif (off depuis 2026-05-21) |
| `core/opr_v5_1.py` | **84 %** | Stratégie prod OPR v5.1 |
| `strategies/opr.py` | **74 %** | Wrapper backtest OPR |
| `broker/projectx_realtime.py` | **74 %** | WebSocket events |
| `broker/projectx_market_realtime.py` | **73 %** | WebSocket market data |
| `core/risk_portfolio.py` | **73 %** | Garde-fou portefeuille (live) |
| `core/event_logger.py` | **59 %** | Log structuré |
| `strategies/opr_v5_1.py` | **52 %** | Wrapper backtest OPR v5.1 |
| `core/strategy_fib_v4.py` | **51 %** | Stratégie prod Fib v4 |
| `core/metrics.py` | **31 %** | Métriques + bootstrap |
| `strategies/opr_v5.py` | **27 %** | (Wrapper backtest v5 — non prod) |
| `broker/live_runner.py` | **23 %** | Daemon de session |
| `broker/projectx_client.py` | **23 %** | Client API ProjectX |
| `broker/telegram_bot.py` | **12 %** | Alertes Telegram |

## Coverage 0 % (non testés)

- `core/analysis_chart.py`, `core/chart.py`, `core/explore_chart.py`
- `core/data_fetcher.py`
- `core/optimizer.py`
- `core/robustness.py`
- `broker/tg_notify.py`
- `strategies/archive/*` (kijun_pb, opr_h4, smc_v1)

## Cibles futures

- **PHASE 1.5** : property-based tests Hypothesis sur `core/risk_topstep.py`
  et `core/risk_portfolio.py` → coverage 90 %+ sur ces deux fichiers.
- À envisager au fil de l'eau : porter `core/strategy_fib_v4.py` à 80 %+
  via des tests scénario sur les branches d'invalidation pivot et wick
  intra-bar.
- `broker/live_runner.py` restera modérément couvert tant qu'on n'a pas
  d'environnement d'intégration. Les tests existants (`test_live_runner_realtime.py`)
  couvrent la partie réconciliation WS↔REST, c'est l'essentiel.

## Note méthodo

Ce baseline n'est pas bloquant. Il sert de référence pour mesurer la
progression au fil des PHASES suivantes. Ne pas chasser le pourcentage
global pour le seul plaisir du chiffre — la qualité du test compte plus
que sa couverture.
