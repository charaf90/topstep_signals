"""
Rapport + heatmaps de l'analyse MFE/MAE SL/TP d'OPR (NQ1/YM1/MES1).

Lit les surfaces output/opr_sltp_<t>/surface_is_oos.csv produites par
scripts/opr_sltp_mfe_mae.py et génère :
  - output/opr_sltp_summary.md          (synthèse cross-ticker)
  - output/opr_sltp_<t>/heatmap.png     (surface E[$net/trade] IS|OOS, RR≥2)

Usage : python scripts/opr_sltp_report.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
TICKERS = ["NQ1", "YM1", "MES1"]


def heatmap(ax, piv, title, vlim):
    im = ax.imshow(
        piv.values,
        origin="lower",
        aspect="auto",
        cmap="RdYlGn",
        vmin=-vlim,
        vmax=vlim,
        extent=[piv.columns.min(), piv.columns.max(), piv.index.min(), piv.index.max()],
    )
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("TP (×ATR)")
    ax.set_ylabel("SL (×ATR)")
    return im


def build_ticker(t: str, md: list[str]):
    f = OUT / f"opr_sltp_{t.lower()}" / "surface_is_oos.csv"
    if not f.exists():
        return
    m = pd.read_csv(f)
    n_is = m[["n_tp_is", "n_sl_is", "n_te_is"]].sum(axis=1)
    n_oos = m[["n_tp_oos", "n_sl_oos", "n_te_oos"]].sum(axis=1)
    m["te_oos"] = m.n_te_oos / n_oos

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    vlim = float(np.nanpercentile(np.abs(m[["exp_net_is", "exp_net_oos"]].values), 95)) or 1.0
    for ax, col, lbl in [(axes[0], "exp_net_is", "IS"), (axes[1], "exp_net_oos", "OOS")]:
        piv = m.pivot(index="sl_atr", columns="tp_atr", values=col)
        im = heatmap(ax, piv, f"{t} — E[$net/trade] {lbl} (blanc = RR<2)", vlim)
    fig.colorbar(im, ax=axes, shrink=0.8, label="E[$net/trade]")
    fig.suptitle(f"OPR {t} — surface d'espérance SL/TP (analyse MFE/MAE M1, RR≥2)", fontsize=11)
    dest = OUT / f"opr_sltp_{t.lower()}" / "heatmap.png"
    fig.savefig(dest, dpi=110, bbox_inches="tight")
    plt.close(fig)

    # stats pour le markdown
    active = m[(m.te_oos < 0.70) & (m.n_te_is / n_is < 0.70)]
    good = active[
        (active.pf_is >= 1.3)
        & (active.pf_oos >= 1.3)
        & (active.exp_net_is > 0)
        & (active.exp_net_oos > 0)
    ]
    corr = m["exp_net_is"].corr(m["exp_net_oos"])
    md.append(f"\n### {t}\n")
    md.append(
        f"- Cellules à **stops actifs** (TE<70% IS&OOS) : {len(active)} ; "
        f"avec PF≥1.3 cohérent IS&OOS : **{len(good)}**"
    )
    md.append(f"- Corrélation E[$] IS↔OOS : {corr:+.2f}")
    md.append(f"- ![heatmap](opr_sltp_{t.lower()}/heatmap.png)")


def main():
    md = [
        "# OPR — Optimisation SL/TP data-driven (MFE/MAE sur M1)\n",
        "Analyse auto-cohérente sur M1 (fév 2025 → mars 2026), entrées OPR primaires "
        "(1er trigger/jour, SL/TP-neutre), trajectoires forward M1, espérance NETTE $/trade "
        "sous contrainte **RR = TP/SL ≥ 2**, walk-forward IS/OOS.\n",
        "## Verdict\n",
        "**Aucun edge RR≥2 de gestion SL/TP robuste** sur NQ1/YM1/MES1. Dans la région où "
        "les stops bindent réellement, 0 cellule tient PF≥1.3 cohérent IS↔OOS. Les seuls "
        "résultats positifs viennent du **coin dégénéré sans stop** (hold-to-EOD), "
        "notamment YM1 (edge directionnel, pas une gestion SL/TP).\n",
        "## Détail par ticker\n",
    ]
    for t in TICKERS:
        build_ticker(t, md)
    (OUT / "opr_sltp_summary.md").write_text("\n".join(md) + "\n")
    print("Écrit :", OUT / "opr_sltp_summary.md")
    for t in TICKERS:
        print("       ", OUT / f"opr_sltp_{t.lower()}" / "heatmap.png")


if __name__ == "__main__":
    main()
