"""
Side-by-side comparison: raw-probs vs calibrated-probs ML-permissive backtest.

Both runs used identical params:
  - edge >= 6%, Kelly 0.25
  - No odds-range, no raw-prob floor, no max-edge cap
  - Moneyline only

The ONLY difference: raw run uses simulation output directly; calibrated run
applies the calibration function that the live app uses.

Usage: python backtest/ml_permissive_cal/compare.py
"""
import os
import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "ml_permissive")
CAL_DIR = os.path.join(ROOT, "ml_permissive_cal")


def _load(directory):
    frames = []
    for p in [os.path.join(directory, "results_2024.csv"),
              os.path.join(directory, "results_2025.csv")]:
        if os.path.exists(p):
            frames.append(pd.read_csv(p))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    return df[df["bet_type"] == "moneyline"].copy()


def _stats(df, filt=None):
    g = df if filt is None else df[filt(df)]
    if g.empty:
        return {"bets": 0, "wr": 0, "wagered": 0, "profit": 0, "roi": 0}
    bets = len(g)
    wins = int(g["won"].sum())
    wager = g["bet_size"].sum()
    profit = g["profit"].sum()
    return {
        "bets":    bets,
        "wr":      wins / bets * 100,
        "wagered": wager,
        "profit":  profit,
        "roi":     profit / wager * 100 if wager else 0,
    }


def _row(label, raw_stats, cal_stats):
    return (f"  {label:<42} "
            f"raw: {raw_stats['bets']:>4} bets, {raw_stats['wr']:>5.1f}% WR, ROI {raw_stats['roi']:>+6.2f}%   |   "
            f"cal: {cal_stats['bets']:>4} bets, {cal_stats['wr']:>5.1f}% WR, ROI {cal_stats['roi']:>+6.2f}%")


def main():
    raw = _load(RAW_DIR)
    cal = _load(CAL_DIR)

    if raw is None:
        print(f"Raw results missing in {RAW_DIR}")
        return
    if cal is None:
        print(f"Calibrated results missing in {CAL_DIR}")
        return

    print("=" * 130)
    print("RAW-PROBS vs CALIBRATED — ML-permissive moneyline backtest (edge >= 6%, Kelly 0.25)")
    print("=" * 130)

    r = _stats(raw); c = _stats(cal)
    print(_row("OVERALL (combined 2024+2025)", r, c))

    for yr in ["2024", "2025"]:
        rr = _stats(raw, lambda d: d["season_year"] == yr) if "season_year" in raw.columns else None
        # fallback: split by date string prefix
        rr = _stats(raw, lambda d: d["date"].str.startswith(yr))
        cc = _stats(cal, lambda d: d["date"].str.startswith(yr))
        print(_row(f"  {yr}", rr, cc))

    print()
    print("BY SIDE")
    print(_row("  Favorites (odds < 0)",
               _stats(raw, lambda d: d["odds"] < 0),
               _stats(cal, lambda d: d["odds"] < 0)))
    print(_row("  Underdogs (odds > 0)",
               _stats(raw, lambda d: d["odds"] > 0),
               _stats(cal, lambda d: d["odds"] > 0)))

    print()
    print("BY MARKET-IMPLIED PROBABILITY (5-point bands)")
    bands = [
        ("<35%",   (0.00, 0.35)),
        ("35-40%", (0.35, 0.40)),
        ("40-45%", (0.40, 0.45)),
        ("45-50%", (0.45, 0.50)),
        ("50-55%", (0.50, 0.55)),
        ("55-60%", (0.55, 0.60)),
        ("60-65%", (0.60, 0.65)),
        ("65%+",   (0.65, 1.01)),
    ]
    for label, (lo, hi) in bands:
        rr = _stats(raw, lambda d: (d["implied_prob"] >= lo) & (d["implied_prob"] < hi))
        cc = _stats(cal, lambda d: (d["implied_prob"] >= lo) & (d["implied_prob"] < hi))
        print(_row(f"  {label}", rr, cc))

    print()
    print("BY EDGE BAND")
    ebands = [
        ("6-8%",   (6,  8)),
        ("8-10%",  (8, 10)),
        ("10-15%", (10, 15)),
        ("15-25%", (15, 25)),
        ("25%+",   (25, 200)),
    ]
    for label, (lo, hi) in ebands:
        rr = _stats(raw, lambda d: (d["edge_pct"] >= lo) & (d["edge_pct"] < hi))
        cc = _stats(cal, lambda d: (d["edge_pct"] >= lo) & (d["edge_pct"] < hi))
        print(_row(f"  {label}", rr, cc))

    print()
    print("CANDIDATE FILTER STRATEGIES (side-by-side)")
    strategies = [
        ("Everything (baseline)",
         lambda d: pd.Series([True] * len(d), index=d.index)),
        ("edge >= 10%",
         lambda d: d["edge_pct"] >= 10),
        ("40-65% implied & edge >= 10%",
         lambda d: (d["implied_prob"] >= 0.40) & (d["implied_prob"] < 0.65) & (d["edge_pct"] >= 10)),
        ("40-65% implied & edge >= 10%, drop 50-55% implied",
         lambda d: (d["implied_prob"] >= 0.40) & (d["implied_prob"] < 0.65) & (d["edge_pct"] >= 10) &
                   ~((d["implied_prob"] >= 0.50) & (d["implied_prob"] < 0.55))),
    ]
    for label, f in strategies:
        rr = _stats(raw, f)
        cc = _stats(cal, f)
        print(_row(f"  {label}", rr, cc))

    print("=" * 130)


if __name__ == "__main__":
    main()
