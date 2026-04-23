"""
The Odds API integration.
Fetches live MLB moneyline, run line, and totals from major sportsbooks.
"""
import requests
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"

# Books available through The Odds API that are relevant to Nevada/major US books.
# DraftKings is listed first — it tends to set sharper lines early.
# Note: Circa Sports and Pinnacle are not available through this API.
BOOKS_OF_INTEREST = [
    "draftkings",
    "betmgm",
    "fanduel",
    "caesars",
    "pointsbet_us",
    "wynnbet",
    "unibet_us",
]

# Display preference order for the UI — best available book is shown on game cards.
# DraftKings preferred as the sharpest readily-available reference.
DISPLAY_BOOK_PREFERENCE = [
    "draftkings",
    "betmgm",
    "fanduel",
    "caesars",
]


def get_odds(api_key: str, markets: str = "h2h,spreads,totals",
             regions: str = "us") -> list:
    """
    Fetch current MLB odds for all games today.

    markets: comma-separated list of:
      h2h      = moneyline
      spreads  = run line (-1.5)
      totals   = over/under

    Returns list of game odds dicts from the API.
    """
    url = f"{BASE_URL}/sports/{SPORT}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american",
        "bookmakers": ",".join(BOOKS_OF_INTEREST),
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Fetched odds for {len(data)} games. "
                    f"Requests remaining: {response.headers.get('x-requests-remaining', '?')}")
        return data
    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            logger.error("Invalid API key for The Odds API.")
        elif response.status_code == 422:
            logger.error("Invalid parameters sent to The Odds API.")
        else:
            logger.error(f"HTTP error fetching odds: {e}")
        return []
    except Exception as e:
        logger.error(f"Failed to fetch odds: {e}")
        return []


def get_remaining_requests(api_key: str) -> Optional[int]:
    """Check how many API requests remain in the current billing period."""
    url = f"{BASE_URL}/sports"
    params = {"apiKey": api_key}
    try:
        response = requests.get(url, params=params, timeout=5)
        remaining = response.headers.get("x-requests-remaining")
        return int(remaining) if remaining else None
    except Exception:
        return None


def parse_odds(raw_odds: list) -> list:
    """
    Parse the raw API response into a clean list of game odds.

    Returns a list of dicts, one per game, with odds from each book.
    """
    parsed = []
    for game in raw_odds:
        game_info = {
            "odds_api_id": game.get("id"),
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "commence_time": game.get("commence_time"),
            "books": [],
        }

        for bookmaker in game.get("bookmakers", []):
            book_name = bookmaker.get("key")
            book_data = {"bookmaker": book_name, "markets": {}}

            for market in bookmaker.get("markets", []):
                market_key = market.get("key")
                outcomes = market.get("outcomes", [])

                if market_key == "h2h":
                    # Moneyline
                    ml_data = {}
                    for outcome in outcomes:
                        team = outcome.get("name")
                        price = outcome.get("price")
                        if team == game.get("home_team"):
                            ml_data["home_price"] = price
                        else:
                            ml_data["away_price"] = price
                    book_data["markets"]["h2h"] = ml_data

                elif market_key == "spreads":
                    # Run line
                    rl_data = {}
                    for outcome in outcomes:
                        team = outcome.get("name")
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if team == game.get("home_team"):
                            rl_data["home_line"] = point
                            rl_data["home_price"] = price
                        else:
                            rl_data["away_line"] = point
                            rl_data["away_price"] = price
                    book_data["markets"]["spreads"] = rl_data

                elif market_key == "totals":
                    # Over/Under
                    totals_data = {}
                    for outcome in outcomes:
                        name = outcome.get("name")  # "Over" or "Under"
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if name == "Over":
                            totals_data["total_line"] = point
                            totals_data["over_price"] = price
                        else:
                            totals_data["under_price"] = price
                    book_data["markets"]["totals"] = totals_data

                elif market_key == "h2h_h1":
                    # F5 moneyline
                    f5_ml = {}
                    for outcome in outcomes:
                        team = outcome.get("name")
                        price = outcome.get("price")
                        if team == game.get("home_team"):
                            f5_ml["home_price"] = price
                        else:
                            f5_ml["away_price"] = price
                    book_data["markets"]["f5_moneyline"] = f5_ml

                elif market_key == "spreads_h1":
                    # F5 run line (-0.5)
                    f5_rl = {}
                    for outcome in outcomes:
                        team = outcome.get("name")
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if team == game.get("home_team"):
                            f5_rl["home_line"] = point
                            f5_rl["home_price"] = price
                        else:
                            f5_rl["away_line"] = point
                            f5_rl["away_price"] = price
                    book_data["markets"]["f5_runline"] = f5_rl

                elif market_key == "totals_h1":
                    # F5 over/under
                    f5_tot = {}
                    for outcome in outcomes:
                        name = outcome.get("name")
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if name == "Over":
                            f5_tot["total_line"] = point
                            f5_tot["over_price"] = price
                        else:
                            f5_tot["under_price"] = price
                    book_data["markets"]["f5_totals"] = f5_tot

            game_info["books"].append(book_data)

        parsed.append(game_info)
    return parsed


def get_f5_odds(api_key: str, odds_api_event_id: str,
                historical_date: str = None) -> dict:
    """
    Fetch F5 (first 5 innings) moneyline + totals for a single game.

    Uses the event-specific endpoint which supports h2h_1st_5_innings and
    totals_1st_5_innings (the bulk endpoint does NOT support these markets).

    Args:
        api_key:            The Odds API key.
        odds_api_event_id:  Event ID from the API.
        historical_date:    ISO8601 UTC string (e.g. "2025-06-01T17:00:00Z")
                            for historical snapshots. Omit for live odds.

    Returns dict of {market_key: [{bookmaker, home_price, away_price, ...}]}
    Market keys returned: f5_moneyline, f5_totals
    """
    if historical_date:
        url = f"{BASE_URL}/historical/sports/{SPORT}/events/{odds_api_event_id}/odds"
    else:
        url = f"{BASE_URL}/sports/{SPORT}/events/{odds_api_event_id}/odds"

    params = {
        "apiKey":      api_key,
        "regions":     "us",
        "markets":     "h2h_1st_5_innings,totals_1st_5_innings",
        "oddsFormat":  "american",
        "bookmakers":  ",".join(BOOKS_OF_INTEREST),
    }
    if historical_date:
        params["date"] = historical_date
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"F5 odds fetch failed for event {odds_api_event_id}: {e}")
        return {}

    # For historical endpoint the payload is nested under "data"
    payload   = data.get("data", data)
    home_team = payload.get("home_team", "")
    result    = {}   # internal_key → list of {bookmaker, ...fields}

    for bookmaker in payload.get("bookmakers", []):
        book_name = bookmaker.get("key")
        for market in bookmaker.get("markets", []):
            market_key = market.get("key")
            outcomes   = market.get("outcomes", [])

            if market_key == "h2h_1st_5_innings":
                parsed = {"bookmaker": book_name}
                for o in outcomes:
                    if o.get("name") == home_team:
                        parsed["home_price"] = o.get("price")
                    else:
                        parsed["away_price"] = o.get("price")
                result.setdefault("f5_moneyline", []).append(parsed)

            elif market_key == "totals_1st_5_innings":
                parsed = {"bookmaker": book_name}
                for o in outcomes:
                    if o.get("name") == "Over":
                        parsed["total_line"] = o.get("point")
                        parsed["over_price"] = o.get("price")
                    else:
                        parsed["under_price"] = o.get("price")
                result.setdefault("f5_totals", []).append(parsed)

    return result


def get_best_price(parsed_odds: list, game_id: str, bet_type: str, side: str) -> Optional[dict]:
    """
    Find the best price available across all books for a given bet.

    bet_type: "h2h", "spreads", "totals"
    side: "home", "away", "over", "under"
    """
    game = next((g for g in parsed_odds if g["odds_api_id"] == game_id), None)
    if not game:
        return None

    best_price = None
    best_book = None

    for book in game.get("books", []):
        market = book.get("markets", {}).get(bet_type, {})
        price_key = f"{side}_price"
        price = market.get(price_key)

        if price is None:
            continue

        # Higher American odds = better for the bettor
        # For favorites (negative), -110 is better than -120
        if best_price is None or price > best_price:
            best_price = price
            best_book = book["bookmaker"]

    if best_price is not None:
        return {"bookmaker": best_book, "price": best_price}
    return None
