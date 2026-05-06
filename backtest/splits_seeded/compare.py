"""
Side-by-side comparison: pitcher splits ON vs OFF.

Both runs use the same --seed, same calibration (standard), same edge floor.
The only difference is whether build_pitcher attaches vs_LHB / vs_RHB platoon
splits to the PitcherProfile, which then makes blend_rates pick the right
pitcher rates per-PA based on the batter's handedness.

Usage: python backtest/splits_seeded/compare.py
"""
import os
import pandas as pd


ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WITH_DIR = os.path.join(ROOT, "splits_seeded", "with_splits")
NO_DIR   = os.path.join(ROOT, "splits_seeded", "no_splits")


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


def _row(label, s_off, s_on):
    def fmt(s):
        return (f"{s['bets']:>4} bets  WR={s['wr']:>5.1f}%  "
                f"ROI={s['roi']:>+6.2f}%  profit=${s['profit']:>+7,.0f}")
    delta_bets = s_on['bets'] - s_off['bets']
    delta_roi  = s_on['roi']  - s_off['roi']
    delta_p    = s_on['profit'] - s_off['profit']
    return (f"  {label:<38}  no-splits: {fmt(s_off)}   |   "
            f"with-splits: {fmt(s_on)}   "
            f"(Δ bets {delta_bets:+d}, Δ ROI {delta_roi:+.2f}pp, Δ profit ${delta_p:+,.0f})")


def main():
    on  = _load(WITH_DIR)
    off = _load(NO_DIR)

    if on is None:
        print(f"with_splits results missing in {WITH_DIR}")
        return
    if off is None:
        print(f"no_splits results missing in {NO_DIR}")
        return

    print("=" * 175)
    print("SEEDED head-to-head — PITCHER SPLITS OFF vs ON  (edge ≥ 6%, Kelly 0.25, calibration=standard)")
    print("Same seed on both runs → identical batter rates and Monte Carlo trajectories.")
    print("Only difference: when ON, pitcher rates are split-adjusted by batter handedness per-PA")
    print("(vs_LHB or vs_RHB CSV rates, with switch hitters resolved to opposite of pitcher hand).")
    print("=" * 175)

    print(_row("OVERALL (combined 2024+2025)", _stats(off), _stats(on)))

    for yr in ["2024", "2025"]:
        f = lambda d, y=yr: d["date"].str.startswith(y)
        print(_row(f"  {yr}", _stats(off, f), _stats(on, f)))

    print()
    print("BY SIDE")
    for lbl, f in [
        ("Away favorites",   lambda d: (d["side"] == "away") & (d["odds"] < 0)),
        ("Away underdogs",   lambda d: (d["side"] == "away") & (d["odds"] > 0)),
        ("Home favorites",   lambda d: (d["side"] == "home") & (d["odds"] < 0)),
        ("Home underdogs",   lambda d: (d["side"] == "home") & (d["odds"] > 0)),
    ]:
        print(_row(lbl, _stats(off, f), _stats(on, f)))

    print()
    print("BY OUR-PROB BAND")
    for lbl, lo, hi in [
        ("raw 50-55%", 0.50, 0.55),
        ("raw 55-60%", 0.55, 0.60),
        ("raw 60-65%", 0.60, 0.65),
        ("raw 65-70%", 0.65, 0.70),
        ("raw 70%+",   0.70, 1.01),
    ]:
        f = lambda d, lo=lo, hi=hi: (d["our_prob"] >= lo) & (d["our_prob"] < hi)
        print(_row(f"  {lbl}", _stats(off, f), _stats(on, f)))

    # Bet flux: how many bets did splits add or remove?
    print()
    on_keys  = set(zip(on["date"], on["matchup"], on["side"]))
    off_keys = set(zip(off["date"], off["matchup"], off["side"]))
    print(f"Bet membership flux:")
    print(f"  bets in BOTH runs:        {len(on_keys & off_keys):>5}")
    print(f"  bets ONLY when splits ON: {len(on_keys - off_keys):>5}  (added by splits)")
    print(f"  bets ONLY when splits OFF:{len(off_keys - on_keys):>5}  (suppressed by splits)")

    print("=" * 175)


if __name__ == "__main__":
    main()
