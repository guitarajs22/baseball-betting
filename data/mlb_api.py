"""
MLB Stats API integration (free, no API key needed).
Uses the statsapi Python library which wraps api.MLB.com.
"""
import statsapi
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


def get_todays_schedule() -> list:
    """Fetch today's MLB schedule with game info."""
    today = date.today().strftime("%m/%d/%Y")
    try:
        schedule = statsapi.schedule(date=today)
        return schedule
    except Exception as e:
        logger.error(f"Failed to fetch schedule: {e}")
        return []


def get_schedule_for_date(game_date: date) -> list:
    """
    Fetch schedule for a specific date.
    Requests all game types: R=Regular, S=Spring Training,
    F=Wild Card, D=Division, L=LCS, W=World Series.
    """
    date_str = game_date.strftime("%m/%d/%Y")
    try:
        games = statsapi.schedule(date=date_str, sportId=1)
        return games
    except Exception as e:
        logger.error(f"Failed to fetch schedule for {date_str}: {e}")
        return []


def fetch_game_lineup(game_pk: int) -> dict:
    """
    Fetch official batting lineups and confirmed starters from the MLB API.

    Lineups are only available after the teams officially submit them —
    typically 60–90 minutes before first pitch.

    Returns one of:
      {'status': 'posted',      'home_batters': [...], 'away_batters': [...],
                                'home_pitcher': {...},  'away_pitcher': {...}}
      {'status': 'not_posted',  'message': '...'}
      {'status': 'error',       'message': '...'}

    Each batter entry: {'mlb_id': int, 'name': str, 'order': int, 'position': str}
    Each pitcher entry: {'mlb_id': int, 'name': str}
    """
    try:
        data = statsapi.boxscore_data(game_pk)

        def _parse_batters(raw_list):
            batters = []
            for p in raw_list:
                order_str = str(p.get("battingOrder") or "0").strip()
                if not order_str or order_str == "0":
                    continue
                try:
                    order = int(order_str) // 100
                except (ValueError, TypeError):
                    continue
                if 1 <= order <= 9:
                    batters.append({
                        "mlb_id":   p.get("personId"),
                        "name":     p.get("name", ""),
                        "order":    order,
                        "position": p.get("position", ""),
                    })
            return sorted(batters, key=lambda x: x["order"])

        home_batters = _parse_batters(data.get("homeBatters", []))
        away_batters = _parse_batters(data.get("awayBatters", []))

        # First pitcher in each list is the starter (index 0 is often a header row
        # with personId=0 — skip those)
        def _parse_starter(pitchers):
            for p in pitchers:
                pid = p.get("personId")
                if pid and int(pid) > 0:
                    return {"mlb_id": int(pid), "name": p.get("name", "")}
            return None

        home_pitcher = _parse_starter(data.get("homePitchers", []))
        away_pitcher = _parse_starter(data.get("awayPitchers", []))

        if not home_batters and not away_batters:
            return {
                "status":  "not_posted",
                "message": "Lineup not yet posted — check back 60–90 min before first pitch.",
            }

        return {
            "status":       "posted",
            "home_batters": home_batters,
            "away_batters": away_batters,
            "home_pitcher": home_pitcher,
            "away_pitcher": away_pitcher,
        }

    except Exception as e:
        logger.error(f"Failed to fetch lineup for game {game_pk}: {e}")
        return {"status": "error", "message": str(e)}


def get_probable_starters(game_pk: int) -> dict:
    """
    Get probable starters for a game.
    Returns {'home': {id, name}, 'away': {id, name}}
    """
    try:
        game = statsapi.boxscore_data(game_pk)
        home_pitcher = game.get("homePitchers", [{}])[0]
        away_pitcher = game.get("awayPitchers", [{}])[0]
        return {
            "home": {"id": home_pitcher.get("personId"), "name": home_pitcher.get("name", "TBD")},
            "away": {"id": away_pitcher.get("personId"), "name": away_pitcher.get("name", "TBD")},
        }
    except Exception as e:
        logger.error(f"Failed to get starters for game {game_pk}: {e}")
        return {"home": {"id": None, "name": "TBD"}, "away": {"id": None, "name": "TBD"}}


def get_all_teams() -> list:
    """Fetch all active MLB teams."""
    try:
        teams = statsapi.get("teams", {"sportId": 1, "activeStatus": "Y"})
        return teams.get("teams", [])
    except Exception as e:
        logger.error(f"Failed to fetch teams: {e}")
        return []


def get_team_roster(team_id: int, season: Optional[int] = None) -> list:
    """Get 40-man roster for a team."""
    if season is None:
        season = date.today().year
    try:
        roster = statsapi.roster(team_id, rosterType="40Man")
        return roster
    except Exception as e:
        logger.error(f"Failed to fetch roster for team {team_id}: {e}")
        return []


def get_player_info(player_id: int) -> dict:
    """Get basic player info (name, position, bats, throws)."""
    try:
        player = statsapi.player_stat_data(player_id, group="[hitting,pitching]", type="career")
        return player
    except Exception as e:
        logger.error(f"Failed to fetch player {player_id}: {e}")
        return {}


def get_umpire_for_game(game_pk: int) -> Optional[dict]:
    """Get the home plate umpire for a game."""
    try:
        game_data = statsapi.get("game", {"gamePk": game_pk})
        officials = game_data.get("liveData", {}).get("boxscore", {}).get("officials", [])
        for official in officials:
            if official.get("officialType") == "Home Plate":
                return {
                    "id": official["official"]["id"],
                    "name": official["official"]["fullName"],
                }
        return None
    except Exception as e:
        logger.error(f"Failed to get umpire for game {game_pk}: {e}")
        return None


def get_live_score(game_pk: int) -> Optional[dict]:
    """
    Get current live score for an in-progress or completed game.
    Returns scores, status, and inning info suitable for the dashboard watchlist.
    """
    try:
        game = statsapi.schedule(game_id=game_pk)[0]
        return {
            "home_score":    game.get("home_score"),
            "away_score":    game.get("away_score"),
            "status":        game.get("status"),          # "In Progress", "Final", "Scheduled", etc.
            "inning":        game.get("current_inning"),
            "inning_state":  game.get("inning_state"),    # "Top", "Bottom", "Middle", "End"
        }
    except Exception as e:
        logger.error(f"Failed to get live score for game {game_pk}: {e}")
        return None


def get_game_result(game_pk: int) -> Optional[dict]:
    """
    Get final score for a completed game. Used for backtesting.
    Returns {'home_score': int, 'away_score': int, 'home_team': str, 'away_team': str}

    When a game_pk has multiple entries (e.g. original date was postponed and
    the game was replayed the next day), prefer the entry with status "Final".
    """
    try:
        entries = statsapi.schedule(game_id=game_pk)
        if not entries:
            return None
        # Prefer a Final entry over a Postponed/Scheduled one
        game = None
        for entry in entries:
            if entry.get("status", "").lower() == "final":
                game = entry
                break
        if game is None:
            game = entries[0]  # fallback to first entry
        return {
            "home_score": game.get("home_score"),
            "away_score": game.get("away_score"),
            "home_team": game.get("home_name"),
            "away_team": game.get("away_name"),
            "status": game.get("status"),
        }
    except Exception as e:
        logger.error(f"Failed to get result for game {game_pk}: {e}")
        return None


def get_first_inning_result(game_pk: int) -> Optional[dict]:
    """
    Return first-inning runs for a completed game.
    Returns {"away": N, "home": N, "rifi": bool} or None if unavailable.
    """
    try:
        data = statsapi.get("game", {"gamePk": game_pk})
        innings = data.get("liveData", {}).get("linescore", {}).get("innings", [])
        if not innings:
            return None
        first = innings[0]
        away_fi = first.get("away", {}).get("runs", 0) or 0
        home_fi = first.get("home", {}).get("runs", 0) or 0
        return {"away": away_fi, "home": home_fi, "rifi": (away_fi + home_fi) > 0}
    except Exception as e:
        logger.error(f"Failed to get first inning result for {game_pk}: {e}")
        return None


def get_f5_result(game_pk: int) -> Optional[dict]:
    """
    Return runs scored through the first 5 innings for a completed game.

    Returns:
        {
            "away": int,       # away team runs in innings 1-5
            "home": int,       # home team runs in innings 1-5
            "total": int,      # combined F5 runs
            "home_leads": bool,
            "away_leads": bool,
            "tied": bool,      # tie after 5 = push on F5 moneyline
        }
    or None if linescore unavailable.
    """
    try:
        data = statsapi.get("game", {"gamePk": game_pk})
        innings = data.get("liveData", {}).get("linescore", {}).get("innings", [])
        if len(innings) < 5:
            return None   # game didn't complete 5 innings (rain, etc.)
        away_runs = sum(
            (inn.get("away", {}).get("runs") or 0) for inn in innings[:5]
        )
        home_runs = sum(
            (inn.get("home", {}).get("runs") or 0) for inn in innings[:5]
        )
        return {
            "away":       away_runs,
            "home":       home_runs,
            "total":      away_runs + home_runs,
            "home_leads": home_runs > away_runs,
            "away_leads": away_runs > home_runs,
            "tied":       home_runs == away_runs,
        }
    except Exception as e:
        logger.error(f"Failed to get F5 result for game {game_pk}: {e}")
        return None


def get_previous_game_pitchers(team_mlb_id: int, before_date) -> dict:
    """
    Find the most recent completed game for a team before before_date,
    and return all pitchers who appeared along with their pitch counts.

    Returns a dict: {mlb_id: {"name": str, "pitches": int, "ip": str}, ...}
    Returns empty dict on any error or if no completed game found.
    """
    try:
        from datetime import timedelta
        if hasattr(before_date, 'strftime'):
            end_dt = before_date
        else:
            end_dt = before_date
        # Search the 7 days before before_date
        if hasattr(end_dt, 'toordinal'):
            # date object
            from datetime import date as _date
            start_dt = end_dt - timedelta(days=7)
            start_str = start_dt.strftime("%m/%d/%Y")
            end_str = (end_dt - timedelta(days=1)).strftime("%m/%d/%Y")
        else:
            from datetime import datetime as _datetime
            start_dt = end_dt - timedelta(days=7)
            start_str = start_dt.strftime("%m/%d/%Y")
            end_str = (end_dt - timedelta(days=1)).strftime("%m/%d/%Y")

        schedule = statsapi.schedule(
            start_date=start_str,
            end_date=end_str,
            sportId=1,
            team=team_mlb_id,
        )

        # Find most recent completed game — MLB API returns several status strings
        # for finished games; accept all of them.
        FINAL_STATUSES = {"Final", "Game Over", "Completed Early"}
        completed = [g for g in schedule if g.get("status") in FINAL_STATUSES]
        if not completed:
            return {}

        # Sort descending by date, take most recent
        completed.sort(key=lambda g: g.get("game_date", ""), reverse=True)
        game_pk = completed[0]["game_id"]

        boxscore = statsapi.boxscore_data(game_pk)

        # Determine if our team is home or away
        team_info = boxscore.get("teamInfo", {})
        home_id = team_info.get("home", {}).get("id")
        away_id = team_info.get("away", {}).get("id")

        if home_id == team_mlb_id:
            pitchers_list = boxscore.get("homePitchers", [])
        elif away_id == team_mlb_id:
            pitchers_list = boxscore.get("awayPitchers", [])
        else:
            # Try to match by checking both sides
            # Fall back to checking the completed game's home/away team IDs
            home_team_id = completed[0].get("home_id")
            away_team_id = completed[0].get("away_id")
            if home_team_id == team_mlb_id:
                pitchers_list = boxscore.get("homePitchers", [])
            else:
                pitchers_list = boxscore.get("awayPitchers", [])

        result = {}
        # Skip index 0 — that's the header row (personId == 0)
        for pitcher in pitchers_list[1:]:
            person_id = pitcher.get("personId")
            if not person_id or int(person_id) == 0:
                continue
            pitches = pitcher.get("p", 0)
            try:
                pitches = int(pitches)
            except (ValueError, TypeError):
                pitches = 0
            result[int(person_id)] = {
                "name":   pitcher.get("name", ""),
                "pitches": pitches,
                "ip":     pitcher.get("ip", "0.0"),
            }

        return result

    except Exception as e:
        logger.error(f"Failed to get previous game pitchers for team {team_mlb_id}: {e}")
        return {}


def get_current_season_relievers(team_mlb_id: int, season: int = None) -> dict:
    """
    Fetch current-season pitching appearance data for a team from the MLB Stats API.

    Returns a dict keyed by MLB player ID:
        { mlb_id: {"name": str, "games": int, "ip": float, "is_reliever": bool} }

    A player is classified as a reliever if games_started / games < 0.5.
    Returns empty dict on any error.
    """
    if season is None:
        season = date.today().year
    try:
        data = statsapi.get("stats", {
            "stats":   "season",
            "group":   "pitching",
            "teamId":  team_mlb_id,
            "season":  season,
            "sportId": 1,
        })
        result = {}
        for entry in data.get("stats", [{}])[0].get("splits", []):
            stat   = entry.get("stat", {})
            player = entry.get("player", {})
            mlb_id = player.get("id")
            if not mlb_id:
                continue
            games         = int(stat.get("gamesPlayed", 0) or 0)
            games_started = int(stat.get("gamesStarted", 0) or 0)
            if games == 0:
                continue
            # Parse IP — stored as float (e.g. 2.2 means 2⅔ innings)
            try:
                ip_raw = float(stat.get("inningsPitched", 0) or 0)
                # Convert MLB's .1/.2 notation to true decimal
                ip_whole = int(ip_raw)
                ip_frac  = round(ip_raw - ip_whole, 1)
                ip = ip_whole + (ip_frac / 0.3 * (1/3))
            except (ValueError, TypeError):
                ip = 0.0
            is_reliever = (games_started / games) < 0.5
            result[mlb_id] = {
                "name":        player.get("fullName", ""),
                "games":       games,
                "games_started": games_started,
                "ip":          round(ip, 1),
                "is_reliever": is_reliever,
            }
        return result
    except Exception as e:
        logger.error(f"Failed to fetch current season relievers for team {team_mlb_id}: {e}")
        return {}


def get_historical_schedule(start_date: str, end_date: str) -> list:
    """
    Fetch all games between two dates. Used for backtesting.
    Date format: 'MM/DD/YYYY'
    """
    try:
        return statsapi.schedule(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.error(f"Failed to fetch historical schedule: {e}")
        return []
