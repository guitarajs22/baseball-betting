"""
Background scheduler for the baseball betting dashboard.

Jobs:
  import_games  — imports tomorrow's MLB schedule every day at 8 PM
  weather       — refreshes weather for today's scheduled games every hour
  umpires       — refreshes umpire assignments for today's games every day at 10 AM

Each job writes its result to SCHEDULER_LOG so the UI can show last-run status.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# In-memory log — survives for the lifetime of the process.
# Structure: { job_id: { "last_run": ISO str, "status": "ok"|"error", "detail": str, "next_run": ISO str } }
SCHEDULER_LOG: dict = {
    "import_games": {"last_run": None, "status": None, "detail": "Never run", "next_run": None},
    "odds":         {"last_run": None, "status": None, "detail": "Never run", "next_run": None},
    "weather":      {"last_run": None, "status": None, "detail": "Never run", "next_run": None},
    "umpires":      {"last_run": None, "status": None, "detail": "Never run", "next_run": None},
}

_scheduler: Optional[BackgroundScheduler] = None


def _log(job_id: str, status: str, detail: str) -> None:
    SCHEDULER_LOG[job_id]["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SCHEDULER_LOG[job_id]["status"]   = status
    SCHEDULER_LOG[job_id]["detail"]   = detail


# ── Venue overrides ───────────────────────────────────────────────────────
# When MLB plays games at a non-team-stadium venue (Mexico Series, Tokyo
# Series, London Series, Field of Dreams, etc.), the schedule API returns
# the actual venue's MLB venue_id but the home team is unchanged. Without
# an override, every game inherits the home team's regular ballpark, so
# park factors / altitude / weather all come from the wrong place.
#
# Map MLB API venue_id → Ballpark.name; the ballpark must already be seeded
# in init_db()'s _intl_venues block. If the lookup misses, we fall back to
# the home team's regular ballpark.
_MLB_VENUE_OVERRIDES = {
    5340: "Estadio Alfredo Harp Helú",   # Mexico City — Mexico Series
    # 4249: "Tokyo Dome",                 # Tokyo Series (add when seeded)
    # 2:    "London Stadium",             # London Series (add when seeded)
}


def _resolve_ballpark_id(mlb_venue_id, home_team):
    """
    Pick the right ballpark for a game.
    - If the schedule's MLB venue_id is in the override map, use that ballpark.
    - Otherwise, fall back to the home team's stadium.
    Returns (ballpark_id_or_None, override_was_applied: bool).
    """
    from database.schema import Ballpark
    if mlb_venue_id and mlb_venue_id in _MLB_VENUE_OVERRIDES:
        bp_name = _MLB_VENUE_OVERRIDES[mlb_venue_id]
        bp = Ballpark.query.filter_by(name=bp_name).first()
        if bp:
            return bp.id, True
        # Override registered but ballpark missing — log and fall through
        logger.warning(f"[scheduler] venue override {mlb_venue_id} → {bp_name} not in DB; using home team's stadium")
    return (home_team.ballpark_id if home_team else None), False


# ── Job 1: Import tomorrow's games ─────────────────────────────────────────

def run_import_games(app, target_date: Optional[date] = None) -> dict:
    """
    Import MLB schedule for `target_date` (defaults to tomorrow).
    Returns a summary dict.
    """
    with app.app_context():
        from database.schema import db, Game, Team, Player, Lineup
        from data.mlb_api import get_schedule_for_date, get_umpire_for_game
        from app import _find_or_create_umpire

        if target_date is None:
            target_date = date.today() + timedelta(days=1)

        schedule = get_schedule_for_date(target_date)
        imported, skipped, errors = 0, 0, []
        PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}

        for g in schedule:
            mlb_game_id = g.get("game_id")
            if not mlb_game_id:
                continue

            existing_game = Game.query.filter_by(mlb_game_id=mlb_game_id).first()
            if existing_game:
                # Handle postponements / reschedules: same game_id, new date
                mlb_status = (g.get("status") or "").strip()
                updated_fields = []
                if existing_game.game_date != target_date and mlb_status not in ("Postponed", "Cancelled"):
                    existing_game.game_date = target_date
                    existing_game.status = "scheduled"
                    updated_fields.append("rescheduled")
                elif mlb_status in ("Postponed", "Cancelled") and existing_game.status not in ("postponed", "cancelled"):
                    existing_game.status = mlb_status.lower()
                    updated_fields.append("status")

                # Re-resolve ballpark for existing games — corrects games imported
                # before an override was added, or when MLB swaps a venue late.
                eg_home = existing_game.home_team
                desired_bp_id, used_override = _resolve_ballpark_id(g.get("venue_id"), eg_home)
                if desired_bp_id and existing_game.ballpark_id != desired_bp_id:
                    old_bp_id = existing_game.ballpark_id
                    existing_game.ballpark_id = desired_bp_id
                    updated_fields.append("ballpark")
                    logger.info(f"[scheduler] Ballpark corrected for {existing_game.away_team.abbreviation}@{eg_home.abbreviation} "
                                f"on {existing_game.game_date}: {old_bp_id} → {desired_bp_id} "
                                f"(venue: {g.get('venue_name')}, override={used_override})")

                if updated_fields:
                    raw_dt = g.get("game_datetime")
                    if raw_dt:
                        try:
                            existing_game.game_time_utc = datetime.strptime(raw_dt, "%Y-%m-%dT%H:%M:%SZ")
                        except ValueError:
                            pass
                    imported += 1
                else:
                    skipped += 1
                continue

            home_team = Team.query.filter_by(mlb_id=g.get("home_id")).first()
            away_team = Team.query.filter_by(mlb_id=g.get("away_id")).first()
            if not home_team:
                hn = g.get("home_name", "")
                home_team = Team.query.filter(Team.name.ilike(f"%{hn.split()[-1]}%")).first() if hn else None
            if not away_team:
                an = g.get("away_name", "")
                away_team = Team.query.filter(Team.name.ilike(f"%{an.split()[-1]}%")).first() if an else None

            if not home_team or not away_team:
                errors.append(f"Unmatched: {g.get('away_name')} @ {g.get('home_name')}")
                continue

            def find_starter(mlb_id, name, team_id):
                if mlb_id:
                    p = Player.query.filter_by(mlb_id=mlb_id).first()
                    if p:
                        return p
                if name:
                    p = Player.query.filter_by(name=name, team_id=team_id).first()
                    if p:
                        return p
                    last = name.split()[-1]
                    p = Player.query.filter(
                        Player.team_id == team_id,
                        Player.name.ilike(f"%{last}%"),
                        Player.position.in_(PITCHER_POSITIONS),
                    ).first()
                    if p:
                        return p
                return None

            home_starter = find_starter(g.get("home_probable_pitcher_id"), g.get("home_probable_pitcher"), home_team.id)
            away_starter = find_starter(g.get("away_probable_pitcher_id"), g.get("away_probable_pitcher"), away_team.id)

            umpire_id = None
            try:
                ump_data = get_umpire_for_game(mlb_game_id)
                if ump_data:
                    ump = _find_or_create_umpire(ump_data.get("id"), ump_data.get("name"))
                    if ump:
                        umpire_id = ump.id
            except Exception:
                pass

            # Parse first-pitch time from MLB API (always UTC)
            game_time_utc = None
            raw_dt = g.get("game_datetime")
            if raw_dt:
                try:
                    game_time_utc = datetime.strptime(raw_dt, "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    pass

            # Resolve ballpark — handles international/neutral-site overrides
            ballpark_id, used_override = _resolve_ballpark_id(g.get("venue_id"), home_team)
            if used_override:
                logger.info(f"[scheduler] Venue override: {away_team.abbreviation}@{home_team.abbreviation} on {target_date} "
                            f"playing at {g.get('venue_name')} (MLB venue_id={g.get('venue_id')})")

            game = Game(
                mlb_game_id=mlb_game_id,
                game_date=target_date,
                game_time_utc=game_time_utc,
                home_team_id=home_team.id,
                away_team_id=away_team.id,
                home_starter_id=home_starter.id if home_starter else None,
                away_starter_id=away_starter.id if away_starter else None,
                ballpark_id=ballpark_id,
                umpire_id=umpire_id,
                status=g.get("status", "scheduled"),
                game_number=int(g.get("game_num", 1) or 1),
                game_type=g.get("game_type", "R") or "R",
            )
            db.session.add(game)
            db.session.flush()

            # Seed projected lineup from most recent previous game
            for team_id in [home_team.id, away_team.id]:
                prev_game = (
                    Game.query
                    .join(Lineup, Lineup.game_id == Game.id)
                    .filter(
                        Lineup.team_id == team_id,
                        Lineup.is_starter == True,
                        Lineup.batting_order.between(1, 9),
                        Game.id != game.id,
                    )
                    .order_by(Game.game_date.desc(), Game.id.desc())
                    .first()
                )
                if not prev_game:
                    continue
                prev_lineup = (
                    Lineup.query
                    .filter_by(game_id=prev_game.id, team_id=team_id, is_starter=True)
                    .filter(Lineup.batting_order.between(1, 9))
                    .order_by(Lineup.batting_order)
                    .all()
                )
                for slot in prev_lineup:
                    db.session.add(Lineup(
                        game_id=game.id, team_id=team_id,
                        player_id=slot.player_id, batting_order=slot.batting_order,
                        position=slot.position, is_starter=True,
                    ))
            imported += 1

        db.session.commit()

        # Flag doubleheaders
        from sqlalchemy import func
        dh_pairs = (
            db.session.query(Game.home_team_id, Game.game_date)
            .filter(Game.game_date == target_date)
            .group_by(Game.home_team_id, Game.game_date)
            .having(func.count(Game.id) > 1)
            .all()
        )
        for home_team_id, gdate in dh_pairs:
            for dh_game in Game.query.filter_by(home_team_id=home_team_id, game_date=gdate).all():
                dh_game.is_doubleheader = True
        db.session.commit()

        return {"imported": imported, "skipped": skipped, "errors": errors, "date": target_date.isoformat()}


def _job_import_games(app) -> None:
    logger.info("[scheduler] import_games: starting")
    try:
        result = run_import_games(app)
        detail = f"Imported {result['imported']} game(s) for {result['date']}, {result['skipped']} skipped"
        if result["errors"]:
            detail += f" | {len(result['errors'])} errors"
        _log("import_games", "ok", detail)
        logger.info(f"[scheduler] import_games: {detail}")
    except Exception as e:
        _log("import_games", "error", str(e))
        logger.error(f"[scheduler] import_games error: {e}")


# ── Job 2: Refresh weather ──────────────────────────────────────────────────

def run_weather_refresh(app, target_date: Optional[date] = None) -> dict:
    """
    Refresh weather for all non-dome, non-final games on `target_date` (defaults to today).
    Also always refreshes tomorrow's games using the forecast API so overnight bets
    have accurate weather data.

    - Today's games  → current weather API (real-time)
    - Tomorrow's games → 5-day forecast API (closest 3-hour block to first pitch)

    Returns a summary dict.
    """
    with app.app_context():
        import os
        from database.schema import db, Game
        from data.weather import fetch_weather, fetch_weather_forecast

        WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
        if not WEATHER_API_KEY:
            return {"error": "No WEATHER_API_KEY configured", "updated": [], "skipped": [], "failed": []}

        if target_date is None:
            target_date = date.today()

        tomorrow = target_date + timedelta(days=1)

        # Collect games for today AND tomorrow
        games = Game.query.filter(
            Game.game_date.in_([target_date, tomorrow]),
            Game.status != "final",
        ).all()

        updated, skipped, failed = [], [], []

        for game in games:
            park  = game.ballpark
            label = (
                f"{game.away_team.abbreviation if game.away_team else '?'}"
                f" @ {game.home_team.abbreviation if game.home_team else '?'}"
                f" ({game.game_date})"
            )

            if not park or park.latitude is None or park.longitude is None:
                failed.append({"game": label, "reason": "No ballpark coordinates"})
                continue
            if park.roof_type == "dome":
                skipped.append({"game": label, "reason": "dome"})
                continue

            try:
                is_tomorrow = (game.game_date == tomorrow)

                if is_tomorrow:
                    # Use forecast API — find slot closest to first pitch time
                    # If game_time_utc is unknown, default to 7pm Eastern (23:00 UTC)
                    if game.game_time_utc:
                        target_utc = game.game_time_utc
                    else:
                        target_utc = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 0, 0)
                    w = fetch_weather_forecast(
                        park.latitude, park.longitude, WEATHER_API_KEY, target_utc
                    )
                else:
                    # Use real-time current weather for today
                    w = fetch_weather(park.latitude, park.longitude, WEATHER_API_KEY)

                game.temperature_f      = w.get("temperature_f")
                game.wind_speed_mph     = w.get("wind_speed_mph")
                game.wind_direction     = w.get("wind_compass")
                game.wind_deg           = w.get("wind_deg")
                game.humidity_pct       = w.get("humidity_pct")
                game.pressure_inhg      = w.get("pressure_inhg")
                game.weather_fetched_at = datetime.utcnow()

                entry = {"game": label, "temp_f": w.get("temperature_f"),
                         "wind": f"{w.get('wind_speed_mph')} mph {w.get('wind_compass')}"}
                if is_tomorrow:
                    entry["forecast_slot"] = w.get("forecast_time", "")
                updated.append(entry)

            except Exception as e:
                failed.append({"game": label, "reason": str(e)})

        db.session.commit()
        return {"updated": updated, "skipped": skipped, "failed": failed}


def _job_weather(app) -> None:
    logger.info("[scheduler] weather: starting")
    try:
        result = run_weather_refresh(app)
        if "error" in result:
            _log("weather", "error", result["error"])
            return
        detail = f"Updated {len(result['updated'])} game(s), {len(result['skipped'])} dome(s) skipped"
        if result["failed"]:
            detail += f", {len(result['failed'])} failed"
        _log("weather", "ok", detail)
        logger.info(f"[scheduler] weather: {detail}")
    except Exception as e:
        _log("weather", "error", str(e))
        logger.error(f"[scheduler] weather error: {e}")


# ── Job 3: Refresh umpire assignments ──────────────────────────────────────

def run_umpire_refresh(app, target_date: Optional[date] = None) -> dict:
    """
    Check for updated home-plate umpire assignments for all scheduled games
    on `target_date` (defaults to today).
    """
    with app.app_context():
        from database.schema import db, Game
        from data.mlb_api import get_umpire_for_game
        from app import _find_or_create_umpire

        if target_date is None:
            target_date = date.today()

        games = Game.query.filter(
            Game.game_date == target_date,
            Game.status != "final",
            Game.mlb_game_id.isnot(None),
        ).all()

        updated, skipped, failed = 0, 0, 0

        for game in games:
            try:
                ump_data = get_umpire_for_game(game.mlb_game_id)
                if not ump_data:
                    skipped += 1
                    continue
                ump = _find_or_create_umpire(ump_data.get("id"), ump_data.get("name"))
                if ump and game.umpire_id != ump.id:
                    game.umpire_id = ump.id
                    updated += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                logger.warning(f"[scheduler] umpire fetch failed for game {game.id}: {e}")

        db.session.commit()
        return {"updated": updated, "skipped": skipped, "failed": failed}


def _job_umpires(app) -> None:
    logger.info("[scheduler] umpires: starting")
    try:
        result = run_umpire_refresh(app)
        detail = f"Updated {result['updated']} assignment(s), {result['skipped']} already set, {result['failed']} failed"
        _log("umpires", "ok", detail)
        logger.info(f"[scheduler] umpires: {detail}")
    except Exception as e:
        _log("umpires", "error", str(e))
        logger.error(f"[scheduler] umpires error: {e}")


# ── Job 4: Refresh odds ────────────────────────────────────────────────────

def run_odds_refresh(app) -> dict:
    """
    Fetch latest odds from The Odds API and update the database.
    Also imports today's and tomorrow's games if they aren't in the DB yet
    so odds always have a game row to attach to.
    Returns a summary dict.
    """
    with app.app_context():
        import os
        from database.schema import db, Game, Team, Odds
        from data.odds_api import get_odds, parse_odds

        api_key = os.getenv("ODDS_API_KEY", "")
        if not api_key:
            return {"error": "No ODDS_API_KEY configured", "updated": 0, "matched": 0}

        # Ensure today's and tomorrow's games exist in DB before refreshing odds
        from datetime import timezone, timedelta as td
        for delta in (0, 1):
            target = date.today() + timedelta(days=delta)
            run_import_games(app, target_date=target)

        raw = get_odds(api_key, markets="h2h,spreads,totals")
        if not raw:
            return {"error": "No data returned from Odds API", "updated": 0, "matched": 0}

        parsed = parse_odds(raw)

        def find_team(api_name):
            t = Team.query.filter(Team.name.ilike(api_name)).first()
            if t:
                return t
            words = api_name.split()
            if len(words) >= 2:
                t = Team.query.filter(Team.name.ilike(f"%{' '.join(words[-2:])}%")).first()
                if t:
                    return t
            if words:
                t = Team.query.filter(Team.name.ilike(f"%{words[-1]}%")).first()
                if t:
                    return t
            return None

        updated = 0
        matched = 0

        for game_data in parsed:
            home_team = find_team(game_data["home_team"])
            away_team = find_team(game_data["away_team"])
            if not home_team or not away_team:
                continue

            commence_time = game_data.get("commence_time", "")
            try:
                from datetime import timezone
                utc_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                eastern_dt = utc_dt.astimezone(timezone(timedelta(hours=-4)))
                game_date = eastern_dt.date()
            except Exception:
                game_date = date.today()

            game = Game.query.filter_by(
                home_team_id=home_team.id,
                away_team_id=away_team.id,
                game_date=game_date,
            ).first()
            if not game:
                for delta in (-1, 1):
                    game = Game.query.filter_by(
                        home_team_id=home_team.id,
                        away_team_id=away_team.id,
                        game_date=game_date + timedelta(days=delta),
                    ).first()
                    if game:
                        break
            if not game:
                continue
            matched += 1

            # Store Odds API event ID for F5 lookups
            if not game.odds_api_id and game_data.get("odds_api_id"):
                game.odds_api_id = game_data["odds_api_id"]

            for book in game_data.get("books", []):
                bookmaker = book["bookmaker"]
                for market_key, market_data in book.get("markets", {}).items():
                    odds_row = Odds.query.filter_by(
                        game_id=game.id,
                        bookmaker=bookmaker,
                        market=market_key,
                    ).first()
                    if odds_row:
                        odds_row.home_price     = market_data.get("home_price")
                        odds_row.away_price     = market_data.get("away_price")
                        odds_row.total_line     = market_data.get("total_line")
                        odds_row.over_price     = market_data.get("over_price")
                        odds_row.under_price    = market_data.get("under_price")
                        odds_row.home_rl_spread = market_data.get("home_line")
                        odds_row.fetched_at     = datetime.utcnow()
                    else:
                        odds_row = Odds(
                            game_id=game.id,
                            bookmaker=bookmaker,
                            market=market_key,
                            home_price=market_data.get("home_price"),
                            away_price=market_data.get("away_price"),
                            total_line=market_data.get("total_line"),
                            over_price=market_data.get("over_price"),
                            under_price=market_data.get("under_price"),
                            home_rl_spread=market_data.get("home_line"),
                            fetched_at=datetime.utcnow(),
                        )
                        db.session.add(odds_row)
                    updated += 1

            # Fetch F5 odds via the per-event endpoint.
            # GATED: the per-event F5 call costs ~2 credits per game (~24-30
            # credits on a 12-15 game slate), which dominated our burn rate.
            # Off by default — set FETCH_F5_ODDS=1 to re-enable for the auto
            # refresh. Game-detail pages can fetch on-demand instead.
            f5_enabled = os.getenv("FETCH_F5_ODDS", "0") == "1"
            if f5_enabled and game.odds_api_id:
                from data.odds_api import get_f5_odds
                api_key_env = os.getenv("ODDS_API_KEY", "")
                f5_data = get_f5_odds(api_key_env, game.odds_api_id)
                for f5_market, f5_book_list in f5_data.items():
                    for f5_book in f5_book_list:
                        bk = f5_book.get("bookmaker")
                        if not bk:
                            continue
                        f5_row = Odds.query.filter_by(
                            game_id=game.id, bookmaker=bk, market=f5_market
                        ).first()
                        if f5_row:
                            f5_row.home_price     = f5_book.get("home_price")
                            f5_row.away_price     = f5_book.get("away_price")
                            f5_row.total_line     = f5_book.get("total_line")
                            f5_row.over_price     = f5_book.get("over_price")
                            f5_row.under_price    = f5_book.get("under_price")
                            f5_row.home_rl_spread = f5_book.get("home_line")
                            f5_row.fetched_at     = datetime.utcnow()
                        else:
                            db.session.add(Odds(
                                game_id=game.id, bookmaker=bk, market=f5_market,
                                home_price=f5_book.get("home_price"),
                                away_price=f5_book.get("away_price"),
                                total_line=f5_book.get("total_line"),
                                over_price=f5_book.get("over_price"),
                                under_price=f5_book.get("under_price"),
                                home_rl_spread=f5_book.get("home_line"),
                                fetched_at=datetime.utcnow(),
                            ))
                        updated += 1

        db.session.commit()

        # Recalculate F5 O/U on existing sims when a new F5 total line arrived
        from database.schema import SimulationResult
        today = date.today()
        future_game_ids = [g.id for g in Game.query.filter(Game.game_date >= today).all()]
        for gid in future_game_ids:
            sim = SimulationResult.query.filter_by(game_id=gid).order_by(
                SimulationResult.created_at.desc()
            ).first()
            if not sim or not sim.score_distribution or sim.f5_home_win_pct is None:
                continue
            f5_tot = Odds.query.filter_by(game_id=gid, market="f5_totals").order_by(
                Odds.fetched_at.desc()
            ).first()
            if f5_tot and f5_tot.total_line:
                import json as _json
                import numpy as _np
                dist = _json.loads(sim.score_distribution)
                hf5 = _np.array(dist.get("home_f5_scores", []))
                af5 = _np.array(dist.get("away_f5_scores", []))
                if len(hf5) > 0:
                    f5_totals = hf5 + af5
                    nf5 = len(f5_totals)
                    sim.f5_over_pct  = round(float(_np.sum(f5_totals > f5_tot.total_line) / nf5), 4)
                    sim.f5_under_pct = round(float(_np.sum(f5_totals < f5_tot.total_line) / nf5), 4)
                    sim.f5_simulated_total_line = f5_tot.total_line
        db.session.commit()

        return {"updated": updated, "matched": matched, "games_in_api": len(parsed)}


def _job_odds(app) -> None:
    logger.info("[scheduler] odds: starting")
    try:
        result = run_odds_refresh(app)
        if "error" in result:
            _log("odds", "error", result["error"])
            return
        detail = f"Updated {result['updated']} odds rows across {result['matched']} game(s)"
        _log("odds", "ok", detail)
        logger.info(f"[scheduler] odds: {detail}")
    except Exception as e:
        _log("odds", "error", str(e))
        logger.error(f"[scheduler] odds error: {e}")


# ── Scheduler init ──────────────────────────────────────────────────────────

def init_scheduler(app) -> None:
    """Start the background scheduler. Call this once at app startup."""
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)

    # Import tomorrow's games every day at 8:00 PM local time
    _scheduler.add_job(
        lambda: _job_import_games(app),
        CronTrigger(hour=20, minute=0),
        id="import_games",
        name="Import tomorrow's games",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Refresh odds every 6 hours.
    # Burn math (with FETCH_F5_ODDS off — the default):
    #   ~6 credits per refresh × 4 refreshes/day = ~24 credits/day
    #   = ~720 credits/month. Configurable via ODDS_REFRESH_HOURS env var.
    _odds_interval_hours = int(os.getenv("ODDS_REFRESH_HOURS", "6") or 6)
    _scheduler.add_job(
        lambda: _job_odds(app),
        IntervalTrigger(hours=_odds_interval_hours),
        id="odds",
        name="Refresh odds",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Refresh weather every hour
    _scheduler.add_job(
        lambda: _job_weather(app),
        IntervalTrigger(hours=1),
        id="weather",
        name="Refresh weather",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Refresh umpire assignments every day at 10:00 AM local time
    _scheduler.add_job(
        lambda: _job_umpires(app),
        CronTrigger(hour=10, minute=0),
        id="umpires",
        name="Refresh umpire assignments",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(f"[scheduler] Started — import_games@20:00, odds@{_odds_interval_hours}h, weather@hourly, umpires@10:00")

    # Startup-fire: only weather (no API cost). Odds startup-fire is GATED —
    # off by default so Railway redeploys don't burn credits. Set
    # FIRE_ODDS_ON_STARTUP=1 if you actually want a fresh fetch on every
    # container restart (and accept the ~6-credit hit per restart).
    import threading
    if os.getenv("FIRE_ODDS_ON_STARTUP", "0") == "1":
        logger.info("[scheduler] FIRE_ODDS_ON_STARTUP=1 → firing odds refresh now")
        threading.Thread(target=lambda: _job_odds(app),    daemon=True).start()
    else:
        logger.info("[scheduler] Skipping startup odds-fire (set FIRE_ODDS_ON_STARTUP=1 to enable)")
    threading.Thread(target=lambda: _job_weather(app), daemon=True).start()

    # Update next_run times in the log
    for job in _scheduler.get_jobs():
        if job.id in SCHEDULER_LOG and job.next_run_time:
            SCHEDULER_LOG[job.id]["next_run"] = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")


def get_status() -> dict:
    """Return current scheduler status for the API/UI."""
    status = {}
    for job_id, entry in SCHEDULER_LOG.items():
        status[job_id] = dict(entry)
        # Refresh next_run from live scheduler state
        if _scheduler:
            job = _scheduler.get_job(job_id)
            if job and job.next_run_time:
                status[job_id]["next_run"] = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
    return status
