"""
import_historical_odds.py — Load historical closing-line odds into the database.

Supports the ArnavSaraogi mlb-odds-scraper JSON dataset:
  https://github.com/ArnavSaraogi/mlb-odds-scraper/releases

Download the release asset (mlb_odds.json, ~76 MB) and run:

  python import_historical_odds.py --file /path/to/mlb_odds.json

Options:
  --file      Path to the JSON file (required)
  --year      Only import a specific year, e.g. 2024 (optional)
  --book      Only import a specific bookmaker, e.g. draftkings (optional)
  --dry-run   Show what would be imported without writing to DB

The script is safe to re-run — duplicate rows are skipped automatically.
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("import_historical_odds")
logger.setLevel(logging.INFO)

# Bootstrap Flask app context
import app as flask_app
ctx = flask_app.app.app_context()
ctx.push()

from database.schema import db, Team, HistoricalOdds

# ---------------------------------------------------------------------------
# Team abbreviation normalisation
# Mapping from SportsBookReview / dataset abbreviations → MLB API abbreviations
# used in our Team table.  Add any new mismatches here.
# ---------------------------------------------------------------------------
ABBR_MAP = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CWS": "CWS", "CIN": "CIN", "CLE": "CLE",
    "COL": "COL", "DET": "DET", "HOU": "HOU", "KC":  "KC",
    "LAA": "LAA", "LAD": "LAD", "MIA": "MIA", "MIL": "MIL",
    "MIN": "MIN", "NYM": "NYM", "NYY": "NYY", "OAK": "OAK",
    "PHI": "PHI", "PIT": "PIT", "SD":  "SD",  "SDP": "SD",
    "SF":  "SF",  "SFG": "SF",  "SEA": "SEA", "STL": "STL",
    "TB":  "TB",  "TEX": "TEX", "TOR": "TOR", "WSH": "WSH",
    # Oakland moved to Sacramento/Las Vegas — possible future key
    "ATH": "ATH",
}

# Preferred bookmaker order for the backtest (sharpest first)
PREFERRED_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars",
                   "bet365", "betrivers", "bovada"]


def _norm_abbr(raw: str) -> str:
    """Normalise a team abbreviation to match what's in our Team table."""
    return ABBR_MAP.get(raw.upper(), raw.upper())


def _team_by_abbr(abbr: str, cache: dict):
    """Lookup a Team row, using a local dict cache to avoid N+1 queries."""
    norm = _norm_abbr(abbr)
    if norm not in cache:
        team = Team.query.filter_by(abbreviation=norm).first()
        if not team:
            # Fuzzy fallback — match by last word of team name
            pass  # leave as None; caller will skip
        cache[norm] = team
    return cache[norm]


def import_json(filepath: str, year_filter: int = None,
                book_filter: str = None, dry_run: bool = False):
    """
    Parse the JSON file and insert rows into historical_odds.

    Returns a summary dict.
    """
    logger.info(f"Loading {filepath} …")

    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    team_cache = {}
    inserted = skipped_dup = skipped_no_team = skipped_no_odds = 0
    total_games = 0

    for date_key, games in sorted(data.items()):
        try:
            game_date = date.fromisoformat(date_key)
        except ValueError:
            continue

        if year_filter and game_date.year != year_filter:
            continue

        for game_entry in games:
            gv = game_entry.get("gameView", {})

            # Only regular-season final games
            if gv.get("gameType") not in ("R", None):
                continue
            if gv.get("gameStatusText", "").lower() not in ("final", "game over", "completed early"):
                continue

            home_abbr_raw = (gv.get("homeTeam") or {}).get("shortName", "")
            away_abbr_raw = (gv.get("awayTeam") or {}).get("shortName", "")

            if not home_abbr_raw or not away_abbr_raw:
                continue

            home_abbr = _norm_abbr(home_abbr_raw)
            away_abbr = _norm_abbr(away_abbr_raw)

            total_games += 1
            odds_section = game_entry.get("odds", {})

            # Collect odds per bookmaker
            book_odds: dict[str, dict] = {}

            for entry in odds_section.get("moneyline", []):
                book = entry.get("sportsbook", "").lower()
                if book_filter and book != book_filter.lower():
                    continue
                line = entry.get("currentLine") or entry.get("openingLine") or {}
                if book not in book_odds:
                    book_odds[book] = {}
                book_odds[book]["home_ml"] = line.get("homeOdds")
                book_odds[book]["away_ml"] = line.get("awayOdds")

            for entry in odds_section.get("pointspread", []):
                book = entry.get("sportsbook", "").lower()
                if book_filter and book != book_filter.lower():
                    continue
                line = entry.get("currentLine") or entry.get("openingLine") or {}
                if book not in book_odds:
                    book_odds[book] = {}
                book_odds[book]["home_rl_odds"]   = line.get("homeOdds")
                book_odds[book]["away_rl_odds"]   = line.get("awayOdds")
                book_odds[book]["home_rl_spread"] = line.get("homeSpread")

            for entry in odds_section.get("totals", []):
                book = entry.get("sportsbook", "").lower()
                if book_filter and book != book_filter.lower():
                    continue
                line = entry.get("currentLine") or entry.get("openingLine") or {}
                if book not in book_odds:
                    book_odds[book] = {}
                book_odds[book]["total_line"] = line.get("total")
                book_odds[book]["over_odds"]  = line.get("overOdds")
                book_odds[book]["under_odds"] = line.get("underOdds")

            if not book_odds:
                skipped_no_odds += 1
                continue

            for book, odds in book_odds.items():
                # Must have at least a moneyline to be useful
                if odds.get("home_ml") is None and odds.get("away_ml") is None:
                    continue

                if dry_run:
                    inserted += 1
                    continue

                try:
                    row = HistoricalOdds(
                        game_date      = game_date,
                        home_team_abbr = home_abbr,
                        away_team_abbr = away_abbr,
                        bookmaker      = book,
                        home_ml        = odds.get("home_ml"),
                        away_ml        = odds.get("away_ml"),
                        home_rl_odds   = odds.get("home_rl_odds"),
                        away_rl_odds   = odds.get("away_rl_odds"),
                        home_rl_spread = odds.get("home_rl_spread"),
                        total_line     = odds.get("total_line"),
                        over_odds      = odds.get("over_odds"),
                        under_odds     = odds.get("under_odds"),
                    )
                    db.session.add(row)
                    db.session.flush()
                    inserted += 1
                except Exception:
                    db.session.rollback()
                    skipped_dup += 1

    if not dry_run:
        db.session.commit()

    return {
        "total_games_in_file": total_games,
        "rows_inserted":       inserted,
        "skipped_duplicates":  skipped_dup,
        "skipped_no_odds":     skipped_no_odds,
        "dry_run":             dry_run,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import historical MLB closing-line odds from JSON into the database"
    )
    parser.add_argument("--file",    required=True, help="Path to mlb_odds.json")
    parser.add_argument("--year",    type=int,      help="Only import this year (e.g. 2024)")
    parser.add_argument("--book",                   help="Only import this bookmaker (e.g. draftkings)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing to the database")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"ERROR: File not found: {args.file}")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  Historical Odds Import")
    print(f"{'='*55}")
    if args.year:
        print(f"  Year filter:       {args.year}")
    if args.book:
        print(f"  Bookmaker filter:  {args.book}")
    if args.dry_run:
        print(f"  Mode:              DRY RUN (no DB writes)")
    print()

    summary = import_json(
        filepath    = args.file,
        year_filter = args.year,
        book_filter = args.book,
        dry_run     = args.dry_run,
    )

    print(f"  Games processed:   {summary['total_games_in_file']:,}")
    print(f"  Rows inserted:     {summary['rows_inserted']:,}")
    print(f"  Skipped (dup):     {summary['skipped_duplicates']:,}")
    print(f"  Skipped (no odds): {summary['skipped_no_odds']:,}")
    if args.dry_run:
        print("\n  (Dry run — nothing was written to the database)")
    print("\n✓ Done.\n")

    ctx.pop()
