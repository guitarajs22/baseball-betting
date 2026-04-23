"""
Side-by-side comparison: seeded standard vs scoped-bypass backtest.

Both runs use the same --seed, so every game gets the identical sim output.
The only remaining difference between the runs is the calibration path:
  - standard       → calibrate_prob (production)
  - scoped-bypass  → calibrate_prob_scoped (favorites-only away bypass)

Usage: python backtest/ml_permissive_cal_seeded/compare.py
"""
import os
import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STD_DIR = os.path.join(ROOT, "ml_permissive_cal_seeded", "standard")
SCP_DIR = os.path.join(ROOT, "ml_permissive_cal_seeded", "scoped_bypass")


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
        return {"bets": 0, "wr": 0, "wager": 0, "profit": 0, "roi": 0}
    bets = len(g)
    wins = int(g["won"].sum())
    wager = g["bet_size"].sum()
    profit = g["profit"].sum()
    return {
        "bets":   bets,
        "wr":     wins / bets * 100,
        "wager":  wager,
        "profit": profit,
        "roi":    profit / wager * 100 if wager else 0,
    }


def _row(label, s_std, s_scp):
    def fmt(s):
        return (f"{s['bets']:>4} bets  WR={s['wr']:>5.1f}%  "
                f"ROI={s['roi']:>+6.2f}%  profit=${s['profit']:>+7,.0f}")
    delta_bets = s_scp['bets'] - s_std['bets']
    delta_roi  = s_scp['roi']  - s_std['roi']
    return (f"  {label:<38}  std: {fmt(s_std)}   |   "
            f"scoped: {fmt(s_scp)}   (Δ bets {delta_bets:+d}, Δ ROI {delta_roi:+.2f}pp)")


def main():
    std = _load(STD_DIR)
    scp = _load(SCP_DIR)

    if std is None:
        print(f"Standard results missing in {STD_DIR}")
        return
    if scp is None:
        print(f"Scoped-bypass results missing in {SCP_DIR}")
        return

    print("=" * 160)
    print("SEEDED head-to-head — STANDARD vs SCOPED-BYPASS (edge ≥ 6%, Kelly 0.25)")
    print("Same seed on both runs → identical sim outputs → differences are PURELY from calibration.")
    print("=" * 160)

    print(_row("OVERALL (combined 2024+2025)", _stats(std), _stats(scp)))

    for yr in ["2024", "2025"]:
        f = lambda d, y=yr: d["date"].str.startswith(y)
        print(_row(f"  {yr}", _stats(std, f), _stats(scp, f)))

    print()
    print("BY SIDE")
    for lbl, f in [
        ("Away favorites (target of fix)", lambda d: (d["side"] == "away") & (d["odds"] < 0)),
        ("Away underdogs (side-effect check)", lambda d: (d["side"] == "away") & (d["odds"] > 0)),
        ("Home favorites (untouched)", lambda d: (d["side"] == "home") & (d["odds"] < 0)),
        ("Home underdogs (untouched)", lambda d: (d["side"] == "home") & (d["odds"] > 0)),
    ]:
        print(_row(lbl, _stats(std, f), _stats(scp, f)))

    print()
    print("AWAY FAVORITES by raw-probability band (the bypass target)")
    for lbl, lo, hi in [
        ("raw 50-55%", 0.50, 0.55),
        ("raw 55-60%", 0.55, 0.60),
        ("raw 60-65%", 0.60, 0.65),
        ("raw 65-70%", 0.65, 0.70),
        ("raw 70%+",   0.70, 1.01),
    ]:
        # NOTE: our_prob in the CSV is the OUTPUT of the chosen calibration
        # path, so we can't filter by raw prob directly from the CSV alone.
        # Approximate by filtering the two runs on our_prob instead. For the
        # standard run our_prob is calibrated; for scoped_bypass it's either
        # calibrated or raw depending on the bypass. We use it as a proxy.
        f = lambda d, lo=lo, hi=hi: (d["side"] == "away") & (d["odds"] < 0) & \
                                     (d["our_prob"] >= lo) & (d["our_prob"] < hi)
        print(_row(f"  {lbl}  (approx, via our_prob)", _stats(std, f), _stats(scp, f)))

    print("=" * 160)


if __name__ == "__main__":
    main()
