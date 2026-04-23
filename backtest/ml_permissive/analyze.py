"""
Analyze ML-permissive backtest results.

Produces:
  - Overall headline stats (bets, win rate, ROI, bankroll growth)
  - Breakdown by odds bucket (heavy fav / strong fav / mild fav / dogs)
  - Breakdown by edge bucket (6-8, 8-10, 10-15, 15%+)
  - Breakdown by raw probability bucket (for favorites)

Usage:
  python backtest/ml_permissive/analyze.py
"""
import argparse
import os
import sys

import pandas as pd


HERE = os.path.dirname(os.path.abspath(__file__))


def load_combined(directory=None):
    base = directory or HERE
    csvs = [os.path.join(base, "results_2024.csv"),
            os.path.join(base, "results_2025.csv")]
    frames = []
    for p in csvs:
        if os.path.exists(p):
            df = pd.read_csv(p)
            df["season"] = os.path.basename(p).replace("results_", "").replace(".csv", "")
            frames.append(df)
        else:
            print(f"MISSING: {p}")
    if not frames:
        print(f"No result CSVs found in {base}")
        sys.exit(1)
    return pd.concat(frames, ignore_index=True)


def summarize(df, label):
    if df.empty:
        print(f"  {label:<30} 0 bets")
        return
    bets     = len(df)
    wagered  = df["bet_size"].sum()
    profit   = df["profit"].sum()
    wins     = int(df["won"].sum())
    wr       = wins / bets * 100 if bets else 0
    roi      = profit / wagered * 100 if wagered else 0
    avg_bet  = wagered / bets if bets else 0
    print(f"  {label:<30} bets={bets:>4}  WR={wr:5.1f}%  "
          f"wagered=${wagered:>9,.0f}  profit=${profit:>+9,.0f}  "
          f"ROI={roi:>+6.2f}%  avg_bet=${avg_bet:>6.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE, help="Directory containing results_2024.csv and results_2025.csv")
    ap.add_argument("--label", default="ML-PERMISSIVE (raw probs)", help="Label printed at top of report")
    args = ap.parse_args()

    df = load_combined(args.dir)
    df = df[df["bet_type"] == "moneyline"].copy()

    print("=" * 100)
    print(f"{args.label} — All moneyline bets with edge >= 6%, Kelly 0.25")
    print("  (no odds-range filter, no raw-prob floor, no max-edge cap)")
    print("=" * 100)

    # Overall
    print("\nOVERALL")
    summarize(df, "Combined 2024+2025")

    # By season
    print("\nBY SEASON")
    for season, g in df.groupby("season"):
        summarize(g, season)

    # By fav/dog
    print("\nBY SIDE")
    summarize(df[df["odds"] < 0], "Favorites (negative odds)")
    summarize(df[df["odds"] > 0], "Underdogs (positive odds)")

    # By market-implied probability bucket (5-point bands)
    # implied_prob is the market's win probability for the side we bet on.
    # A 40% implied means underdogs; a 60% implied means favorites.
    print("\nBY MARKET-IMPLIED PROBABILITY BUCKET (the side we bet on)")
    print("  (= market's win probability implied by the line we took, in 5-point bands)")
    imp_buckets = [
        ("≤ 25%           (+300 or longer)",         df[df["implied_prob"] < 0.25]),
        ("25-30%          (+234 to +299)",           df[(df["implied_prob"] >= 0.25) & (df["implied_prob"] < 0.30)]),
        ("30-35%          (+191 to +233)",           df[(df["implied_prob"] >= 0.30) & (df["implied_prob"] < 0.35)]),
        ("35-40%          (+151 to +190)",           df[(df["implied_prob"] >= 0.35) & (df["implied_prob"] < 0.40)]),
        ("40-45%          (+123 to +150)",           df[(df["implied_prob"] >= 0.40) & (df["implied_prob"] < 0.45)]),
        ("45-50%          (+101 to +122)",           df[(df["implied_prob"] >= 0.45) & (df["implied_prob"] < 0.50)]),
        ("50-55%          (-101 to -122)",           df[(df["implied_prob"] >= 0.50) & (df["implied_prob"] < 0.55)]),
        ("55-60%          (-123 to -149)",           df[(df["implied_prob"] >= 0.55) & (df["implied_prob"] < 0.60)]),
        ("60-65%          (-150 to -185)",           df[(df["implied_prob"] >= 0.60) & (df["implied_prob"] < 0.65)]),
        ("65-70%          (-186 to -233)",           df[(df["implied_prob"] >= 0.65) & (df["implied_prob"] < 0.70)]),
        ("70-75%          (-234 to -300)",           df[(df["implied_prob"] >= 0.70) & (df["implied_prob"] < 0.75)]),
        ("≥ 75%           (-301 or shorter)",        df[df["implied_prob"] >= 0.75]),
    ]
    for label, g in imp_buckets:
        summarize(g, label)

    # By edge bucket (1-point bins up to 15%, then wider for small-sample tail)
    print("\nBY EDGE BUCKET (1-point bins)")
    edge_buckets = [
        ("6-7%",    df[(df["edge_pct"] >= 6)  & (df["edge_pct"] < 7)]),
        ("7-8%",    df[(df["edge_pct"] >= 7)  & (df["edge_pct"] < 8)]),
        ("8-9%",    df[(df["edge_pct"] >= 8)  & (df["edge_pct"] < 9)]),
        ("9-10%",   df[(df["edge_pct"] >= 9)  & (df["edge_pct"] < 10)]),
        ("10-11%",  df[(df["edge_pct"] >= 10) & (df["edge_pct"] < 11)]),
        ("11-12%",  df[(df["edge_pct"] >= 11) & (df["edge_pct"] < 12)]),
        ("12-13%",  df[(df["edge_pct"] >= 12) & (df["edge_pct"] < 13)]),
        ("13-14%",  df[(df["edge_pct"] >= 13) & (df["edge_pct"] < 14)]),
        ("14-15%",  df[(df["edge_pct"] >= 14) & (df["edge_pct"] < 15)]),
        ("15-17%",  df[(df["edge_pct"] >= 15) & (df["edge_pct"] < 17)]),
        ("17-20%",  df[(df["edge_pct"] >= 17) & (df["edge_pct"] < 20)]),
        ("20-25%",  df[(df["edge_pct"] >= 20) & (df["edge_pct"] < 25)]),
        ("25-30%",  df[(df["edge_pct"] >= 25) & (df["edge_pct"] < 30)]),
        ("30%+",    df[df["edge_pct"] >= 30]),
    ]
    for label, g in edge_buckets:
        summarize(g, label)

    # By raw prob (favorites only)
    print("\nBY RAW PROBABILITY (favorites only)")
    favs = df[df["odds"] < 0]
    prob_buckets = [
        ("50-55%", favs[(favs["our_prob"] >= 0.50) & (favs["our_prob"] < 0.55)]),
        ("55-60%", favs[(favs["our_prob"] >= 0.55) & (favs["our_prob"] < 0.60)]),
        ("60-65%", favs[(favs["our_prob"] >= 0.60) & (favs["our_prob"] < 0.65)]),
        ("65-70%", favs[(favs["our_prob"] >= 0.65) & (favs["our_prob"] < 0.70)]),
        ("70-75%", favs[(favs["our_prob"] >= 0.70) & (favs["our_prob"] < 0.75)]),
        ("75%+",   favs[favs["our_prob"] >= 0.75]),
    ]
    for label, g in prob_buckets:
        summarize(g, label)

    # Cross-tab: market-implied probability × edge band
    print("\nCROSS-TAB: ROI by market-implied prob × edge band")
    print("  (each cell shows: bets / ROI%. '--' = 0 bets)")
    print()

    # Row buckets — market-implied probability (the line we took)
    row_buckets = [
        ("<35%",    (0.00, 0.35)),
        ("35-40%",  (0.35, 0.40)),
        ("40-45%",  (0.40, 0.45)),
        ("45-50%",  (0.45, 0.50)),
        ("50-55%",  (0.50, 0.55)),
        ("55-60%",  (0.55, 0.60)),
        ("60-65%",  (0.60, 0.65)),
        ("65%+",    (0.65, 1.01)),
    ]
    # Column buckets — edge band
    col_buckets = [
        ("6-8%",   (6,  8)),
        ("8-10%",  (8, 10)),
        ("10-15%", (10, 15)),
        ("15-25%", (15, 25)),
        ("25%+",   (25, 100)),
    ]

    # Print header
    header_label = "implied / edge"
    col_header = "  " + f"{header_label:<14}" + "".join(f"{c[0]:>16}" for c in col_buckets) + f"{'ROW TOTAL':>18}"
    print(col_header)
    print("  " + "-" * (len(col_header) - 2))

    for row_label, (p_lo, p_hi) in row_buckets:
        row_df = df[(df["implied_prob"] >= p_lo) & (df["implied_prob"] < p_hi)]
        line = f"  {row_label:<14}"
        for _, (e_lo, e_hi) in col_buckets:
            cell = row_df[(row_df["edge_pct"] >= e_lo) & (row_df["edge_pct"] < e_hi)]
            if cell.empty:
                line += f"{'--':>16}"
            else:
                wagered = cell["bet_size"].sum()
                profit  = cell["profit"].sum()
                roi     = profit / wagered * 100 if wagered else 0
                line += f"{len(cell):>4}  {roi:>+6.1f}%{'':>3}"
        # Row total
        if row_df.empty:
            line += f"{'--':>18}"
        else:
            w = row_df["bet_size"].sum()
            p = row_df["profit"].sum()
            r = p / w * 100 if w else 0
            line += f"{len(row_df):>6}  {r:>+6.1f}%{'':>3}"
        print(line)

    # Column totals
    print("  " + "-" * (len(col_header) - 2))
    line = f"  {'COL TOTAL':<14}"
    for _, (e_lo, e_hi) in col_buckets:
        col = df[(df["edge_pct"] >= e_lo) & (df["edge_pct"] < e_hi)]
        if col.empty:
            line += f"{'--':>16}"
        else:
            w = col["bet_size"].sum()
            p = col["profit"].sum()
            r = p / w * 100 if w else 0
            line += f"{len(col):>4}  {r:>+6.1f}%{'':>3}"
    # Grand total
    w = df["bet_size"].sum()
    p = df["profit"].sum()
    r = p / w * 100 if w else 0
    line += f"{len(df):>6}  {r:>+6.1f}%{'':>3}"
    print(line)

    # Bankroll trajectory
    print("\nBANKROLL TRAJECTORY")
    df_sorted = df.sort_values(["date"]).reset_index(drop=True)
    if not df_sorted.empty:
        start_br = 1000.0
        end_br = start_br + df_sorted["profit"].sum()
        peak_br = start_br
        trough_br = start_br
        running = start_br
        for _, row in df_sorted.iterrows():
            running += row["profit"]
            peak_br = max(peak_br, running)
            trough_br = min(trough_br, running)
        print(f"  Start:  ${start_br:>10,.2f}")
        print(f"  Peak:   ${peak_br:>10,.2f}")
        print(f"  Trough: ${trough_br:>10,.2f}")
        print(f"  End:    ${end_br:>10,.2f}")
        print(f"  Growth: {(end_br - start_br) / start_br * 100:+.1f}%")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
