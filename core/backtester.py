"""
Runner universel : exécute n'importe quelle stratégie du dossier strategies/.

Contrat attendu de chaque module stratégie :
    STRATEGY_ID : str                    — tag de version (ex. "opr-v4")
    TICKERS     : list[str]              — actifs supportés
    run_backtest(df_15m, ticker,
                 tf=None, params=None,
                 topstep_guard=True) -> pd.DataFrame

Optionnel (chart) :
    plot_day(df_15m, ticker, date_str,
             day_trades, output_path)    — chart d'une journée

Charts portfolio (réutilisables par toutes les stratégies) :
    plot_equity_curve(trades_df, output_path, is_end_date)
    plot_drawdown_underwater(trades_df, output_path)
    plot_monthly_heatmap(trades_df, output_path)
    plot_hourly_distribution(trades_df, output_path)
    plot_correlation_rolling(trades_df, ref_dfs, output_path, window)
    generate_portfolio_charts(trades_df, strategy_id, output_dir, ref_dfs)
"""

import random
from pathlib import Path

import pandas as pd

from core import metrics as m

# ══════════════════════════════════════════════════════════════════════════════
# Runner principal
# ══════════════════════════════════════════════════════════════════════════════


def run_for_ticker(
    strategy,
    df_15m: pd.DataFrame,
    ticker: str,
    tf: dict | None = None,
    params: dict | None = None,
    topstep_guard: bool = True,
    plot: bool = False,
    n_sample_charts: int = 10,
    output_dir: Path | None = None,
    verbose: bool = True,
) -> dict:
    """
    Exécute une stratégie sur un ticker et retourne ses métriques.

    Args:
        strategy        : module strategies/xxx.py
        df_15m          : DataFrame OHLCV 15m
        ticker          : "MES1" | "NQ1" | "YM1"
        tf              : dict de timeframes pré-construits (optionnel)
        params          : dict de params pour override config (optimisation)
        topstep_guard   : activer le garde-fou Topstep dans la boucle jour
        plot            : générer des graphiques sur N jours aléatoires
        n_sample_charts : nombre de jours à tracer (défaut 10)
        output_dir      : répertoire de sortie (CSV + charts)
        verbose         : afficher le rapport

    Returns:
        {"df_trades": pd.DataFrame, "stats": dict, "topstep": dict,
         "ticker": str, "strategy_id": str}
    """
    strategy_id = getattr(strategy, "STRATEGY_ID", "unknown")

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"  BACKTEST — {ticker}  [{strategy_id}]")
        print(f"{'=' * 60}")
        print(f"  {len(df_15m):,} bougies [{df_15m.index.min()} → {df_15m.index.max()}]")

    df_trades = strategy.run_backtest(
        df_15m, ticker, tf=tf, params=params, topstep_guard=topstep_guard
    )

    stats = m.compute_stats(df_trades)
    topstep = m.compute_topstep(df_trades, n_bootstrap=1000)

    if verbose:
        m.print_stats_report(stats, ticker, strategy_id)
        m.print_topstep_report(topstep, ticker)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = getattr(strategy, "CSV_SUFFIX", f"_{strategy_id.replace('-', '_')}")
        csv_path = output_dir / f"backtest_{ticker}{suffix}.csv"
        df_trades.to_csv(csv_path, index=False)
        if verbose:
            print(f"  ✓ {csv_path}")

    if plot and len(df_trades) > 0:
        _generate_sample_charts(
            strategy,
            df_15m,
            ticker,
            df_trades,
            output_dir=output_dir,
            n=n_sample_charts,
            verbose=verbose,
        )

    return {
        "df_trades": df_trades,
        "stats": stats,
        "topstep": topstep,
        "ticker": ticker,
        "strategy_id": strategy_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Charts sur N jours aléatoires
# ══════════════════════════════════════════════════════════════════════════════


def _generate_sample_charts(
    strategy,
    df_15m: pd.DataFrame,
    ticker: str,
    df_trades: pd.DataFrame,
    output_dir: Path | None,
    n: int = 10,
    verbose: bool = True,
):
    """Génère des charts sur N jours tirés aléatoirement parmi les jours avec fills."""
    if not hasattr(strategy, "plot_day"):
        return

    filled = (
        df_trades[df_trades["result"] != "NOT_FILLED"]
        if "result" in df_trades.columns
        else df_trades
    )
    if len(filled) == 0:
        return

    filled_dates = sorted(filled["date"].unique())
    sample_dates = random.sample(filled_dates, min(n, len(filled_dates)))
    sample_dates.sort()

    chart_dir = (output_dir or Path("output")) / "charts" / ticker
    chart_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"\n  ▸ Génération de {len(sample_dates)} chart(s) [{ticker}] → {chart_dir}")

    for date_str in sample_dates:
        day_trades = df_trades[df_trades["date"] == date_str].to_dict("records")
        out_path = str(chart_dir / f"{date_str}.png")
        try:
            strategy.plot_day(df_15m, ticker, date_str, day_trades, out_path)
        except Exception as e:
            if verbose:
                print(f"  [!] chart {date_str}: {e}")

    if verbose:
        print(f"  ✓ {len(sample_dates)} chart(s) → {chart_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# Charts portfolio (mutualisés — utilisables par toutes les stratégies)
# ══════════════════════════════════════════════════════════════════════════════


def _filled(trades_df: pd.DataFrame) -> pd.DataFrame:
    if trades_df is None or len(trades_df) == 0:
        return pd.DataFrame()
    if "result" in trades_df.columns:
        return trades_df[trades_df["result"] != "NOT_FILLED"].copy()
    return trades_df.copy()


def plot_equity_curve(
    trades_df: pd.DataFrame,
    output_path: str,
    is_end_date: str | None = "2025-09-30",
    title: str | None = None,
):
    """Equity cumulée portefeuille avec marquage IS/OOS, par ticker."""
    import matplotlib.pyplot as plt

    df = _filled(trades_df)
    if df.empty:
        return
    df = df.sort_values("date").reset_index(drop=True)
    df["cum_pnl"] = df["pnl"].cumsum()

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(
        pd.to_datetime(df["date"]), df["cum_pnl"], color="#26a69a", lw=1.6, label="Equity portfolio"
    )
    ax.fill_between(
        pd.to_datetime(df["date"]),
        0,
        df["cum_pnl"],
        where=df["cum_pnl"] > 0,
        alpha=0.15,
        color="#26a69a",
    )
    ax.fill_between(
        pd.to_datetime(df["date"]),
        0,
        df["cum_pnl"],
        where=df["cum_pnl"] <= 0,
        alpha=0.15,
        color="#ef5350",
    )

    if "ticker" in df.columns:
        for tk, grp in df.groupby("ticker"):
            grp = grp.sort_values("date")
            ax.plot(
                pd.to_datetime(grp["date"]), grp["pnl"].cumsum(), lw=0.9, alpha=0.55, label=f"{tk}"
            )

    if is_end_date:
        ax.axvline(
            pd.to_datetime(is_end_date),
            color="#ffb74d",
            ls="--",
            lw=1,
            label=f"IS/OOS @ {is_end_date}",
        )

    ax.axhline(0, color="#787b86", lw=0.6)
    ax.set_title(title or "Equity curve cumulée", fontsize=12, fontweight="bold")
    ax.set_ylabel("P&L cumulé ($)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.patch.set_facecolor("#13131f")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    for s in ax.spines.values():
        s.set_color("#787b86")
    ax.title.set_color("white")
    ax.yaxis.label.set_color("white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_drawdown_underwater(trades_df: pd.DataFrame, output_path: str):
    """Drawdown underwater du portefeuille (en $)."""
    import matplotlib.pyplot as plt

    df = _filled(trades_df)
    if df.empty:
        return
    df = df.sort_values("date").reset_index(drop=True)
    cum = df["pnl"].cumsum()
    peak = cum.cummax()
    dd = cum - peak

    fig, ax = plt.subplots(figsize=(13, 4.5))
    ax.fill_between(
        pd.to_datetime(df["date"]), dd, 0, color="#ef5350", alpha=0.45, label="Drawdown"
    )
    ax.plot(pd.to_datetime(df["date"]), dd, color="#ef5350", lw=0.8)
    ax.axhline(-2000, color="#ffb74d", ls="--", lw=1, label="Trailing DD Topstep (-$2 000)")
    ax.axhline(0, color="#787b86", lw=0.6)
    ax.set_title(
        f"Drawdown underwater — DD max ${float(dd.min()):,.0f}", fontsize=12, fontweight="bold"
    )
    ax.set_ylabel("DD ($)")
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.patch.set_facecolor("#13131f")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    for s in ax.spines.values():
        s.set_color("#787b86")
    ax.title.set_color("white")
    ax.yaxis.label.set_color("white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_monthly_heatmap(trades_df: pd.DataFrame, output_path: str):
    """P&L mensuel par actif (heatmap)."""
    import matplotlib.pyplot as plt

    df = _filled(trades_df)
    if df.empty or "ticker" not in df.columns:
        return
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M").astype(str)
    pivot = df.groupby(["month", "ticker"])["pnl"].sum().unstack(fill_value=0.0).sort_index()

    fig, ax = plt.subplots(figsize=(12, max(3.5, 0.5 * len(pivot))))
    vmax = max(abs(pivot.values.min()), abs(pivot.values.max()), 1.0)
    im = ax.imshow(pivot.values, aspect="auto", cmap="RdYlGn", vmin=-vmax, vmax=vmax)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)

    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.values[i, j]
            ax.text(
                j,
                i,
                f"{v:+.0f}",
                ha="center",
                va="center",
                color="black" if abs(v) < vmax * 0.4 else "white",
                fontsize=8,
            )

    ax.set_title("P&L mensuel par actif ($)", fontsize=12, fontweight="bold")
    fig.colorbar(im, ax=ax, label="P&L net ($)")
    fig.patch.set_facecolor("#13131f")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    ax.title.set_color("white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_hourly_distribution(
    trades_df: pd.DataFrame, output_path: str, hour_col: str = "fill_time", tz_ny: bool = True
):
    """Distribution horaire des fills (en heure NY si tz_ny)."""
    from zoneinfo import ZoneInfo

    import matplotlib.pyplot as plt

    df = _filled(trades_df)
    if df.empty or hour_col not in df.columns:
        return
    # utc=True normalise les timezones mélangés (et tz-naive ↦ UTC)
    ts = pd.to_datetime(df[hour_col], errors="coerce", utc=True)
    if ts.isna().all():
        return
    if tz_ny:
        hours = ts.dt.tz_convert(ZoneInfo("America/New_York")).dt.hour
    else:
        hours = ts.dt.hour
    hours = hours.dropna().astype(int)
    if len(hours) == 0:
        return

    fig, ax = plt.subplots(figsize=(11, 4.5))
    counts = hours.value_counts().sort_index()
    ax.bar(counts.index, counts.values, color="#26a69a", alpha=0.85)
    ax.set_xlabel("Heure NY")
    ax.set_ylabel("Nombre de fills")
    ax.set_xticks(range(0, 24))
    ax.set_title(f"Distribution horaire des fills — n={len(hours)}", fontsize=12, fontweight="bold")
    ax.grid(alpha=0.25)
    fig.patch.set_facecolor("#13131f")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    for s in ax.spines.values():
        s.set_color("#787b86")
    ax.title.set_color("white")
    ax.xaxis.label.set_color("white")
    ax.yaxis.label.set_color("white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_correlation_rolling(
    trades_df: pd.DataFrame,
    ref_strategies_dfs: dict[str, pd.DataFrame],
    output_path: str,
    window: int = 60,
):
    """
    Corrélation Spearman rolling (window jours) du P&L daily de la stratégie
    courante vs chaque stratégie de référence.
    """
    import matplotlib.pyplot as plt

    def daily_pnl(df: pd.DataFrame) -> pd.Series:
        f = _filled(df)
        if f.empty or "date" not in f.columns:
            return pd.Series(dtype=float)
        return f.groupby("date")["pnl"].sum().sort_index()

    base = daily_pnl(trades_df)
    if len(base) < window:
        return

    fig, ax = plt.subplots(figsize=(13, 4.5))
    base.index = pd.to_datetime(base.index)
    for name, ref in ref_strategies_dfs.items():
        ref_daily = daily_pnl(ref)
        if ref_daily.empty:
            continue
        ref_daily.index = pd.to_datetime(ref_daily.index)
        joined = pd.concat([base.rename("a"), ref_daily.rename("b")], axis=1).fillna(0.0)
        if len(joined) < window:
            continue
        roll = joined["a"].rolling(window).corr(joined["b"])
        ax.plot(roll.index, roll.values, lw=1.2, label=f"vs {name}")

    ax.axhline(0, color="#787b86", lw=0.6)
    ax.axhline(0.5, color="#ffb74d", ls="--", lw=0.8, label="Seuil 0.5")
    ax.set_ylim(-1.05, 1.05)
    ax.set_ylabel("Corrélation Pearson")
    ax.set_title(f"Corrélation rolling {window}j daily P&L", fontsize=12, fontweight="bold")
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.patch.set_facecolor("#13131f")
    ax.set_facecolor("#1a1a2e")
    ax.tick_params(colors="white")
    for s in ax.spines.values():
        s.set_color("#787b86")
    ax.title.set_color("white")
    ax.yaxis.label.set_color("white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def generate_portfolio_charts(
    trades_df: pd.DataFrame,
    strategy_id: str,
    output_dir: Path,
    ref_strategies_dfs: dict[str, pd.DataFrame] | None = None,
    is_end_date: str | None = "2025-09-30",
) -> dict[str, str]:
    """
    Génère les 4-5 charts portefeuille standard pour une stratégie.

    Retourne un dict {nom_chart: chemin_fichier}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    eq = output_dir / f"equity_curve_{strategy_id}.png"
    plot_equity_curve(
        trades_df, str(eq), is_end_date=is_end_date, title=f"Equity curve — {strategy_id}"
    )
    paths["equity"] = str(eq)

    dd = output_dir / f"drawdown_underwater_{strategy_id}.png"
    plot_drawdown_underwater(trades_df, str(dd))
    paths["drawdown"] = str(dd)

    heat = output_dir / f"monthly_heatmap_{strategy_id}.png"
    plot_monthly_heatmap(trades_df, str(heat))
    paths["heatmap"] = str(heat)

    hourly = output_dir / f"hourly_distribution_{strategy_id}.png"
    plot_hourly_distribution(trades_df, str(hourly))
    paths["hourly"] = str(hourly)

    if ref_strategies_dfs:
        corr = output_dir / f"correlation_rolling_{strategy_id}.png"
        plot_correlation_rolling(trades_df, ref_strategies_dfs, str(corr))
        paths["correlation"] = str(corr)

    return paths
