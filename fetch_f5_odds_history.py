"""
Historical F5 Odds Pre-Fetcher
==============================
Fetches first-5-innings moneyline and totals from The Odds API for every
game in a season and writes them to a JSON cache file.

The cache file does NOT require matching to existing DB game rows — it stores
data keyed by (away_team, home_team, date) so the backtest can look up F5 odds
for any game regardless of whether it's in the database.

Usage:
    python fetch_f5_odds_history.py --season 2025
    python fetch_f5_odds_history.py --season 2024
    python fetch_f5_odds_history.py --season 2025 --start 2025-06-01 --end 2025-08-31
    python fetch_f5_odds_history.py --status   (show what's cached)

Cache file: backtest/f5_odds_cache.json
  Key: "AWAY @ HOME YYYY-MM-DD"  (e.g. "Boston Red Sox @ Baltimore Orioles 2025-06-01")
  Value: {
      "f5_home_ml":    -158,   # best available F5 moneyline for home team
      "f5_away_ml":    +134,   # best available F5 moneyline for away team
      "f5_total_line": 4.5,    # F5 total line
      "f5_over_price": -115,   # F5 over price
      "f5_under_price":-105,   # F5 under price
      "bookmaker":     "draftkings",
      "fetched_at":    "2025-06-01T16:40:00Z"
  }

Cost: 1 API request per game (~2,430/season).
"""

import os
import sys
import json
import time
import argparse
import logging
import datetime
import requests

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BASE_URL      = "https://api.the-odds-api.com/v4"
SPORT         = "baseball_mlb"
MARKETS       = "h2h_1st_5_innings,totals_1st_5_innings"
CACHE_FILE    = os.path.join(os.path.dirname(__file__), "backtest", "f5_odds_cache.json")
REQUEST_DELAY = 0.25   # seconds between API calls

# Preferred book order — first available wins
BOOK_PREFERENCE = ["draftkings", "betmgm", "fanduel", "caesars",
                   "pointsbet_us", "betonlineag", "bovada", "mybookieag"]


def _get(url, params, label=""):
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 429:
            wait = 10 * (attempt + 1)
            log.warning(f"Rate limited on {label}, waiting {wait}s …")
            time.sleep(wait)
            continue
        return r
    return r


def _cache_key(away_team: str, home_team: str, game_date: str) -> str:
    """Stable lookup key for the cache."""
    return f"{away_team} @ {home_team} {game_date}"


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _best_f5(bookmakers: list, home_team: str) -> dict:
    """
    Extract the best available F5 moneyline and totals from a list of bookmakers.
    Prefers books in BOOK_PREFERENCE order.
    Returns a dict or {} if no F5 data found.
    """
    ml_by_book   = {}   # book_key → {home_ml, away_ml}
    tot_by_book  = {}   # book_key → {total_line, over_price, under_price}

    for bm in bookmakers:
        book = bm.get("key", "")
        for mkt in bm.get("markets", []):
            key      = mkt.get("key")
            outcomes = mkt.get("outcomes", [])

            if key == "h2h_1st_5_innings":
                parsed = {}
                for o in outcomes:
                    if o.get("name") == home_team:
                        parsed["f5_home_ml"] = o.get("price")
                    else:
                        parsed["f5_away_ml"] = o.get("price")
                if parsed:
                    ml_by_book[book] = parsed

            elif key == "totals_1st_5_innings":
                parsed = {}
                for o in outcomes:
                    if o.get("name") == "Over":
                        parsed["f5_total_line"] = o.get("point")
                        parsed["f5_over_price"] = o.get("price")
                    else:
                        parsed["f5_under_price"] = o.get("price")
                if parsed:
                    tot_by_book[book] = parsed

    if not ml_by_book:
        return {}

    # Pick best book in preference order
    ml_book = next((b for b in BOOK_PREFERENCE if b in ml_by_book), None)
    if not ml_book:
        ml_book = next(iter(ml_by_book))

    result = {"bookmaker": ml_book}
    result.update(ml_by_book[ml_book])

    # Add totals from same book if available, else any book
    tot_book = ml_book if ml_book in tot_by_book else \
               next((b for b in BOOK_PREFERENCE if b in tot_by_book), None) or \
               (next(iter(tot_by_book)) if tot_by_book else None)
    if tot_book:
        result.update(tot_by_book[tot_book])

    return result


def fetch_season(season: int, start_date: datetime.date = None,
                 end_date: datetime.date = None, dry_run: bool = False):
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        log.error("ODDS_API_KEY not set in .env")
        sys.exit(1)

    season_start = start_date or datetime.date(season, 3, 20)
    season_end   = end_date   or datetime.date(season, 10, 5)
    today        = datetime.date.today()
    if season_end >= today:
        season_end = today - datetime.timedelta(days=1)

    log.info(f"Fetching F5 odds for {season} season: {season_start} → {season_end}")

    cache = load_cache()
    total_games   = 0
    skipped_exist = 0
    fetched       = 0
    no_f5         = 0
    errors        = 0
    requests_used = 0

    current = season_start
    last_save = datetime.datetime.now()

    while current <= season_end:
        date_str = current.strftime("%Y-%m-%dT12:00:00Z")

        # Get event list for this date
        r = _get(
            f"{BASE_URL}/historical/sports/{SPORT}/events/",
            {"apiKey": api_key, "date": date_str},
            label=f"events {current}",
        )
        time.sleep(REQUEST_DELAY)
        requests_used += 1

        if r.status_code != 200:
            log.warning(f"{current}: events fetch failed ({r.status_code})")
            current += datetime.timedelta(days=1)
            continue

        events    = r.json().get("data", [])
        day_count = 0

        for ev in events:
            event_id  = ev["id"]
            game_time = ev["commence_time"]
            away_name = ev.get("away_team", "")
            home_name = ev.get("home_team", "")
            date_key  = current.isoformat()
            cache_key = _cache_key(away_name, home_name, date_key)
            total_games += 1

            # Skip if already cached
            if cache_key in cache:
                skipped_exist += 1
                continue

            if dry_run:
                log.info(f"  [DRY RUN] {away_name} @ {home_name}")
                continue

            # Fetch F5 odds ~30 min before first pitch
            gt = datetime.datetime.fromisoformat(game_time.replace("Z", "+00:00"))
            fetch_ts = (gt - datetime.timedelta(minutes=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            r2 = _get(
                f"{BASE_URL}/historical/sports/{SPORT}/events/{event_id}/odds/",
                {
                    "apiKey":     api_key,
                    "regions":    "us",
                    "markets":    MARKETS,
                    "oddsFormat": "american",
                    "date":       fetch_ts,
                },
                label=f"F5 {away_name[:10]} @ {home_name[:10]}",
            )
            time.sleep(REQUEST_DELAY)
            requests_used += 1

            if r2.status_code != 200:
                log.warning(f"  F5 fetch failed {away_name} @ {home_name}: {r2.status_code}")
                errors += 1
                continue

            payload    = r2.json().get("data", {})
            bookmakers = payload.get("bookmakers", [])
            home_api   = payload.get("home_team", home_name)

            if not bookmakers:
                no_f5 += 1
                continue

            best = _best_f5(bookmakers, home_api)
            if not best:
                no_f5 += 1
                continue

            best["fetched_at"] = fetch_ts
            cache[cache_key]   = best
            fetched  += 1
            day_count += 1

        # Save cache periodically (every 2 minutes) and at end of each day
        now = datetime.datetime.now()
        if (now - last_save).seconds >= 120 or day_count > 0:
            save_cache(cache)
            last_save = now

        remaining = r.headers.get("x-requests-remaining", "?")
        if day_count > 0 or len(events) > 0:
            log.info(
                f"  {current}  {len(events):2d} events | "
                f"{day_count:2d} F5 stored | "
                f"API remaining: {remaining}"
            )

        current += datetime.timedelta(days=1)

    save_cache(cache)

    log.info("=" * 60)
    log.info(f"Done. Season {season}")
    log.info(f"  Games found:      {total_games}")
    log.info(f"  F5 odds fetched:  {fetched}")
    log.info(f"  Already cached:   {skipped_exist}")
    log.info(f"  No F5 available:  {no_f5}")
    log.info(f"  Errors:           {errors}")
    log.info(f"  API requests used:{requests_used}")
    log.info(f"  Cache file:       {CACHE_FILE}")
    log.info(f"  Total cached:     {len(cache)} games")


def show_status():
    cache = load_cache()
    if not cache:
        print(f"Cache is empty: {CACHE_FILE}")
        return

    by_season = {}
    ml_count  = 0
    tot_count = 0
    for key, val in cache.items():
        # key format: "Away @ Home YYYY-MM-DD"
        date_part = key.split()[-1]
        season    = date_part[:4]
        by_season[season] = by_season.get(season, 0) + 1
        if "f5_home_ml"    in val: ml_count  += 1
        if "f5_total_line" in val: tot_count += 1

    print(f"F5 odds cache: {CACHE_FILE}")
    print(f"{'Season':>8}  {'Games':>8}")
    print("-" * 20)
    for s in sorted(by_season):
        print(f"{s:>8}  {by_season[s]:>8}")
    print(f"\nTotal games cached: {len(cache)}")
    print(f"  With F5 moneyline: {ml_count}")
    print(f"  With F5 totals:    {tot_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pre-fetch historical F5 odds from The Odds API"
    )
    parser.add_argument("--season",  type=int, help="Season year (e.g. 2025)")
    parser.add_argument("--start",   help="Start date YYYY-MM-DD")
    parser.add_argument("--end",     help="End date   YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without making API calls")
    parser.add_argument("--status",  action="store_true",
                        help="Show what's already cached and exit")
    args = parser.parse_args()

    if args.status:
        show_status()
        sys.exit(0)

    if not args.season:
        parser.error("--season is required (e.g. --season 2025)")

    start = datetime.date.fromisoformat(args.start) if args.start else None
    end   = datetime.date.fromisoformat(args.end)   if args.end   else None

    fetch_season(args.season, start_date=start, end_date=end, dry_run=args.dry_run)
