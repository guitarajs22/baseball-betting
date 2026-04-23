"""
Pre-fetch historical lineups from the MLB Stats API and save to a local cache.

For each completed game in the date range, this pulls the boxscore and records:
  - The actual starting pitcher for each team (first pitcher listed)
  - The starting batting order (positions 1–9, batting_order ending in "00")

The cache is keyed by "{away_abbr}@{home_abbr}_{YYYY-MM-DD}" so the backtest
can look up lineups without any API calls during the run.

Usage:
    python -m backtest.build_lineup_cache --start 2024-04-01 --end 2024-09-30

Output:
    backtest/lineup_cache.json

Runtime: ~25–45 minutes for a full season (~2,400 games at 1 req/sec).
Progress is saved after every day so you can safely interrupt and resume.
"""

import argparse
import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = "backtest/lineup_cache.json"
# Seconds to wait between API calls — stay well under any rate limit
REQUEST_DELAY = 0.5


def _fetch_day(date_str: str) -> list:
    """Return all Final games for a date (MM/DD/YYYY format)."""
    import statsapi
    try:
        games = statsapi.schedule(date=date_str, sportId=1)
        return [g for g in games if g.get("status") in ("Final", "Game Over", "Completed Early")]
    except Exception as e:
        logger.warning(f"  Schedule fetch failed for {date_str}: {e}")
        return []


def _fetch_boxscore(game_pk: int):
    """Return raw boxscore_data dict or None on error."""
    import statsapi
    try:
        return statsapi.boxscore_data(game_pk)
    except Exception as e:
        logger.warning(f"  Boxscore fetch failed for game_pk={game_pk}: {e}")
        return None


def _extract_lineup(box: dict, side: str) -> dict:
    """
    Extract the starting lineup and pitcher from one side of a boxscore.

    Returns:
        {
            "sp_id":    int | None,     # MLB player ID of the starting pitcher
            "sp_name":  str,
            "batters":  [               # batting order positions 1–9
                {"mlb_id": int, "name": str, "order": int},
                ...
            ]
        }
    """
    pitchers = box.get(f"{side}Pitchers", [])
    batters_raw = box.get(f"{side}Batters", [])

    # Starting pitcher = first real entry (index 0 is the header row)
    sp_id, sp_name = None, ""
    if len(pitchers) > 1:
        sp = pitchers[1]
        sp_id = sp.get("personId") or None
        sp_name = sp.get("name", "")
        if sp_id == 0:
            sp_id = None

    # Starting batters — battingOrder "100","200",...,"900"
    batters = []
    for b in batters_raw[1:]:
        order_str = str(b.get("battingOrder", ""))
        if order_str.endswith("00") and len(order_str) == 3:
            pid = b.get("personId")
            if pid and pid != 0:
                batters.append({
                    "mlb_id": int(pid),
                    "name":   b.get("name", ""),
                    "order":  int(order_str) // 100,  # "300" → 3
                })

    return {"sp_id": sp_id, "sp_name": sp_name, "batters": batters}


def _team_abbr(box: dict, side: str) -> str:
    """Pull team abbreviation from boxscore teamInfo."""
    info = box.get("teamInfo", {}).get(side, {})
    return info.get("abbreviation") or info.get("teamName", "")[:3].upper()


def build_cache(start_date: str, end_date: str, cache_path: str = DEFAULT_CACHE_PATH):
    """
    Fetch all games in [start_date, end_date] and populate the lineup cache.
    Resumes from existing cache so interrupted runs don't restart from scratch.
    """
    # Load existing cache
    cache: dict = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)
        logger.info(f"Loaded existing cache: {len(cache)} entries from {cache_path}")
    else:
        logger.info(f"Starting fresh cache → {cache_path}")

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")
    total_days = (end - start).days + 1

    games_fetched  = 0
    games_skipped  = 0
    games_failed   = 0
    day_num        = 0

    current = start
    while current <= end:
        day_num += 1
        date_str = current.strftime("%m/%d/%Y")
        date_key = current.strftime("%Y-%m-%d")
        logger.info(f"[{day_num}/{total_days}] {date_key} ...")

        games = _fetch_day(date_str)
        time.sleep(REQUEST_DELAY)

        day_fetched = 0
        for g in games:
            game_pk   = g["game_id"]
            home_name = g.get("home_name", "")
            away_name = g.get("away_name", "")

            # Build a provisional key using team name suffixes — will be replaced
            # with abbreviations once we have the boxscore
            prov_key = f"{away_name.split()[-1]}@{home_name.split()[-1]}_{date_key}"

            # Skip if already cached (check both provisional and abbr-based keys)
            already = any(
                k.endswith(f"_{date_key}") and
                (home_name.split()[-1] in k or away_name.split()[-1] in k)
                for k in cache
            )
            if already:
                games_skipped += 1
                continue

            box = _fetch_boxscore(game_pk)
            time.sleep(REQUEST_DELAY)

            if box is None:
                games_failed += 1
                continue

            home_data = _extract_lineup(box, "home")
            away_data = _extract_lineup(box, "away")

            home_abbr = _team_abbr(box, "home")
            away_abbr = _team_abbr(box, "away")

            # Primary key: "LAD@SF_2024-06-15"
            key = f"{away_abbr}@{home_abbr}_{date_key}"
            cache[key] = {
                "game_pk":   game_pk,
                "date":      date_key,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "home":      home_data,
                "away":      away_data,
            }
            games_fetched += 1
            day_fetched   += 1

        # Save after every day so progress isn't lost on interrupt
        if day_fetched > 0:
            with open(cache_path, "w") as f:
                json.dump(cache, f)

        current += timedelta(days=1)

    # Final save
    with open(cache_path, "w") as f:
        json.dump(cache, f)

    logger.info("=" * 50)
    logger.info(f"Done. Cache entries: {len(cache)}")
    logger.info(f"  Fetched this run: {games_fetched}")
    logger.info(f"  Already cached:   {games_skipped}")
    logger.info(f"  Failed:           {games_failed}")
    logger.info(f"  Saved to: {cache_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build historical lineup cache from MLB API")
    parser.add_argument("--start",  default="2024-04-01",                 help="Start date YYYY-MM-DD")
    parser.add_argument("--end",    default="2024-09-30",                 help="End date YYYY-MM-DD")
    parser.add_argument("--output", default=DEFAULT_CACHE_PATH,           help="Output JSON file path")
    args = parser.parse_args()
    build_cache(args.start, args.end, args.output)
