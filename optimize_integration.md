# Intégration de `core/robustness.py` dans `optimize.py`

## Patch minimal — ajouter en fin de pipeline

Une fois la phase walk-forward terminée et la meilleure config sélectionnée :

```python
# En tête de optimize.py
from core.robustness import (
    run_full_robustness,
    format_summary_markdown,
    format_telegram_html,
)
from config import TOPSTEP_TRAILING_DD, MACRO_EVENT_DATES   # déjà présents

# ... pipeline d'optimisation existant ...
# best_params, oos_trades_df = walk_forward(...)

# ── Robustesse statistique (post-optimisation) ──────────────────────
# Calcul du DD Topstep restant en se basant sur l'état live actuel
import json
try:
    with open("state/live_state.json") as f:
        live_state = json.load(f)
    dd_consumed = abs(live_state.get("trailing_dd_consumed", 0.0))
    topstep_dd_remaining = max(0.0, TOPSTEP_TRAILING_DD - dd_consumed)
except (FileNotFoundError, json.JSONDecodeError):
    topstep_dd_remaining = float(TOPSTEP_TRAILING_DD)   # par défaut, full limit

# Nombre de configs testées (taille du PARAM_GRID)
n_configs = len(list(itertools.product(*PARAM_GRID.values())))

robustness = run_full_robustness(
    trades=oos_trades_df,
    n_strategies_tested=n_configs,
    topstep_dd_remaining=topstep_dd_remaining,
    seed=42,
)

# Affichage console
print(format_summary_markdown(robustness))

# Sauvegarde dans le dossier output (utilisé par la PHASE 8 du skill)
out_dir = pathlib.Path("output")
out_dir.mkdir(exist_ok=True)
with open(out_dir / f"robustness_{strategy_id}.json", "w") as f:
    json.dump(robustness, f, indent=2, default=str)
with open(out_dir / f"robustness_{strategy_id}.md", "w") as f:
    f.write(format_summary_markdown(robustness))

# Notification Telegram
from broker.tg_notify import send_html
send_html(format_telegram_html(robustness, strategy_id))
```

## Verdict augmenté

Pour intégrer la robustesse au verdict automatique de `core/metrics.py`, ajouter
ces critères en aval du verdict de base :

```python
def augment_verdict(base_verdict: str, robustness: dict) -> tuple[str, list[str]]:
    """
    Affine le verdict 🟢🟡🔴 à la lumière des analyses de robustesse.

    Rétrograde si :
      - bootstrap PF < seuil Bonferroni
      - PSR(0) < 80 %
      - MC DD breach Topstep > 5 %
      - ≥ 2 régimes en ❌
    """
    flags = []

    bp = robustness.get("bootstrap_pf", {})
    bf = robustness.get("bonferroni", {})
    if "error" not in bp and bf:
        if bp["p_above_threshold"] < bf["bootstrap_threshold_pct"]:
            flags.append(
                f"bootstrap {bp['p_above_threshold']:.1f}% < "
                f"seuil Bonferroni {bf['bootstrap_threshold_pct']:.1f}%"
            )

    psr = robustness.get("psr", {})
    if "error" not in psr and psr.get("psr_pct", 100) < 80:
        flags.append(f"PSR(0) {psr['psr_pct']:.1f}% < 80%")

    mc = robustness.get("monte_carlo_dd", {})
    if "error" not in mc and mc.get("dd_topstep_breach_pct", 0) > 5:
        flags.append(f"MC DD breach Topstep {mc['dd_topstep_breach_pct']:.1f}%")

    regime_fails = sum(1 for r in robustness.get("regime_stress", [])
                       if r.get("verdict") == "❌")
    if regime_fails >= 2:
        flags.append(f"{regime_fails} régimes en échec")

    # Application
    if not flags:
        return base_verdict, []

    # Rétrogradation : 🟢 → 🟡 → 🔴
    downgrade = {"🟢": "🟡", "🟡": "🔴", "🔴": "🔴"}
    return downgrade[base_verdict], flags
```

## Appel depuis le skill `/new-strategy`

Dans la PHASE 4 du SKILL.md, remplacer la section "Analyse OOS approfondie"
par une simple invocation :

```python
from core.robustness import run_full_robustness, format_summary_markdown
results = run_full_robustness(oos_df, n_strategies_tested=N, topstep_dd_remaining=DD)
print(format_summary_markdown(results))
```

→ le bloc `⚙️ ANALYSE OPTIMISATION` se rédige à partir de ces résultats
sans avoir à réimplémenter chaque test.

## Dépendances

`core/robustness.py` importe uniquement :
- `numpy`
- `pandas`
- `scipy.stats` (déjà dans tes dépendances pour ATR/ADX)
- `math` (stdlib)

Aucune nouvelle dépendance à installer.

## Tests

Le module a un `if __name__ == "__main__"` qui le rend exécutable :

```bash
python -m core.robustness
```

Sortie attendue : auto-test sur 200 trades synthétiques, tous les blocs
de robustesse affichés en markdown + preview Telegram.

## À ajouter à `config.py` si pas déjà présent

```python
# ── Frictions de marché ──────────────────────────────────────────────
SLIPPAGE_TICKS_PER_TICKER = {"MES1": 1, "NQ1": 2, "YM1": 1}
COMMISSION_RT_PER_CONTRACT = 1.40   # $/contrat round-trip

# ── Calendrier macro ─────────────────────────────────────────────────
# Format : ["YYYY-MM-DD", ...]
# À maintenir manuellement ou via script d'ingestion (cf. BLS, Fed, BLS JOLTS)
MACRO_EVENT_DATES = [
    # 2025
    "2025-10-08",  # FOMC minutes
    "2025-10-29",  # FOMC
    "2025-11-07",  # NFP
    # ... à compléter
]
```
