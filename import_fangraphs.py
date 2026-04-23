"""
import_fangraphs.py — Import FanGraphs CSVs for one or all seasons.

Run this every Sunday after exporting from FanGraphs.com.

Usage:
    python import_fangraphs.py              # import all seasons with CSVs present
    python import_fangraphs.py --season 2026  # current season only (fastest)
    python import_fangraphs.py --season 2024 2025 2026  # specific seasons

──────────────────────────────────────────────────────────────────────────────
STEP-BY-STEP: HOW TO EXPORT FROM FANGRAPHS (do this every Sunday)
──────────────────────────────────────────────────────────────────────────────

You need 5 CSV files per season. Save them to exports/{year}/ exactly as named.
You are a FanGraphs Premium subscriber, so all splits are available to you.

─── BATTING (3 files) ────────────────────────────────────────────────────────

 1. OVERALL batting — exports/2026/batting_overall_2026.csv
    URL: https://www.fangraphs.com/leaders/major-league?pos=all&stats=bat&lg=all&qual=10&type=8&season=2026&month=0&season1=2026&ind=0
    → Set "Min PA" to 10
    → Click "Export Data" at the bottom

 2. vs LEFT-HANDED PITCHERS — exports/2026/batting_vs_lhp_2026.csv
    URL: https://www.fangraphs.com/leaders/splits-leaderboards?splitArr=1&splitArrPitch=1&statgroup=2&startDate=2026-03-01&endDate=2026-11-01&players=&mycount=0&pgPos=0&z=0&type=0&sortDir=default&sortStat=WAR&playerType=0&startInning=1&endInning=9&pitcher_handedness=L&batter_handedness=&game_type=R&groupBy=season&statstype=rate&season=2026&minpa=5
    → This is the Splits Leaderboard filtered to vs LHP
    → Simpler path: Leaders → Splits → Batting → "Vs. LHP" → Export Data

 3. vs RIGHT-HANDED PITCHERS — exports/2026/batting_vs_rhp_2026.csv
    Same as above but filter to "Vs. RHP"
    → Leaders → Splits → Batting → "Vs. RHP" → Export Data

─── PITCHING (2 files) ───────────────────────────────────────────────────────

 4. STARTING PITCHERS — exports/2026/pitching_sp_overall_2026.csv
    URL: https://www.fangraphs.com/leaders/major-league?pos=all&stats=pit&lg=all&qual=1&type=8&season=2026&month=0&season1=2026&ind=0&rost=0
    → Set "Min IP" to 1, set "Pos" filter to "SP"
    → Click "Export Data"

 5. RELIEF PITCHERS — exports/2026/pitching_rp_overall_2026.csv
    Same page, change "Pos" filter to "RP"
    → Click "Export Data"

─── FOR PAST SEASONS (one-time only) ─────────────────────────────────────────

Do the same exports for 2024 and 2025 — just change the season year in the URL
and save to exports/2024/ and exports/2025/.

Past season CSVs never change, so you only need to do this once.

─── FILE NAMING (must match exactly) ─────────────────────────────────────────

  exports/2024/batting_overall_2024.csv
  exports/2024/batting_vs_lhp_2024.csv
  exports/2024/batting_vs_rhp_2024.csv
  exports/2024/pitching_sp_overall_2024.csv
  exports/2024/pitching_rp_overall_2024.csv

  exports/2025/batting_overall_2025.csv
  exports/2025/batting_vs_lhp_2025.csv
  exports/2025/batting_vs_rhp_2025.csv
  exports/2025/pitching_sp_overall_2025.csv
  exports/2025/pitching_rp_overall_2025.csv

  exports/2026/batting_overall_2026.csv
  exports/2026/batting_vs_lhp_2026.csv
  exports/2026/batting_vs_rhp_2026.csv
  exports/2026/pitching_sp_overall_2026.csv
  exports/2026/pitching_rp_overall_2026.csv

──────────────────────────────────────────────────────────────────────────────
"""

import argparse
import logging
import os
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EXPORTS_DIR = os.path.join(os.path.dirname(__file__), "exports")

BATTING_FILES = [
    ("overall", "batting_overall_{year}.csv"),
    ("vs_LHP",  "batting_vs_lhp_{year}.csv"),
    ("vs_RHP",  "batting_vs_rhp_{year}.csv"),
]

PITCHING_FILES = [
    ("SP", "overall", "pitching_sp_overall_{year}.csv"),
    ("RP", "overall", "pitching_rp_overall_{year}.csv"),
]


def _files_present(season: int) -> dict:
    """Return which expected CSVs are actually present for a given season."""
    present = {}
    for split, fname in BATTING_FILES:
        path = os.path.join(EXPORTS_DIR, str(season), fname.format(year=season))
        present[fname.format(year=season)] = os.path.exists(path)
    for role, split, fname in PITCHING_FILES:
        path = os.path.join(EXPORTS_DIR, str(season), fname.format(year=season))
        present[fname.format(year=season)] = os.path.exists(path)
    return present


def _print_status():
    """Print which CSVs are present vs missing across all seasons."""
    print("\nFanGraphs CSV status:")
    print("─" * 60)
    for season in [2024, 2025, 2026]:
        present = _files_present(season)
        found  = sum(1 for v in present.values() if v)
        total  = len(present)
        print(f"\n  {season}  ({found}/{total} files present)")
        for fname, exists in present.items():
            icon = "✓" if exists else "✗"
            print(f"    {icon}  {fname}")
    print()


def import_season(season: int) -> bool:
    """
    Run import_data.py for one season (stats-only, no roster re-sync).
    Returns True if at least one CSV was found and imported.
    """
    present = _files_present(season)
    if not any(present.values()):
        logger.info(f"  Season {season}: no CSVs found — skipping")
        return False

    found_files = [f for f, exists in present.items() if exists]
    logger.info(f"  Season {season}: found {len(found_files)} CSV(s) — importing...")

    from import_data import import_fangraphs_batting, import_fangraphs_pitching
    import_fangraphs_batting(season)
    import_fangraphs_pitching(season)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import FanGraphs CSVs into the baseball betting database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--season", type=int, nargs="+",
        default=None,
        help="Season(s) to import, e.g. --season 2026 or --season 2024 2025 2026. "
             "Default: all seasons that have at least one CSV present."
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Just show which CSVs are present — don't import anything."
    )
    args = parser.parse_args()

    if args.status:
        _print_status()
        sys.exit(0)

    # Determine which seasons to import
    if args.season:
        seasons = args.season
    else:
        # Auto-detect: all seasons from 2024 up through current year that have files
        current_year = date.today().year
        seasons = [y for y in range(2024, current_year + 1)
                   if any(_files_present(y).values())]
        if not seasons:
            print("\nNo FanGraphs CSVs found in exports/")
            print("Run 'python import_fangraphs.py --status' to see what's missing.")
            print("See the instructions at the top of this file for how to export from FanGraphs.\n")
            sys.exit(1)

    from app import app, init_db
    with app.app_context():
        init_db()
        print(f"\nImporting seasons: {seasons}\n")
        imported_any = False
        for season in sorted(seasons):
            if import_season(season):
                imported_any = True

        if imported_any:
            print("\n✓ Import complete. Re-simulate today's games to use the updated stats.")
        else:
            print("\nNo CSVs found to import. Check exports/ directory.")
