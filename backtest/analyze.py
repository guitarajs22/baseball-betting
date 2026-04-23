"""
Backtest Results Analyzer
=========================
Usage:
    python -m backtest.analyze backtest/results_2025_v6.csv
    python -m backtest.analyze backtest/results_2025_v6.csv --monthly
    python -m backtest.analyze backtest/results_2025_v6.csv --compare backtest/results_2025_v5.csv
"""

import csv
import sys
import argparse
from collections import defaultdict


# ── Helpers ───────────────────────────────────────────────────────────────────

def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def pct(n, d):
    return f"{n/d*100:.1f}%" if d else "—"


def roi(pnl, wag):
    return f"{pnl/wag*100:+.1f}%" if wag else "—"


def summary_line(rows, label=""):
    if not rows:
        return f"  {label or 'No bets'}"
    won   = sum(1 for r in rows if r["won"] == "True")
    lost  = sum(1 for r in rows if r["won"] == "False")
    push  = sum(1 for r in rows if r["won"] not in ("True", "False"))
    wag   = sum(float(r["bet_size"]) for r in rows)
    pnl   = sum(float(r["profit"])   for r in rows)
    wr    = pct(won, won + lost)
    r_str = roi(pnl, wag)
    tag   = f"  {label:35s}" if label else "  "
    return f"{tag}{len(rows):4d} bets | {won}W/{lost}L | {wr:>6} WR | wagered ${wag:>7,.0f} | P&L ${pnl:>+8,.2f} | ROI {r_str}"


def bucket_table(rows, key_fn, label_fn, title):
    buckets = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append(r)
    if not buckets:
        print(f"  (no data)")
        return
    print(f"\n{title}")
    print(f"  {'Bucket':<18} {'Bets':>5} {'W/L':>9} {'WR':>7} {'Wagered':>10} {'P&L':>10} {'ROI':>8}")
    print(f"  {'-'*75}")
    for k in sorted(buckets):
        rows_b = buckets[k]
        won  = sum(1 for r in rows_b if r["won"] == "True")
        lost = sum(1 for r in rows_b if r["won"] == "False")
        wag  = sum(float(r["bet_size"]) for r in rows_b)
        pnl  = sum(float(r["profit"])   for r in rows_b)
        wr   = pct(won, won + lost)
        r_s  = roi(pnl, wag)
        flag = " ✓" if pnl > 0 else " ✗"
        print(f"  {label_fn(k):<18} {len(rows_b):>5} {won}W/{lost:<4}L {wr:>7} ${wag:>9,.0f} ${pnl:>+9,.2f} {r_s:>8}{flag}")


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze(path, monthly=False, compare_path=None):
    rows = load(path)
    if not rows:
        print("No rows found.")
        return

    favorites  = [r for r in rows if r.get("is_favorite") == "True"]
    underdogs  = [r for r in rows if r.get("is_underdog")  == "True"]
    totals_ov  = [r for r in rows if r["bet_type"] == "totals" and r["side"].startswith("over")]
    totals_un  = [r for r in rows if r["bet_type"] == "totals" and r["side"].startswith("under")]
    totals_all = [r for r in rows if r["bet_type"] == "totals"]

    start = rows[0]["date"]
    end   = rows[-1]["date"]
    end_bk = float(rows[-1]["bankroll"])
    net    = end_bk - 1000.0

    print("=" * 78)
    print(f"  BACKTEST ANALYSIS — {path}")
    print(f"  Season: {start} → {end}     Final bankroll: ${end_bk:,.2f}  ({net:+,.2f})")
    print("=" * 78)

    # ── Overall ──────────────────────────────────────────────────────────────
    print("\n── OVERALL ─────────────────────────────────────────────────────────────────")
    print(summary_line(rows, "All bets"))
    print(summary_line(favorites,  "  Favorites (ML)"))
    print(summary_line(underdogs,  "  Underdogs (ML)"))
    print(summary_line(totals_all, "  Totals"))

    # ── Favorites breakdown ───────────────────────────────────────────────────
    print("\n── FAVORITES ───────────────────────────────────────────────────────────────")

    def fav_prob_bucket(r):
        p = float(r["our_prob"])
        lo = int(p * 100) // 5 * 5
        return lo

    def fav_odds_bucket(r):
        ml = int(r["odds"])
        if ml >= -115:  return 0
        if ml >= -140:  return 1
        if ml >= -170:  return 2
        if ml >= -200:  return 3
        return 4

    prob_labels = {lo: f"{lo}-{lo+5}% raw" for lo in range(55, 85, 5)}
    odds_labels = {
        0: "−100 to −115",
        1: "−116 to −140",
        2: "−141 to −170",
        3: "−171 to −200",
        4: "−201 to −225",
    }

    bucket_table(favorites, fav_prob_bucket,
                 lambda k: prob_labels.get(k, f"{k}%+"),
                 "  By raw probability:")
    bucket_table(favorites, fav_odds_bucket,
                 lambda k: odds_labels.get(k, f">{k}"),
                 "  By odds range:")

    # ── Underdogs breakdown ───────────────────────────────────────────────────
    print("\n── UNDERDOGS (+131 to +200) ─────────────────────────────────────────────────")

    def udg_odds_bucket(r):
        ml = int(r["odds"])
        if ml <= 140:  return 0
        if ml <= 160:  return 1
        if ml <= 180:  return 2
        return 3

    udg_labels = {
        0: "+131 to +140",
        1: "+141 to +160",
        2: "+161 to +180",
        3: "+181 to +200",
    }

    def udg_prob_bucket(r):
        p = float(r["our_prob"])
        lo = int(p * 100) // 5 * 5
        return lo

    udg_prob_labels = {lo: f"{lo}-{lo+5}% raw" for lo in range(35, 65, 5)}

    bucket_table(underdogs, udg_odds_bucket,
                 lambda k: udg_labels.get(k, f"{k}"),
                 "  By odds range:")
    bucket_table(underdogs, udg_prob_bucket,
                 lambda k: udg_prob_labels.get(k, f"{k}%+"),
                 "  By raw probability:")

    # ── Totals breakdown ─────────────────────────────────────────────────────
    print("\n── TOTALS ──────────────────────────────────────────────────────────────────")
    print(summary_line(totals_ov,  "Overs"))
    print(summary_line(totals_un,  "Unders"))

    def tot_prob_bucket(r):
        p = float(r["our_prob"])
        lo = int(p * 100) // 2 * 2
        return lo

    tot_prob_labels = {lo: f"{lo}-{lo+2}% raw" for lo in range(54, 72, 2)}

    bucket_table(totals_all, tot_prob_bucket,
                 lambda k: tot_prob_labels.get(k, f"{k}%+"),
                 "  By raw probability:")

    # ── Monthly breakdown ────────────────────────────────────────────────────
    if monthly:
        print("\n── MONTHLY PERFORMANCE ─────────────────────────────────────────────────────")

        def month_key(r):
            return r["date"][:7]   # "2025-04"

        months = defaultdict(list)
        for r in rows:
            months[month_key(r)].append(r)

        print(f"  {'Month':<12} {'Bets':>5} {'W/L':>9} {'WR':>7} {'P&L':>10} {'ROI':>8}  {'FAV':>4} {'UDG':>4} {'TOT':>4}")
        print(f"  {'-'*70}")
        for mo in sorted(months):
            mo_rows = months[mo]
            won   = sum(1 for r in mo_rows if r["won"] == "True")
            lost  = sum(1 for r in mo_rows if r["won"] == "False")
            wag   = sum(float(r["bet_size"]) for r in mo_rows)
            pnl   = sum(float(r["profit"])   for r in mo_rows)
            wr    = pct(won, won + lost)
            r_s   = roi(pnl, wag)
            fav_c = sum(1 for r in mo_rows if r.get("is_favorite") == "True")
            udg_c = sum(1 for r in mo_rows if r.get("is_underdog") == "True")
            tot_c = sum(1 for r in mo_rows if r["bet_type"] == "totals")
            flag  = " ✓" if pnl > 0 else " ✗"
            print(f"  {mo:<12} {len(mo_rows):>5} {won}W/{lost:<4}L {wr:>7} ${pnl:>+9,.2f} {r_s:>8}{flag}  {fav_c:>4} {udg_c:>4} {tot_c:>4}")

    # ── Comparison ───────────────────────────────────────────────────────────
    if compare_path:
        prev = load(compare_path)
        print(f"\n── COMPARISON vs {compare_path} ─────────────────────────────────")

        def comp_stats(r_list):
            if not r_list: return (0, 0, 0.0, 0.0)
            won = sum(1 for r in r_list if r["won"] == "True")
            wag = sum(float(r["bet_size"]) for r in r_list)
            pnl = sum(float(r["profit"])   for r in r_list)
            return (len(r_list), won, wag, pnl)

        def comp_row(label, curr, prev_rows):
            cn, cw, cwag, cpnl = comp_stats(curr)
            pn, pw, pwag, ppnl = comp_stats(prev_rows)
            c_roi = f"{cpnl/cwag*100:+.1f}%" if cwag else "—"
            p_roi = f"{ppnl/pwag*100:+.1f}%" if pwag else "—"
            c_wr  = pct(cw, cn)
            p_wr  = pct(pw, pn)
            print(f"  {label:<20} current: {cn:>4} bets | {c_wr:>6} WR | {c_roi:>7} ROI"
                  f"   prev: {pn:>4} bets | {p_wr:>6} WR | {p_roi:>7} ROI")

        prev_fav = [r for r in prev if r.get("is_favorite") == "True"]
        prev_udg = [r for r in prev if r.get("is_underdog") == "True"]
        prev_tot = [r for r in prev if r["bet_type"] == "totals"]

        comp_row("All bets",   rows,       prev)
        comp_row("Favorites",  favorites,  prev_fav)
        comp_row("Underdogs",  underdogs,  prev_udg)
        comp_row("Totals",     totals_all, prev_tot)

    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze backtest results CSV")
    parser.add_argument("file", help="Path to results CSV")
    parser.add_argument("--monthly",  action="store_true", help="Show monthly breakdown")
    parser.add_argument("--compare",  metavar="FILE",      help="Compare against another CSV")
    args = parser.parse_args()

    analyze(args.file, monthly=args.monthly, compare_path=args.compare)
