"""
auto_import.py — Daily scheduled import script.

Runs without the Flask web server — uses the app's database directly.
Typically called by the Claude scheduled task every morning at 8 AM.

What it does:
  1. Imports games for TODAY from the MLB Stats API (new games only)
  2. Imports games for TOMORROW so you can start reviewing matchups early
  3. Settles any finished games from YESTERDAY (fetches final scores + grades bets)
  4. Refreshes odds from The Odds API for today's games

Usage:
  python auto_import.py
  python auto_import.py --date 2026-04-05   (import a specific date instead)
"""
import os
import sys
import logging
from datetime import date, timedelta

# Make sure we can import the app
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

# Silence non-critical logs during script run
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("auto_import")
logger.setLevel(logging.INFO)

# Bootstrap Flask app context (no web server needed)
import app as flask_app
ctx = flask_app.app.app_context()
ctx.push()

from database.schema import db, Game, Team, Player, Odds, BetRecommendation, BankrollLog
from data.mlb_api import get_schedule_for_date, get_game_result
from data.odds_api import get_odds, parse_odds
from models.kelly import american_to_decimal

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# ── helpers copied from app.py context ────────────────────────────────────────

PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}


def _find_or_create_umpire(mlb_id, name):
    from database.schema import Umpire
    from data.umpire_data import UMPIRE_SEED_DATA
    if not name:
        return None
    ump = Umpire.query.filter_by(mlb_id=mlb_id).first() if mlb_id else None
    if not ump:
        ump = Umpire.query.filter(Umpire.name.ilike(f"%{name.split()[-1]}%")).first()
    if not ump:
        seed = next((s for s in UMPIRE_SEED_DATA if name.split()[-1].lower() in s[0].lower()), None)
        ump = Umpire(
            name=name,
            mlb_id=mlb_id,
            walk_rate_impact=seed[1] if seed else 0.0,
            k_rate_impact=seed[2] if seed else 0.0,
            runs_per_game_impact=seed[3] if seed else 0.0,
            called_strike_rate=seed[4] if seed else 0.329,
        )
        db.session.add(ump)
        db.session.flush()
    return ump


def _import_date(target_date: date) -> dict:
    """Import all games for target_date from the MLB API. Returns summary dict."""
    from data.mlb_api import get_umpire_for_game

    schedule = get_schedule_for_date(target_date)
    imported = skipped = 0
    errors = []

    for g in schedule:
        mlb_game_id = g.get("game_id")
        if not mlb_game_id:
            continue

        if Game.query.filter_by(mlb_game_id=mlb_game_id).first():
            skipped += 1
            continue

        home_team = Team.query.filter_by(mlb_id=g.get("home_id")).first()
        away_team = Team.query.filter_by(mlb_id=g.get("away_id")).first()

        if not home_team:
            hn = g.get("home_name", "")
            home_team = Team.query.filter(
                Team.name.ilike(f"%{hn.split()[-1]}%")
            ).first() if hn else None
        if not away_team:
            an = g.get("away_name", "")
            away_team = Team.query.filter(
                Team.name.ilike(f"%{an.split()[-1]}%")
            ).first() if an else None

        if not home_team or not away_team:
            errors.append(f"{g.get('away_name')} @ {g.get('home_name')}: teams not found")
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

        home_starter = find_starter(
            g.get("home_probable_pitcher_id"),
            g.get("home_probable_pitcher"),
            home_team.id,
        )
        away_starter = find_starter(
            g.get("away_probable_pitcher_id"),
            g.get("away_probable_pitcher"),
            away_team.id,
        )

        umpire_id = None
        try:
            ump_data = get_umpire_for_game(mlb_game_id)
            if ump_data:
                ump = _find_or_create_umpire(ump_data.get("id"), ump_data.get("name"))
                if ump:
                    umpire_id = ump.id
        except Exception:
            pass

        game = Game(
            mlb_game_id=mlb_game_id,
            game_date=target_date,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            home_starter_id=home_starter.id if home_starter else None,
            away_starter_id=away_starter.id if away_starter else None,
            ballpark_id=home_team.ballpark_id,
            umpire_id=umpire_id,
            status=g.get("status", "scheduled"),
        )
        db.session.add(game)
        imported += 1

    db.session.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}


def _settle_date(target_date: date) -> dict:
    """Fetch final scores for all non-final games on target_date and grade bets."""
    games = Game.query.filter(
        Game.status != "final",
        Game.mlb_game_id != None,
        Game.game_date == target_date,
    ).all()

    settled = skipped = 0
    for game in games:
        result = get_game_result(game.mlb_game_id)
        if not result:
            continue
        if result.get("status") not in {"Final", "Game Over", "Completed Early"}:
            skipped += 1
            continue

        game.home_score = result["home_score"]
        game.away_score = result["away_score"]
        game.total_runs = result["home_score"] + result["away_score"]
        game.home_win   = result["home_score"] > result["away_score"]
        game.status     = "final"
        db.session.commit()

        # Inline bet resolution (mirrors _resolve_bets in app.py)
        home_s = game.home_score
        away_s = game.away_score
        total  = home_s + away_s

        totals_row = Odds.query.filter_by(game_id=game.id, market="totals").order_by(
            Odds.fetched_at.desc()
        ).first()
        total_line = totals_row.total_line if totals_row else None

        recs = BetRecommendation.query.filter_by(game_id=game.id).filter(
            BetRecommendation.won == None
        ).all()

        for rec in recs:
            won = None
            if rec.bet_type == "moneyline":
                won = (home_s > away_s) if rec.side == "home" else (away_s > home_s)
            elif rec.bet_type == "totals" and total_line:
                if rec.side == "over":
                    won = total > total_line
                elif rec.side == "under":
                    won = total < total_line
            elif rec.bet_type == "runline":
                won = (home_s - away_s >= 2) if rec.side == "home" else (home_s - away_s <= 1)

            if won is not None:
                rec.won = won
                if rec.placed and rec.recommended_bet:
                    profit = (
                        round((american_to_decimal(rec.price) - 1) * rec.recommended_bet, 2)
                        if won else -round(rec.recommended_bet, 2)
                    )
                    rec.profit_loss = profit
                    latest = BankrollLog.query.order_by(BankrollLog.id.desc()).first()
                    current_br = latest.amount if latest else 1000.0
                    away_ab = game.away_team.abbreviation if game.away_team else "?"
                    home_ab = game.home_team.abbreviation if game.home_team else "?"
                    note = (
                        f"{'WIN' if won else 'LOSS'}: {rec.bet_type.upper()} {rec.side.upper()} "
                        f"@ {'+' if rec.price > 0 else ''}{rec.price} ({away_ab}@{home_ab})"
                    )
                    db.session.add(BankrollLog(amount=current_br + profit, change=profit, note=note))

        db.session.commit()
        settled += 1

    return {"settled": settled, "skipped": skipped}


def _refresh_odds() -> dict:
    """Pull latest odds from The Odds API and save to DB."""
    if not ODDS_API_KEY:
        return {"error": "No ODDS_API_KEY configured in .env"}

    from datetime import datetime
    raw    = get_odds(ODDS_API_KEY, markets="h2h,spreads,totals")
    parsed = parse_odds(raw)
    saved  = 0

    for game_data in parsed:
        home_team = Team.query.filter(
            Team.name.ilike(f"%{game_data['home_team'].split()[-1]}%")
        ).first()
        away_team = Team.query.filter(
            Team.name.ilike(f"%{game_data['away_team'].split()[-1]}%")
        ).first()
        if not home_team or not away_team:
            continue

        commence = game_data.get("commence_time", "")
        try:
            game_date = datetime.fromisoformat(commence.replace("Z", "+00:00")).date()
        except Exception:
            game_date = date.today()

        game = Game.query.filter_by(
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            game_date=game_date,
        ).first()
        if not game:
            continue

        for book in game_data.get("books", []):
            for market_key, market_data in book.get("markets", {}).items():
                row = Odds(
                    game_id=game.id,
                    bookmaker=book["bookmaker"],
                    market=market_key,
                    home_price=market_data.get("home_price"),
                    away_price=market_data.get("away_price"),
                    total_line=market_data.get("total_line"),
                    over_price=market_data.get("over_price"),
                    under_price=market_data.get("under_price"),
                )
                db.session.add(row)
                saved += 1

    db.session.commit()
    return {"odds_rows_saved": saved}


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Auto-import MLB games and settle bets")
    parser.add_argument("--date", help="Override today's date (YYYY-MM-DD)")
    args = parser.parse_args()

    today     = date.fromisoformat(args.date) if args.date else date.today()
    tomorrow  = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)

    print(f"\n{'='*55}")
    print(f"  Baseball Betting — Auto Import  ({today})")
    print(f"{'='*55}\n")

    # 1. Settle yesterday's finished games
    print(f"[1/4] Settling finished games from {yesterday}…")
    s = _settle_date(yesterday)
    print(f"      Settled: {s['settled']}  |  Still live/pending: {s['skipped']}\n")

    # 2. Import today's schedule
    print(f"[2/4] Importing today's schedule ({today})…")
    r = _import_date(today)
    print(f"      Imported: {r['imported']}  |  Already on file: {r['skipped']}")
    if r.get("errors"):
        for e in r["errors"]:
            print(f"      ⚠ {e}")
    print()

    # 3. Import tomorrow's schedule (so you can look ahead)
    print(f"[3/4] Importing tomorrow's schedule ({tomorrow})…")
    r2 = _import_date(tomorrow)
    print(f"      Imported: {r2['imported']}  |  Already on file: {r2['skipped']}")
    if r2.get("errors"):
        for e in r2["errors"]:
            print(f"      ⚠ {e}")
    print()

    # 4. Refresh live odds
    print("[4/4] Refreshing odds from The Odds API…")
    o = _refresh_odds()
    if "error" in o:
        print(f"      ⚠ {o['error']}")
    else:
        print(f"      Odds rows saved: {o['odds_rows_saved']}")
    print()

    print("✓ Done.\n")
    ctx.pop()
