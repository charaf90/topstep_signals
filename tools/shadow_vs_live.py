#!/usr/bin/env python3
"""Compare le state du shadow runner à celui du live runner.

Lit `state/live_state.json` et `state/shadow_state.json`, agrège leurs
`placed_tags` et produit un rapport :
  - signaux communs (mêmes décisions)
  - signaux uniquement live (le shadow a manqué)
  - signaux uniquement shadow (le shadow a vu en plus — divergence ?)
  - écarts de paramètres sur signaux communs (entry / sl / tp / n_ct)

Usage :
    python tools/shadow_vs_live.py                 # rapport stdout
    python tools/shadow_vs_live.py --date 2026-05-25
    python tools/shadow_vs_live.py --json          # sortie machine-readable

Le shadow ne posant jamais d'ordres réels, on ne compare PAS le PnL
(le shadow aurait toujours 0). On compare ce qui aurait été DÉCIDÉ.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import LIVE_STATE_FILE, SHADOW_STATE_FILE  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# Modèles
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class TagComparison:
    """Comparaison d'un tag présent dans live et/ou shadow."""

    tag: str
    in_live: bool
    in_shadow: bool
    live_entry: float | None = None
    shadow_entry: float | None = None
    live_sl: float | None = None
    shadow_sl: float | None = None
    live_tp: float | None = None
    shadow_tp: float | None = None
    live_n_ct: int | None = None
    shadow_n_ct: int | None = None

    @property
    def has_diff(self) -> bool:
        if not (self.in_live and self.in_shadow):
            return True
        fields = [
            (self.live_entry, self.shadow_entry),
            (self.live_sl, self.shadow_sl),
            (self.live_tp, self.shadow_tp),
            (self.live_n_ct, self.shadow_n_ct),
        ]
        return any(a != b for a, b in fields)


@dataclass
class ComparisonReport:
    """Rapport agrégé."""

    date_filter: str | None = None
    common_tags: list[TagComparison] = field(default_factory=list)
    only_in_live: list[str] = field(default_factory=list)
    only_in_shadow: list[str] = field(default_factory=list)

    @property
    def n_diverging(self) -> int:
        return sum(1 for t in self.common_tags if t.has_diff)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_filter": self.date_filter,
            "n_common": len(self.common_tags),
            "n_diverging": self.n_diverging,
            "n_only_live": len(self.only_in_live),
            "n_only_shadow": len(self.only_in_shadow),
            "only_in_live": list(self.only_in_live),
            "only_in_shadow": list(self.only_in_shadow),
            "common_tags": [
                {
                    "tag": t.tag,
                    "has_diff": t.has_diff,
                    "live": {
                        "entry": t.live_entry,
                        "sl": t.live_sl,
                        "tp": t.live_tp,
                        "n_ct": t.live_n_ct,
                    },
                    "shadow": {
                        "entry": t.shadow_entry,
                        "sl": t.shadow_sl,
                        "tp": t.shadow_tp,
                        "n_ct": t.shadow_n_ct,
                    },
                }
                for t in self.common_tags
            ],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _filter_tags_by_date(tags: dict[str, Any], date_str: str | None) -> dict[str, Any]:
    """Filtre les tags dont placed_at ou fill_time tombe sur date_str."""
    if not date_str:
        return tags
    out = {}
    for tag, info in tags.items():
        ts = info.get("placed_at") or info.get("fill_time") or ""
        if ts.startswith(date_str):
            out[tag] = info
    return out


def compare(
    live_state: dict[str, Any],
    shadow_state: dict[str, Any],
    date_filter: str | None = None,
) -> ComparisonReport:
    """Compare les placed_tags des deux states. Retourne un ComparisonReport."""
    live_tags = _filter_tags_by_date(live_state.get("placed_tags", {}), date_filter)
    shadow_tags = _filter_tags_by_date(shadow_state.get("placed_tags", {}), date_filter)

    report = ComparisonReport(date_filter=date_filter)
    common = set(live_tags) & set(shadow_tags)
    only_live = set(live_tags) - set(shadow_tags)
    only_shadow = set(shadow_tags) - set(live_tags)

    for tag in sorted(common):
        live_info = live_tags[tag]
        shadow_info = shadow_tags[tag]
        report.common_tags.append(
            TagComparison(
                tag=tag,
                in_live=True,
                in_shadow=True,
                live_entry=live_info.get("entry"),
                shadow_entry=shadow_info.get("entry"),
                live_sl=live_info.get("sl"),
                shadow_sl=shadow_info.get("sl"),
                live_tp=live_info.get("tp"),
                shadow_tp=shadow_info.get("tp"),
                live_n_ct=live_info.get("n_ct"),
                shadow_n_ct=shadow_info.get("n_ct"),
            )
        )

    report.only_in_live = sorted(only_live)
    report.only_in_shadow = sorted(only_shadow)
    return report


def format_report_human(report: ComparisonReport) -> str:
    title = "SHADOW vs LIVE"
    if report.date_filter:
        title += f" — {report.date_filter}"

    lines = [
        "=" * 60,
        f"  {title}",
        "=" * 60,
        f"  Tags communs        : {len(report.common_tags)}",
        f"  Tags divergents     : {report.n_diverging}",
        f"  Uniquement live     : {len(report.only_in_live)}",
        f"  Uniquement shadow   : {len(report.only_in_shadow)}",
    ]

    if report.only_in_live:
        lines.append("\n  ▸ Présents uniquement côté LIVE (shadow a raté) :")
        for tag in report.only_in_live[:10]:
            lines.append(f"    • {tag}")
        if len(report.only_in_live) > 10:
            lines.append(f"    … +{len(report.only_in_live) - 10} autres")

    if report.only_in_shadow:
        lines.append("\n  ▸ Présents uniquement côté SHADOW (live a raté) :")
        for tag in report.only_in_shadow[:10]:
            lines.append(f"    • {tag}")
        if len(report.only_in_shadow) > 10:
            lines.append(f"    … +{len(report.only_in_shadow) - 10} autres")

    diverging = [t for t in report.common_tags if t.has_diff]
    if diverging:
        lines.append("\n  ▸ Divergences sur tags communs (entry/sl/tp/n_ct) :")
        for t in diverging[:10]:
            lines.append(
                f"    • {t.tag}\n"
                f"      live   : entry={t.live_entry} sl={t.live_sl} tp={t.live_tp} n_ct={t.live_n_ct}\n"
                f"      shadow : entry={t.shadow_entry} sl={t.shadow_sl} tp={t.shadow_tp} n_ct={t.shadow_n_ct}"
            )
        if len(diverging) > 10:
            lines.append(f"    … +{len(diverging) - 10} autres")

    lines.append("")
    is_aligned = report.n_diverging == 0 and not report.only_in_live and not report.only_in_shadow
    lines.append(f"  {'✅ SHADOW ALIGNÉ AU LIVE' if is_aligned else '⚠️  DIVERGENCE DÉTECTÉE'}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare shadow_state vs live_state")
    parser.add_argument(
        "--live",
        default=LIVE_STATE_FILE,
        help=f"State live (défaut : {LIVE_STATE_FILE})",
    )
    parser.add_argument(
        "--shadow",
        default=SHADOW_STATE_FILE,
        help=f"State shadow (défaut : {SHADOW_STATE_FILE})",
    )
    parser.add_argument("--date", default=None, help="Filtre par date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    args = parser.parse_args()

    live = _load_state(args.live)
    shadow = _load_state(args.shadow)

    if not live and not shadow:
        print("⚠️  Aucun state trouvé (live ni shadow).", file=sys.stderr)
        return 2

    report = compare(live, shadow, date_filter=args.date)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_report_human(report))

    # Exit code 0 = aligné, 1 = divergence
    is_aligned = report.n_diverging == 0 and not report.only_in_live and not report.only_in_shadow
    return 0 if is_aligned else 1


if __name__ == "__main__":
    sys.exit(main())
