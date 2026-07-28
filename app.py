"""
Baseball Betting Dashboard — Flask Web App
Run with: python app.py
Then open: http://localhost:5000
"""
import os
import csv as csv_mod
import json
import logging
import threading
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

load_dotenv()

from database.schema import db, Team, Player, PlayerStats, PitchingStats, Game, Lineup
from database.schema import SimulationResult, Odds, BetRecommendation, BankrollLog, Umpire, Ballpark
from database.schema import BullpenAvailability, AppSettings, TeamDefense, CatcherFraming
from data.odds_api import get_odds, parse_odds, get_best_price, get_remaining_requests
from data.mlb_api import get_todays_schedule, get_all_teams
from data.fangraphs import get_import_instructions
from data.weather import fetch_weather
from data.umpire_data import UMPIRE_SEED_DATA
from data.mlb_api import get_umpire_for_game
from models.simulation import (run_simulations, calculate_over_under, calculate_runline,
                                 GameInputs, BatterProfile, PitcherProfile,
                                 BullpenProfile, ParkFactors, WeatherFactors, UmpireFactors,
                                 DefenseFactors, CatcherFactors,
                                 build_batter_profile, build_pitcher_profile,
                                 build_pitcher_split_rates)
from models.kelly import analyze_bet, american_to_implied_prob, american_to_decimal
from models.calibration import calibrate_prob
from data.mlb_api import get_game_result, get_first_inning_result, get_f5_result
import scheduler as sched

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

# ---------------------------------------------------------------------------
# Database path — Railway-aware
# ---------------------------------------------------------------------------
# - Local dev: database/betting.db (next to this file)
# - Railway:   /data/betting.db on a persistent volume
#
# Railway sets DATABASE_URL to something like `sqlite:////data/betting.db`.
# On first boot, if the volume is empty, we seed it from the repo copy so
# the deployed app starts with our imported teams/players/bets/bankroll.
_REPO_DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "database", "betting.db"))
_DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_REPO_DB}")

if _DATABASE_URL.startswith("sqlite:///"):
    # Strip the sqlite:// scheme to get the on-disk path.
    #   sqlite:///relative/path.db    -> relative/path.db
    #   sqlite:////absolute/path.db   -> /absolute/path.db
    sqlite_path = _DATABASE_URL[len("sqlite:///"):]
    # Flask-SQLAlchemy resolves relative paths against app.instance_path,
    # NOT the cwd. Always pin to an absolute path to avoid surprises.
    sqlite_path = os.path.abspath(sqlite_path)
    # Ensure parent directory exists (Railway mounts volumes like this)
    parent = os.path.dirname(sqlite_path)
    if parent and not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    # Seed from repo DB if target is empty (first Railway boot, fresh volume)
    if (not os.path.exists(sqlite_path) or os.path.getsize(sqlite_path) == 0) \
            and os.path.exists(_REPO_DB) and sqlite_path != _REPO_DB:
        try:
            import shutil
            shutil.copyfile(_REPO_DB, sqlite_path)
            logger.info(f"Seeded database from {_REPO_DB} -> {sqlite_path}")
        except OSError as e:
            logger.warning(f"Could not seed database to {sqlite_path}: {e}")
    # Rebuild URI with 4-slash absolute form
    _DATABASE_URL = f"sqlite:///{sqlite_path}"

app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
BANKROLL_START  = 1000.0

# ---------------------------------------------------------------------------
# Auth (Flask-Login, single hardcoded user)
# ---------------------------------------------------------------------------
APP_USERNAME = os.getenv("APP_USERNAME", "andrew")
APP_PASSWORD = os.getenv("APP_PASSWORD", "WhatsTheEdge")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class AppUser(UserMixin):
    """Single-user auth — no database table, just the env-var credentials."""
    def __init__(self, username):
        self.id = username

    def get_id(self):
        return self.id


@login_manager.user_loader
def load_user(user_id):
    if user_id == APP_USERNAME:
        return AppUser(APP_USERNAME)
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == APP_USERNAME and password == APP_PASSWORD:
            login_user(AppUser(APP_USERNAME), remember=True)
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.before_request
def _require_login():
    """Require login for every route except login/logout/static."""
    public = {"login", "logout", "static"}
    if request.endpoint in public:
        return None
    if current_user.is_authenticated:
        return None
    # API-style endpoints → 401; otherwise redirect to /login
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))

# ---------------------------------------------------------------------------
# Jinja2 filters
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Timezone filters — DB stores naive UTC, UI shows Pacific (PDT/PST aware)
# ---------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    _PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    # Fallback: pure -7 offset. Only hits if zoneinfo + tzdata both missing.
    from datetime import timezone as _tz, timedelta as _td
    _PACIFIC_TZ = _tz(_td(hours=-7))


def _to_pacific(utc_dt):
    """Treat a naive datetime as UTC and return a Pacific-aware datetime."""
    if utc_dt is None:
        return None
    from datetime import timezone as _tz
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=_tz.utc)
    return utc_dt.astimezone(_PACIFIC_TZ)


@app.template_filter("pacific_time")
def pacific_time_filter(utc_dt):
    """Convert a UTC datetime to Pacific time + DST-correct abbr, e.g. '4:05 PM PDT'."""
    local = _to_pacific(utc_dt)
    if local is None:
        return ""
    abbr = local.strftime("%Z") or "PT"
    return local.strftime("%-I:%M %p") + f" {abbr}"


@app.template_filter("pacific")
def pacific_filter(utc_dt, fmt="%-I:%M %p"):
    """Flexible Pacific-time formatter — pass any strftime format string."""
    local = _to_pacific(utc_dt)
    if local is None:
        return ""
    return local.strftime(fmt)


# ---------------------------------------------------------------------------
# Helper: get current bankroll
# ---------------------------------------------------------------------------

def get_current_bankroll() -> float:
    latest = BankrollLog.query.order_by(BankrollLog.id.desc()).first()
    return latest.amount if latest else BANKROLL_START


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

@app.template_filter("last_first")
def last_first_filter(name: str) -> str:
    """Convert 'First Last' → 'Last, First'.  Handles Jr./Sr./II etc."""
    if not name:
        return name or ""
    parts = name.strip().split()
    if len(parts) == 1:
        return name
    # Keep generational suffixes glued to the surname
    if len(parts) >= 3 and parts[-1].lower().rstrip(".") in _NAME_SUFFIXES:
        last  = " ".join(parts[-2:])   # e.g. "Guerrero Jr."
        first = " ".join(parts[:-2])   # e.g. "Vladimir"
    else:
        last  = parts[-1]
        first = " ".join(parts[:-1])
    return f"{last}, {first}" if first else last


@app.context_processor
def inject_bankroll():
    """Inject bankroll_display into every template so the navbar always shows it."""
    try:
        amount = get_current_bankroll()
        return {"bankroll_display": f"{amount:,.2f}"}
    except Exception:
        return {"bankroll_display": "—"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    """Main dashboard — bet watchlist, top picks, recent results."""
    today = date.today()
    bankroll = get_current_bankroll()

    # ── Placed bet watchlist: all games with at least one open placed bet ──
    # Uses a 3-day window (yesterday → tomorrow) so bets never fall off due to
    # game_date mismatches, late-night logging, or games stored under the wrong date.
    from datetime import timedelta
    window_start = today - timedelta(days=1)
    window_end   = today + timedelta(days=1)
    placed_today = (
        BetRecommendation.query
        .join(Game, BetRecommendation.game_id == Game.id)
        .filter(
            Game.game_date >= window_start,
            Game.game_date <= window_end,
            BetRecommendation.placed == True,  # noqa: E712
            BetRecommendation.won == None,     # noqa: E711 — pending only, not settled
        )
        .all()
    )
    watchlist_game_ids = list({b.game_id for b in placed_today})
    watchlist_games    = (
        Game.query
        .filter(Game.id.in_(watchlist_game_ids))
        .order_by(Game.game_date.asc(), Game.game_time_utc.asc())
        .all()
        if watchlist_game_ids else []
    )
    # Group bets by game for easy template access
    bets_by_game = {}
    for bet in placed_today:
        bets_by_game.setdefault(bet.game_id, []).append(bet)

    # ── Top picks: today's unplaced recs with positive edge, best edge first ──
    top_picks = (
        BetRecommendation.query
        .join(Game, BetRecommendation.game_id == Game.id)
        .filter(
            Game.game_date == today,
            BetRecommendation.placed == False,  # noqa: E712
            BetRecommendation.won == None,       # noqa: E711
            BetRecommendation.edge_pct > 0,
        )
        .order_by(BetRecommendation.edge_pct.desc())
        .limit(6)
        .all()
    )

    # ── Recent results: last 5 settled placed bets ────────────────────────────
    recent_results = (
        BetRecommendation.query
        .filter(
            BetRecommendation.placed == True,   # noqa: E712
            BetRecommendation.won != None,       # noqa: E711
        )
        .order_by(BetRecommendation.created_at.desc())
        .limit(5)
        .all()
    )

    # ── Overall stats bar ─────────────────────────────────────────────────────
    all_placed   = BetRecommendation.query.filter_by(placed=True).all()
    graded       = [b for b in all_placed if b.won is not None]
    bet_wins     = len([b for b in graded if b.won])
    bet_losses   = len([b for b in graded if not b.won])
    bet_pending  = len([b for b in all_placed if b.won is None])
    bet_total_pl = sum(b.profit_loss or 0 for b in all_placed if b.profit_loss is not None)
    total_wagered = sum((b.actual_bet_size or b.recommended_bet or 0) for b in graded)
    roi = (bet_total_pl / total_wagered * 100) if total_wagered > 0 else 0

    # CLV summary — only placed bets with closing line data
    clv_bets   = [b for b in all_placed if b.clv_cents is not None]
    avg_clv    = (sum(b.clv_cents for b in clv_bets) / len(clv_bets)) if clv_bets else None
    clv_sample = len(clv_bets)

    return render_template(
        "dashboard.html",
        today=today,
        bankroll=bankroll,
        watchlist_games=watchlist_games,
        bets_by_game=bets_by_game,
        top_picks=top_picks,
        recent_results=recent_results,
        bet_wins=bet_wins,
        bet_losses=bet_losses,
        bet_pending=bet_pending,
        bet_total_pl=bet_total_pl,
        roi=roi,
        avg_clv=avg_clv,
        clv_sample=clv_sample,
    )


@app.route("/api/live-scores")
def api_live_scores():
    """
    Return current live scores for a list of game IDs.
    Called by the dashboard every 60 seconds to update the watchlist.
    Query param: ?ids=1&ids=2&ids=3
    """
    from data.mlb_api import get_live_score
    game_ids = request.args.getlist("ids", type=int)
    if not game_ids:
        return jsonify({})

    games = Game.query.filter(Game.id.in_(game_ids)).all()
    result = {}
    for game in games:
        if game.status == "final":
            # Use stored scores — no need to hit the API
            result[game.id] = {
                "status":       "Final",
                "home_score":   game.home_score,
                "away_score":   game.away_score,
                "inning":       None,
                "inning_state": None,
            }
        elif game.mlb_game_id:
            live = get_live_score(game.mlb_game_id)
            if live:
                result[game.id] = live
                # Snapshot closing odds the first time a game goes live
                if (live.get("status") == "In Progress"
                        and game.status != "final"
                        and game.closing_odds_locked_at is None):
                    try:
                        _snapshot_closing_odds(game)
                        game.status = "live"
                        db.session.commit()
                    except Exception as e:
                        logger.warning(f"[CLV] snapshot failed for game {game.id}: {e}")
        else:
            result[game.id] = {
                "status":       game.status or "Scheduled",
                "home_score":   None,
                "away_score":   None,
                "inning":       None,
                "inning_state": None,
            }

    return jsonify(result)


@app.route("/games")
def games_list():
    """List all games with simulation status and odds."""
    target_date_str = request.args.get("date", date.today().isoformat())
    target_date = date.fromisoformat(target_date_str)
    games = Game.query.filter(
        Game.game_date == target_date,
        ~Game.status.in_(["postponed", "cancelled"]),
    ).all()

    # Build odds summary per game (preferred book: DraftKings → any available)
    PREFERRED_BOOKS = ["draftkings", "betmgm", "fanduel", "caesars"]
    odds_map = {}
    sims_map = {}
    for game in games:
        # Latest sim
        sim = SimulationResult.query.filter_by(game_id=game.id).order_by(
            SimulationResult.created_at.desc()
        ).first()
        sims_map[game.id] = sim

        # Pull all latest odds rows for this game
        all_odds = Odds.query.filter_by(game_id=game.id).order_by(
            Odds.fetched_at.desc()
        ).all()

        # Organize by market → bookmaker
        by_market = {}
        for row in all_odds:
            by_market.setdefault(row.market, {})[row.bookmaker] = row

        def pick_book(market_dict):
            """Return the odds row from the best available book."""
            for book in PREFERRED_BOOKS:
                if book in market_dict:
                    return market_dict[book]
            # Fall back to first available
            return next(iter(market_dict.values())) if market_dict else None

        h2h_row    = pick_book(by_market.get("h2h", {}))
        totals_row = pick_book(by_market.get("totals", {}))
        spreads_row = pick_book(by_market.get("spreads", {}))

        def implied(price):
            """American odds → implied probability as a percentage string."""
            if price is None:
                return None
            try:
                prob = american_to_implied_prob(int(price))
                return round(prob * 100, 1)
            except Exception:
                return None

        odds_map[game.id] = {
            "h2h": {
                "book": h2h_row.bookmaker if h2h_row else None,
                "home_price": h2h_row.home_price if h2h_row else None,
                "away_price": h2h_row.away_price if h2h_row else None,
                "home_prob":  implied(h2h_row.home_price) if h2h_row else None,
                "away_prob":  implied(h2h_row.away_price) if h2h_row else None,
            },
            "totals": {
                "book": totals_row.bookmaker if totals_row else None,
                "total_line": totals_row.total_line if totals_row else None,
                "over_price": totals_row.over_price if totals_row else None,
                "under_price": totals_row.under_price if totals_row else None,
            },
        }

    # Build recs_map: one entry per (game, bet_type, side) showing the latest
    # recommendation plus how much has already been placed on that side.
    game_ids = [g.id for g in games]
    recs_map = {}
    if game_ids:
        all_recs = (
            BetRecommendation.query
            .filter(BetRecommendation.game_id.in_(game_ids))
            .filter(BetRecommendation.won == None)       # noqa: E711
            .filter(BetRecommendation.edge_pct > 0)
            .order_by(BetRecommendation.edge_pct.desc())
            .all()
        )
        # Group by (game_id, bet_type, side): pick the best unplaced rec as the
        # "current" recommendation; accumulate already-placed wagers separately.
        from collections import defaultdict
        _best   = {}   # (game_id, bet_type, side) -> best unplaced rec
        _placed = defaultdict(float)  # (game_id, bet_type, side) -> total wagered

        for rec in all_recs:
            # Normalize side: strip line suffix so "over 8.5" and "over 8.0" both
            # map to "over" — ensures only the best-edged Over/Under rec shows per game.
            normalized_side = rec.side.split()[0] if rec.side else (rec.side or '')
            key = (rec.game_id, rec.bet_type, normalized_side)
            if rec.placed:
                _placed[key] += (rec.actual_bet_size or 0)
            else:
                if key not in _best:
                    _best[key] = rec

        # Attach already_wagered to each best rec, then add to recs_map
        for key, rec in _best.items():
            rec._already_wagered = _placed.get(key, 0)
            recs_map.setdefault(rec.game_id, []).append(rec)

        # Sort each game's list by edge descending
        for gid in recs_map:
            recs_map[gid].sort(key=lambda r: r.edge_pct, reverse=True)

    bankroll  = get_current_bankroll()
    min_edge  = float(get_setting('min_edge_pct', 6.0))

    # Pre-compute calibrated probabilities and threshold prices for each game.
    # This MUST match _generate_recommendations exactly so the game card can't
    # show a signal the rec engine would reject.
    #
    # Rules (same as _generate_recommendations, ML-permissive-cal validated +7.93% ROI):
    #   - ALL sides use calibrated probability (no raw-prob underdog bypass)
    #   - Edge floor: 8% (moneyline)
    #   - Skip if market-implied >= 65% (heavy-fav cut, -2.46% ROI in backtest)
    #   - Bet-size uses calibrated prob, so "Bet $X" only shows when CAL edge is positive
    ML_MIN_EDGE    = max(min_edge, 8.0)
    ML_MAX_IMPLIED = 0.65

    def _threshold_odds(our_prob, edge_floor):
        """Worst American price that clears BOTH the edge floor AND the heavy-fav cut.

        The rec engine rejects when book-implied >= 65% regardless of edge, so the
        widget must cap its threshold there. Otherwise the card would invite a bet
        the engine would never save.
        """
        target_implied = our_prob - edge_floor / 100.0
        if target_implied <= 0:
            return None   # no price can produce a positive edge
        # Cap at heavy-fav line: if 8% edge would need market-implied >= 65%,
        # the rec engine blocks it, so shown threshold should reflect reality.
        effective_implied = min(target_implied, ML_MAX_IMPLIED - 0.001)
        if effective_implied <= 0:
            return None
        decimal = 1.0 / effective_implied
        if decimal >= 2.0:
            return f"+{round((decimal - 1) * 100)}"
        return f"-{round(100 / (decimal - 1))}"

    thresholds_map = {}
    for game in games:
        sim = sims_map.get(game.id)
        if not sim:
            continue
        entry = {}
        for side, raw in [("home", sim.home_win_pct), ("away", sim.away_win_pct)]:
            cal = calibrate_prob(raw, "moneyline", side)
            thr = _threshold_odds(cal, ML_MIN_EDGE)
            entry[side] = {
                "raw":           round(raw * 100, 1),
                "cal":           round(cal * 100, 1),
                "threshold":     thr if thr else "—",
                "edge_floor":    ML_MIN_EDGE,
                "prob_for_calc": round(cal, 4),   # cal drives JS bet-size calc
            }
        thresholds_map[game.id] = entry

    return render_template(
        "games.html",
        games=games,
        target_date=target_date,
        odds_map=odds_map,
        sims_map=sims_map,
        recs_map=recs_map,
        thresholds_map=thresholds_map,
        bankroll=bankroll,
        min_edge=min_edge,
    )


@app.route("/game/<int:game_id>")
def game_detail(game_id):
    """Detailed view for one game — lineup, simulation, odds, recommendations."""
    game = Game.query.get_or_404(game_id)
    home_team = game.home_team
    away_team = game.away_team

    home_lineup_rows = Lineup.query.filter_by(game_id=game_id, team_id=game.home_team_id).order_by(
        Lineup.batting_order
    ).all()
    away_lineup_rows = Lineup.query.filter_by(game_id=game_id, team_id=game.away_team_id).order_by(
        Lineup.batting_order
    ).all()

    # Dicts for pre-selecting dropdowns in the inline edit form
    home_lineup_dict = {l.batting_order: l.player_id for l in home_lineup_rows}
    away_lineup_dict = {l.batting_order: l.player_id for l in away_lineup_rows}

    # Build a stats map for inline display: {player_id: PlayerStats row}
    # Seeded with lineup players here; extended below once home/away_batters
    # are available so pool chips in the drag-and-drop editor also show wRC+.
    _lineup_player_ids = [l.player_id for l in home_lineup_rows + away_lineup_rows]
    player_stats_map: dict = {}
    # display_wrc_map holds a PA-weighted blended wRC+ for display so that tiny
    # early-season samples don't mislead (e.g. 37 PA wRC+=20 overriding a 161
    # full-season). Blends current + prior year weighted by PA.
    display_wrc_map: dict = {}
    if _lineup_player_ids:
        _stats_rows = (
            PlayerStats.query
            .filter(
                PlayerStats.player_id.in_(_lineup_player_ids),
                PlayerStats.split == "overall",
            )
            .order_by(PlayerStats.season.desc())
            .all()
        )
        # Group by player_id keeping all seasons so we can blend
        from collections import defaultdict
        _seasons_by_player = defaultdict(list)
        for row in _stats_rows:
            _seasons_by_player[row.player_id].append(row)

        for pid, rows in _seasons_by_player.items():
            # rows already sorted season desc — first is most recent
            player_stats_map[pid] = rows[0]
            # Compute blended display wRC+
            cur  = rows[0]
            prev = rows[1] if len(rows) > 1 else None
            cur_pa  = cur.pa or 0
            cur_wrc = cur.wrc_plus
            FULL_SEASON_PA = 550
            if cur_wrc is not None and cur_pa >= FULL_SEASON_PA:
                # Full season — show as-is
                display_wrc_map[pid] = int(round(cur_wrc))
            elif cur_wrc is not None and prev and prev.wrc_plus is not None:
                # Blend: weight current by actual PA, prior by full-season equivalent
                prior_pa  = min(prev.pa or 0, FULL_SEASON_PA)
                blended   = (cur_wrc * cur_pa + prev.wrc_plus * prior_pa) / (cur_pa + prior_pa)
                display_wrc_map[pid] = int(round(blended))
            elif cur_wrc is not None:
                display_wrc_map[pid] = int(round(cur_wrc))
            elif prev and prev.wrc_plus is not None:
                display_wrc_map[pid] = int(round(prev.wrc_plus))

    # Pre-compute lineup quality stats for the display header —
    # saves doing messy loops inside Jinja2
    def _lineup_display_stats(lineup_rows, opp_throws="R"):
        """
        Returns a dict with:
          avg_wrc      — avg wRC+ across batters who have stats (int or None)
          platoon_count — number of batters with handedness advantage vs opp SP
          hot_count    — batters whose last-15-day wRC+ is 15+ above season avg
        """
        wrc_values, platoon_count, hot_count = [], 0, 0
        for row in lineup_rows:
            st = player_stats_map.get(row.player_id)
            # Use blended display wRC+ for avg so tiny early-season samples don't skew lineup quality
            blended_wrc = display_wrc_map.get(row.player_id)
            if blended_wrc is not None:
                wrc_values.append(blended_wrc)
            if st and st.wrc_plus is not None and st.wrc_plus_last_15 is not None:
                if st.wrc_plus_last_15 >= st.wrc_plus + 15:
                    hot_count += 1
            player = row.player
            if player and player.bats:
                bats = player.bats
                has_edge = (
                    bats == "S"
                    or (opp_throws == "R" and bats == "L")
                    or (opp_throws == "L" and bats == "R")
                )
                if has_edge:
                    platoon_count += 1
        return {
            "avg_wrc":      int(sum(wrc_values) / len(wrc_values)) if wrc_values else None,
            "platoon_count": platoon_count,
            "hot_count":    hot_count,
            "total":        len(lineup_rows),
        }

    _away_opp_throws = (game.home_starter.throws or "R") if game.home_starter else "R"
    _home_opp_throws = (game.away_starter.throws or "R") if game.away_starter else "R"
    away_lineup_stats = _lineup_display_stats(away_lineup_rows, _away_opp_throws)
    home_lineup_stats = _lineup_display_stats(home_lineup_rows, _home_opp_throws)

    # Player lists for inline lineup editor
    PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}

    def _batters_by_usage(team_id):
        """Return active non-pitchers for a team, sorted by how often they've
        appeared in any lineup in the DB (most-used first, then alphabetical)."""
        players = Player.query.filter(
            Player.team_id == team_id,
            Player.active == True,          # noqa: E712
            ~Player.position.in_(PITCHER_POSITIONS),
        ).all()

        # Count lineup appearances per player for this team
        counts = {
            row.player_id: row.appearances
            for row in db.session.query(
                Lineup.player_id,
                db.func.count(Lineup.id).label("appearances"),
            )
            .filter(Lineup.team_id == team_id)
            .group_by(Lineup.player_id)
            .all()
        }

        # Sort: most appearances first; ties broken alphabetically
        return sorted(players, key=lambda p: (-counts.get(p.id, 0), p.name))

    home_batters = _batters_by_usage(game.home_team_id)
    away_batters = _batters_by_usage(game.away_team_id)

    # Extend stats map to cover all pool players (not just those in the lineup)
    _pool_ids = [p.id for p in home_batters + away_batters
                 if p.id not in player_stats_map]
    if _pool_ids:
        _pool_seasons = defaultdict(list)
        for row in (PlayerStats.query
                    .filter(PlayerStats.player_id.in_(_pool_ids),
                            PlayerStats.split == "overall")
                    .order_by(PlayerStats.season.desc()).all()):
            _pool_seasons[row.player_id].append(row)
        for pid, rows in _pool_seasons.items():
            if pid not in player_stats_map:
                player_stats_map[pid] = rows[0]
            if pid not in display_wrc_map:
                cur  = rows[0]
                prev = rows[1] if len(rows) > 1 else None
                cur_pa  = cur.pa or 0
                cur_wrc = cur.wrc_plus
                FULL_SEASON_PA = 550
                if cur_wrc is not None and cur_pa >= FULL_SEASON_PA:
                    display_wrc_map[pid] = int(round(cur_wrc))
                elif cur_wrc is not None and prev and prev.wrc_plus is not None:
                    prior_pa = min(prev.pa or 0, FULL_SEASON_PA)
                    blended  = (cur_wrc * cur_pa + prev.wrc_plus * prior_pa) / (cur_pa + prior_pa)
                    display_wrc_map[pid] = int(round(blended))
                elif cur_wrc is not None:
                    display_wrc_map[pid] = int(round(cur_wrc))
                elif prev and prev.wrc_plus is not None:
                    display_wrc_map[pid] = int(round(prev.wrc_plus))

    def _pitchers_by_usage(team_id):
        """Return active pitchers for a team, sorted by starter appearances."""
        players = Player.query.filter(
            Player.team_id == team_id,
            Player.active == True,          # noqa: E712
            Player.position.in_(PITCHER_POSITIONS),
        ).all()
        counts = {
            row.player_id: row.appearances
            for row in db.session.query(
                Lineup.player_id,
                db.func.count(Lineup.id).label("appearances"),
            )
            .filter(Lineup.team_id == team_id)
            .group_by(Lineup.player_id)
            .all()
        }
        return sorted(players, key=lambda p: (-counts.get(p.id, 0), p.name))

    home_pitchers = _pitchers_by_usage(game.home_team_id)
    away_pitchers = _pitchers_by_usage(game.away_team_id)
    all_umpires = Umpire.query.order_by(Umpire.name).all()

    # ── Bullpen availability ──────────────────────────────────────────────────
    from datetime import timedelta

    def _reliever_pool(team_id):
        """
        All active pitchers for a team with rest-day availability info.
        Sorted: 3+ days rest first, then unknown (no data), then limited, then tired.
        The selected starter for THIS game is excluded from the pool.
        """
        starter_ids = {game.home_starter_id, game.away_starter_id} - {None}
        pitchers = Player.query.filter(
            Player.team_id == team_id,
            Player.active == True,          # noqa: E712
            Player.position.in_(PITCHER_POSITIONS),
            ~Player.id.in_(starter_ids),
        ).all()

        cutoff = game.game_date - timedelta(days=7)

        # All-time bullpen appearance count per pitcher (higher = closer / high-leverage)
        bp_counts = {
            row.player_id: row.count
            for row in db.session.query(
                Lineup.player_id,
                db.func.count(Lineup.id).label("count"),
            )
            .join(Game, Lineup.game_id == Game.id)
            .filter(
                Lineup.team_id == team_id,
                Lineup.batting_order >= 10,
            )
            .group_by(Lineup.player_id)
            .all()
        }

        # Last bullpen appearance per pitcher (batting_order >= 10 in Lineup)
        bp_recent = {
            row.player_id: row.last_game
            for row in db.session.query(
                Lineup.player_id,
                db.func.max(Game.game_date).label("last_game"),
            )
            .join(Game, Lineup.game_id == Game.id)
            .filter(
                Lineup.team_id == team_id,
                Lineup.batting_order >= 10,
                Game.game_date >= cutoff,
                Game.game_date < game.game_date,
            )
            .group_by(Lineup.player_id)
            .all()
        }

        # Last starting appearance per pitcher
        recent_home = Game.query.filter(
            Game.home_starter_id.in_([p.id for p in pitchers]),
            Game.game_date >= cutoff,
            Game.game_date < game.game_date,
        ).all()
        recent_away = Game.query.filter(
            Game.away_starter_id.in_([p.id for p in pitchers]),
            Game.game_date >= cutoff,
            Game.game_date < game.game_date,
        ).all()
        st_recent = {}
        for g in recent_home:
            pid = g.home_starter_id
            if pid and (pid not in st_recent or g.game_date > st_recent[pid]):
                st_recent[pid] = g.game_date
        for g in recent_away:
            pid = g.away_starter_id
            if pid and (pid not in st_recent or g.game_date > st_recent[pid]):
                st_recent[pid] = g.game_date

        # Load BullpenAvailability overrides for this game (set by "Load Yesterday's Pitchers")
        avail_overrides = {
            r.player_id: r
            for r in BullpenAvailability.query.filter_by(game_id=game.id, team_id=team_id).all()
        }

        pool = []
        for p in pitchers:
            last_bp = bp_recent.get(p.id)
            last_st = st_recent.get(p.id)
            last = max(filter(None, [last_bp, last_st]), default=None)
            if last:
                rest = (game.game_date - last).days
            else:
                rest = 99  # no recent data → assume fresh

            if rest <= 1:
                avail_class, avail_label = "danger",  "0-1d"
            elif rest == 2:
                avail_class, avail_label = "warning", "2d"
            elif rest < 99:
                avail_class, avail_label = "success", f"{rest}d"
            else:
                avail_class, avail_label = "secondary", "—"

            # Override with BullpenAvailability data if present
            ba = avail_overrides.get(p.id)
            pitched_yday  = False
            pitches_yday  = None
            bp_unavailable = False
            if ba is not None and not ba.available:
                avail_class    = "danger"
                avail_label    = (f"{ba.pitches_yesterday}p" if ba.pitches_yesterday else "OUT")
                pitched_yday   = True
                pitches_yday   = ba.pitches_yesterday
                bp_unavailable = True

            pool.append({
                "player":        p,
                "rest_days":     rest,
                "avail_class":   avail_class,
                "avail_label":   avail_label,
                "appearances":   bp_counts.get(p.id, 0),
                "pitched_yday":  pitched_yday,
                "pitches_yday":  pitches_yday,
                "bp_unavailable": bp_unavailable,
            })

        def _sort(x):
            r    = x["rest_days"]
            apps = x["appearances"]
            # Pitchers marked unavailable from yesterday sort to the bottom
            if x["bp_unavailable"]: return (4, -apps, 0, x["player"].name)
            # Within each rest tier: most appearances first (closers > setup > middle)
            if r >= 3:   return (0, -apps, -r,  x["player"].name)
            if r == 99:  return (1, -apps,  0,  x["player"].name)
            if r == 2:   return (2, -apps,  0,  x["player"].name)
            return           (3, -apps,  0,  x["player"].name)

        pool.sort(key=_sort)
        return pool

    home_reliever_pool = _reliever_pool(game.home_team_id)
    away_reliever_pool = _reliever_pool(game.away_team_id)

    # Pre-saved bullpen slots for this game (batting_order 10-14)
    home_bullpen_dict = {
        l.batting_order: l.player_id
        for l in Lineup.query.filter_by(game_id=game_id, team_id=game.home_team_id)
        .filter(Lineup.batting_order >= 10).all()
    }
    away_bullpen_dict = {
        l.batting_order: l.player_id
        for l in Lineup.query.filter_by(game_id=game_id, team_id=game.away_team_id)
        .filter(Lineup.batting_order >= 10).all()
    }

    latest_sim = SimulationResult.query.filter_by(game_id=game_id).order_by(
        SimulationResult.created_at.desc()
    ).first()
    latest_odds = Odds.query.filter_by(game_id=game_id).order_by(
        Odds.fetched_at.desc()
    ).all()
    recommendations = BetRecommendation.query.filter_by(game_id=game_id).order_by(
        BetRecommendation.created_at.desc()
    ).limit(10).all()
    bankroll = get_current_bankroll()
    umpire = Umpire.query.get(game.umpire_id) if game.umpire_id else None

    # Organize odds by market for the manual-entry pre-fill and display
    odds_by_market: dict = {}
    for row in latest_odds:
        odds_by_market.setdefault(row.market, []).append(row)

    def _best_odds_row(market):
        """Return most recently fetched row for a market, preferring non-Manual sources."""
        rows = odds_by_market.get(market, [])
        if not rows:
            return None
        # Prefer API-sourced rows (not Manual) then fall back
        api_rows = [r for r in rows if r.bookmaker.lower() != "manual"]
        return (api_rows or rows)[0]

    h2h_odds       = _best_odds_row("h2h")
    totals_odds    = _best_odds_row("totals")
    spreads_odds   = _best_odds_row("spreads")
    f5_totals_odds = _best_odds_row("f5_totals")

    # Determine which team is the run-line favorite (-1.5) vs underdog (+1.5).
    # Infer from moneyline: the team with lower (more negative) American odds is the favorite.
    if (h2h_odds and h2h_odds.away_price is not None
            and h2h_odds.home_price is not None):
        away_is_rl_favorite = h2h_odds.away_price < h2h_odds.home_price
    else:
        away_is_rl_favorite = False  # default: home team is the favorite

    away_rl_line = "-1.5" if away_is_rl_favorite else "+1.5"
    home_rl_line = "+1.5" if away_is_rl_favorite else "-1.5"

    # ── Staleness detection ───────────────────────────────────────────────────
    _now = datetime.utcnow()

    # Odds: stale if the most recently fetched row is > 4 hours old
    _api_odds = [r for r in latest_odds if r.bookmaker.lower() != "manual" and r.fetched_at]
    latest_odds_fetch = max((_r.fetched_at for _r in _api_odds), default=None)
    odds_age_hours = ((_now - latest_odds_fetch).total_seconds() / 3600) if latest_odds_fetch else None
    odds_stale    = odds_age_hours is not None and odds_age_hours > 4
    odds_age_str  = (f"{int(odds_age_hours)}h ago" if odds_age_hours is not None and odds_age_hours >= 1
                     else (f"{int(odds_age_hours * 60)}m ago" if odds_age_hours is not None else None))

    # Weather: stale if weather_fetched_at (or lineup_confirmed_at as fallback) > 3 hours ago
    _wt = game.weather_fetched_at or game.lineup_confirmed_at
    weather_age_hours = ((_now - _wt).total_seconds() / 3600) if _wt else None
    is_dome = game.ballpark and game.ballpark.roof_type == "dome"
    weather_missing = (not is_dome and game.status != "final"
                       and game.temperature_f is None)
    weather_stale   = (not is_dome and weather_age_hours is not None and weather_age_hours > 3)
    weather_age_str = (f"{int(weather_age_hours)}h ago" if weather_age_hours is not None and weather_age_hours >= 1
                       else (f"{int((weather_age_hours or 0) * 60)}m ago" if weather_age_hours is not None else None))

    # Simulation: stale if the sim was run before odds were last refreshed
    sim_stale = (latest_sim is not None and latest_odds_fetch is not None
                 and latest_sim.created_at < latest_odds_fetch)

    # Lineup: warn if game is not final and no batters are saved
    lineup_missing = (game.status != "final"
                      and len(home_lineup_rows) == 0 and len(away_lineup_rows) == 0)

    # ── Bet signals — always computed from sim alone, no market odds required ──
    # Collect the best available market prices to compare against thresholds
    _f5_ml_row   = _best_odds_row("f5_moneyline")
    _f5_tot_row  = _best_odds_row("f5_totals")
    _mo = {}
    if h2h_odds:
        _mo['away_ml'] = h2h_odds.away_price
        _mo['home_ml'] = h2h_odds.home_price
    if totals_odds:
        _mo['over_price']  = totals_odds.over_price
        _mo['under_price'] = totals_odds.under_price
        _mo['total_line']  = totals_odds.total_line
    if _f5_ml_row:
        _mo['f5_away_ml'] = _f5_ml_row.away_price
        _mo['f5_home_ml'] = _f5_ml_row.home_price
    if _f5_tot_row:
        _mo['f5_over_price']  = _f5_tot_row.over_price
        _mo['f5_under_price'] = _f5_tot_row.under_price
        _mo['f5_total_line']  = _f5_tot_row.total_line

    bet_signals = compute_bet_signals(
        latest_sim, bankroll,
        away_abbr=away_team.abbreviation if away_team else 'Away',
        home_abbr=home_team.abbreviation if home_team else 'Home',
        market_odds=_mo,
    )

    return render_template(
        "game_detail.html",
        game=game,
        home_team=home_team,
        away_team=away_team,
        home_lineup=home_lineup_rows,
        away_lineup=away_lineup_rows,
        home_lineup_dict=home_lineup_dict,
        away_lineup_dict=away_lineup_dict,
        player_stats_map=player_stats_map,
        display_wrc_map=display_wrc_map,
        away_lineup_stats=away_lineup_stats,
        home_lineup_stats=home_lineup_stats,
        home_batters=home_batters,
        away_batters=away_batters,
        home_pitchers=home_pitchers,
        away_pitchers=away_pitchers,
        all_umpires=all_umpires,
        sim=latest_sim,
        odds=latest_odds,
        h2h_odds=h2h_odds,
        totals_odds=totals_odds,
        spreads_odds=spreads_odds,
        away_rl_line=away_rl_line,
        home_rl_line=home_rl_line,
        recommendations=recommendations,
        bankroll=bankroll,
        umpire=umpire,
        home_reliever_pool=home_reliever_pool,
        away_reliever_pool=away_reliever_pool,
        home_bullpen_dict=home_bullpen_dict,
        away_bullpen_dict=away_bullpen_dict,
        odds_stale=odds_stale,
        odds_age_str=odds_age_str,
        weather_stale=weather_stale,
        weather_missing=weather_missing,
        weather_age_str=weather_age_str,
        sim_stale=sim_stale,
        lineup_missing=lineup_missing,
        bet_signals=bet_signals,
        f5_totals_odds=f5_totals_odds,
        min_edge_pct=get_setting('min_edge_pct', 6.0),
        kelly_fraction=get_setting('kelly_fraction', 0.25),
    )


@app.route("/game/new", methods=["GET", "POST"])
def new_game():
    """Manually create a game and enter lineups."""
    teams = Team.query.order_by(Team.name).all()
    players = Player.query.filter_by(active=True).order_by(Player.name).all()
    PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}
    pitchers = [p for p in players if p.position in PITCHER_POSITIONS]

    if request.method == "POST":
        form = request.form
        home_team_id = int(form["home_team_id"])
        # Auto-assign the home team's ballpark
        home_team_obj = Team.query.get(home_team_id)
        ballpark_id = home_team_obj.ballpark_id if home_team_obj else None

        game = Game(
            game_date=date.fromisoformat(form["game_date"]),
            home_team_id=home_team_id,
            away_team_id=int(form["away_team_id"]),
            home_starter_id=int(form["home_starter_id"]) if form.get("home_starter_id") else None,
            away_starter_id=int(form["away_starter_id"]) if form.get("away_starter_id") else None,
            ballpark_id=ballpark_id,
            temperature_f=float(form["temperature_f"]) if form.get("temperature_f") else None,
            wind_speed_mph=float(form["wind_speed_mph"]) if form.get("wind_speed_mph") else None,
            wind_direction=form.get("wind_direction"),
            humidity_pct=float(form["humidity_pct"]) if form.get("humidity_pct") else None,
            pressure_inhg=float(form["pressure_inhg"]) if form.get("pressure_inhg") else None,
        )
        db.session.add(game)
        db.session.flush()  # Get game.id before commit

        # Save lineups
        for i in range(1, 10):
            home_pid = form.get(f"home_batter_{i}")
            away_pid = form.get(f"away_batter_{i}")
            if home_pid:
                db.session.add(Lineup(
                    game_id=game.id, team_id=game.home_team_id,
                    player_id=int(home_pid), batting_order=i, is_starter=True
                ))
            if away_pid:
                db.session.add(Lineup(
                    game_id=game.id, team_id=game.away_team_id,
                    player_id=int(away_pid), batting_order=i, is_starter=True
                ))

        db.session.commit()
        flash(f"Game created! Now run the simulation.", "success")
        return redirect(url_for("game_detail", game_id=game.id))

    return render_template("new_game.html", teams=teams, players=players,
                            pitchers=pitchers, today=date.today().isoformat())


@app.route("/game/<int:game_id>/lineup", methods=["GET", "POST"])
def edit_lineup(game_id):
    """Add or replace lineups (and update starters/weather) for an existing game."""
    game = Game.query.get_or_404(game_id)
    PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}

    if request.method == "POST":
        form = request.form

        # Update starters
        if form.get("home_starter_id"):
            game.home_starter_id = int(form["home_starter_id"])
        if form.get("away_starter_id"):
            game.away_starter_id = int(form["away_starter_id"])

        # Update weather
        game.temperature_f   = float(form["temperature_f"])   if form.get("temperature_f")   else game.temperature_f
        game.wind_speed_mph  = float(form["wind_speed_mph"])  if form.get("wind_speed_mph")  else game.wind_speed_mph
        game.wind_direction  = form.get("wind_direction")     or game.wind_direction
        game.humidity_pct    = float(form["humidity_pct"])    if form.get("humidity_pct")    else game.humidity_pct
        game.pressure_inhg   = float(form["pressure_inhg"])   if form.get("pressure_inhg")   else game.pressure_inhg

        # Update umpire
        umpire_id_str = form.get("umpire_id")
        game.umpire_id = int(umpire_id_str) if umpire_id_str else game.umpire_id

        # Replace lineups — delete existing, insert new
        Lineup.query.filter_by(game_id=game_id).delete()

        # Batting order (slots 1-9)
        for i in range(1, 10):
            home_pid = form.get(f"home_batter_{i}")
            away_pid = form.get(f"away_batter_{i}")
            if home_pid:
                db.session.add(Lineup(
                    game_id=game_id, team_id=game.home_team_id,
                    player_id=int(home_pid), batting_order=i, is_starter=True,
                ))
            if away_pid:
                db.session.add(Lineup(
                    game_id=game_id, team_id=game.away_team_id,
                    player_id=int(away_pid), batting_order=i, is_starter=True,
                ))

        # Bullpen selections (slots 1-5 → stored as batting_order 10-14)
        for i in range(1, 6):
            home_pid = form.get(f"home_reliever_{i}")
            away_pid = form.get(f"away_reliever_{i}")
            if home_pid:
                db.session.add(Lineup(
                    game_id=game_id, team_id=game.home_team_id,
                    player_id=int(home_pid), batting_order=i + 9, is_starter=False,
                ))
            if away_pid:
                db.session.add(Lineup(
                    game_id=game_id, team_id=game.away_team_id,
                    player_id=int(away_pid), batting_order=i + 9, is_starter=False,
                ))

        game.lineup_confirmed_at = datetime.utcnow()
        db.session.commit()
        flash("Lineup saved!", "success")
        return redirect(url_for("game_detail", game_id=game_id))

    # GET — load existing lineup into {batting_order: player_id} dicts
    home_lineup = {
        l.batting_order: l.player_id
        for l in Lineup.query.filter_by(game_id=game_id, team_id=game.home_team_id).all()
    }
    away_lineup = {
        l.batting_order: l.player_id
        for l in Lineup.query.filter_by(game_id=game_id, team_id=game.away_team_id).all()
    }

    home_batters  = Player.query.filter(
        Player.team_id == game.home_team_id, Player.active == True,
        ~Player.position.in_(PITCHER_POSITIONS)
    ).order_by(Player.name).all()
    away_batters  = Player.query.filter(
        Player.team_id == game.away_team_id, Player.active == True,
        ~Player.position.in_(PITCHER_POSITIONS)
    ).order_by(Player.name).all()
    home_pitchers = Player.query.filter(
        Player.team_id == game.home_team_id, Player.active == True,
        Player.position.in_(PITCHER_POSITIONS)
    ).order_by(Player.name).all()
    away_pitchers = Player.query.filter(
        Player.team_id == game.away_team_id, Player.active == True,
        Player.position.in_(PITCHER_POSITIONS)
    ).order_by(Player.name).all()

    # Build wRC+ map for batter dropdowns and sort best players to the top
    season = _best_season(game.game_date.year)
    all_batter_ids = [p.id for p in home_batters] + [p.id for p in away_batters]
    wrc_rows = PlayerStats.query.filter(
        PlayerStats.player_id.in_(all_batter_ids),
        PlayerStats.season == season,
        PlayerStats.split == "overall",
    ).all()
    wrc_map = {s.player_id: s.wrc_plus for s in wrc_rows if s.wrc_plus is not None}
    home_batters = sorted(home_batters, key=lambda p: wrc_map.get(p.id, 0), reverse=True)
    away_batters = sorted(away_batters, key=lambda p: wrc_map.get(p.id, 0), reverse=True)

    all_umpires = Umpire.query.order_by(Umpire.name).all()

    return render_template(
        "lineup.html",
        game=game,
        home_batters=home_batters,
        away_batters=away_batters,
        home_pitchers=home_pitchers,
        away_pitchers=away_pitchers,
        home_lineup=home_lineup,
        away_lineup=away_lineup,
        all_umpires=all_umpires,
        wrc_map=wrc_map,
    )


def _best_season(game_year: int) -> int:
    """
    Return the most recent season that has stats loaded.
    Falls back up to 3 years so pre-season games (e.g. 2026 game using 2025 stats) still work.
    """
    from sqlalchemy import func
    for yr in range(game_year, game_year - 4, -1):
        count = db.session.query(func.count(PlayerStats.id)).filter_by(season=yr).scalar()
        if count and count > 0:
            return yr
    return game_year  # fallback even if empty


@app.route("/simulate/<int:game_id>", methods=["POST"])
def run_simulation(game_id):
    """Run 10,000 Monte Carlo simulations for a game."""
    game = Game.query.get_or_404(game_id)
    season = _best_season(game.game_date.year)

    # ── Auto-fetch weather if missing or stale (>3 hours old) ────────────────
    # The hourly scheduler may not have run yet (e.g. first simulation of the
    # day). Weather failure never blocks the sim — it just runs with no adjustment.
    _is_dome = game.ballpark and game.ballpark.roof_type == "dome"
    if WEATHER_API_KEY and not _is_dome and game.ballpark and game.ballpark.latitude:
        _age_secs = (
            (datetime.utcnow() - game.weather_fetched_at).total_seconds()
            if game.weather_fetched_at else float("inf")
        )
        if game.temperature_f is None or _age_secs > 10800:   # missing or >3 h old
            try:
                _w = fetch_weather(game.ballpark.latitude, game.ballpark.longitude,
                                   WEATHER_API_KEY)
                game.temperature_f      = _w.get("temperature_f")
                game.wind_speed_mph     = _w.get("wind_speed_mph")
                game.wind_direction     = _w.get("wind_compass")
                game.wind_deg           = _w.get("wind_deg")
                game.humidity_pct       = _w.get("humidity_pct")
                game.pressure_inhg      = _w.get("pressure_inhg")
                game.weather_fetched_at = datetime.utcnow()
                db.session.flush()
                app.logger.info(f"[sim] Auto-fetched weather for game {game_id}: "
                                f"{game.temperature_f}°F, "
                                f"{game.wind_speed_mph}mph from {game.wind_deg}°")
            except Exception as _we:
                app.logger.warning(f"[sim] Weather fetch failed for game {game_id}: {_we} "
                                   f"— simulating without weather adjustment")

    home_lineup_rows = Lineup.query.filter_by(
        game_id=game_id, team_id=game.home_team_id, is_starter=True
    ).filter(Lineup.batting_order <= 9).order_by(Lineup.batting_order).all()

    away_lineup_rows = Lineup.query.filter_by(
        game_id=game_id, team_id=game.away_team_id, is_starter=True
    ).filter(Lineup.batting_order <= 9).order_by(Lineup.batting_order).all()

    if len(home_lineup_rows) < 9 or len(away_lineup_rows) < 9:
        return jsonify({"error": "Both lineups must have 9 batters entered before simulating."}), 400

    try:
        return _run_simulation_inner(
            game, game_id, season, home_lineup_rows, away_lineup_rows
        )
    except Exception as exc:
        import traceback
        app.logger.error("Simulation failed for game %s: %s\n%s",
                         game_id, exc, traceback.format_exc())
        db.session.rollback()
        return jsonify({"error": f"Simulation error: {exc}"}), 500


def _run_simulation_inner(game, game_id, season, home_lineup_rows, away_lineup_rows):
    """Inner simulation logic — separated so the route can catch all exceptions."""

    # ---------------------------------------------------------------------------
    # Multi-year weighted blending helpers
    #
    # Recency weights: most-recent season × 5, previous × 4, two-years-ago × 3.
    # Each season's contribution = PA (or IP×4.3) × weight.
    # Effective sample size passed to build_*_profile() for regression:
    #   weighted_avg_pa = sum(pa_i * w_i) / sum(w_i)
    # This is a weighted average PA per season rather than a raw sum.
    # Rationale: a veteran in April has raw PA ~1030 (3 seasons summed), which
    # barely triggers any regression even though the current 40 PA are noisy.
    # The weighted average (~473) gives an honest "single-season equivalent"
    # confidence level — still high for veterans, but allows appropriate
    # regression on unstable early-season rates like singles and triples.
    # Rookies (only current season) are unaffected: weighted avg == raw PA.
    # ---------------------------------------------------------------------------
    _SEASON_OFFSETS = [(0, 5), (-1, 4), (-2, 3)]  # (offset from `season`, weight)

    _BATTER_RATE_FIELDS = [
        "single_rate", "double_rate", "triple_rate", "hr_rate",
        "walk_rate", "strikeout_rate", "out_rate",
    ]
    _BATTER_DEFAULTS = dict(
        single_rate=0.150, double_rate=0.047, triple_rate=0.005,
        hr_rate=0.030, walk_rate=0.084, strikeout_rate=0.226, out_rate=0.458,
    )

    _PITCHER_RATE_FIELDS = [
        "single_rate_allowed", "double_rate_allowed", "triple_rate_allowed",
        "hr_rate_allowed", "walk_rate_allowed", "strikeout_rate", "out_rate",
    ]
    _PITCHER_DEFAULTS = dict(
        single_rate_allowed=0.150, double_rate_allowed=0.047, triple_rate_allowed=0.005,
        hr_rate_allowed=0.030, walk_rate_allowed=0.084, strikeout_rate=0.226, out_rate=0.458,
    )

    def _blend_batter(player_id: int, split: str):
        """
        Collect batter stats rows across up to 3 seasons and return
        (blended_rates_dict, effective_pa).  Returns (None, 0) if no data.
        """
        weighted = []
        for offset, w in _SEASON_OFFSETS:
            row = PlayerStats.query.filter_by(
                player_id=player_id, season=season + offset, split=split
            ).first()
            if row and (row.pa or 0) > 0:
                # Scale the recency multiplier for current season when PA is
                # below a full-season threshold (~550 PA).
                # Prevents a hot/cold April start from overriding years of data.
                if offset == 0:
                    scale = min(1.0, (row.pa or 0) / 550.0)
                    w = w * scale
                weighted.append((row, w))

        if not weighted:
            return None, 0

        total_w = sum((r.pa or 0) * w for r, w in weighted)
        if total_w <= 0:
            return None, 0

        blended = {
            f: sum((getattr(r, f) or 0) * (r.pa or 0) * w for r, w in weighted) / total_w
            for f in _BATTER_RATE_FIELDS
        }
        # Weighted-average effective PA (see comment above _SEASON_OFFSETS).
        # sum(w) is the sum of blend weights; total_w / sum(w) = weighted avg PA.
        sum_w = sum(w for _, w in weighted)
        effective_pa = int(round(total_w / sum_w)) if sum_w > 0 else 0
        return blended, effective_pa

    def _blend_pitcher(player_id: int, split: str):
        """
        Collect pitcher stats rows across up to 3 seasons and return
        (blended_rates_dict, total_ip, avg_stamina).  Returns (None, 0, 6.0) if no data.
        """
        weighted = []
        for offset, w in _SEASON_OFFSETS:
            row = PitchingStats.query.filter_by(
                player_id=player_id, season=season + offset, split=split
            ).first()
            if row and (row.ip or 0) > 0:
                # Scale the recency multiplier down for current season when IP is
                # below a full-season threshold (160 IP for SP, 60 IP for RP).
                # A 5× bonus for 15 IP would unfairly override 150 IP of prior data.
                if offset == 0:
                    full_season_ip = 60.0 if (row.role == "RP") else 160.0
                    scale = min(1.0, (row.ip or 0) / full_season_ip)
                    w = w * scale
                weighted.append((row, w))

        if not weighted:
            return None, 0.0, 6.0

        # Weight by IP × season_weight
        total_w = sum((r.ip or 0) * w for r, w in weighted)
        if total_w <= 0:
            return None, 0.0, 6.0

        blended = {
            f: sum((getattr(r, f) or 0) * (r.ip or 0) * w for r, w in weighted) / total_w
            for f in _PITCHER_RATE_FIELDS
        }
        # Weighted-average effective IP (same logic as _blend_batter).
        # Prevents raw IP sum (~350 IP across 3 seasons) from suppressing
        # regression on a pitcher who has only 20 IP so far this season.
        sum_w = sum(w for _, w in weighted)
        total_ip = round(total_w / sum_w, 1) if sum_w > 0 else 0.0

        # Stamina: use most recent season with starts data
        stamina = 6.0
        for row, _ in weighted:
            if row.games_started and row.games_started > 0:
                stamina = row.ip / row.games_started
                break

        return blended, total_ip, stamina

    def build_batter(player: Player, season: int, opp_hand: str = "R") -> BatterProfile:
        """
        Build a batter profile blending up to 3 seasons of data.
        Uses handedness split when available (≥20 PA), falls back to overall.
        Recent seasons weighted more heavily (5/4/3).
        """
        split = "vs_LHP" if opp_hand == "L" else "vs_RHP"

        # Try handedness split first
        rates, eff_pa = _blend_batter(player.id, split)

        # Fall back to overall if not enough split data
        if not rates or eff_pa < 20:
            overall_rates, overall_pa = _blend_batter(player.id, "overall")
            if overall_rates and overall_pa > 0:
                rates, eff_pa = overall_rates, overall_pa

        if not rates:
            return BatterProfile(
                name=player.name, bats=player.bats or "R",
                **_BATTER_DEFAULTS,
            )

        return build_batter_profile(
            name=player.name, bats=player.bats or "R",
            pa=eff_pa,
            single_rate=rates.get("single_rate", 0.150),
            double_rate=rates.get("double_rate", 0.047),
            triple_rate=rates.get("triple_rate", 0.005),
            hr_rate=rates.get("hr_rate", 0.030),
            walk_rate=rates.get("walk_rate", 0.084),
            strikeout_rate=rates.get("strikeout_rate", 0.226),
            out_rate=rates.get("out_rate", 0.458),
        )

    # Minimum IP in a single platoon split before we trust it. Below this
    # threshold the sample is too noisy and we fall back to overall rates.
    # ~50 IP is roughly 200 batters faced — enough for K/BB rates to stabilize.
    _PITCHER_SPLIT_MIN_IP = 50.0

    def build_pitcher(player: Player, season: int) -> PitcherProfile:
        """
        Build a pitcher profile blending up to 3 seasons of data.
        Recent seasons weighted more heavily (5/4/3).
        Also populates platoon splits (vs_LHB / vs_RHB) when sample is large
        enough — these are used per-PA inside blend_rates() based on the
        actual batter's handedness.
        """
        rates, total_ip, stamina = _blend_pitcher(player.id, "overall")
        if not rates:
            return PitcherProfile(
                name=player.name, throws=player.throws or "R",
                **_PITCHER_DEFAULTS,
                stamina=6.0,
            )

        # Build the platoon-splits dict. If a split is missing or the sample
        # is below threshold, leave it out and blend_rates falls back to
        # overall for batters of that hand.
        splits = {}
        for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
            split_rates, split_ip, _ = _blend_pitcher(player.id, db_split)
            if split_rates and split_ip >= _PITCHER_SPLIT_MIN_IP:
                splits[bat_hand] = build_pitcher_split_rates(split_rates, split_ip)

        profile = build_pitcher_profile(
            name=player.name, throws=player.throws or "R",
            ip=total_ip,
            single_rate_allowed=rates.get("single_rate_allowed", 0.150),
            double_rate_allowed=rates.get("double_rate_allowed", 0.047),
            triple_rate_allowed=rates.get("triple_rate_allowed", 0.005),
            hr_rate_allowed=rates.get("hr_rate_allowed", 0.030),
            walk_rate_allowed=rates.get("walk_rate_allowed", 0.084),
            strikeout_rate=rates.get("strikeout_rate", 0.226),
            out_rate=rates.get("out_rate", 0.458),
            stamina=stamina,
        )
        if splits:
            profile.splits = splits
        return profile

    def build_bullpen(team_id: int, season: int) -> BullpenProfile:
        _league_avg = BullpenProfile(
            single_rate_allowed=0.155, double_rate_allowed=0.048,
            triple_rate_allowed=0.004, hr_rate_allowed=0.038,
            walk_rate_allowed=0.090, strikeout_rate=0.220, out_rate=0.445,
        )

        def _avg_relievers(stats_rows):
            if not stats_rows:
                return None
            # Weight by IP so high-usage relievers matter more than mop-up guys
            total_ip = sum(r.ip or 0 for r in stats_rows) or 1.0
            def wavg(field):
                return sum((getattr(r, field) or 0) * (r.ip or 0)
                           for r in stats_rows) / total_ip
            # Apply regression to the aggregate bullpen profile
            bp = build_pitcher_profile(
                name="Bullpen", throws="R",
                ip=total_ip,
                single_rate_allowed=wavg("single_rate_allowed"),
                double_rate_allowed=wavg("double_rate_allowed"),
                triple_rate_allowed=wavg("triple_rate_allowed"),
                hr_rate_allowed=wavg("hr_rate_allowed"),
                walk_rate_allowed=wavg("walk_rate_allowed"),
                strikeout_rate=wavg("strikeout_rate"),
                out_rate=wavg("out_rate"),
                is_reliever=True,
            )
            return BullpenProfile(
                single_rate_allowed=bp.single_rate_allowed,
                double_rate_allowed=bp.double_rate_allowed,
                triple_rate_allowed=bp.triple_rate_allowed,
                hr_rate_allowed=bp.hr_rate_allowed,
                walk_rate_allowed=bp.walk_rate_allowed,
                strikeout_rate=bp.strikeout_rate,
                out_rate=bp.out_rate,
            )

        # Prefer specifically selected bullpen pitchers for this game (batting_order 10-14)
        bp_rows = Lineup.query.filter_by(
            game_id=game_id, team_id=team_id, is_starter=False
        ).filter(Lineup.batting_order >= 10).all()
        if bp_rows:
            player_ids = [r.player_id for r in bp_rows]
            selected_stats = (
                db.session.query(PitchingStats)
                .join(Player)
                .filter(Player.id.in_(player_ids),
                        PitchingStats.season == season,
                        PitchingStats.split == "overall")
                .all()
            )
            result = _avg_relievers(selected_stats)
            if result:
                return result

        # Fall back: average all rostered relievers for the team
        all_stats = (
            db.session.query(PitchingStats)
            .join(Player)
            .filter(Player.team_id == team_id, PitchingStats.season == season,
                    PitchingStats.role == "RP", PitchingStats.split == "overall")
            .all()
        )
        return _avg_relievers(all_stats) or _league_avg

    # Determine starter throwing hands for split selection
    # Home batters face the AWAY starter; away batters face the HOME starter
    away_starter_hand = (game.away_starter.throws or "R") if game.away_starter else "R"
    home_starter_hand = (game.home_starter.throws or "R") if game.home_starter else "R"

    # Build profiles — pass opposing pitcher hand so correct split is used
    home_batters = [build_batter(r.player, season, opp_hand=away_starter_hand) for r in home_lineup_rows]
    away_batters = [build_batter(r.player, season, opp_hand=home_starter_hand) for r in away_lineup_rows]
    home_starter = build_pitcher(game.home_starter, season) if game.home_starter else build_pitcher(
        Player(name="TBD", throws="R"), season)
    away_starter = build_pitcher(game.away_starter, season) if game.away_starter else build_pitcher(
        Player(name="TBD", throws="R"), season)

    # Check if BullpenAvailability records exist for this game
    _has_avail_records = BullpenAvailability.query.filter_by(game_id=game_id).first() is not None

    def build_bullpen_from_availability(team_id, gid, ssn):
        """Build BullpenProfile using only available pitchers for this game."""
        avail_records = BullpenAvailability.query.filter_by(
            game_id=gid, team_id=team_id, available=True
        ).all()
        # None means "no filter" (no records at all); empty set means "no one available"
        available_player_ids = {r.player_id for r in avail_records} if avail_records is not None else None

        query = (
            db.session.query(PitchingStats)
            .join(Player)
            .filter(
                Player.team_id == team_id,
                PitchingStats.season == ssn,
                PitchingStats.role == "RP",
                PitchingStats.split == "overall",
            )
        )
        if available_player_ids is not None:
            query = query.filter(PitchingStats.player_id.in_(available_player_ids))

        relievers = query.all()
        return relievers

    if _has_avail_records:
        home_bp_stats = build_bullpen_from_availability(game.home_team_id, game_id, season)
        away_bp_stats = build_bullpen_from_availability(game.away_team_id, game_id, season)

        def _build_bullpen_from_stats(stats_rows):
            _league_avg = BullpenProfile(
                single_rate_allowed=0.155, double_rate_allowed=0.048,
                triple_rate_allowed=0.004, hr_rate_allowed=0.038,
                walk_rate_allowed=0.090, strikeout_rate=0.220, out_rate=0.445,
            )
            if not stats_rows:
                return _league_avg
            total_ip = sum(r.ip or 0 for r in stats_rows) or 1.0
            def wavg(field):
                return sum((getattr(r, field) or 0) * (r.ip or 0) for r in stats_rows) / total_ip
            bp = build_pitcher_profile(
                name="Bullpen", throws="R",
                ip=total_ip,
                single_rate_allowed=wavg("single_rate_allowed"),
                double_rate_allowed=wavg("double_rate_allowed"),
                triple_rate_allowed=wavg("triple_rate_allowed"),
                hr_rate_allowed=wavg("hr_rate_allowed"),
                walk_rate_allowed=wavg("walk_rate_allowed"),
                strikeout_rate=wavg("strikeout_rate"),
                out_rate=wavg("out_rate"),
                is_reliever=True,
            )
            return BullpenProfile(
                single_rate_allowed=bp.single_rate_allowed,
                double_rate_allowed=bp.double_rate_allowed,
                triple_rate_allowed=bp.triple_rate_allowed,
                hr_rate_allowed=bp.hr_rate_allowed,
                walk_rate_allowed=bp.walk_rate_allowed,
                strikeout_rate=bp.strikeout_rate,
                out_rate=bp.out_rate,
            )

        home_bullpen = _build_bullpen_from_stats(home_bp_stats)
        away_bullpen = _build_bullpen_from_stats(away_bp_stats)

        # Override starter stamina if max_pitches is set on the game
        if game.home_starter_max_pitches:
            home_starter.stamina = game.home_starter_max_pitches / 15.0
        if game.away_starter_max_pitches:
            away_starter.stamina = game.away_starter_max_pitches / 15.0
    else:
        home_bullpen = build_bullpen(game.home_team_id, season)
        away_bullpen = build_bullpen(game.away_team_id, season)

    # Park factors
    park = ParkFactors()
    if game.ballpark:
        park = ParkFactors(
            runs=game.ballpark.park_factor_runs or 1.0,
            hr=game.ballpark.park_factor_hr or 1.0,
            hits=game.ballpark.park_factor_hits or 1.0,
        )

    # Weather adjustments — domes are climate-controlled, weather has zero effect.
    # Coefficients sourced from Alan Nathan / Robert Adair baseball physics research.
    is_dome = game.ballpark and game.ballpark.roof_type == "dome"
    weather = WeatherFactors()
    if not is_dome and game.temperature_f:
        import math

        # ── Temperature ──────────────────────────────────────────────────────
        # ~3% more HRs per 10°F above 72°F (air density / ball-travel effect).
        temp_adj = (game.temperature_f - 72) * 0.003

        # ── Wind ─────────────────────────────────────────────────────────────
        # Use actual wind degrees + park CF bearing to get the "out" component.
        # wind_deg: direction wind comes FROM (0=N, 90=E, 180=S, 270=W).
        # cf_bearing_deg: compass bearing from home plate toward CF.
        # out_component: +1 = pure tailwind out to CF, -1 = pure headwind in.
        wind_adj = wind_hit_adj = 0.0
        wind_mph = game.wind_speed_mph or 0.0
        if wind_mph > 0:
            cf_bearing = (game.ballpark.cf_bearing_deg
                          if game.ballpark and game.ballpark.cf_bearing_deg is not None
                          else None)
            if cf_bearing is not None and game.wind_deg is not None:
                # Compute out-component using vector projection
                wind_toward_deg = (game.wind_deg + 180) % 360
                angle_diff_rad  = math.radians(wind_toward_deg - cf_bearing)
                out_component   = math.cos(angle_diff_rad)
            elif game.wind_direction:
                # Fallback: use text direction ("out" / "in") if degrees missing
                d = game.wind_direction.lower()
                if "out" in d:
                    out_component = 1.0
                elif "in" in d:
                    out_component = -1.0
                else:
                    out_component = 0.0
            else:
                out_component = 0.0

            # ~11% more HRs per 10 mph directly out (fly-ball distance effect).
            wind_adj     = out_component * wind_mph * 0.0011
            # Doubles/triples: smaller effect — line drives less affected by carry.
            wind_hit_adj = out_component * wind_mph * 0.0005

        # ── Barometric pressure ───────────────────────────────────────────────
        # ~1.7% per inHg below standard sea-level pressure.
        # NOTE: altitude effect is already captured in park factors — this only
        # models intraday weather-system variation (typically ±0.3–0.5 inHg).
        pressure_adj = 0.0
        if game.pressure_inhg:
            pressure_adj = (29.92 - game.pressure_inhg) * 0.017

        # ── Humidity ──────────────────────────────────────────────────────────
        # Humid air is slightly less dense → ball travels marginally farther.
        # ~0.5% per 10% RH above 50% (very small; correctly signed per physics).
        humidity_adj = 0.0
        if game.humidity_pct:
            humidity_adj = (game.humidity_pct - 50) * 0.00005

        weather = WeatherFactors(
            temp_adj=round(temp_adj, 4),
            wind_adj=round(wind_adj, 4),
            wind_hit_adj=round(wind_hit_adj, 4),
            pressure_adj=round(pressure_adj, 4),
            humidity_adj=round(humidity_adj, 4),
        )

    # Umpire adjustments
    umpire = UmpireFactors()
    if game.umpire_id:
        u = Umpire.query.get(game.umpire_id)
        if u:
            umpire = UmpireFactors(
                runs_per_game_impact=u.runs_per_game_impact or 0,
                walk_rate_impact=u.walk_rate_impact or 0,
                k_rate_impact=u.k_rate_impact or 0,
            )

    # Team defense (Outs Above Average). Falls back to most recent available
    # season if the current season has no data yet (e.g. early April).
    home_defense = _load_team_defense(game.home_team_id, game.game_date.year)
    away_defense = _load_team_defense(game.away_team_id, game.game_date.year)

    # Starting catcher pitch framing (shadow-zone CSR). Applied when the
    # corresponding team is in the field (home catcher vs. away batters, etc.).
    home_catcher = _load_catcher_factors(game.id, game.home_team_id, game.game_date.year)
    away_catcher = _load_catcher_factors(game.id, game.away_team_id, game.game_date.year)

    inputs = GameInputs(
        home_lineup=home_batters,
        away_lineup=away_batters,
        home_starter=home_starter,
        away_starter=away_starter,
        home_bullpen=home_bullpen,
        away_bullpen=away_bullpen,
        park=park,
        weather=weather,
        umpire=umpire,
        home_defense=home_defense,
        away_defense=away_defense,
        home_catcher=home_catcher,
        away_catcher=away_catcher,
    )

    # ── Hybrid sim: splits-ON drives moneyline/F5 ML/runline projections, ──
    # splits-OFF drives totals/F5 totals projections.
    # Backtest evidence (2024+2025 multi-market, real lineups):
    #   ML:     splits-ON wins 2-for-2 (+24pp ROI in 2024, +14pp in 2025)
    #   Totals: splits-ON loses 2-for-2 (−9pp ROI in 2024, −31pp in 2025)
    # Building one PitcherProfile copy per starter with .splits=None and
    # running a second 10k sim is ~10s extra compute per game.
    results = run_simulations(inputs, n=10000)

    from dataclasses import replace as _dc_replace
    from copy import copy as _copy
    def _no_splits(pp):
        if pp is None or getattr(pp, "splits", None) is None:
            return pp
        pp2 = _copy(pp); pp2.splits = None; return pp2
    inputs_no_splits = _dc_replace(
        inputs,
        home_starter=_no_splits(home_starter),
        away_starter=_no_splits(away_starter),
    )
    results_totals = run_simulations(inputs_no_splits, n=10000)

    sim = SimulationResult(
        game_id=game_id,
        num_simulations=10000,
        # ML / F5 ML / cover / FI / RIFI — splits-ON sim (pre-computed fields)
        home_win_pct=results["home_win_pct"],
        away_win_pct=results["away_win_pct"],
        # Run averages — use splits-OFF so downstream tooling that surfaces
        # "expected runs" is consistent with the totals projections we'll bet.
        home_avg_runs=results_totals["home_avg_runs"],
        away_avg_runs=results_totals["away_avg_runs"],
        total_avg_runs=results_totals["total_avg_runs"],
        total_std_dev=results_totals["total_std_dev"],
        # Stored distribution is splits-OFF since most downstream readers
        # (api_ou_prob, bulk-recalc totals analysis, F5 totals recompute, F5
        # runline cover) use it to derive market-specific probs. F5 splits-OFF
        # is preferred per 2025 backtest (F5 ML +15.17% vs +12.84% splits-ON).
        score_distribution=results_totals["score_distribution"],
        # First-inning runs and RIFI — splits-ON (untested but theoretically
        # benefits from same starter-handedness signal as full-game ML).
        away_fi_score_pct=results.get("away_fi_score_pct"),
        home_fi_score_pct=results.get("home_fi_score_pct"),
        rifi_pct=results.get("rifi_pct"),
        # F5 ML / F5 RL — splits-OFF (2025 backtest showed splits-OFF wins
        # F5 ML +2.33pp ROI; F5 markets are starter-only so the platoon
        # overshoot we hedge against on totals also leaks into F5 ML).
        f5_home_win_pct=results_totals.get("f5_home_win_pct"),
        f5_away_win_pct=results_totals.get("f5_away_win_pct"),
        f5_tie_pct=results_totals.get("f5_tie_pct"),
        f5_home_avg_runs=results_totals.get("f5_home_avg_runs"),
        f5_away_avg_runs=results_totals.get("f5_away_avg_runs"),
    )
    db.session.add(sim)
    db.session.flush()

    # Calculate O/U and run line vs current odds line.
    # OVER/UNDER use the splits-OFF sim (results_totals).
    latest_odds = Odds.query.filter_by(game_id=game_id, market="totals").order_by(
        Odds.fetched_at.desc()
    ).first()
    if latest_odds and latest_odds.total_line:
        ou = calculate_over_under(results_totals, latest_odds.total_line)
        sim.over_pct = ou["over"]
        sim.under_pct = ou["under"]
        sim.simulated_total_line = latest_odds.total_line

    rl = calculate_runline(results, 1.5)
    # Determine which team is laying -1.5 from the live odds spread.
    # home_rl_spread < 0 means home is -1.5 (favorite); > 0 means home is +1.5.
    home_spread = (latest_odds.home_rl_spread if latest_odds and latest_odds.home_rl_spread is not None else -1.5)
    if home_spread < 0:
        # Home is -1.5: store P(home wins by 2+) and P(away +1.5 covers)
        sim.home_cover_runline_pct = rl["home_minus_cover"]
        sim.away_cover_runline_pct = round(1.0 - rl["home_minus_cover"], 4)
    else:
        # Home is +1.5: store P(home +1.5 covers) and P(away wins by 2+)
        sim.away_cover_runline_pct = rl["away_minus_cover"]
        sim.home_cover_runline_pct = round(1.0 - rl["away_minus_cover"], 4)

    # ── F5 O/U and run line (-0.5) vs manual F5 odds ──────────────────────
    # F5 over/under uses splits-OFF sim (sim.score_distribution stored is
    # splits-OFF, see above). F5 runline (-0.5) is a "who scores more"
    # question — ML-adjacent — so it uses splits-ON.
    f5_odds = Odds.query.filter_by(game_id=game_id, market="f5_totals").order_by(
        Odds.fetched_at.desc()
    ).first()
    if f5_odds and f5_odds.total_line and results_totals.get("f5_home_win_pct") is not None:
        import json as _json
        import numpy as _np
        dist_t = _json.loads(results_totals["score_distribution"])
        hf5 = _np.array(dist_t["home_f5_scores"])
        af5 = _np.array(dist_t["away_f5_scores"])
        f5_totals = hf5 + af5
        nf5 = len(f5_totals)
        sim.f5_over_pct  = round(float(_np.sum(f5_totals > f5_odds.total_line) / nf5), 4)
        sim.f5_under_pct = round(float(_np.sum(f5_totals < f5_odds.total_line) / nf5), 4)
        sim.f5_simulated_total_line = f5_odds.total_line

    # F5 runline (-0.5): just win outright (no ties count as cover) — splits-OFF sim
    # (matches F5 ML choice above; backtest favored splits-OFF for all F5 markets).
    if results_totals.get("f5_home_win_pct") is not None:
        import json as _json
        import numpy as _np
        dist_f5rl = _json.loads(results_totals["score_distribution"])
        hf5 = _np.array(dist_f5rl["home_f5_scores"])
        af5 = _np.array(dist_f5rl["away_f5_scores"])
        nf5 = len(hf5)
        sim.f5_home_cover_pct = round(float(_np.sum(hf5 > af5) / nf5), 4)
        sim.f5_away_cover_pct = round(float(_np.sum(af5 > hf5) / nf5), 4)

    db.session.commit()

    # Auto-generate bet recommendations if odds are available
    _generate_recommendations(game, sim)

    return jsonify({
        "success": True,
        "home_win_pct": results["home_win_pct"] * 100,
        "away_win_pct": results["away_win_pct"] * 100,
        "home_avg_runs": results["home_avg_runs"],
        "away_avg_runs": results["away_avg_runs"],
        "total_avg_runs": results["total_avg_runs"],
        "redirect": url_for("game_detail", game_id=game_id),
    })


@app.route("/api/game/<int:game_id>/bullpen")
def api_bullpen_data(game_id):
    """Return both teams' pitching staff and current bullpen availability records."""
    game = Game.query.get_or_404(game_id)
    season = _best_season(game.game_date.year)

    def _default_starter_pitches(player) -> int:
        """
        Estimate a starter's typical pitch count from their IP/GS ratio.
        Looks back up to 2 seasons for the best available data.
        Formula: avg_ip_per_start × 15 pitches/inning, clamped 75–110.
        """
        if not player:
            return 95
        for yr in (season, season - 1):
            s = PitchingStats.query.filter_by(
                player_id=player.id, season=yr, role="SP", split="overall"
            ).first()
            if s and s.ip and s.games_started:
                avg_ip = s.ip / s.games_started
                return min(110, max(75, round(avg_ip * 15)))
        return 95  # MLB average when no data available

    def _default_reliever_pitches(stats) -> int:
        """
        Estimate a reliever's typical outing length from their IP/G ratio.
        Formula: avg_ip_per_appearance × 15 pitches/inning, clamped 12–40.
        """
        if stats and stats.ip and stats.games:
            avg_ip = stats.ip / stats.games
            return min(40, max(12, round(avg_ip * 15)))
        return 20  # typical reliever default

    def team_bullpen_info(team, starter, starter_max_pitches):
        if not team:
            return {"team": "?", "starter": {"player_id": None, "name": "TBD", "max_pitches": None, "default_pitches": 95}, "relievers": []}

        # --- Step 1: Try current-season MLB API data to identify active relievers ---
        current_year = game.game_date.year
        from data.mlb_api import get_current_season_relievers
        current_season_data = {}
        if team.mlb_id:
            try:
                current_season_data = get_current_season_relievers(team.mlb_id, current_year)
            except Exception:
                current_season_data = {}

        # Build a player mlb_id → Player lookup for this team's active roster
        roster_players = Player.query.filter_by(team_id=team.id, active=True).all()
        mlb_id_to_player = {p.mlb_id: p for p in roster_players if p.mlb_id}

        # Build a stats lookup: player_id → best available RP stats row
        def _best_rp_stats(player_id):
            for yr in (season, season - 1):
                s = PitchingStats.query.filter_by(
                    player_id=player_id, season=yr,
                    role="RP", split="overall"
                ).first()
                if s:
                    return s
            return None

        # Build availability lookup for this game
        avail_records = {
            r.player_id: r
            for r in BullpenAvailability.query.filter_by(game_id=game_id, team_id=team.id).all()
        }

        # --- Step 2: Filter strategy ---
        # If current season has ≥ 4 relievers → show only those (sorted by games desc), cap at 7
        # Otherwise → fall back to top 7 by IP from prior-season RP stats for current roster
        current_rp_mlb_ids = {
            mlb_id for mlb_id, info in current_season_data.items()
            if info.get("is_reliever")
        }

        USE_CURRENT = len(current_rp_mlb_ids) >= 4

        relievers = []

        if USE_CURRENT:
            # Only include players found in current-season API data as relievers
            for mlb_id in current_rp_mlb_ids:
                player = mlb_id_to_player.get(mlb_id)
                if not player:
                    continue  # not on DB roster (trade/call-up lag) — skip
                stats = _best_rp_stats(player.id)
                rec = avail_records.get(player.id)
                api_info = current_season_data[mlb_id]
                relievers.append({
                    "player_id":         player.id,
                    "name":              player.name,
                    "ip":                stats.ip if stats else api_info.get("ip", 0.0),
                    "games":             api_info.get("games", 0),
                    "available":         rec.available if rec else True,
                    "max_pitches":       rec.max_pitches if rec else None,
                    "default_pitches":   _default_reliever_pitches(stats),
                    "sort_order":        rec.sort_order if rec else 999,
                    "pitched_yesterday": rec.pitched_yesterday if rec else False,
                    "pitches_yesterday": rec.pitches_yesterday if rec else None,
                })
            # Sort: user order first, then by current-season games desc
            relievers.sort(key=lambda r: (r["sort_order"] if r["sort_order"] < 999 else 999, -r.get("games", 0)))
            relievers = relievers[:7]  # cap at 7

        else:
            # Fallback: roster-based, RP stats from prior seasons, top 7 by IP
            for player in roster_players:
                stats = _best_rp_stats(player.id)
                if not stats:
                    continue  # no RP history → starter or non-pitcher, skip
                rec = avail_records.get(player.id)
                relievers.append({
                    "player_id":         player.id,
                    "name":              player.name,
                    "ip":                stats.ip or 0.0,
                    "games":             stats.games or 0,
                    "available":         rec.available if rec else True,
                    "max_pitches":       rec.max_pitches if rec else None,
                    "default_pitches":   _default_reliever_pitches(stats),
                    "sort_order":        rec.sort_order if rec else 999,
                    "pitched_yesterday": rec.pitched_yesterday if rec else False,
                    "pitches_yesterday": rec.pitches_yesterday if rec else None,
                })
            # Sort: user order first, then by IP desc; cap at 7
            relievers.sort(key=lambda r: (r["sort_order"] if r["sort_order"] < 999 else 999, -r["ip"]))
            relievers = relievers[:7]

        starter_info = {
            "player_id":     starter.id if starter else None,
            "name":          starter.name if starter else "TBD",
            "max_pitches":   starter_max_pitches,
            "default_pitches": _default_starter_pitches(starter),
        }

        return {
            "team":      team.abbreviation,
            "starter":   starter_info,
            "relievers": relievers,
        }

    return jsonify({
        "home": team_bullpen_info(game.home_team, game.home_starter, game.home_starter_max_pitches),
        "away": team_bullpen_info(game.away_team, game.away_starter, game.away_starter_max_pitches),
    })


@app.route("/api/game/<int:game_id>/bullpen/load-yesterday", methods=["POST"])
def api_bullpen_load_yesterday(game_id):
    """Load previous-day pitch data for both teams and pre-populate availability."""
    from data.mlb_api import get_previous_game_pitchers
    game = Game.query.get_or_404(game_id)
    season = _best_season(game.game_date.year)
    _pitcher_positions = {"SP", "RP", "P", "TWP"}
    starter_ids = {game.home_starter_id, game.away_starter_id} - {None}
    debug_log = []

    try:
        for side in ['home', 'away']:
            team = game.home_team if side == 'home' else game.away_team
            if not team or not team.mlb_id:
                debug_log.append(f"{side}: no team or mlb_id — skipped")
                continue

            yesterday_pitchers = get_previous_game_pitchers(team.mlb_id, game.game_date)
            debug_log.append(
                f"{side} ({team.abbreviation}): found {len(yesterday_pitchers)} pitchers "
                f"in previous game — {[v['name'] for v in yesterday_pitchers.values()]}"
            )

            all_pitchers = Player.query.filter(
                Player.team_id == team.id,
                Player.active == True,              # noqa: E712
                Player.position.in_(_pitcher_positions),
            ).all()

            matched = 0
            for player in all_pitchers:
                if player.id in starter_ids:
                    continue
                if not player.mlb_id:
                    continue

                pitched = player.mlb_id in yesterday_pitchers
                pitches = yesterday_pitchers.get(player.mlb_id, {}).get("pitches", 0) if pitched else None
                if pitched:
                    matched += 1
                    debug_log.append(f"  → {player.name} (mlb_id={player.mlb_id}) pitched: {pitches}p")

                existing = BullpenAvailability.query.filter_by(game_id=game_id, player_id=player.id).first()
                if existing:
                    existing.pitched_yesterday = pitched
                    existing.pitches_yesterday = pitches
                    existing.available         = not pitched
                else:
                    db.session.add(BullpenAvailability(
                        game_id=game_id,
                        player_id=player.id,
                        team_id=team.id,
                        available=not pitched,
                        pitched_yesterday=pitched,
                        pitches_yesterday=pitches,
                    ))

            debug_log.append(f"  {matched} pitchers marked unavailable for {team.abbreviation}")

        db.session.commit()
        logger.info("[bullpen] load-yesterday: " + " | ".join(debug_log))

    except Exception as e:
        logger.error(f"[bullpen] load-yesterday error: {e}", exc_info=True)
        db.session.rollback()
        return jsonify({"error": str(e), "debug": debug_log}), 500

    response = api_bullpen_data(game_id)
    # Attach debug info so the JS can log it to console
    if hasattr(response, 'get_json'):
        data = response.get_json()
        data["_debug"] = debug_log
        return jsonify(data)
    return response


@app.route("/api/game/<int:game_id>/bullpen/save", methods=["POST"])
def api_bullpen_save(game_id):
    """Save bullpen availability settings and starter pitch count limits."""
    game = Game.query.get_or_404(game_id)
    data = request.get_json() or {}

    if "home_starter_max_pitches" in data:
        game.home_starter_max_pitches = data["home_starter_max_pitches"] or None
    if "away_starter_max_pitches" in data:
        game.away_starter_max_pitches = data["away_starter_max_pitches"] or None

    for r in data.get("relievers", []):
        player_id = r.get("player_id")
        if not player_id:
            continue
        existing = BullpenAvailability.query.filter_by(game_id=game_id, player_id=player_id).first()
        if existing:
            existing.available   = r.get("available", True)
            existing.max_pitches = r.get("max_pitches") or None
            existing.sort_order  = r.get("sort_order", 0)
        else:
            player = Player.query.get(player_id)
            if player:
                db.session.add(BullpenAvailability(
                    game_id=game_id,
                    player_id=player_id,
                    team_id=player.team_id,
                    available=r.get("available", True),
                    max_pitches=r.get("max_pitches") or None,
                    sort_order=r.get("sort_order", 0),
                ))

    db.session.commit()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# App Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default=None):
    """Read a value from the AppSettings table. Returns float/int/str as stored."""
    row = AppSettings.query.filter_by(key=key).first()
    if row is None or row.value is None:
        return default
    try:
        v = row.value.strip()
        return float(v) if '.' in v else int(v)
    except (ValueError, TypeError):
        return row.value or default


def save_setting(key: str, value):
    """Upsert a value into the AppSettings table."""
    row = AppSettings.query.filter_by(key=key).first()
    if row is None:
        row = AppSettings(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
        row.updated_at = datetime.utcnow()
    db.session.commit()


def calc_threshold_odds(sim_prob: float, min_edge_pct: float):
    """
    Return American-odds threshold: the worst price to accept so the bet has
    exactly min_edge_pct% edge.

    'Or better' means book_price >= threshold (higher integer = more favorable
    in American odds — e.g. -108 > -115, +160 > +145).

    Returns None if the probability math is degenerate.
    """
    threshold_implied = sim_prob - (min_edge_pct / 100.0)
    if threshold_implied <= 0.01 or threshold_implied >= 0.99:
        return None
    if threshold_implied >= 0.5:
        # Favorite territory — negative American odds
        return round(-(threshold_implied / (1.0 - threshold_implied)) * 100)
    else:
        # Underdog territory — positive American odds
        return round(((1.0 - threshold_implied) / threshold_implied) * 100)


def compute_bet_signals(sim: SimulationResult, bankroll: float,
                        away_abbr: str = "Away", home_abbr: str = "Home",
                        market_odds: dict = None) -> list:
    """
    Build a list of actionable bet signals purely from simulation probabilities.
    Each signal shows: what market, sim%, threshold price to look for, Kelly bet.
    If market_odds are supplied, also compares current book price → BET/PASS.

    market_odds format (all optional):
        {
          'away_ml': int, 'home_ml': int,
          'over_price': int, 'under_price': int, 'total_line': float,
          'f5_away_ml': int, 'f5_home_ml': int,
          'f5_over_price': int, 'f5_under_price': int, 'f5_total_line': float,
        }
    """
    if sim is None:
        return []

    min_edge   = float(get_setting('min_edge_pct',   6.0))
    kelly_frac = float(get_setting('kelly_fraction', 0.25))
    mo = market_odds or {}
    signals = []

    def _signal(market_label, sim_prob, min_e, mkt_type,
                book_price=None, line=None):
        """Build one signal row."""
        thr = calc_threshold_odds(sim_prob, min_e)
        if thr is None:
            return
        analysis = analyze_bet(sim_prob, thr, bankroll, kelly_fraction=kelly_frac)
        kelly_amt = analysis['recommended_bet']

        # Compare book price if we have it
        if book_price is not None:
            book_beats = book_price >= thr   # higher int = better odds for bettor
            book_analysis = analyze_bet(sim_prob, book_price, bankroll,
                                        kelly_fraction=kelly_frac)
            book_kelly = book_analysis['recommended_bet']
            book_edge  = book_analysis['edge_pct']
        else:
            book_beats = None
            book_kelly = None
            book_edge  = None

        signals.append({
            'market':        market_label,
            'sim_pct':       sim_prob,
            'threshold':     thr,
            'kelly_at_thr':  kelly_amt,
            'line':          line,
            'mkt_type':      mkt_type,      # 'fg' or 'f5'
            'side':          mkt_type + '_' + ('over' if 'Over' in market_label
                                               else 'under' if 'Under' in market_label
                                               else 'away_ml' if away_abbr in market_label
                                               else 'home_ml'),
            # book info (may be None)
            'book_price':    book_price,
            'book_beats':    book_beats,
            'book_kelly':    book_kelly,
            'book_edge':     book_edge,
        })

    # ── Full-game moneyline ───────────────────────────────────────────────────
    ML_MIN = max(min_edge, 8.0)
    away_thr = calc_threshold_odds(sim.away_win_pct, ML_MIN)
    if away_thr is not None:
        _signal(f'{away_abbr} ML', sim.away_win_pct, ML_MIN, 'fg',
                book_price=mo.get('away_ml'))
    home_thr = calc_threshold_odds(sim.home_win_pct, ML_MIN)
    if home_thr is not None:
        _signal(f'{home_abbr} ML', sim.home_win_pct, ML_MIN, 'fg',
                book_price=mo.get('home_ml'))

    # ── Full-game totals ──────────────────────────────────────────────────────
    total_line = mo.get('total_line') or sim.simulated_total_line
    if sim.over_pct and total_line:
        _signal(f'Over {total_line}',  sim.over_pct,  max(min_edge, 8.0), 'fg',
                book_price=mo.get('over_price'),  line=total_line)
        _signal(f'Under {total_line}', sim.under_pct, max(min_edge, 7.0), 'fg',
                book_price=mo.get('under_price'), line=total_line)

    # ── F5 moneyline ─────────────────────────────────────────────────────────
    F5_MIN = max(min_edge, 6.0)
    if sim.f5_home_win_pct is not None:
        _signal(f'{away_abbr} F5 ML', sim.f5_away_win_pct, F5_MIN, 'f5',
                book_price=mo.get('f5_away_ml'))
        _signal(f'{home_abbr} F5 ML', sim.f5_home_win_pct, F5_MIN, 'f5',
                book_price=mo.get('f5_home_ml'))

    # ── F5 totals ─────────────────────────────────────────────────────────────
    f5_line = mo.get('f5_total_line') or sim.f5_simulated_total_line
    if sim.f5_over_pct is not None and f5_line:
        _signal(f'F5 Over {f5_line}',  sim.f5_over_pct,  F5_MIN, 'f5',
                book_price=mo.get('f5_over_price'),  line=f5_line)
        _signal(f'F5 Under {f5_line}', sim.f5_under_pct, F5_MIN, 'f5',
                book_price=mo.get('f5_under_price'), line=f5_line)

    return signals


def _generate_recommendations(game: Game, sim: SimulationResult):
    """Auto-generate bet recommendations after a simulation or odds update.

    Clears any previous unplaced recommendations for this game before
    regenerating, so stale picks don't accumulate when odds change.
    Already-placed bets are never deleted.
    """
    # No point recommending bets on a completed game
    if game.status == "final":
        return

    bankroll = get_current_bankroll()

    # Read user-configured edge threshold from settings (default 6%)
    MIN_EDGE_PCT = float(get_setting('min_edge_pct', 6.0))

    # Remove ALL old unplaced recommendations (won or not) — they're stale.
    # Previously only won==None were deleted, which left auto-graded-but-unplaced
    # records behind. On re-simulation those stacked up as duplicates.
    BetRecommendation.query.filter_by(game_id=game.id, placed=False).delete()
    db.session.flush()

    odds_rows = Odds.query.filter_by(game_id=game.id).order_by(Odds.fetched_at.desc()).all()

    # Books the user can actually place bets at — only recommend prices from these.
    # Fallback to any available book if none of these have data.
    USER_BOOKS = {"betmgm", "caesars", "williamhill_us", "mybookieag"}

    # Group odds rows by market
    from collections import defaultdict
    by_market = defaultdict(list)
    for row in odds_rows:
        by_market[row.market].append(row)

    def best_row_for(rows, field):
        """Return (row, price) with the highest value of `field` across user's books.
        Falls back to all books if no user book has data for this field.
        """
        # Try user's books first
        user_candidates = [(r, getattr(r, field)) for r in rows
                           if r.bookmaker in USER_BOOKS and getattr(r, field) is not None]
        if user_candidates:
            return max(user_candidates, key=lambda x: x[1])
        # Fallback: any book
        all_candidates = [(r, getattr(r, field)) for r in rows
                          if getattr(r, field) is not None]
        if not all_candidates:
            return None, None
        return max(all_candidates, key=lambda x: x[1])

    # MAX_EDGE raised to 30% — line movement has validated high-edge picks.
    # Running fresh backtest to recalibrate; keep a ceiling to block data errors.
    MAX_EDGE = 30.0

    # Hard cap on underdog odds — never recommend a bet where the market price
    # is longer than +400.  Backtest showed 4 extreme-odds bets (up to +4000)
    # went 0-4; implied_prob miscalculation inflated edge on massive underdogs.
    MAX_UNDERDOG_ODDS = 400

    # Moneyline — filters from the ML-permissive calibrated backtest
    # (backtest/ml_permissive_cal, 2024+2025 combined, 524 bets, +7.93% ROI baseline).
    #
    # CURRENT RULES (user-chosen based on backtest analysis):
    #   1. Calibrated probabilities on both sides (no raw-prob bypass for dogs).
    #   2. Edge floor raised from 6% → 8%.
    #   3. Cut anything with market-implied probability >= 65% (heavy favorites).
    #      Rationale: 65%+ implied bucket returned -2.46% ROI across 105 bets even
    #      with calibration. Below 65%, every band from 35-65% was profitable.
    #   4. Keep the 50-55% "pickem" band — it returned +6.69% ROI calibrated,
    #      unlike the raw-probs version where it was a loss zone.
    #
    # What was dropped:
    #   - ML_MIN_RAW raw-prob floor (calibration already handles confidence)
    #   - UDG_MIN_ODDS / UDG_MAX_ODDS odds-range window (now unified by implied-prob filter)
    #   - Raw-prob bypass for underdogs (+131-+200) — all sides use calibration now
    ML_MIN_EDGE    = max(MIN_EDGE_PCT, 8.0)
    ML_MAX_IMPLIED = 0.65                     # cut heavy favorites (implied >= 65%)

    for side, raw_prob, field in [
        ("home", sim.home_win_pct, "home_price"),
        ("away", sim.away_win_pct, "away_price"),
    ]:
        row, price = best_row_for(by_market.get("h2h", []), field)
        if row and price:
            if american_to_implied_prob(price) >= ML_MAX_IMPLIED:
                continue   # heavy favorite zone — backtest says -2.46% ROI, skip
            our_prob = calibrate_prob(raw_prob, "moneyline", side)
            analysis = analyze_bet(our_prob, price, bankroll)
            if ML_MIN_EDGE <= analysis["edge_pct"] <= MAX_EDGE:
                thr = calc_threshold_odds(our_prob, ML_MIN_EDGE)
                _save_recommendation(game, sim, row, side, "moneyline", price, our_prob, analysis, bankroll, thr)

    # Totals — 9% EV floor across overs and unders.
    # Minimum raw probability: 58% (from v4 2025 backtest, 193 bets).
    #
    # Framing-enabled 2024+2025 backtest threshold sweep:
    #   EV ≥ 6%: 430 bets, 55.8% WR, +7.7% ROI
    #   EV ≥ 8%: 221 bets, 57.0% WR, +8.6% ROI
    #   EV ≥ 9%: 104 bets, 64.4% WR, +22.7% ROI  ← sweet spot
    # → Floor raised from 6% to 9% (catcher-framing feature made the sim
    #   more confident on a noisy sub-tier of 6-8% edges).
    #
    # Validated totals probability range from v4 2025 backtest (193 bets):
    #   54-58% raw: losing — skip
    #   58-64% raw: 54-63% WR, +6% to +24% ROI — sweet spot
    #   NOTE: 0.70 floor was set for v2 calibrated probs (inflated).
    #         With v4 identity calibration, raw probs never reach 0.70.
    TOTALS_MIN_RAW  = 0.58
    OVER_MIN_EDGE  = max(MIN_EDGE_PCT, 9.0)
    UNDER_MIN_EDGE = max(MIN_EDGE_PCT, 9.0)

    # NOTE: OVER suppression was tried briefly (May 2026) when splits-on
    # totals showed overs at -39.81% ROI / unders at +17.59%. Subsequent
    # splits-OFF A/B (2025 main, real lineups, same seed) flipped the
    # picture entirely:  overs +38.42% ROI / unders -1.18% — splits, not
    # overs, were the actual problem. We now use the hybrid sim (splits-on
    # for ML projections, splits-off for totals projections), so over recs
    # come from the splits-off pass and are healthy. Suppression removed.

    # Push probability: how often the total lands exactly on the line.
    # Non-zero only for whole-number lines (e.g. 8, 9).  A push returns your
    # stake, so it adds value — the effective win probability is higher than
    # the raw sim probability, and the threshold price is more forgiving.
    # Formula: effective_prob = our_prob + push_pct / decimal_odds
    _totals_push_pct = max(0.0, round(1.0 - (sim.over_pct or 0) - (sim.under_pct or 0), 4))

    if sim.over_pct:
        for side, raw_prob, field, min_e in [
            ("over",  sim.over_pct,  "over_price",  OVER_MIN_EDGE),
            ("under", sim.under_pct, "under_price", UNDER_MIN_EDGE),
        ]:
            if raw_prob < TOTALS_MIN_RAW:
                continue  # below validated probability range — no data to support recommendation
            row, price = best_row_for(by_market.get("totals", []), field)
            if row and price:
                if price > MAX_UNDERDOG_ODDS:
                    continue  # skip extreme-priced totals lines
                our_prob = calibrate_prob(raw_prob, "totals", side)

                # Adjust for push value on whole-number lines.
                # push_pct / decimal_odds converts the push recovery to a win-probability
                # equivalent, giving a correct edge and a more accurate threshold.
                # For half-line totals (e.g. 8.5) push_pct == 0 so nothing changes.
                decimal_odds = american_to_decimal(price)
                effective_prob = min(0.99, our_prob + _totals_push_pct / decimal_odds)

                analysis = analyze_bet(effective_prob, price, bankroll)
                if analysis["edge_pct"] > MAX_EDGE:
                    continue
                if analysis["edge_pct"] >= min_e:
                    thr = calc_threshold_odds(effective_prob, min_e)
                    # Embed the line in the side so grading always uses the line at bet time
                    side_with_line = f"{side} {row.total_line}" if row.total_line is not None else side
                    # Store raw our_prob as model_probability so the display shows the
                    # true sim% (e.g. 63%), not the push-adjusted effective probability.
                    _save_recommendation(game, sim, row, side_with_line, "totals", price, our_prob, analysis, bankroll, thr)

    # Run line — DISABLED. Backtest: home RL -8.8% ROI across 77 bets (large enough sample).
    # Model consistently overestimates run line edge — not a reliable signal.

    # First inning (RIFI / NRFI) — requires manual odds entry (market: "first_inning")
    # over_price = RIFI Yes odds, under_price = NRFI/No odds
    FI_MIN_EDGE = max(MIN_EDGE_PCT, 6.0)
    if sim.rifi_pct is not None:
        nrfi_pct = round(1.0 - sim.rifi_pct, 4)
        fi_rows = by_market.get("first_inning", [])
        for side, raw_prob, field in [
            ("rifi",  sim.rifi_pct, "over_price"),
            ("nrfi",  nrfi_pct,     "under_price"),
        ]:
            row, price = best_row_for(fi_rows, field)
            if row and price:
                if price > MAX_UNDERDOG_ODDS:
                    continue
                our_prob = calibrate_prob(raw_prob, "first_inning", side)
                analysis = analyze_bet(our_prob, price, bankroll)
                if analysis["edge_pct"] > MAX_EDGE:
                    continue
                if analysis["edge_pct"] >= FI_MIN_EDGE:
                    thr = calc_threshold_odds(our_prob, FI_MIN_EDGE)
                    _save_recommendation(game, sim, row, side, "first_inning", price, our_prob, analysis, bankroll, thr)

    # First 5 innings — moneyline, totals, runline (-0.5)
    # Uses manual odds entry (markets: "f5_moneyline", "f5_totals", "f5_runline")
    # F5 is driven by starter matchup quality — use same 6% floor as first inning.
    F5_MIN_EDGE = max(MIN_EDGE_PCT, 6.0)
    if sim.f5_home_win_pct is not None:
        # F5 Moneyline
        f5_ml_rows = by_market.get("f5_moneyline", [])
        for side, raw_prob, field in [
            ("f5_home", sim.f5_home_win_pct, "home_price"),
            ("f5_away", sim.f5_away_win_pct, "away_price"),
        ]:
            row, price = best_row_for(f5_ml_rows, field)
            if row and price:
                if price > MAX_UNDERDOG_ODDS:
                    continue
                our_prob = calibrate_prob(raw_prob, "f5_moneyline", side)
                analysis = analyze_bet(our_prob, price, bankroll)
                if F5_MIN_EDGE <= analysis["edge_pct"] <= MAX_EDGE:
                    thr = calc_threshold_odds(our_prob, F5_MIN_EDGE)
                    _save_recommendation(game, sim, row, side, "f5_moneyline", price, our_prob, analysis, bankroll, thr)

        # F5 Totals
        # NOTE: F5 totals also use the hybrid sim (splits-off pass).
        if sim.f5_over_pct is not None:
            f5_tot_rows = by_market.get("f5_totals", [])
            for side, raw_prob, field, min_e in [
                ("f5_over",  sim.f5_over_pct,  "over_price",  OVER_MIN_EDGE),
                ("f5_under", sim.f5_under_pct, "under_price", UNDER_MIN_EDGE),
            ]:
                row, price = best_row_for(f5_tot_rows, field)
                if row and price:
                    if price > MAX_UNDERDOG_ODDS:
                        continue
                    our_prob = calibrate_prob(raw_prob, "f5_totals", side)
                    analysis = analyze_bet(our_prob, price, bankroll)
                    if F5_MIN_EDGE <= analysis["edge_pct"] <= MAX_EDGE:
                        thr = calc_threshold_odds(our_prob, F5_MIN_EDGE)
                        side_with_line = f"{side} {row.total_line}" if row.total_line is not None else side
                        _save_recommendation(game, sim, row, side_with_line, "f5_totals", price, our_prob, analysis, bankroll, thr)

        # F5 Run line (-0.5) — just win outright
        f5_rl_rows = by_market.get("f5_runline", [])
        for side, raw_prob, field in [
            ("f5_home_rl", sim.f5_home_cover_pct, "home_price"),
            ("f5_away_rl", sim.f5_away_cover_pct, "away_price"),
        ]:
            row, price = best_row_for(f5_rl_rows, field)
            if row and price:
                if price > MAX_UNDERDOG_ODDS:
                    continue
                our_prob = calibrate_prob(raw_prob, "f5_runline", side)
                analysis = analyze_bet(our_prob, price, bankroll)
                if F5_MIN_EDGE <= analysis["edge_pct"] <= MAX_EDGE:
                    thr = calc_threshold_odds(our_prob, F5_MIN_EDGE)
                    _save_recommendation(game, sim, row, side, "f5_runline", price, our_prob, analysis, bankroll, thr)

    db.session.commit()


def _save_recommendation(game, sim, odds_row, side, bet_type, price, our_prob, analysis, bankroll, threshold_odds_val=None):
    rec = BetRecommendation(
        game_id=game.id,
        simulation_id=sim.id,
        bet_type=bet_type,
        side=side,
        bookmaker=odds_row.bookmaker,
        price=price,
        model_probability=our_prob,
        implied_probability=analysis["implied_probability"] / 100,
        edge_pct=analysis["edge_pct"],
        ev_pct=analysis["ev_pct"],
        kelly_fraction=analysis["kelly_fraction"],
        recommended_bet=analysis["recommended_bet"],
        bankroll_at_time=bankroll,
        threshold_odds=threshold_odds_val,
    )
    db.session.add(rec)


# ---------------------------------------------------------------------------
# Game result recording & bet resolution
# ---------------------------------------------------------------------------

def _snapshot_closing_odds(game: Game) -> int:
    """
    Snapshot the current Odds table as closing prices for all placed bets on this game.
    Should be called once when the game transitions to "In Progress", BEFORE books
    switch to live in-game lines.  Sets closing_odds_locked_at on the game to prevent
    double-snapshotting.

    Returns the number of bets updated.
    """
    if game.closing_odds_locked_at is not None:
        return 0  # already snapshotted

    placed_recs = BetRecommendation.query.filter_by(game_id=game.id, placed=True).filter(
        BetRecommendation.price != None,        # noqa: E711
        BetRecommendation.closing_price == None, # noqa: E711 — not yet snapshotted
    ).all()

    if not placed_recs:
        game.closing_odds_locked_at = datetime.utcnow()
        db.session.commit()
        return 0

    h2h_row = Odds.query.filter_by(game_id=game.id, market="h2h").order_by(
        Odds.fetched_at.desc()
    ).first()
    totals_row = Odds.query.filter_by(game_id=game.id, market="totals").order_by(
        Odds.fetched_at.desc()
    ).first()

    def _closing_price_for(rec):
        bt   = rec.bet_type or ""
        side = (rec.side or "").lower().split()[0]
        if bt == "moneyline":
            if not h2h_row:
                return None
            if side == "home":
                return h2h_row.home_price
            if side == "away":
                return h2h_row.away_price
            home_abbr = game.home_team.abbreviation.lower() if game.home_team else ""
            away_abbr = game.away_team.abbreviation.lower() if game.away_team else ""
            if side == home_abbr:
                return h2h_row.home_price
            if side == away_abbr:
                return h2h_row.away_price
        elif bt == "totals":
            if not totals_row:
                return None
            return totals_row.over_price if side == "over" else totals_row.under_price
        elif bt == "f5_moneyline":
            f5_ml = Odds.query.filter_by(game_id=game.id, market="f5_moneyline").order_by(
                Odds.fetched_at.desc()).first()
            if not f5_ml:
                return None
            if side == "f5_home":
                return f5_ml.home_price
            if side == "f5_away":
                return f5_ml.away_price
        elif bt == "f5_totals":
            f5_tot = Odds.query.filter_by(game_id=game.id, market="f5_totals").order_by(
                Odds.fetched_at.desc()).first()
            if not f5_tot:
                return None
            return f5_tot.over_price if side == "f5_over" else f5_tot.under_price
        return None

    def _clv_cents_calc(open_price: int, close_price: int) -> float:
        def to_implied(p):
            if p > 0:
                return 100 / (p + 100)
            return abs(p) / (abs(p) + 100)
        imp_open  = to_implied(open_price)
        imp_close = to_implied(close_price)
        return round((imp_close - imp_open) * 100, 2)

    updated = 0
    for rec in placed_recs:
        closing = _closing_price_for(rec)
        if closing:
            rec.closing_price = closing
            rec.clv_cents     = _clv_cents_calc(rec.price, closing)
            updated += 1

    game.closing_odds_locked_at = datetime.utcnow()
    db.session.commit()
    logger.info(f"[CLV] Snapshotted closing odds for game {game.id} ({updated} bets updated)")
    return updated


def _resolve_bets(game: Game) -> int:
    """
    Auto-grade all unresolved BetRecommendations for a completed game.
    Returns the count of bets resolved.
    For placed bets, calculates P&L and logs to the bankroll.
    """
    home = game.home_score
    away = game.away_score
    total = home + away

    # Fetch most recent odds rows — used for grading AND closing line value
    totals_row = Odds.query.filter_by(game_id=game.id, market="totals").order_by(
        Odds.fetched_at.desc()
    ).first()
    total_line = totals_row.total_line if totals_row else None

    h2h_row = Odds.query.filter_by(game_id=game.id, market="h2h").order_by(
        Odds.fetched_at.desc()
    ).first()

    def _closing_price_for(rec):
        """Return the closing market price for this bet type/side, or None."""
        bt   = rec.bet_type or ""
        side = (rec.side or "").lower().split()[0]
        if bt == "moneyline":
            if not h2h_row:
                return None
            if side in ("home",):
                return h2h_row.home_price
            if side in ("away",):
                return h2h_row.away_price
            # Team abbreviation stored as side
            home_abbr = game.home_team.abbreviation.lower() if game.home_team else ""
            away_abbr = game.away_team.abbreviation.lower() if game.away_team else ""
            if side == home_abbr:
                return h2h_row.home_price
            if side == away_abbr:
                return h2h_row.away_price
        elif bt == "totals":
            if not totals_row:
                return None
            return totals_row.over_price if side == "over" else totals_row.under_price
        elif bt == "f5_moneyline":
            f5_ml = Odds.query.filter_by(game_id=game.id, market="f5_moneyline").order_by(
                Odds.fetched_at.desc()).first()
            if not f5_ml:
                return None
            if side == "f5_home":
                return f5_ml.home_price
            if side == "f5_away":
                return f5_ml.away_price
        elif bt == "f5_totals":
            f5_tot = Odds.query.filter_by(game_id=game.id, market="f5_totals").order_by(
                Odds.fetched_at.desc()).first()
            if not f5_tot:
                return None
            return f5_tot.over_price if side == "f5_over" else f5_tot.under_price
        return None

    def _clv_cents(open_price: int, close_price: int) -> float:
        """
        CLV in American-odds cents — positive means you got a better number.
        Converts both prices to implied probability, takes the difference,
        then converts back to cents on a standard -110 scale for readability.
        A +10 clv_cents means the line moved 10 cents against you after you bet.
        """
        def to_implied(p):
            if p > 0:
                return 100 / (p + 100)
            return abs(p) / (abs(p) + 100)
        # Lower implied prob at open = better odds for the bettor
        imp_open  = to_implied(open_price)
        imp_close = to_implied(close_price)
        # Positive clv = closing implied is higher (line hardened) = you got a better number
        return round((imp_close - imp_open) * 100, 2)

    recs = BetRecommendation.query.filter_by(game_id=game.id).filter(
        BetRecommendation.won == None,          # noqa: E711 — SQLAlchemy IS NULL
        BetRecommendation.profit_loss == None,  # noqa: E711 — skip pushes (pl=0.0)
    ).all()

    # Fetch F5 (first-5-innings) result once per game — used by all F5 branches.
    # Falls back to None if the game didn't complete 5 innings (rain shortened).
    _f5_cached = None
    _f5_fetched = False
    def _f5():
        nonlocal _f5_cached, _f5_fetched
        if not _f5_fetched:
            _f5_fetched = True
            if game.mlb_game_id:
                _f5_cached = get_f5_result(game.mlb_game_id)
        return _f5_cached

    resolved = 0
    for rec in recs:
        won = None

        is_push = False

        if rec.bet_type == "moneyline":
            # Normalize side: model bets store "home"/"away"; manual bets may store
            # a team abbreviation (e.g. "ari", "ATL"). Resolve both forms.
            side_lc = rec.side.lower() if rec.side else ""
            home_abbr_lc = game.home_team.abbreviation.lower() if game.home_team else ""
            away_abbr_lc = game.away_team.abbreviation.lower() if game.away_team else ""
            if side_lc in ("home", home_abbr_lc):
                won = (home > away)
            elif side_lc in ("away", away_abbr_lc):
                won = (away > home)

        elif rec.bet_type == "totals":
            # Parse side field — model bets store "over"/"under"; manual bets store "over 8.5"/"under 8"
            parts = rec.side.lower().split()
            ou_dir = parts[0] if parts else ""   # "over" or "under"

            # Determine which line to grade against.
            # Always prefer the embedded line in the side field (e.g. "over 9.0") —
            # this is the line that existed when the bet was created/placed.
            # The Odds table's total_line can get overwritten by live/updated lines
            # during or after the game, which would cause incorrect grading.
            line_to_use = total_line  # fallback if no embedded line
            if len(parts) >= 2:
                try:
                    embedded_line = float(parts[1])
                    line_to_use = embedded_line  # always trust the embedded line
                except ValueError:
                    pass

            if line_to_use is not None:
                if total == line_to_use:
                    is_push = True          # exact match = push, money back
                elif ou_dir == "over":
                    won = (total > line_to_use)
                elif ou_dir == "under":
                    won = (total < line_to_use)

        elif rec.bet_type == "first_inning":
            # NRFI = No Run First Inning (won if neither team scored in the 1st)
            # RIFI = Run In First Inning (won if either team scored in the 1st)
            fi = get_first_inning_result(game.mlb_game_id) if game.mlb_game_id else None
            if fi is not None:
                fi_side = rec.side.lower() if rec.side else ""
                rifi_happened = fi.get("rifi", False)
                if fi_side == "nrfi":
                    won = not rifi_happened
                elif fi_side == "rifi":
                    won = rifi_happened

        elif rec.bet_type == "runline":
            # Determine which team was -1.5 from the stored spread direction.
            # The ML favorite is always -1.5; home_rl_spread stores the home team's line.
            spreads_row = Odds.query.filter_by(game_id=rec.game_id, market="spreads").order_by(
                Odds.fetched_at.desc()
            ).first()
            home_spread = (spreads_row.home_rl_spread if spreads_row and spreads_row.home_rl_spread is not None else -1.5)
            rl_side = rec.side.lower() if rec.side else ""
            rl_home_abbr = game.home_team.abbreviation.lower() if game.home_team else ""
            rl_away_abbr = game.away_team.abbreviation.lower() if game.away_team else ""
            is_home_side = rl_side in ("home", rl_home_abbr)
            is_away_side = rl_side in ("away", rl_away_abbr)
            if home_spread < 0:
                # Home is -1.5: home covers by winning 2+; away +1.5 covers by keeping within 1
                if is_home_side:
                    won = (home - away >= 2)
                elif is_away_side:
                    won = (home - away < 2)
            else:
                # Home is +1.5: away is -1.5 (must win by 2+); home +1.5 covers by keeping within 1
                if is_away_side:
                    won = (away - home >= 2)
                elif is_home_side:
                    won = (away - home < 2)

        elif rec.bet_type == "f5_moneyline":
            # F5 ML: side stored as "f5_home" or "f5_away". Tie after 5 = push.
            f5 = _f5()
            if f5 is not None:
                side_lc = (rec.side or "").lower()
                if f5["tied"]:
                    is_push = True
                elif side_lc == "f5_home":
                    won = f5["home_leads"]
                elif side_lc == "f5_away":
                    won = f5["away_leads"]

        elif rec.bet_type == "f5_totals":
            # F5 totals: side stored as "f5_over 4.5" or "f5_under 4.5". Grade against
            # the embedded line — same convention as full-game totals.
            f5 = _f5()
            if f5 is not None:
                parts = (rec.side or "").lower().split()
                ou_dir = parts[0] if parts else ""     # "f5_over" or "f5_under"
                line_to_use = None
                if len(parts) >= 2:
                    try:
                        line_to_use = float(parts[1])
                    except ValueError:
                        pass
                # Fallback to the latest f5_totals row's line if not embedded
                if line_to_use is None:
                    f5_totals_row = Odds.query.filter_by(
                        game_id=rec.game_id, market="f5_totals"
                    ).order_by(Odds.fetched_at.desc()).first()
                    if f5_totals_row and f5_totals_row.total_line:
                        line_to_use = f5_totals_row.total_line
                if line_to_use is not None:
                    if f5["total"] == line_to_use:
                        is_push = True
                    elif ou_dir == "f5_over":
                        won = (f5["total"] > line_to_use)
                    elif ou_dir == "f5_under":
                        won = (f5["total"] < line_to_use)

        elif rec.bet_type == "f5_runline":
            # F5 RL is -0.5 (must win outright in F5, no ties count as cover).
            # Side stored as "f5_home_rl" or "f5_away_rl".
            f5 = _f5()
            if f5 is not None:
                side_lc = (rec.side or "").lower()
                if side_lc == "f5_home_rl":
                    won = f5["home_leads"]  # tie loses; only outright win covers -0.5
                elif side_lc == "f5_away_rl":
                    won = f5["away_leads"]

        away_abbr = game.away_team.abbreviation if game.away_team else "?"
        home_abbr = game.home_team.abbreviation if game.home_team else "?"

        if is_push:
            # Push: bet is resolved, no win/loss, money returned
            rec.won = None
            rec.profit_loss = 0.0
            resolved += 1
            if rec.placed:
                current = get_current_bankroll()
                note = (
                    f"PUSH: {rec.bet_type.upper()} {rec.side.upper()} "
                    f"@ {'+' if rec.price > 0 else ''}{rec.price} "
                    f"({away_abbr} @ {home_abbr}) — money returned"
                )
                db.session.add(BankrollLog(amount=current, change=0, note=note))

        elif won is not None:
            rec.won = won
            resolved += 1

            # Capture closing line value for placed bets — only as a fallback
            # if the game-start snapshot didn't run (e.g. dashboard wasn't open).
            if rec.placed and rec.price and rec.closing_price is None:
                closing = _closing_price_for(rec)
                if closing:
                    rec.closing_price = closing
                    rec.clv_cents = _clv_cents(rec.price, closing)

            # Only track P&L for bets the user marked as placed
            bet_size = rec.actual_bet_size or rec.recommended_bet
            if rec.placed and bet_size:
                if won:
                    profit = round((american_to_decimal(rec.price) - 1) * bet_size, 2)
                else:
                    profit = -round(bet_size, 2)

                rec.profit_loss = profit
                current = get_current_bankroll()
                note = (
                    f"{'WIN' if won else 'LOSS'}: {rec.bet_type.upper()} {rec.side.upper()} "
                    f"@ {'+' if rec.price > 0 else ''}{rec.price} "
                    f"({away_abbr} @ {home_abbr})"
                )
                log = BankrollLog(amount=current + profit, change=profit, note=note)
                db.session.add(log)

    db.session.commit()
    return resolved


@app.route("/api/game/<int:game_id>/fetch-result")
def api_fetch_game_result(game_id):
    """
    Fetch the final score from the MLB Stats API and auto-resolve bets.
    Called via AJAX from the game detail page.
    """
    game = Game.query.get_or_404(game_id)

    if not game.mlb_game_id:
        return jsonify({"error": "No MLB game ID on file — enter the score manually below."}), 400

    result = get_game_result(game.mlb_game_id)
    if not result:
        return jsonify({"error": "Could not reach the MLB API. Try again in a moment."}), 502

    final_statuses = {"Final", "Game Over", "Completed Early"}
    if result.get("status") not in final_statuses:
        return jsonify({
            "pending": True,
            "status": result.get("status", "unknown"),
            "message": f"Game not final yet (status: {result.get('status', '?')}). Check back after the game ends.",
        }), 200

    game.home_score = result["home_score"]
    game.away_score = result["away_score"]
    game.total_runs  = result["home_score"] + result["away_score"]
    game.home_win    = result["home_score"] > result["away_score"]
    game.status      = "final"
    db.session.commit()

    resolved = _resolve_bets(game)

    return jsonify({
        "success": True,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "bets_resolved": resolved,
    })


@app.route("/game/<int:game_id>/result", methods=["POST"])
def record_game_result(game_id):
    """Manually record the final score for a game."""
    game = Game.query.get_or_404(game_id)
    try:
        home_score = int(request.form["home_score"])
        away_score = int(request.form["away_score"])
    except (KeyError, ValueError):
        flash("Please enter valid scores for both teams.", "danger")
        return redirect(url_for("game_detail", game_id=game_id))

    game.home_score = home_score
    game.away_score = away_score
    game.total_runs  = home_score + away_score
    game.home_win    = home_score > away_score
    game.status      = "final"
    db.session.commit()

    resolved = _resolve_bets(game)
    away_abbr = game.away_team.abbreviation if game.away_team else "?"
    home_abbr = game.home_team.abbreviation if game.home_team else "?"
    flash(
        f"Score recorded: {away_abbr} {away_score}, {home_abbr} {home_score}. "
        f"{resolved} bet(s) auto-graded.",
        "success",
    )
    return redirect(url_for("game_detail", game_id=game_id))


@app.route("/game/<int:game_id>/odds/manual", methods=["POST"])
def manual_odds(game_id):
    """Save manually entered odds for a game (moneyline, totals, run line)."""
    game = Game.query.get_or_404(game_id)
    form = request.form

    bookmaker = (form.get("bookmaker") or "Manual").strip() or "Manual"
    now = datetime.utcnow()

    def _int(val):
        """Parse an American odds string to int, or None if blank/invalid."""
        if val is None:
            return None
        val = str(val).strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    def _float(val):
        if val is None:
            return None
        val = str(val).strip()
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    # ── Moneyline (h2h) ──────────────────────────────────────────────────────
    home_ml = _int(form.get("home_ml"))
    away_ml = _int(form.get("away_ml"))
    if home_ml is not None or away_ml is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="h2h").first()
        if row:
            row.home_price = home_ml
            row.away_price = away_ml
            row.fetched_at = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="h2h",
                home_price=home_ml, away_price=away_ml, fetched_at=now,
            ))

    # ── Totals ────────────────────────────────────────────────────────────────
    total_line = _float(form.get("total_line"))
    over_odds  = _int(form.get("over_odds"))
    under_odds = _int(form.get("under_odds"))
    if total_line is not None or over_odds is not None or under_odds is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="totals").first()
        if row:
            row.total_line = total_line
            row.over_price  = over_odds
            row.under_price = under_odds
            row.fetched_at  = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="totals",
                total_line=total_line, over_price=over_odds, under_price=under_odds,
                fetched_at=now,
            ))

    # ── Run Line (spreads) ────────────────────────────────────────────────────
    home_rl = _int(form.get("home_rl"))
    away_rl = _int(form.get("away_rl"))
    if home_rl is not None or away_rl is not None:
        # Infer spread direction from the moneyline: the ML favorite is ALWAYS -1.5.
        # Run line price alone is unreliable — moderate favorites can be priced
        # at negative odds even when giving -1.5 runs.
        h2h_row = Odds.query.filter_by(game_id=game_id, market="h2h").order_by(
            Odds.fetched_at.desc()
        ).first()
        if h2h_row and h2h_row.home_price is not None and h2h_row.away_price is not None:
            # Lower (more negative) ML = the favorite = they are giving -1.5
            home_is_favorite = h2h_row.home_price < h2h_row.away_price
            inferred_home_spread = -1.5 if home_is_favorite else 1.5
        elif home_rl is not None:
            # No ML in DB yet — fall back to run line price direction
            # (heavy favorites tend to be + money on run line)
            inferred_home_spread = -1.5 if home_rl > 0 else 1.5
        else:
            inferred_home_spread = -1.5  # safe default: home team -1.5
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="spreads").first()
        if row:
            row.home_price    = home_rl
            row.away_price    = away_rl
            row.home_rl_spread = inferred_home_spread
            row.fetched_at    = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="spreads",
                home_price=home_rl, away_price=away_rl,
                home_rl_spread=inferred_home_spread, fetched_at=now,
            ))

    # ── First Inning (RIFI / NRFI) ────────────────────────────────────────────
    # over_price = RIFI Yes odds, under_price = NRFI odds
    rifi_odds = _int(form.get("rifi_odds"))
    nrfi_odds = _int(form.get("nrfi_odds"))
    if rifi_odds is not None or nrfi_odds is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="first_inning").first()
        if row:
            row.over_price  = rifi_odds
            row.under_price = nrfi_odds
            row.fetched_at  = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="first_inning",
                over_price=rifi_odds, under_price=nrfi_odds, fetched_at=now,
            ))

    # ── F5 Moneyline ──────────────────────────────────────────────────────
    f5_away_ml = _int(form.get("f5_away_ml"))
    f5_home_ml = _int(form.get("f5_home_ml"))
    if f5_away_ml is not None or f5_home_ml is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="f5_moneyline").first()
        if row:
            row.away_price = f5_away_ml
            row.home_price = f5_home_ml
            row.fetched_at = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="f5_moneyline",
                away_price=f5_away_ml, home_price=f5_home_ml, fetched_at=now,
            ))

    # ── F5 Totals ─────────────────────────────────────────────────────────
    f5_total_line = _float(form.get("f5_total_line"))
    f5_over_odds  = _int(form.get("f5_over_odds"))
    f5_under_odds = _int(form.get("f5_under_odds"))
    if f5_total_line is not None or f5_over_odds is not None or f5_under_odds is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="f5_totals").first()
        if row:
            row.total_line  = f5_total_line
            row.over_price  = f5_over_odds
            row.under_price = f5_under_odds
            row.fetched_at  = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="f5_totals",
                total_line=f5_total_line, over_price=f5_over_odds, under_price=f5_under_odds,
                fetched_at=now,
            ))

    # ── F5 Run line (-0.5) ────────────────────────────────────────────────
    f5_away_rl = _int(form.get("f5_away_rl"))
    f5_home_rl = _int(form.get("f5_home_rl"))
    if f5_away_rl is not None or f5_home_rl is not None:
        row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market="f5_runline").first()
        if row:
            row.away_price = f5_away_rl
            row.home_price = f5_home_rl
            row.fetched_at = now
        else:
            db.session.add(Odds(
                game_id=game_id, bookmaker=bookmaker, market="f5_runline",
                away_price=f5_away_rl, home_price=f5_home_rl, fetched_at=now,
            ))

    db.session.commit()

    # If a simulation already exists, regenerate recommendations against the new odds
    latest_sim = SimulationResult.query.filter_by(game_id=game_id).order_by(
        SimulationResult.created_at.desc()
    ).first()
    if latest_sim:
        _generate_recommendations(game, latest_sim)

    flash(f"Odds saved for {game.away_team.abbreviation} @ {game.home_team.abbreviation}."
          + (" Recommendations updated." if latest_sim else ""), "success")
    return redirect(url_for("game_detail", game_id=game_id))


@app.route("/bet/<int:bet_id>/place", methods=["POST"])
def place_bet(bet_id):
    """Mark a bet as placed with the actual dollar amount wagered."""
    rec = BetRecommendation.query.get_or_404(bet_id)
    data = request.get_json() or {}
    try:
        amount = round(float(data.get("amount", rec.recommended_bet or 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero"}), 400

    rec.placed = True
    rec.actual_bet_size = amount
    db.session.commit()
    return jsonify({"success": True, "placed": True, "amount": amount})


@app.route("/bet/<int:bet_id>/unplace", methods=["POST"])
def unplace_bet(bet_id):
    """Remove a placed bet — only allowed before it's been graded."""
    rec = BetRecommendation.query.get_or_404(bet_id)
    if rec.won is not None:
        return jsonify({"error": "Cannot unplace a bet that has already been graded"}), 400
    rec.placed = False
    rec.actual_bet_size = None
    db.session.commit()
    return jsonify({"success": True, "placed": False})


@app.route("/bet/<int:bet_id>/delete", methods=["POST"])
def delete_bet(bet_id):
    """
    Permanently delete a bet record.

    - Ungraded bets (won=None, no P&L): deleted cleanly.
    - Graded bets with non-zero P&L: a reversal BankrollLog entry is created first
      so the bankroll stays accurate, then the bet is deleted.
    - Graded bets that are pushes (profit_loss=0): deleted cleanly.
    """
    rec = BetRecommendation.query.get_or_404(bet_id)

    # If the bet was graded with a real P&L, reverse it in the bankroll log
    reversal_amount = None
    if rec.placed and rec.profit_loss and rec.profit_loss != 0:
        reversal = -rec.profit_loss   # flip the sign
        current  = get_current_bankroll()
        game     = Game.query.get(rec.game_id)
        teams    = ""
        if game and game.away_team and game.home_team:
            teams = f" ({game.away_team.abbreviation} @ {game.home_team.abbreviation})"
        note = (
            f"DELETED BET reversal: {rec.bet_type.upper()} {(rec.side or '').upper()}"
            f" @ {'+' if (rec.price or 0) > 0 else ''}{rec.price or '?'}{teams}"
        )
        db.session.add(BankrollLog(amount=current + reversal, change=reversal, note=note))
        reversal_amount = reversal

    db.session.delete(rec)
    db.session.commit()

    return jsonify({
        "success": True,
        "reversal": reversal_amount,
    })


def _apply_model_stats(rec: BetRecommendation) -> bool:
    """
    Look up the simulation for this bet's game and populate model_probability,
    implied_probability, edge_pct, ev_pct, kelly_fraction.
    Returns True if stats were successfully applied.
    Works for both new and existing manual bets.
    """
    if not rec.price:
        return False

    sim = SimulationResult.query.filter_by(game_id=rec.game_id).order_by(
        SimulationResult.id.desc()
    ).first()
    if not sim:
        return False

    game_obj  = rec.game
    home_abbr = (game_obj.home_team.abbreviation.lower() if game_obj and game_obj.home_team else "")
    away_abbr = (game_obj.away_team.abbreviation.lower() if game_obj and game_obj.away_team else "")
    ou        = rec.side.lower().split()[0]   # first word: "over", "under", "home", "away", etc.

    model_prob = None
    if rec.bet_type == "moneyline":
        if ou in ("home", home_abbr):
            model_prob = sim.home_win_pct
        elif ou in ("away", away_abbr):
            model_prob = sim.away_win_pct
    elif rec.bet_type == "totals":
        if ou == "over":
            model_prob = sim.over_pct
        elif ou == "under":
            model_prob = sim.under_pct
    elif rec.bet_type == "runline":
        if ou in ("home", home_abbr):
            model_prob = sim.home_cover_runline_pct
        elif ou in ("away", away_abbr):
            model_prob = sim.away_cover_runline_pct
    elif rec.bet_type == "first_inning":
        if ou == "rifi" and sim.rifi_pct is not None:
            model_prob = sim.rifi_pct
        elif ou == "nrfi" and sim.rifi_pct is not None:
            model_prob = 1.0 - sim.rifi_pct
    elif rec.bet_type == "f5_moneyline":
        if ou == "f5_home" and sim.f5_home_win_pct is not None:
            model_prob = sim.f5_home_win_pct
        elif ou == "f5_away" and sim.f5_away_win_pct is not None:
            model_prob = sim.f5_away_win_pct
    elif rec.bet_type == "f5_totals":
        if ou == "f5_over" and sim.f5_over_pct is not None:
            model_prob = sim.f5_over_pct
        elif ou == "f5_under" and sim.f5_under_pct is not None:
            model_prob = sim.f5_under_pct
    elif rec.bet_type == "f5_runline":
        if ou == "f5_home_rl" and sim.f5_home_cover_pct is not None:
            model_prob = sim.f5_home_cover_pct
        elif ou == "f5_away_rl" and sim.f5_away_cover_pct is not None:
            model_prob = sim.f5_away_cover_pct

    if model_prob is None:
        return False

    analysis = analyze_bet(model_prob, rec.price, rec.bankroll_at_time or get_current_bankroll())
    rec.model_probability   = model_prob
    rec.implied_probability = analysis["implied_probability"] / 100
    rec.edge_pct            = analysis["edge_pct"]
    rec.ev_pct              = analysis["ev_pct"]
    rec.kelly_fraction      = analysis["kelly_fraction"]
    return True


@app.route("/bet/manual", methods=["POST"])
def log_manual_bet():
    """Log a bet the user placed that wasn't generated by the model."""
    data = request.get_json() or {}
    game_id = data.get("game_id")
    if not game_id:
        return jsonify({"error": "game_id required"}), 400
    Game.query.get_or_404(game_id)   # validate game exists

    price = data.get("price")
    bet_size = float(data.get("actual_bet_size") or 0) or None
    implied = american_to_implied_prob(int(price)) if price else 0.0

    bet_type = data.get("bet_type", "moneyline")
    side     = data.get("side", "").strip().lower()

    rec = BetRecommendation(
        game_id=game_id,
        bet_type=bet_type,
        side=side,
        bookmaker=data.get("bookmaker", "").strip(),
        price=int(price) if price else None,
        actual_bet_size=bet_size,
        recommended_bet=bet_size,           # for manual bets, recommended = actual
        model_probability=0.0,
        implied_probability=implied,
        edge_pct=0.0,
        ev_pct=0.0,
        kelly_fraction=0.0,
        bankroll_at_time=get_current_bankroll(),
        placed=True,
        is_manual=True,
        notes=data.get("notes", "").strip() or None,
    )
    db.session.add(rec)
    db.session.flush()   # get rec.id without full commit

    # ── Populate edge/EV from simulation ──────────────────────────────────────
    _apply_model_stats(rec)

    # ── Handle won field if result was provided upfront ───────────────────────
    won_val = data.get("won", "pending")
    if won_val == "push":
        rec.won = None
        rec.profit_loss = 0.0
    elif won_val == "won":
        rec.won = True
        if bet_size and price:
            rec.profit_loss = round((american_to_decimal(int(price)) - 1) * bet_size, 2)
    elif won_val == "lost":
        rec.won = False
        if bet_size:
            rec.profit_loss = -round(bet_size, 2)
    # else "pending" → won=None, profit_loss=None (defaults)

    db.session.commit()
    return jsonify({"success": True, "id": rec.id})


@app.route("/bet/<int:bet_id>/edit", methods=["POST"])
def edit_bet(bet_id):
    """Edit details of any bet — amount, odds, book, notes, or manually grade result."""
    rec = BetRecommendation.query.get_or_404(bet_id)
    data = request.get_json() or {}

    if "actual_bet_size" in data:
        val = data["actual_bet_size"]
        rec.actual_bet_size = float(val) if val else None
        if rec.is_manual:
            rec.recommended_bet = rec.actual_bet_size   # keep in sync for manual bets

    if "price" in data and data["price"] not in (None, ""):
        rec.price = int(data["price"])

    if "bookmaker" in data:
        rec.bookmaker = (data["bookmaker"] or "").strip()

    if "notes" in data:
        rec.notes = (data["notes"] or "").strip() or None

    if "side" in data and rec.is_manual:
        rec.side = (data["side"] or "").strip().lower()

    if "bet_type" in data and rec.is_manual:
        rec.bet_type = (data["bet_type"] or "moneyline").strip()

    # Recalculate edge/EV: use manually entered model % if provided, otherwise pull from sim
    if "model_probability" in data and data["model_probability"] not in (None, ""):
        try:
            model_prob = float(data["model_probability"]) / 100.0   # UI sends as percentage
            price = rec.price or (int(data["price"]) if data.get("price") else None)
            if model_prob > 0 and price:
                analysis = analyze_bet(model_prob, price, rec.bankroll_at_time or get_current_bankroll())
                rec.model_probability   = model_prob
                rec.implied_probability = analysis["implied_probability"] / 100
                rec.edge_pct            = analysis["edge_pct"]
                rec.ev_pct              = analysis["ev_pct"]
                rec.kelly_fraction      = analysis["kelly_fraction"]
        except (ValueError, TypeError):
            pass
    elif rec.is_manual and not rec.model_probability:
        # No manual entry — try auto-populating from sim
        _apply_model_stats(rec)

    # Manual result grading (works for any bet, not just manual)
    if "won" in data:
        won_val  = data["won"]
        prev_won = rec.won
        prev_pl  = rec.profit_loss
        game     = rec.game
        away     = game.away_team.abbreviation if game.away_team else "?"
        home     = game.home_team.abbreviation if game.home_team else "?"
        tag      = " [manual]" if rec.is_manual else ""

        # Helper: is this a genuine state change that needs a new bankroll log entry?
        def _result_changed(new_won, new_pl):
            # push→push, won→won, lost→lost, pending→pending = no change
            return not (rec.won == new_won and rec.profit_loss == new_pl)

        if won_val == "push":
            if _result_changed(None, 0.0):
                rec.won = None
                rec.profit_loss = 0.0
                if rec.placed:
                    current = get_current_bankroll()
                    note = (f"PUSH: {rec.bet_type.upper()} {rec.side.upper()} "
                            f"@ {'+' if rec.price > 0 else ''}{rec.price} "
                            f"({away} @ {home}) — money returned{tag}")
                    db.session.add(BankrollLog(amount=current, change=0, note=note))
        elif won_val == "won":
            bet_size = rec.actual_bet_size or rec.recommended_bet
            new_pl   = round((american_to_decimal(rec.price) - 1) * float(bet_size), 2) if (bet_size and rec.price) else None
            if _result_changed(True, new_pl):
                rec.won = True
                rec.profit_loss = new_pl
                if rec.placed and new_pl is not None:
                    current = get_current_bankroll()
                    note = (f"WIN: {rec.bet_type.upper()} {rec.side.upper()} "
                            f"@ {'+' if rec.price > 0 else ''}{rec.price} "
                            f"({away} @ {home}){tag}")
                    db.session.add(BankrollLog(amount=current + new_pl, change=new_pl, note=note))
        elif won_val == "lost":
            bet_size = rec.actual_bet_size or rec.recommended_bet
            new_pl   = -round(float(bet_size), 2) if bet_size else None
            if _result_changed(False, new_pl):
                rec.won = False
                rec.profit_loss = new_pl
                if rec.placed and new_pl is not None:
                    current = get_current_bankroll()
                    note = (f"LOSS: {rec.bet_type.upper()} {rec.side.upper()} "
                            f"@ {'+' if rec.price > 0 else ''}{rec.price} "
                            f"({away} @ {home}){tag}")
                    db.session.add(BankrollLog(amount=current + new_pl, change=new_pl, note=note))
        else:  # "pending"
            rec.won = None
            rec.profit_loss = None

    db.session.commit()
    return jsonify({"success": True})


@app.route("/games/settle")
def settle_games():
    """
    Batch fetch final scores from MLB API for all non-final games
    from today or earlier, then auto-resolve bets.
    Returns JSON — called via AJAX from the games list page.
    """
    games = Game.query.filter(
        Game.status != "final",
        Game.mlb_game_id != None,  # noqa: E711
        Game.game_date <= date.today(),
    ).all()

    settled = 0
    skipped = 0
    errors  = []

    for game in games:
        result = get_game_result(game.mlb_game_id)
        if not result:
            errors.append(f"API error for {game.away_team.abbreviation}@{game.home_team.abbreviation}")
            continue

        final_statuses = {"Final", "Game Over", "Completed Early"}
        if result.get("status") not in final_statuses:
            skipped += 1
            continue

        game.home_score = result["home_score"]
        game.away_score = result["away_score"]
        game.total_runs  = result["home_score"] + result["away_score"]
        game.home_win    = result["home_score"] > result["away_score"]
        game.status      = "final"
        db.session.commit()
        _resolve_bets(game)
        settled += 1

    # Also catch games already marked final but whose bets were never graded
    # (e.g. if a previous settle attempt crashed before _resolve_bets ran).
    ungraded_ids = (
        db.session.query(BetRecommendation.game_id)
        .filter(
            BetRecommendation.placed == True,        # noqa: E712
            BetRecommendation.won == None,           # noqa: E711
            BetRecommendation.profit_loss == None,   # noqa: E711
        )
        .distinct()
        .all()
    )
    if ungraded_ids:
        id_list = [row[0] for row in ungraded_ids]
        orphaned = Game.query.filter(
            Game.id.in_(id_list),
            Game.status == "final",
            Game.home_score != None,  # noqa: E711
            Game.away_score != None,  # noqa: E711
        ).all()
        for game in orphaned:
            _resolve_bets(game)
            settled += 1

    return jsonify({
        "success":       True,
        "games_settled": settled,
        "games_skipped": skipped,
        "errors":        errors,
    })


@app.route("/umpires/seed")
def seed_umpires():
    """
    Seed the database with all active MLB umpires and their 2024 tendency data.
    Safe to run multiple times — skips existing records, updates tendencies.
    """
    added = 0
    updated = 0
    for (name, walk_imp, k_imp, runs_imp, csr) in UMPIRE_SEED_DATA:
        u = Umpire.query.filter_by(name=name).first()
        if u:
            u.walk_rate_impact      = walk_imp
            u.k_rate_impact         = k_imp
            u.runs_per_game_impact  = runs_imp
            u.called_strike_rate    = csr
            updated += 1
        else:
            db.session.add(Umpire(
                name=name,
                walk_rate_impact=walk_imp,
                k_rate_impact=k_imp,
                runs_per_game_impact=runs_imp,
                called_strike_rate=csr,
            ))
            added += 1
    db.session.commit()

    if request.headers.get("Accept", "").startswith("text/html"):
        flash(f"Umpire data seeded: {added} added, {updated} updated.", "success")
        return redirect(url_for("dashboard"))
    return jsonify({"success": True, "added": added, "updated": updated})


def _load_team_defense(team_id: int, season: int) -> DefenseFactors:
    """
    Fetch DefenseFactors for a team/season.

    Falls back to the most recent prior season if the current season has no
    defensive data yet (e.g. early-April games before enough BIP have accrued).
    Returns neutral DefenseFactors() if no data exists at all.
    """
    if not team_id:
        return DefenseFactors()

    # Try the requested season first
    td = TeamDefense.query.filter_by(team_id=team_id, season=season).first()

    # Fall back to the most recent season with data
    if not td:
        td = (TeamDefense.query
              .filter(TeamDefense.team_id == team_id, TeamDefense.season < season)
              .order_by(TeamDefense.season.desc())
              .first())

    if not td:
        return DefenseFactors()

    return DefenseFactors(oaa=td.oaa or 0.0)


def _load_catcher_factors(game_id: int, team_id: int, season: int) -> CatcherFactors:
    """
    Fetch CatcherFactors for the starting catcher of `team_id` in a game.

    Resolution order:
      1. Lineup row for this game with position="C" and is_starter=True.
      2. Any Lineup row for this game with position="C" (e.g. catcher marked
         as part of batting order but not explicitly starter).
      3. The team's primary catcher in the Player table (active, position="C").

    Once a catcher is identified, pull their CatcherFraming row for the
    requested season, falling back to the most recent prior season if none.
    Returns neutral CatcherFactors() if no match/data exists.
    """
    if not team_id:
        return CatcherFactors()

    # 1 & 2: Look up starting catcher from the game's lineup
    catcher_player_id = None
    if game_id:
        lineup_c = (Lineup.query
                    .filter_by(game_id=game_id, team_id=team_id, position="C")
                    .order_by(Lineup.is_starter.desc())
                    .first())
        if lineup_c:
            catcher_player_id = lineup_c.player_id

    # 3: Fall back to team's primary active catcher.
    # We pick the catcher with the most framing pitches in the current season
    # (= the primary starter by playing time). This is more reliable than
    # grabbing "first active C" which can be a backup or stale roster entry.
    if not catcher_player_id:
        primary_c = (
            db.session.query(Player)
            .join(CatcherFraming, CatcherFraming.player_id == Player.id)
            .filter(Player.team_id == team_id,
                    Player.position == "C",
                    Player.active == True,
                    CatcherFraming.season == season)
            .order_by(CatcherFraming.shadow_pitches.desc())
            .first()
        )
        # If no current-season framing data, fall further back to ANY active C
        if not primary_c:
            primary_c = (Player.query
                         .filter_by(team_id=team_id, position="C", active=True)
                         .first())
        if primary_c:
            catcher_player_id = primary_c.id

    if not catcher_player_id:
        return CatcherFactors()

    # Pull framing stats for this catcher — try requested season, else most recent
    cf = CatcherFraming.query.filter_by(
        player_id=catcher_player_id, season=season
    ).first()
    if not cf:
        cf = (CatcherFraming.query
              .filter(CatcherFraming.player_id == catcher_player_id,
                      CatcherFraming.season < season)
              .order_by(CatcherFraming.season.desc())
              .first())

    if not cf:
        return CatcherFactors()

    return CatcherFactors(csr_diff=cf.csr_diff or 0.0)


def _find_or_create_umpire(mlb_id: int, name: str) -> "Umpire | None":
    """
    Look up an umpire by mlb_id first, then by name.
    Creates a blank record if not found so at least the name is stored.
    """
    if mlb_id:
        u = Umpire.query.filter_by(mlb_id=mlb_id).first()
        if u:
            return u
    if name:
        u = Umpire.query.filter_by(name=name).first()
        if u:
            if mlb_id and not u.mlb_id:
                u.mlb_id = mlb_id   # backfill the ID
            return u
        # New umpire — create a neutral record
        u = Umpire(name=name, mlb_id=mlb_id)
        db.session.add(u)
        db.session.flush()
        return u
    return None


@app.route("/games/import")
def import_games():
    """
    Fetch today's (or a given date's) schedule from the MLB Stats API
    and create Game rows in the database for any games not already stored.
    """
    date_str = request.args.get("date", date.today().isoformat())
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str}"}), 400

    from data.mlb_api import get_schedule_for_date
    schedule = get_schedule_for_date(target_date)

    imported = 0
    skipped = 0
    errors = []

    for g in schedule:
        # Skip non-regular / non-playoff games if status is weird
        mlb_game_id = g.get("game_id")
        if not mlb_game_id:
            continue

        # Already in DB? Check if starters need updating.
        existing_game = Game.query.filter_by(mlb_game_id=mlb_game_id).first()

        # Match teams by mlb_id first, then fall back to name
        home_mlb_id = g.get("home_id")
        away_mlb_id = g.get("away_id")
        home_team = Team.query.filter_by(mlb_id=home_mlb_id).first()
        away_team = Team.query.filter_by(mlb_id=away_mlb_id).first()

        if not home_team:
            home_name = g.get("home_name", "")
            home_team = Team.query.filter(
                Team.name.ilike(f"%{home_name.split()[-1]}%")
            ).first() if home_name else None

        if not away_team:
            away_name = g.get("away_name", "")
            away_team = Team.query.filter(
                Team.name.ilike(f"%{away_name.split()[-1]}%")
            ).first() if away_name else None

        if not home_team or not away_team:
            errors.append(f"Could not match teams: {g.get('away_name')} @ {g.get('home_name')}")
            continue

        # Match probable starters with the resilient ladder (handles trades,
        # callups, brand-new rookies via MLB API fallback).
        from data.mlb_api import find_or_create_pitcher
        home_starter = find_or_create_pitcher(
            g.get("home_probable_pitcher_id"),
            g.get("home_probable_pitcher"),
            home_team.id, db, Player,
        )
        away_starter = find_or_create_pitcher(
            g.get("away_probable_pitcher_id"),
            g.get("away_probable_pitcher"),
            away_team.id, db, Player,
        )

        # ── If game already exists, update date/status/starters/umpire as needed ──
        if existing_game:
            updated_fields = []

            # Postponement / reschedule: MLB reuses the same game_id on the new date.
            # If the game is appearing on a different date than stored, move it.
            mlb_status = (g.get("status") or "").strip()
            if existing_game.game_date != target_date and mlb_status not in ("Postponed", "Cancelled"):
                existing_game.game_date = target_date
                existing_game.status = "scheduled"
                updated_fields.append("rescheduled")
            elif mlb_status in ("Postponed", "Cancelled") and existing_game.status not in ("postponed", "cancelled"):
                existing_game.status = mlb_status.lower()
                updated_fields.append("status")

            # Update game time if it changed (postponed games often get a new first-pitch time)
            raw_dt = g.get("game_datetime")
            if raw_dt and updated_fields:
                try:
                    new_time = datetime.strptime(raw_dt, "%Y-%m-%dT%H:%M:%SZ")
                    existing_game.game_time_utc = new_time
                except ValueError:
                    pass

            if existing_game.home_starter_id is None and home_starter:
                existing_game.home_starter_id = home_starter.id
                updated_fields.append("home_starter")
            if existing_game.away_starter_id is None and away_starter:
                existing_game.away_starter_id = away_starter.id
                updated_fields.append("away_starter")
            if existing_game.umpire_id is None:
                try:
                    ump_data = get_umpire_for_game(mlb_game_id)
                    if ump_data:
                        ump = _find_or_create_umpire(ump_data.get("id"), ump_data.get("name"))
                        if ump:
                            existing_game.umpire_id = ump.id
                            updated_fields.append("umpire")
                except Exception:
                    pass

            # Re-resolve ballpark — corrects games imported before a venue
            # override was added, or when MLB late-swaps a venue.
            from scheduler import _resolve_ballpark_id as _resolve_bp
            eg_home = existing_game.home_team
            desired_bp_id, used_override = _resolve_bp(g.get("venue_id"), eg_home)
            if desired_bp_id and existing_game.ballpark_id != desired_bp_id:
                old_bp_id = existing_game.ballpark_id
                existing_game.ballpark_id = desired_bp_id
                updated_fields.append("ballpark")
                logger.info(f"[/games/import] Ballpark corrected for {existing_game.away_team.abbreviation}@{eg_home.abbreviation} "
                            f"on {existing_game.game_date}: {old_bp_id} → {desired_bp_id} "
                            f"(venue: {g.get('venue_name')}, override={used_override})")

            if updated_fields:
                db.session.commit()
                imported += 1
            else:
                skipped += 1
            continue

        # Try to get the assigned home plate umpire from the MLB API
        umpire_id = None
        try:
            ump_data = get_umpire_for_game(mlb_game_id)
            if ump_data:
                ump = _find_or_create_umpire(ump_data.get("id"), ump_data.get("name"))
                if ump:
                    umpire_id = ump.id
        except Exception:
            pass  # Umpire not yet assigned — that's fine

        game_number = int(g.get("game_num", 1) or 1)
        game_type   = g.get("game_type", "R") or "R"   # S=Spring, R=Regular, etc.

        # Parse first-pitch time from MLB API (always UTC)
        game_time_utc = None
        raw_dt = g.get("game_datetime")
        if raw_dt:
            try:
                game_time_utc = datetime.strptime(raw_dt, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass

        # Resolve ballpark — handles international/neutral-site overrides (Mexico/Tokyo/etc)
        from scheduler import _resolve_ballpark_id as _resolve_bp
        ballpark_id, used_override = _resolve_bp(g.get("venue_id"), home_team)
        if used_override:
            logger.info(f"[/games/import] Venue override: {away_team.abbreviation}@{home_team.abbreviation} on {target_date} "
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
            game_number=game_number,
            game_type=game_type,
        )
        db.session.add(game)
        db.session.flush()  # get game.id before copying lineups

        # ── Copy most recent lineup for each team as a projected starting point ──
        # The official lineup is only posted ~60-90 min before first pitch.
        # Using yesterday's lineup lets you run simulations immediately after import.
        lineups_seeded = 0
        for team_id in [home_team.id, away_team.id]:
            # Find the most recent game this team played that has lineup data
            prev_game = (
                Game.query
                .join(Lineup, Lineup.game_id == Game.id)
                .filter(
                    Lineup.team_id == team_id,
                    Lineup.is_starter == True,        # batters only, not bullpen
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
                    game_id=game.id,
                    team_id=team_id,
                    player_id=slot.player_id,
                    batting_order=slot.batting_order,
                    position=slot.position,
                    is_starter=True,
                ))
                lineups_seeded += 1

        imported += 1

    db.session.commit()

    # ── Flag doubleheaders ──────────────────────────────────────────────────
    # Any home team with 2+ games on the same date = doubleheader.
    # Mark both games so the UI can show a DH badge.
    from sqlalchemy import func
    dh_pairs = (
        db.session.query(Game.home_team_id, Game.game_date)
        .filter(Game.game_date == target_date)
        .group_by(Game.home_team_id, Game.game_date)
        .having(func.count(Game.id) > 1)
        .all()
    )
    for home_team_id, gdate in dh_pairs:
        dh_games = Game.query.filter_by(
            home_team_id=home_team_id, game_date=gdate
        ).order_by(Game.game_number).all()
        for dh_game in dh_games:
            dh_game.is_doubleheader = True
    db.session.commit()

    result = {
        "success": True,
        "date": date_str,
        "imported": imported,
        "skipped": skipped,
    }
    if errors:
        result["errors"] = errors

    # If called from the browser (not AJAX), redirect back to the games list
    if request.headers.get("Accept", "").startswith("text/html"):
        flash(f"Imported {imported} game(s) for {target_date.strftime('%B %d, %Y')}."
              + (f" {len(errors)} team(s) could not be matched." if errors else ""), "success")
        return redirect(url_for("games_list", date=date_str))

    return jsonify(result)


@app.route("/api/recalculate-recs")
def api_recalculate_recs():
    """
    Re-run _generate_recommendations() for every simulated game today using
    the odds already in the DB — no Odds API call, no quota spent.

    Useful when a line has moved since the last simulation and the main games
    tab shows stale / missing recommendations.
    """
    today_games = Game.query.filter(
        Game.game_date == date.today(),
        ~Game.status.in_(["postponed", "cancelled", "final"]),
    ).all()

    updated = 0
    skipped = 0
    for game in today_games:
        sim = SimulationResult.query.filter_by(game_id=game.id).order_by(
            SimulationResult.created_at.desc()
        ).first()
        if sim:
            _generate_recommendations(game, sim)
            updated += 1
        else:
            skipped += 1

    db.session.commit()
    return jsonify({
        "ok": True,
        "games_recalculated": updated,
        "games_skipped_no_sim": skipped,
        "message": f"Recommendations recalculated for {updated} game(s).",
    })


@app.route("/debug/odds")
@login_required
def debug_odds():
    """Diagnostic endpoint — verifies The Odds API key is working.
    Reveals only the first 8 + last 4 chars of the key (never the full key)."""
    import requests as _r
    if not ODDS_API_KEY:
        return jsonify({"key_set": False, "error": "ODDS_API_KEY env var is empty or missing"}), 200

    fingerprint = f"{ODDS_API_KEY[:8]}...{ODDS_API_KEY[-4:]}"
    out = {
        "key_set":     True,
        "fingerprint": fingerprint,
        "key_length":  len(ODDS_API_KEY),
    }
    # Hit /sports endpoint to check key validity + remaining quota
    try:
        resp = _r.get("https://api.the-odds-api.com/v4/sports",
                      params={"apiKey": ODDS_API_KEY}, timeout=5)
        out["status_code"] = resp.status_code
        out["remaining_requests"] = resp.headers.get("x-requests-remaining")
        out["used_requests"]      = resp.headers.get("x-requests-used")
        if resp.status_code != 200:
            out["api_message"] = resp.text[:200]
    except Exception as e:
        out["error"] = f"Network error: {type(e).__name__}: {e}"
    # Try fetching MLB odds
    try:
        games = get_odds(ODDS_API_KEY, markets="h2h")
        out["mlb_games_returned"] = len(games)
        if games:
            out["sample_game"] = f"{games[0].get('away_team')} @ {games[0].get('home_team')}"
    except Exception as e:
        out["mlb_error"] = f"{type(e).__name__}: {e}"
    return jsonify(out), 200


@app.route("/odds/refresh")
def refresh_odds():
    """Fetch latest odds from The Odds API and update the database."""
    if not ODDS_API_KEY:
        return jsonify({"error": "No Odds API key configured."}), 500

    raw = get_odds(ODDS_API_KEY, markets="h2h,spreads,totals")
    parsed = parse_odds(raw)

    updated = 0
    matched = 0
    unmatched_teams = []

    def find_team(api_name):
        """Match an Odds API team name to a DB team.
        Tries exact name, then city+nickname partial, then last-word fallback.
        Handles edge cases like 'Red Sox' / 'White Sox' correctly.
        """
        # 1. Exact full name match (most reliable)
        t = Team.query.filter(Team.name.ilike(api_name)).first()
        if t:
            return t
        # 2. Last two words (handles "Red Sox", "White Sox", "Blue Jays")
        words = api_name.split()
        if len(words) >= 2:
            last_two = " ".join(words[-2:])
            t = Team.query.filter(Team.name.ilike(f"%{last_two}%")).first()
            if t:
                return t
        # 3. Last word fallback (single-word nicknames like Cubs, Yankees)
        if words:
            t = Team.query.filter(Team.name.ilike(f"%{words[-1]}%")).first()
            if t:
                return t
        return None

    for game_data in parsed:
        home_team = find_team(game_data["home_team"])
        away_team = find_team(game_data["away_team"])
        if not home_team or not away_team:
            unmatched_teams.append(f"{game_data['away_team']} @ {game_data['home_team']}")
            continue

        # Parse the commence_time from the API to match by game date
        # Use Eastern time so a 7pm EDT game doesn't shift to the next UTC day
        commence_time = game_data.get("commence_time", "")
        try:
            from datetime import timezone, timedelta
            utc_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            # Convert UTC → Eastern (UTC-4 during EDT, UTC-5 during EST)
            # Use a simple -4 offset (EDT covers the full MLB season Apr–Oct)
            eastern_dt = utc_dt.astimezone(timezone(timedelta(hours=-4)))
            game_date = eastern_dt.date()
        except Exception:
            game_date = date.today()

        game = Game.query.filter_by(
            home_team_id=home_team.id, away_team_id=away_team.id,
            game_date=game_date
        ).first()
        if not game:
            # Try adjacent day in case of timezone edge case
            for delta in (-1, 1):
                game = Game.query.filter_by(
                    home_team_id=home_team.id, away_team_id=away_team.id,
                    game_date=game_date + timedelta(days=delta)
                ).first()
                if game:
                    break
        if not game:
            unmatched_teams.append(
                f"{away_team.abbreviation} @ {home_team.abbreviation} on {game_date} (not in DB)"
            )
            continue
        matched += 1

        # Store Odds API event ID for F5 lookups
        if not game.odds_api_id and game_data.get("odds_api_id"):
            game.odds_api_id = game_data["odds_api_id"]

        for book in game_data.get("books", []):
            bookmaker = book["bookmaker"]
            for market_key, market_data in book.get("markets", {}).items():
                # Update existing row if present, otherwise insert a new one
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

        # Fetch F5 odds for this game via the per-event endpoint.
        # GATED — see scheduler.py comment. Off by default.
        f5_enabled = os.getenv("FETCH_F5_ODDS", "0") == "1"
        if f5_enabled and game.odds_api_id:
            from data.odds_api import get_f5_odds
            f5_data = get_f5_odds(ODDS_API_KEY, game.odds_api_id)
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

    # Regenerate recommendations for any simulated game that got new odds.
    # Also recalculate F5 O/U if a new F5 total line arrived.
    recs_updated = 0
    updated_game_ids = {
        o.game_id
        for o in Odds.query.filter(
            Odds.game_id.in_(
                db.session.query(Game.id).filter(Game.game_date >= date.today())
            )
        ).all()
    }
    for gid in updated_game_ids:
        sim = SimulationResult.query.filter_by(game_id=gid).order_by(
            SimulationResult.created_at.desc()
        ).first()
        # Always regenerate recommendations when odds update, as long as a
        # simulation exists. Previously this required F5 data (f5_home_win_pct)
        # which caused moneyline/totals recommendations to never refresh for
        # games without F5 odds entered.
        if sim and sim.score_distribution and sim.f5_home_win_pct is not None:
            # Recalculate F5 O/U against latest total line
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
        if sim:
            g = Game.query.get(gid)
            _generate_recommendations(g, sim)
            recs_updated += 1

    api_count = len(parsed)
    debug_info = {
        "api_games_fetched": api_count,
        "games_matched": matched,
        "rows_updated": updated,
        "recs_updated": recs_updated,
        "unmatched": unmatched_teams,
    }

    if api_count == 0:
        msg = "The Odds API returned 0 games. Your API key may be out of quota, or no MLB games are listed yet."
        if "application/json" not in request.headers.get("Accept", ""):
            flash(msg, "warning")
            return redirect(url_for("games_list"))
        return jsonify({"success": False, "error": msg, **debug_info})

    # If called directly in the browser (not via AJAX), redirect back to games
    if "application/json" not in request.headers.get("Accept", ""):
        flash(
            f"Odds refreshed — {updated} line(s) updated across {matched} game(s)."
            + (f" Recommendations updated for {recs_updated} game(s)." if recs_updated else "")
            + (f" {len(unmatched_teams)} game(s) from API not in DB." if unmatched_teams else ""),
            "success" if updated > 0 else "warning"
        )
        return redirect(url_for("games_list"))

    return jsonify({"success": True, **debug_info})


@app.route("/api/game/<int:game_id>/fetch-lineup")
def api_fetch_lineup(game_id):
    """
    Fetch official batting lineups from the MLB Stats API and match players
    to our database. Returns JSON ready for the lineup editor dropdowns.
    """
    from data.mlb_api import fetch_game_lineup

    game = Game.query.get_or_404(game_id)

    if not game.mlb_game_id:
        return jsonify({"status": "error", "message": "This game has no MLB game ID — lineup auto-fill is not available."}), 400

    result = fetch_game_lineup(game.mlb_game_id)

    if result["status"] != "posted":
        return jsonify(result)

    def _match_player(mlb_id, name, team_id):
        """Find a player in our DB by mlb_id first, then name."""
        if mlb_id:
            p = Player.query.filter_by(mlb_id=int(mlb_id)).first()
            if p:
                return {"id": p.id, "name": p.name, "position": p.position}
        if name:
            p = Player.query.filter_by(name=name, team_id=team_id).first()
            if p:
                return {"id": p.id, "name": p.name, "position": p.position}
            last = name.split()[-1]
            p = Player.query.filter(
                Player.team_id == team_id,
                Player.name.ilike(f"%{last}%"),
            ).first()
            if p:
                return {"id": p.id, "name": p.name, "position": p.position}
        return None

    home_lineup = {}
    for b in result["home_batters"]:
        matched = _match_player(b["mlb_id"], b["name"], game.home_team_id)
        if matched:
            home_lineup[str(b["order"])] = matched

    away_lineup = {}
    for b in result["away_batters"]:
        matched = _match_player(b["mlb_id"], b["name"], game.away_team_id)
        if matched:
            away_lineup[str(b["order"])] = matched

    home_pitcher = None
    if result.get("home_pitcher"):
        home_pitcher = _match_player(
            result["home_pitcher"]["mlb_id"],
            result["home_pitcher"]["name"],
            game.home_team_id,
        )

    away_pitcher = None
    if result.get("away_pitcher"):
        away_pitcher = _match_player(
            result["away_pitcher"]["mlb_id"],
            result["away_pitcher"]["name"],
            game.away_team_id,
        )

    total_batters = len(result["home_batters"]) + len(result["away_batters"])
    total_matched = len(home_lineup) + len(away_lineup)

    return jsonify({
        "status":        "posted",
        "home_lineup":   home_lineup,
        "away_lineup":   away_lineup,
        "home_pitcher":  home_pitcher,
        "away_pitcher":  away_pitcher,
        "matched":       total_matched,
        "total_batters": total_batters,
        "message":       f"Lineup fetched — matched {total_matched} of {total_batters} players to your roster.",
    })


@app.route("/api/weather/<int:game_id>")
def api_weather(game_id):
    """
    Fetch current weather for this game's ballpark from OpenWeatherMap.
    Returns JSON ready to auto-fill the lineup weather form.
    """
    if not WEATHER_API_KEY:
        return jsonify({
            "error": "No weather API key configured.",
            "help": "Sign up free at openweathermap.org, then add WEATHER_API_KEY=yourkey to your .env file."
        }), 503

    game = Game.query.get_or_404(game_id)
    park = game.ballpark

    if not park or park.latitude is None or park.longitude is None:
        return jsonify({"error": "No ballpark coordinates on file for this game."}), 400

    # Skip weather for domes — it has no effect on the simulation
    if park.roof_type == "dome":
        return jsonify({
            "dome": True,
            "message": f"{park.name} is a dome — weather has no effect on the simulation.",
        })

    try:
        weather = fetch_weather(park.latitude, park.longitude, WEATHER_API_KEY)
    except ValueError as e:
        return jsonify({"error": str(e)}), 502

    # Add context for the UI
    weather["park_name"] = park.name
    weather["roof_type"] = park.roof_type or "open"

    return jsonify(weather)


@app.route("/api/weather/bulk")
def api_weather_bulk():
    """
    Fetch and save weather for every non-dome game on a given date.
    Query param: ?date=YYYY-MM-DD  (defaults to today)
    Returns a summary of what was updated, skipped, or failed.
    """
    if not WEATHER_API_KEY:
        return jsonify({
            "error": "No weather API key configured.",
            "help": "Sign up free at openweathermap.org, then add WEATHER_API_KEY=yourkey to your .env file."
        }), 503

    date_str = request.args.get("date", date.today().isoformat())
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str}"}), 400

    games = Game.query.filter_by(game_date=target_date).all()
    if not games:
        return jsonify({"error": "No games found for that date."}), 404

    updated, skipped, failed = [], [], []

    for game in games:
        label = f"{game.away_team.abbreviation if game.away_team else '?'} @ {game.home_team.abbreviation if game.home_team else '?'}"
        park = game.ballpark

        if not park or park.latitude is None or park.longitude is None:
            failed.append({"game": label, "reason": "No ballpark coordinates"})
            continue

        if park.roof_type == "dome":
            skipped.append({"game": label, "reason": "dome"})
            continue

        try:
            w = fetch_weather(park.latitude, park.longitude, WEATHER_API_KEY)
            game.temperature_f       = w.get("temperature_f")
            game.wind_speed_mph      = w.get("wind_speed_mph")
            game.wind_direction      = w.get("wind_compass")   # e.g. "NW", "SSE"
            game.wind_deg            = w.get("wind_deg")       # raw degrees, 0-359
            game.humidity_pct        = w.get("humidity_pct")
            game.pressure_inhg       = w.get("pressure_inhg")
            game.weather_fetched_at  = datetime.utcnow()
            updated.append({
                "game": label,
                "temp_f": w.get("temperature_f"),
                "wind": f"{w.get('wind_speed_mph')} mph {w.get('wind_compass')}",
            })
        except Exception as e:
            failed.append({"game": label, "reason": str(e)})

    db.session.commit()

    return jsonify({
        "success": True,
        "date": date_str,
        "updated": updated,
        "skipped_domes": skipped,
        "failed": failed,
        "summary": f"{len(updated)} updated, {len(skipped)} domes skipped, {len(failed)} failed",
    })


@app.route("/bankroll", methods=["GET", "POST"])
def bankroll():
    """View and update bankroll with bet performance stats."""
    if request.method == "POST":
        form = request.form
        current = get_current_bankroll()
        change = float(form.get("change", 0))
        new_amount = current + change
        note = form.get("note", "")
        log = BankrollLog(amount=new_amount, change=change, note=note)
        db.session.add(log)
        db.session.commit()
        flash(f"Bankroll updated to ${new_amount:.2f}", "success")
        return redirect(url_for("bankroll"))

    history = BankrollLog.query.order_by(BankrollLog.date.desc()).limit(100).all()
    current = get_current_bankroll()

    # Bet performance stats
    all_placed = BetRecommendation.query.filter_by(placed=True).all()
    graded     = [b for b in all_placed if b.won is not None]
    wins       = [b for b in graded if b.won]
    losses     = [b for b in graded if not b.won]
    total_pl      = sum(b.profit_loss or 0 for b in all_placed if b.profit_loss is not None)
    total_wagered = sum((b.actual_bet_size or b.recommended_bet or 0) for b in graded)
    roi           = (total_pl / total_wagered * 100) if total_wagered > 0 else 0
    pending_count = len([b for b in all_placed if b.won is None])

    # Bankroll chart data (chronological)
    chart_history = BankrollLog.query.order_by(BankrollLog.date.asc()).all()
    chart_dates   = [l.date.strftime("%Y-%m-%d %H:%M") for l in chart_history]
    chart_amounts = [l.amount for l in chart_history]
    if not chart_history:
        chart_dates   = [date.today().isoformat()]
        chart_amounts = [BANKROLL_START]

    return render_template(
        "bankroll.html",
        current=current,
        history=history,
        wins=len(wins),
        losses=len(losses),
        total_pl=total_pl,
        roi=roi,
        total_wagered=total_wagered,
        pending=pending_count,
        chart_dates=chart_dates,
        chart_amounts=chart_amounts,
        bankroll_start=BANKROLL_START,
    )


@app.route("/bets")
def bets_page():
    """Full bet history with filters and performance stats."""

    # Auto-resolve any bets on games that are now final but haven't been graded yet.
    # This makes the page self-healing — no need to manually hit the settle button.
    ungraded_game_ids = (
        db.session.query(BetRecommendation.game_id)
        .filter(
            BetRecommendation.won == None,         # noqa: E711
            BetRecommendation.profit_loss == None, # noqa: E711 — skip pushes (pl=0.0)
        )
        .distinct()
        .all()
    )
    if ungraded_game_ids:
        id_list = [row[0] for row in ungraded_game_ids]
        final_games = Game.query.filter(
            Game.id.in_(id_list),
            Game.status == "final",
            Game.home_score != None,   # noqa: E711
            Game.away_score != None,   # noqa: E711
        ).all()
        for g in final_games:
            _resolve_bets(g)

        # Also sync status for "scheduled" games whose status may have changed.
        # Handles: game postponed (show POSTPONED badge), or game played but DB
        # wasn't updated yet (sync score + grade bets automatically).
        scheduled_games = Game.query.filter(
            Game.id.in_(id_list),
            Game.status.in_(["scheduled", "Scheduled"]),
            Game.mlb_game_id != None,  # noqa: E711
        ).all()
        status_changed = False
        newly_final = []
        for g in scheduled_games:
            try:
                result = get_game_result(g.mlb_game_id)
                if not result:
                    continue
                api_status = result.get("status", "").lower()
                if api_status == "final":
                    hs = result.get("home_score")
                    as_ = result.get("away_score")
                    if hs is not None and as_ is not None:
                        g.status     = "final"
                        g.home_score = int(hs)
                        g.away_score = int(as_)
                        status_changed = True
                        newly_final.append(g)
                elif api_status in ("postponed", "cancelled"):
                    g.status = api_status
                    status_changed = True
            except Exception:
                pass
        if status_changed:
            db.session.commit()
        for g in newly_final:
            _resolve_bets(g)

    # Auto-populate model stats for any manual bet still missing them
    missing_stats = BetRecommendation.query.filter(
        BetRecommendation.is_manual == True,        # noqa: E712
        BetRecommendation.placed   == True,         # noqa: E712
        BetRecommendation.model_probability == 0.0,
    ).all()
    if missing_stats:
        updated = 0
        for b in missing_stats:
            if _apply_model_stats(b):
                updated += 1
        if updated:
            db.session.commit()

    filter_type = request.args.get("filter", "placed")

    query = BetRecommendation.query
    if filter_type == "placed":
        query = query.filter(BetRecommendation.placed == True)   # noqa: E712
    elif filter_type == "won":
        query = query.filter(BetRecommendation.placed == True,
                             BetRecommendation.won == True)       # noqa: E712
    elif filter_type == "lost":
        query = query.filter(BetRecommendation.placed == True,
                             BetRecommendation.won == False)      # noqa: E712
    elif filter_type == "pending":
        query = query.filter(BetRecommendation.placed == True,
                             BetRecommendation.won == None)       # noqa: E711
    # "all" — no extra filter

    bets = query.order_by(BetRecommendation.created_at.desc()).all()

    # Global stats (all placed bets, not just the filtered view)
    all_placed    = BetRecommendation.query.filter_by(placed=True).all()
    graded        = [b for b in all_placed if b.won is not None]
    wins          = [b for b in graded if b.won]
    losses        = [b for b in graded if not b.won]
    total_pl      = sum(b.profit_loss or 0 for b in all_placed if b.profit_loss is not None)
    total_wagered = sum((b.actual_bet_size or b.recommended_bet or 0) for b in graded)
    roi           = (total_pl / total_wagered * 100) if total_wagered > 0 else 0
    pending_count = len([b for b in all_placed if b.won is None])

    # Build a game lookup map so the template can show team abbreviations
    game_ids = list({b.game_id for b in bets})
    games_list = Game.query.filter(Game.id.in_(game_ids)).all() if game_ids else []
    games_map  = {g.id: g for g in games_list}

    # Recent + upcoming games for the manual bet game dropdown (last 7 days + today)
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=7)
    manual_games = (
        Game.query
        .filter(Game.game_date >= cutoff)
        .order_by(Game.game_date.desc(), Game.id.asc())
        .all()
    )

    return render_template(
        "bets.html",
        bets=bets,
        games_map=games_map,
        filter_type=filter_type,
        wins=len(wins),
        losses=len(losses),
        total_pl=total_pl,
        roi=roi,
        total_wagered=total_wagered,
        pending=pending_count,
        manual_games=manual_games,
    )


@app.route("/fangraphs-help")
def fangraphs_help():
    """Show FanGraphs export instructions."""
    instructions = get_import_instructions()
    return render_template("fangraphs_help.html", instructions=instructions)


@app.route("/api/game/<int:game_id>/ou-prob")
def api_ou_prob(game_id):
    """Return over/under probability for a given total line using the latest simulation."""
    line = request.args.get("line", type=float)
    if line is None:
        return jsonify({"error": "line parameter required"}), 400

    sim = SimulationResult.query.filter_by(game_id=game_id).order_by(
        SimulationResult.created_at.desc()
    ).first()
    if not sim or not sim.score_distribution:
        return jsonify({"error": "no simulation found"}), 404

    import json as _json
    import numpy as _np
    dist   = _json.loads(sim.score_distribution)
    home   = _np.array(dist["home_scores"])
    away   = _np.array(dist["away_scores"])
    totals = home + away
    n      = len(totals)
    over_p  = round(float(_np.sum(totals > line) / n), 4)
    under_p = round(float(_np.sum(totals < line) / n), 4)
    return jsonify({"over": over_p, "under": under_p, "push": round(1 - over_p - under_p, 4), "line": line})


@app.route("/api/simulation/<int:game_id>")
def api_simulation(game_id):
    """JSON endpoint for simulation results (used by charts)."""
    sim = SimulationResult.query.filter_by(game_id=game_id).order_by(
        SimulationResult.created_at.desc()
    ).first()
    if not sim:
        return jsonify({"error": "No simulation found"}), 404

    dist = json.loads(sim.score_distribution) if sim.score_distribution else {}
    return jsonify({
        "home_win_pct": sim.home_win_pct,
        "away_win_pct": sim.away_win_pct,
        "home_avg_runs": sim.home_avg_runs,
        "away_avg_runs": sim.away_avg_runs,
        "total_avg_runs": sim.total_avg_runs,
        "over_pct": sim.over_pct,
        "under_pct": sim.under_pct,
        "home_cover_pct": sim.home_cover_runline_pct,
        "away_cover_pct": sim.away_cover_runline_pct,
        "score_distribution": dist,
    })


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------

_backtest_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "status": "idle",
    "results": None,
    "error": None,
}
_backtest_lock = threading.Lock()

BACKTEST_RESULTS_PATH = os.path.join(os.path.dirname(__file__), "backtest", "results_latest.json")
BACKTEST_CSV_PATH     = os.path.join(os.path.dirname(__file__), "backtest", "results.csv")


def _load_saved_backtest():
    """Load the most recent backtest results JSON from disk (if it exists)."""
    if os.path.exists(BACKTEST_RESULTS_PATH):
        try:
            with open(BACKTEST_RESULTS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """View and edit app-wide configuration (edge threshold, Kelly fraction, etc.)."""
    message = None
    if request.method == "POST":
        min_edge = request.form.get("min_edge_pct", "6.0").strip()
        kelly_frac = request.form.get("kelly_fraction", "0.25").strip()
        try:
            min_edge_val = float(min_edge)
            kelly_val    = float(kelly_frac)
            if not (0.5 <= min_edge_val <= 25.0):
                raise ValueError("Edge must be between 0.5 and 25")
            if not (0.05 <= kelly_val <= 1.0):
                raise ValueError("Kelly fraction must be between 0.05 and 1.0")
            save_setting("min_edge_pct",    min_edge_val)
            save_setting("kelly_fraction",  kelly_val)
            message = ("success", "Settings saved. Re-simulate today's games to apply the new thresholds.")
        except ValueError as e:
            message = ("danger", f"Invalid value: {e}")

    current = {
        "min_edge_pct":   get_setting("min_edge_pct",   6.0),
        "kelly_fraction": get_setting("kelly_fraction",  0.25),
    }
    return render_template("settings.html", current=current, message=message,
                           admin_status=ADMIN_TASK_STATUS)


# ---------------------------------------------------------------------------
# Admin tasks (FanGraphs import, roster sync) — fire-and-forget background
# ---------------------------------------------------------------------------
# These can take 30-90 seconds, so we run them in a background thread and
# expose progress via ADMIN_TASK_STATUS. The settings page reads it to render
# a status badge, and /admin/status returns it as JSON for live polling.

ADMIN_TASK_STATUS = {
    "fangraphs": {"state": "idle", "detail": "Never run", "started_at": None, "finished_at": None},
    "rosters":   {"state": "idle", "detail": "Never run", "started_at": None, "finished_at": None},
}


def _run_admin_task(task_id, fn):
    """Run an admin task in a background thread, track status in ADMIN_TASK_STATUS."""
    import threading, traceback
    def _wrapped():
        ADMIN_TASK_STATUS[task_id] = {
            "state":       "running",
            "detail":      "In progress…",
            "started_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
        }
        try:
            with app.app_context():
                detail = fn()
            ADMIN_TASK_STATUS[task_id] = {
                "state":       "ok",
                "detail":      detail or "Completed.",
                "started_at":  ADMIN_TASK_STATUS[task_id]["started_at"],
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.error(f"[admin] {task_id} failed: {e}\n{traceback.format_exc()}")
            ADMIN_TASK_STATUS[task_id] = {
                "state":       "error",
                "detail":      f"{type(e).__name__}: {e}",
                "started_at":  ADMIN_TASK_STATUS[task_id]["started_at"],
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
    threading.Thread(target=_wrapped, daemon=True).start()


@app.route("/admin/import-fangraphs", methods=["POST"])
def admin_import_fangraphs():
    """Run FanGraphs CSV import against this environment's DB.
    Reads CSVs from exports/{year}/ folder (which is in the repo)."""
    if ADMIN_TASK_STATUS["fangraphs"]["state"] == "running":
        flash("FanGraphs import is already running. Check back in a minute.", "danger")
        return redirect(url_for("settings_page"))

    seasons_arg = request.form.get("seasons", "").strip()
    if seasons_arg:
        try:
            seasons = [int(s) for s in seasons_arg.split(",")]
        except ValueError:
            flash(f"Invalid seasons: {seasons_arg}", "danger")
            return redirect(url_for("settings_page"))
    else:
        # Default: every season that has at least one CSV present
        seasons = []
        for y in range(2024, date.today().year + 1):
            year_dir = os.path.join(os.path.dirname(__file__), "exports", str(y))
            if os.path.isdir(year_dir) and any(f.endswith(".csv") for f in os.listdir(year_dir)):
                seasons.append(y)

    if not seasons:
        flash("No FanGraphs CSVs found in exports/ — push them to the repo first.", "danger")
        return redirect(url_for("settings_page"))

    def _do_import():
        from import_data import import_fangraphs_batting, import_fangraphs_pitching
        results = []
        for season in seasons:
            import_fangraphs_batting(season)
            import_fangraphs_pitching(season)
            results.append(str(season))
        return f"Imported FanGraphs CSVs for season(s): {', '.join(results)}"

    _run_admin_task("fangraphs", _do_import)
    flash(f"FanGraphs import started for season(s) {seasons}. "
          f"Refresh the page in ~30-60s to see status.", "success")
    return redirect(url_for("settings_page"))


@app.route("/admin/update-rosters", methods=["POST"])
def admin_update_rosters():
    """Sync rosters + handedness + pitching stats from MLB API."""
    if ADMIN_TASK_STATUS["rosters"]["state"] == "running":
        flash("Roster sync is already running. Check back in a minute.", "danger")
        return redirect(url_for("settings_page"))

    season = int(request.form.get("season") or date.today().year)
    mode   = request.form.get("mode", "all")  # all|stats|rosters|handedness

    def _do_sync():
        from update_rosters import sync_rosters, sync_handedness, sync_pitching_stats
        if mode == "handedness":
            sync_handedness()
            return f"Handedness synced for season {season}."
        if mode == "rosters":
            sync_rosters(season)
            sync_handedness()
            return f"Rosters + handedness synced for season {season}."
        if mode == "stats":
            sync_pitching_stats(season)
            return f"Pitching stats synced for season {season}."
        # default: full sync
        sync_rosters(season)
        sync_handedness()
        sync_pitching_stats(season)
        return f"Full sync (rosters + handedness + pitching stats) done for season {season}."

    _run_admin_task("rosters", _do_sync)
    flash(f"Roster/stats sync started for season {season}, mode={mode}. "
          f"This usually takes 1-2 minutes — refresh the page to see status.", "success")
    return redirect(url_for("settings_page"))


@app.route("/admin/status")
def admin_status():
    """JSON snapshot of admin task progress — used for live polling from the UI."""
    return jsonify(ADMIN_TASK_STATUS)


@app.route("/admin/debug-game/<int:game_id>")
@login_required
def admin_debug_game(game_id):
    """Diagnostic — dump key fields from a Game row."""
    g = Game.query.get_or_404(game_id)
    return jsonify({
        "id": g.id,
        "mlb_game_id": g.mlb_game_id,
        "game_date": g.game_date.isoformat() if g.game_date else None,
        "home_team_id": g.home_team_id,
        "away_team_id": g.away_team_id,
        "home_team": g.home_team.abbreviation if g.home_team else None,
        "away_team": g.away_team.abbreviation if g.away_team else None,
        "home_starter_id": g.home_starter_id,
        "away_starter_id": g.away_starter_id,
        "home_starter_name": g.home_starter.name if g.home_starter else None,
        "away_starter_name": g.away_starter.name if g.away_starter else None,
        "ballpark_id": g.ballpark_id,
        "status": g.status,
    })


@app.route("/admin/debug-find-pitcher")
@login_required
def admin_debug_find_pitcher():
    """Diagnostic — runs find_or_create_pitcher and reports which step matched.
    Pass ?name=...&team=ABBR (e.g. ?name=Payton%20Tolle&team=BOS).
    Lets us see why a pitcher isn't being matched on Railway when it works locally."""
    import statsapi as _stats
    name = request.args.get("name", "").strip()
    team_abbr = request.args.get("team", "").strip().upper()
    out = {"name": name, "team": team_abbr, "steps": []}

    if not name or not team_abbr:
        return jsonify({"error": "need ?name= and ?team= params"}), 400

    team = Team.query.filter_by(abbreviation=team_abbr).first()
    if not team:
        return jsonify({"error": f"unknown team {team_abbr}"}), 400
    out["team_id"] = team.id

    PITCHER_POSITIONS = {"SP", "RP", "P", "TWP"}
    last = name.split()[-1]

    # Step 2
    p = Player.query.filter_by(name=name, team_id=team.id).first()
    out["steps"].append({"step": 2, "name": "exact name + team_id",
                         "match": (p.id if p else None)})

    # Step 3
    p = Player.query.filter(
        Player.team_id == team.id,
        Player.name.ilike(f"%{last}%"),
        Player.position.in_(PITCHER_POSITIONS),
    ).first()
    out["steps"].append({"step": 3, "name": "fuzzy last + team_id",
                         "match": (p.id if p else None)})

    # Step 4
    cand4 = Player.query.filter(
        Player.name == name,
        Player.position.in_(PITCHER_POSITIONS),
    ).all()
    out["steps"].append({"step": 4, "name": "exact-name global pitchers",
                         "candidates": [(c.id, c.name, c.team_id) for c in cand4]})

    # Step 5
    cand5 = Player.query.filter(
        Player.name.ilike(f"%{last}%"),
        Player.position.in_(PITCHER_POSITIONS),
    ).all()
    out["steps"].append({"step": 5, "name": "fuzzy last global pitchers",
                         "candidates": [(c.id, c.name, c.team_id) for c in cand5]})

    # Step 6 — MLB API
    try:
        people = _stats.lookup_player(name)
        out["steps"].append({"step": 6, "name": "MLB API lookup_player",
                             "results": [{"id": pp.get("id"),
                                          "fullName": pp.get("fullName"),
                                          "position": (pp.get("primaryPosition") or {}).get("abbreviation"),
                                          "throws": (pp.get("pitchHand") or {}).get("code"),
                                          } for pp in people]})
    except Exception as e:
        out["steps"].append({"step": 6, "name": "MLB API lookup_player",
                             "error": f"{type(e).__name__}: {e}"})

    return jsonify(out)


@app.route("/backtest")
def backtest_page():
    """Backtest configuration + results page."""
    with _backtest_lock:
        state = dict(_backtest_state)

    saved = _load_saved_backtest()
    # If a completed run is already in memory, prefer it; else fall back to file
    if state.get("results"):
        results = state["results"]
    else:
        results = saved

    from database.schema import HistoricalOdds
    hist_odds_count = HistoricalOdds.query.count()

    return render_template("backtest.html", state=state, results=results,
                           hist_odds_count=hist_odds_count)


@app.route("/backtest/run", methods=["POST"])
def backtest_run():
    """Launch a backtest in a background thread."""
    with _backtest_lock:
        if _backtest_state["running"]:
            return jsonify({"error": "A backtest is already running. Please wait."}), 409

        start_date     = request.form.get("start_date", "2024-04-01")
        end_date       = request.form.get("end_date",   "2024-06-30")
        min_edge       = float(request.form.get("min_edge",       2.0))
        kelly_fraction = float(request.form.get("kelly_fraction", 0.25))
        max_bet_pct    = float(request.form.get("max_bet_pct",    5.0)) / 100.0
        bankroll_start = float(request.form.get("bankroll_start", 1000.0))

        _backtest_state.update({
            "running":  True,
            "progress": 0,
            "total":    0,
            "status":   f"Starting… loading schedule for {start_date} → {end_date}",
            "results":  None,
            "error":    None,
        })

    def _run():
        from backtest.backtest import simulate_season_backtest

        def on_progress(day, total):
            with _backtest_lock:
                _backtest_state["progress"] = day
                _backtest_state["total"]    = total
                _backtest_state["status"]   = f"Simulating day {day} of {total}…"

        try:
            with app.app_context():
                summary = simulate_season_backtest(
                    app_context=None,
                    start_date=start_date,
                    end_date=end_date,
                    min_edge=min_edge,
                    kelly_fraction=kelly_fraction,
                    max_bet_pct=max_bet_pct,
                    starting_bankroll=bankroll_start,
                    progress_callback=on_progress,
                )

            # Merge per-bet CSV rows into results for charting
            bets_data = []
            if os.path.exists(BACKTEST_CSV_PATH):
                with open(BACKTEST_CSV_PATH, newline="") as f:
                    for row in csv_mod.DictReader(f):
                        bets_data.append(row)

            full_results = {**summary, "bets": bets_data}

            # Persist to disk
            os.makedirs(os.path.dirname(BACKTEST_RESULTS_PATH), exist_ok=True)
            with open(BACKTEST_RESULTS_PATH, "w") as f:
                json.dump(full_results, f)

            with _backtest_lock:
                _backtest_state.update({
                    "running": False,
                    "status":  "complete",
                    "results": full_results,
                })

        except Exception as exc:
            logger.exception("Backtest thread error")
            with _backtest_lock:
                _backtest_state.update({
                    "running": False,
                    "status":  "error",
                    "error":   str(exc),
                })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/backtest/status")
def backtest_status():
    """JSON endpoint polled by the front-end to get progress."""
    with _backtest_lock:
        state = dict(_backtest_state)
    # Don't serialize full results here (can be large); send summary only
    if state.get("results"):
        state["results"] = {k: v for k, v in state["results"].items() if k != "bets"}
    return jsonify(state)


# ---------------------------------------------------------------------------
# Init DB and seed data
# ---------------------------------------------------------------------------

@app.route("/api/scheduler/status")
def api_scheduler_status():
    """Return last-run / next-run status for all scheduled jobs."""
    return jsonify(sched.get_status())


@app.route("/api/scheduler/run/<job_id>", methods=["POST"])
def api_scheduler_run(job_id):
    """Manually trigger a scheduler job by ID."""
    runners = {
        "import_games": lambda: sched.run_import_games(app, date.today() + __import__('datetime').timedelta(days=1)),
        "weather":      lambda: sched.run_weather_refresh(app),
        "umpires":      lambda: sched.run_umpire_refresh(app),
    }
    if job_id not in runners:
        return jsonify({"error": f"Unknown job: {job_id}"}), 404
    try:
        result = runners[job_id]()
        sched.SCHEDULER_LOG[job_id]["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        sched.SCHEDULER_LOG[job_id]["status"]   = "ok"
        sched.SCHEDULER_LOG[job_id]["detail"]   = str(result)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        sched.SCHEDULER_LOG[job_id]["status"] = "error"
        sched.SCHEDULER_LOG[job_id]["detail"] = str(e)
        return jsonify({"error": str(e)}), 500


@app.route("/quick-entry")
def quick_entry():
    """Quick odds entry page — enter lines from Don Best, get instant analysis."""
    today = date.today()
    games = (
        Game.query
        .filter(Game.game_date == today, Game.status != "final")
        .order_by(Game.game_time_utc)
        .all()
    )
    # Pre-populate with any existing manual or API odds
    odds_by_game = {}
    for game in games:
        entry = {}
        for market in ["h2h", "totals", "f5_moneyline", "f5_totals"]:
            row = (Odds.query
                   .filter_by(game_id=game.id, market=market)
                   .order_by(Odds.fetched_at.desc())
                   .first())
            if row:
                entry[market] = row
        odds_by_game[game.id] = entry
    from datetime import timedelta as _timedelta
    return render_template("quick_entry.html", games=games, odds_by_game=odds_by_game,
                           today=today, timedelta=_timedelta)


@app.route("/quick-entry/analyze", methods=["POST"])
def quick_entry_analyze():
    """
    Receive odds for one or more games, save them, run/use simulation,
    apply research-validated filters, return profitable bets as JSON.
    """
    try:
        return _quick_entry_analyze_inner()
    except Exception as e:
        import traceback
        logger.error(f"quick_entry_analyze error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e), "bets": []}), 500


def _quick_entry_analyze_inner():
    import json as _json
    import numpy as _np
    from models.simulation import calculate_f5_over_under

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    bankroll   = get_current_bankroll()
    MIN_EDGE   = float(get_setting('min_edge_pct', 6.0))
    MAX_EDGE   = 30.0
    results    = []

    def _int(v):
        try:
            return int(v) if v not in (None, "", "null") else None
        except (ValueError, TypeError):
            return None

    def _float(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (ValueError, TypeError):
            return None

    for entry in data.get("games", []):
        game_id = entry.get("game_id")
        game    = Game.query.get(game_id)
        if not game:
            continue

        now = datetime.utcnow()
        bookmaker = "Manual"

        # ── Save odds to DB so sim can use them ───────────────────────────
        home_ml    = _int(entry.get("home_ml"))
        away_ml    = _int(entry.get("away_ml"))
        total_line = _float(entry.get("total_line"))
        over_odds  = _int(entry.get("over_odds"))
        under_odds = _int(entry.get("under_odds"))
        f5_home_ml = _int(entry.get("f5_home_ml"))
        f5_away_ml = _int(entry.get("f5_away_ml"))
        f5_total   = _float(entry.get("f5_total"))
        f5_over    = _int(entry.get("f5_over"))
        f5_under   = _int(entry.get("f5_under"))

        def _upsert_odds(market, **kwargs):
            row = Odds.query.filter_by(game_id=game_id, bookmaker=bookmaker, market=market).first()
            if row:
                for k, v in kwargs.items():
                    setattr(row, k, v)
                row.fetched_at = now
            else:
                row = Odds(game_id=game_id, bookmaker=bookmaker, market=market,
                           fetched_at=now, **kwargs)
                db.session.add(row)

        if home_ml is not None or away_ml is not None:
            _upsert_odds("h2h", home_price=home_ml, away_price=away_ml)
        if total_line is not None or over_odds is not None or under_odds is not None:
            _upsert_odds("totals", total_line=total_line, over_price=over_odds, under_price=under_odds)
        if f5_home_ml is not None or f5_away_ml is not None:
            _upsert_odds("f5_moneyline", home_price=f5_home_ml, away_price=f5_away_ml)
        if f5_total is not None or f5_over is not None or f5_under is not None:
            _upsert_odds("f5_totals", total_line=f5_total, over_price=f5_over, under_price=f5_under)

        db.session.commit()

        # ── Get or run simulation ─────────────────────────────────────────
        sim = (SimulationResult.query
               .filter_by(game_id=game_id)
               .order_by(SimulationResult.simulated_at.desc())
               .first())

        if not sim:
            # Trigger inline simulation (re-uses same logic as /game/<id>/simulate)
            try:
                from models.simulation import (
                    run_simulations, GameInputs, BatterProfile, PitcherProfile,
                    BullpenProfile, ParkFactors, build_batter_profile, build_pitcher_profile,
                    calculate_over_under, calculate_runline,
                )
                from database.schema import PlayerStats, PitchingStats, Player

                season = game.game_date.year

                def _lg_bat(name=""):
                    return BatterProfile(name=name, bats="R",
                        single_rate=0.149, double_rate=0.046, triple_rate=0.004,
                        hr_rate=0.034, walk_rate=0.083, strikeout_rate=0.225, out_rate=0.459)

                def _lineup(team_id, throws="R"):
                    split = "vs_LHP" if throws == "L" else "vs_RHP"
                    stats = (PlayerStats.query.join(Player)
                             .filter(Player.team_id==team_id, PlayerStats.season==season,
                                     PlayerStats.split==split)
                             .order_by(PlayerStats.woba.desc()).limit(9).all())
                    if len(stats) < 5:
                        stats = (PlayerStats.query.join(Player)
                                 .filter(Player.team_id==team_id, PlayerStats.season==season,
                                         PlayerStats.split=="overall")
                                 .order_by(PlayerStats.woba.desc()).limit(9).all())
                    batters = [build_batter_profile(
                        name=s.player.name, bats=s.player.bats or "R", pa=s.pa or 0,
                        single_rate=s.single_rate or 0.150, double_rate=s.double_rate or 0.047,
                        triple_rate=s.triple_rate or 0.005, hr_rate=s.hr_rate or 0.030,
                        walk_rate=s.walk_rate or 0.084, strikeout_rate=s.strikeout_rate or 0.226,
                        out_rate=s.out_rate or 0.458) for s in stats]
                    while len(batters) < 9:
                        batters.append(_lg_bat())
                    return batters[:9]

                def _starter(team_id):
                    rotation = (PitchingStats.query.join(Player)
                                .filter(Player.team_id==team_id, PitchingStats.season==season,
                                        PitchingStats.role=="SP", PitchingStats.split=="overall",
                                        PitchingStats.games_started > 0)
                                .order_by(PitchingStats.games_started.desc()).limit(6).all())
                    if not rotation:
                        return PitcherProfile(name="TBD", throws="R",
                            single_rate_allowed=0.150, double_rate_allowed=0.047,
                            triple_rate_allowed=0.005, hr_rate_allowed=0.030,
                            walk_rate_allowed=0.084, strikeout_rate=0.226, out_rate=0.458, stamina=6.0)
                    tip = sum(s.ip or 0 for s in rotation) or 1.0
                    def wv(f): return sum((getattr(s,f) or 0)*(s.ip or 0) for s in rotation)/tip
                    ts = sum(s.games_started or 0 for s in rotation) or 1
                    lhp = sum((s.ip or 0) for s in rotation if s.player.throws=="L")
                    profile = build_pitcher_profile(name="Rotation Avg",
                        throws="L" if lhp > tip/2 else "R", ip=tip,
                        single_rate_allowed=wv("single_rate_allowed"),
                        double_rate_allowed=wv("double_rate_allowed"),
                        triple_rate_allowed=wv("triple_rate_allowed"),
                        hr_rate_allowed=wv("hr_rate_allowed"),
                        walk_rate_allowed=wv("walk_rate_allowed"),
                        strikeout_rate=wv("strikeout_rate"),
                        out_rate=wv("out_rate"), stamina=tip/ts)

                    # Build rotation-average platoon splits the same way:
                    # for each batter handedness, query the rotation's vs_LHB
                    # or vs_RHB rows, IP-weight them, and regress.
                    rotation_player_ids = [s.player_id for s in rotation]
                    rotation_splits = {}
                    for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
                        split_rows = (PitchingStats.query
                                      .filter(PitchingStats.player_id.in_(rotation_player_ids),
                                              PitchingStats.season==season,
                                              PitchingStats.role=="SP",
                                              PitchingStats.split==db_split)
                                      .all())
                        if not split_rows:
                            continue
                        split_tip = sum(r.ip or 0 for r in split_rows)
                        # Same threshold as single-game path: 50 IP minimum
                        # (~200 batters faced in this split across the rotation)
                        if split_tip < 50.0:
                            continue
                        def split_wv(f, rows=split_rows, tip=split_tip):
                            return sum((getattr(r, f) or 0) * (r.ip or 0) for r in rows) / tip
                        rotation_splits[bat_hand] = build_pitcher_split_rates({
                            "single_rate_allowed":   split_wv("single_rate_allowed"),
                            "double_rate_allowed":   split_wv("double_rate_allowed"),
                            "triple_rate_allowed":   split_wv("triple_rate_allowed"),
                            "hr_rate_allowed":       split_wv("hr_rate_allowed"),
                            "walk_rate_allowed":     split_wv("walk_rate_allowed"),
                            "strikeout_rate":        split_wv("strikeout_rate"),
                            "out_rate":              split_wv("out_rate"),
                        }, split_tip)
                    if rotation_splits:
                        profile.splits = rotation_splits
                    return profile

                def _bullpen(team_id):
                    rels = (PitchingStats.query.join(Player)
                            .filter(Player.team_id==team_id, PitchingStats.season==season,
                                    PitchingStats.role=="RP", PitchingStats.split=="overall").all())
                    if not rels:
                        return BullpenProfile(single_rate_allowed=0.155, double_rate_allowed=0.048,
                            triple_rate_allowed=0.004, hr_rate_allowed=0.038,
                            walk_rate_allowed=0.090, strikeout_rate=0.220, out_rate=0.445)
                    tip = sum(r.ip or 0 for r in rels) or 1.0
                    def wv(f): return sum((getattr(r,f) or 0)*(r.ip or 0) for r in rels)/tip
                    bp = build_pitcher_profile(name="Bullpen", throws="R", ip=tip,
                        single_rate_allowed=wv("single_rate_allowed"),
                        double_rate_allowed=wv("double_rate_allowed"),
                        triple_rate_allowed=wv("triple_rate_allowed"),
                        hr_rate_allowed=wv("hr_rate_allowed"),
                        walk_rate_allowed=wv("walk_rate_allowed"),
                        strikeout_rate=wv("strikeout_rate"), out_rate=wv("out_rate"), is_reliever=True)
                    return BullpenProfile(
                        single_rate_allowed=bp.single_rate_allowed,
                        double_rate_allowed=bp.double_rate_allowed,
                        triple_rate_allowed=bp.triple_rate_allowed,
                        hr_rate_allowed=bp.hr_rate_allowed,
                        walk_rate_allowed=bp.walk_rate_allowed,
                        strikeout_rate=bp.strikeout_rate, out_rate=bp.out_rate)

                # Use the game's own ballpark (which may be a neutral/intl
                # venue like Mexico City), NOT the home team's regular stadium.
                park = ParkFactors()
                if game.ballpark:
                    park = ParkFactors(
                        runs=game.ballpark.park_factor_runs or 1.0,
                        hr=game.ballpark.park_factor_hr or 1.0,
                        hits=game.ballpark.park_factor_hits or 1.0)

                hsp = _starter(game.home_team_id)
                asp = _starter(game.away_team_id)
                sim_inputs = GameInputs(
                    home_lineup=_lineup(game.home_team_id, asp.throws),
                    away_lineup=_lineup(game.away_team_id, hsp.throws),
                    home_starter=hsp, away_starter=asp,
                    home_bullpen=_bullpen(game.home_team_id),
                    away_bullpen=_bullpen(game.away_team_id),
                    park=park)
                # Hybrid sim: splits-ON drives ML, splits-OFF drives totals.
                # See same logic in run_simulation() above for backtest rationale.
                sim_results = run_simulations(sim_inputs, n=10000)

                from dataclasses import replace as _dc_replace
                from copy import copy as _copy
                def _no_splits(pp):
                    if pp is None or getattr(pp, "splits", None) is None:
                        return pp
                    pp2 = _copy(pp); pp2.splits = None; return pp2
                sim_inputs_no_splits = _dc_replace(
                    sim_inputs,
                    home_starter=_no_splits(hsp),
                    away_starter=_no_splits(asp),
                )
                sim_results_totals = run_simulations(sim_inputs_no_splits, n=10000)

                sim = SimulationResult(
                    game_id=game_id,
                    num_simulations=10000,
                    # Full-game ML — splits-ON
                    home_win_pct=sim_results["home_win_pct"],
                    away_win_pct=sim_results["away_win_pct"],
                    # F5 ML — splits-OFF (2025 F5 backtest: +2.33pp ROI vs splits-ON)
                    f5_home_win_pct=sim_results_totals.get("f5_home_win_pct"),
                    f5_away_win_pct=sim_results_totals.get("f5_away_win_pct"),
                    f5_tie_pct=sim_results_totals.get("f5_tie_pct"),
                    # Run averages + stored distribution — splits-OFF (totals)
                    home_avg_runs=sim_results_totals["home_avg_runs"],
                    away_avg_runs=sim_results_totals["away_avg_runs"],
                    total_avg_runs=sim_results_totals["total_avg_runs"],
                    total_std_dev=sim_results_totals["total_std_dev"],
                    score_distribution=sim_results_totals["score_distribution"],
                    f5_home_avg_runs=sim_results_totals.get("f5_home_avg_runs"),
                    f5_away_avg_runs=sim_results_totals.get("f5_away_avg_runs"),
                )
                db.session.add(sim)
                db.session.commit()
            except Exception as e:
                results.append({"game_id": game_id, "error": f"Sim failed: {e}"})
                continue

        if not sim:
            results.append({"game_id": game_id, "error": "No simulation available"})
            continue

        # ── Full-game moneyline analysis ─────────────────────────────────
        # Matches the filter logic in the main recommendation engine:
        #   - Calibrated probabilities
        #   - Edge floor: 8%
        #   - Cut market-implied prob >= 65% (heavy favorites)
        #   (See lines ~2173 for full rationale.)
        ML_MIN_EDGE    = max(MIN_EDGE, 8.0)
        ML_MAX_IMPLIED = 0.65

        for side, raw_prob, ml in [
            ("home", sim.home_win_pct, home_ml),
            ("away", sim.away_win_pct, away_ml),
        ]:
            if ml is None:
                continue
            if american_to_implied_prob(ml) >= ML_MAX_IMPLIED:
                continue   # heavy favorite zone — skip
            our_prob   = calibrate_prob(raw_prob, "moneyline", side)
            edge_floor = ML_MIN_EDGE

            a = analyze_bet(our_prob, ml, bankroll)
            if edge_floor <= a["edge_pct"] <= MAX_EDGE:
                results.append({
                    "game":       f"{game.away_team.abbreviation} @ {game.home_team.abbreviation}",
                    "game_id":    game_id,
                    "bet_type":   "Moneyline",
                    "side":       side.title(),
                    "team":       game.home_team.name if side == "home" else game.away_team.name,
                    "our_prob":   round(our_prob * 100, 1),
                    "mkt_prob":   round(american_to_implied_prob(ml) * 100, 1),
                    "edge":       round(a["edge_pct"], 1),
                    "ev":         round(a["ev_pct"], 1),
                    "bet_size":   round(min(a["recommended_bet"], bankroll * 0.05, 500), 0),
                    "odds":       f"+{ml}" if ml > 0 else str(ml),
                    "kelly_raw":  round(a["recommended_bet"], 2),
                })

        # ── Full-game totals analysis ─────────────────────────────────────
        if total_line is not None and over_odds is not None and under_odds is not None:
            try:
                dist = _json.loads(sim.score_distribution)
                home_scores = _np.array(dist["home_scores"])
                away_scores = _np.array(dist["away_scores"])
                totals      = home_scores + away_scores
                n           = len(totals)
                over_prob   = round(float(_np.sum(totals > total_line) / n), 4)
                under_prob  = round(float(_np.sum(totals < total_line) / n), 4)

                TOTALS_MIN_RAW = 0.58
                for side, our_raw, ml in [
                    ("over",  over_prob,  over_odds),
                    ("under", under_prob, under_odds),
                ]:
                    if our_raw < TOTALS_MIN_RAW:
                        continue
                    our_p = calibrate_prob(our_raw, "totals", side)
                    a = analyze_bet(our_p, ml, bankroll)
                    edge_floor = max(MIN_EDGE, 6.0)
                    if edge_floor <= a["edge_pct"] <= MAX_EDGE:
                        results.append({
                            "game":      f"{game.away_team.abbreviation} @ {game.home_team.abbreviation}",
                            "game_id":   game_id,
                            "bet_type":  "Total",
                            "side":      f"{side.title()} {total_line}",
                            "team":      "—",
                            "our_prob":  round(our_p * 100, 1),
                            "mkt_prob":  round(american_to_implied_prob(ml) * 100, 1),
                            "edge":      round(a["edge_pct"], 1),
                            "ev":        round(a["ev_pct"], 1),
                            "bet_size":  round(min(a["recommended_bet"], bankroll * 0.05, 500), 0),
                            "odds":      f"+{ml}" if ml > 0 else str(ml),
                            "kelly_raw": round(a["recommended_bet"], 2),
                        })
            except Exception:
                pass

        # ── F5 moneyline analysis ─────────────────────────────────────────
        f5_home_prob = sim.f5_home_win_pct
        f5_away_prob = sim.f5_away_win_pct
        if f5_home_prob and f5_away_prob:
            for side, our_prob, ml in [
                ("home", f5_home_prob, f5_home_ml),
                ("away", f5_away_prob, f5_away_ml),
            ]:
                if ml is None:
                    continue
                a = analyze_bet(our_prob, ml, bankroll)
                edge_floor = max(MIN_EDGE, 6.0)
                if edge_floor <= a["edge_pct"] <= MAX_EDGE:
                    results.append({
                        "game":      f"{game.away_team.abbreviation} @ {game.home_team.abbreviation}",
                        "game_id":   game_id,
                        "bet_type":  "F5 ML",
                        "side":      side.title(),
                        "team":      game.home_team.name if side == "home" else game.away_team.name,
                        "our_prob":  round(our_prob * 100, 1),
                        "mkt_prob":  round(american_to_implied_prob(ml) * 100, 1),
                        "edge":      round(a["edge_pct"], 1),
                        "ev":        round(a["ev_pct"], 1),
                        "bet_size":  round(min(a["recommended_bet"], bankroll * 0.05, 500), 0),
                        "odds":      f"+{ml}" if ml > 0 else str(ml),
                        "kelly_raw": round(a["recommended_bet"], 2),
                    })

        # ── F5 totals analysis ────────────────────────────────────────────
        if f5_total is not None and f5_over is not None and f5_under is not None:
            try:
                f5_ou = calculate_f5_over_under(
                    {"score_distribution": sim.score_distribution}, f5_total)
                F5_TOTALS_MIN = 0.55
                for side, our_raw, ml in [
                    ("over",  f5_ou["over"],  f5_over),
                    ("under", f5_ou["under"], f5_under),
                ]:
                    if our_raw < F5_TOTALS_MIN:
                        continue
                    a = analyze_bet(our_raw, ml, bankroll)
                    edge_floor = max(MIN_EDGE, 6.0)
                    if edge_floor <= a["edge_pct"] <= MAX_EDGE:
                        results.append({
                            "game":      f"{game.away_team.abbreviation} @ {game.home_team.abbreviation}",
                            "game_id":   game_id,
                            "bet_type":  "F5 Total",
                            "side":      f"{side.title()} {f5_total}",
                            "team":      "—",
                            "our_prob":  round(our_raw * 100, 1),
                            "mkt_prob":  round(american_to_implied_prob(ml) * 100, 1),
                            "edge":      round(a["edge_pct"], 1),
                            "ev":        round(a["ev_pct"], 1),
                            "bet_size":  round(min(a["recommended_bet"], bankroll * 0.05, 500), 0),
                            "odds":      f"+{ml}" if ml > 0 else str(ml),
                            "kelly_raw": round(a["recommended_bet"], 2),
                        })
            except Exception:
                pass

    # Sort by edge descending
    results.sort(key=lambda x: x.get("edge", 0), reverse=True)
    return jsonify({"bets": results, "bankroll": bankroll})


def init_db():
    """Create all tables if they don't exist, and run any pending column migrations."""
    with app.app_context():
        db.create_all()

        # ── Column migrations ───────────────────────────────────────────────
        # SQLAlchemy won't add new columns to existing tables automatically.
        # We use raw ALTER TABLE here, wrapped in try/except so it's safe to
        # run every startup — SQLite raises an error if the column already exists.
        migrations = [
            "ALTER TABLE games ADD COLUMN game_number INTEGER DEFAULT 1",
            "ALTER TABLE games ADD COLUMN is_doubleheader BOOLEAN DEFAULT 0",
            "ALTER TABLE games ADD COLUMN game_type VARCHAR(5) DEFAULT 'R'",
            "ALTER TABLE historical_odds ADD COLUMN home_rl_spread REAL",
            "ALTER TABLE games ADD COLUMN wind_deg INTEGER",
            "ALTER TABLE ballparks ADD COLUMN cf_bearing_deg REAL",
            "ALTER TABLE games ADD COLUMN weather_fetched_at DATETIME",
            "ALTER TABLE games ADD COLUMN lineup_confirmed_at DATETIME",
            "ALTER TABLE games ADD COLUMN game_time_utc DATETIME",
            "ALTER TABLE simulation_results ADD COLUMN away_fi_score_pct REAL",
            "ALTER TABLE simulation_results ADD COLUMN home_fi_score_pct REAL",
            "ALTER TABLE simulation_results ADD COLUMN rifi_pct REAL",
            "ALTER TABLE bet_recommendations ADD COLUMN actual_bet_size REAL",
            "ALTER TABLE games ADD COLUMN home_starter_max_pitches INTEGER",
            "ALTER TABLE games ADD COLUMN away_starter_max_pitches INTEGER",
            "ALTER TABLE bullpen_availability ADD COLUMN sort_order INTEGER DEFAULT 0",
            "ALTER TABLE bet_recommendations ADD COLUMN is_manual BOOLEAN DEFAULT 0",
            "ALTER TABLE bet_recommendations ADD COLUMN notes TEXT",
            "ALTER TABLE bet_recommendations ADD COLUMN threshold_odds INTEGER",
            "ALTER TABLE bet_recommendations ADD COLUMN closing_price INTEGER",
            "ALTER TABLE bet_recommendations ADD COLUMN clv_cents REAL",
            "ALTER TABLE games ADD COLUMN closing_odds_locked_at DATETIME",
            # AppSettings table — created by SQLAlchemy on first run, but add the
            # CREATE TABLE IF NOT EXISTS guard here too so existing DBs get it.
            """CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY,
                key VARCHAR(50) UNIQUE NOT NULL,
                value VARCHAR(200),
                updated_at DATETIME
            )""",
        ]
        for sql in migrations:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()   # column already exists — safe to ignore

        # ── Idempotent seed: international/neutral-site venues ─────────────────
        # MLB sometimes plays games outside the home team's stadium (Mexico
        # Series, Tokyo Series, London Series, Field of Dreams, etc.). When that
        # happens, the home team is still "home" but the park factors, altitude,
        # weather, and roof type all need to come from the actual venue.
        #
        # We seed these here (idempotent — only inserted if missing) and the
        # scheduler maps the MLB API's venue_id to the right ballpark_id at
        # import time.
        _intl_venues = [
            {
                "name": "Estadio Alfredo Harp Helú",
                "city": "Mexico City",
                "state": "MX",
                "latitude": 19.4153,
                "longitude": -99.0997,
                # Highest-altitude pro ballpark in the world (above Coors at 5,200 ft).
                # Air density ~22% lower than sea level → balls travel ~10% farther.
                "altitude_feet": 7349.0,
                # Estimates calibrated against early MLB samples there
                # (2023 SD-SF, 2024 HOU-COL, etc. — total runs/HRs were extreme).
                # Slightly above Coors for runs, well above for HR rate.
                "park_factor_runs": 1.30,
                "park_factor_hr":   1.40,
                "park_factor_hits": 1.10,
                "roof_type":        "open",
                "cf_bearing_deg":   30.0,  # rough — home plate faces ~NE
            },
        ]
        for v in _intl_venues:
            existing = Ballpark.query.filter_by(name=v["name"]).first()
            if existing is None:
                bp = Ballpark(**v)
                db.session.add(bp)
                try:
                    db.session.commit()
                    logger.info(f"[init_db] Seeded venue: {v['name']}")
                except Exception as e:
                    db.session.rollback()
                    logger.warning(f"[init_db] Could not seed venue {v['name']}: {e}")

        # ── One-time dedup: remove stale unplaced recommendations ──────────────
        # A previous bug left `won`-graded records with placed=False alive when
        # a game was re-simulated, because the delete only targeted won==None rows.
        # This runs once and cleans up any duplicates already in the DB.
        # For each (game_id, bet_type, side) group of unplaced recs, keep only
        # the single most-recent one; delete the rest.
        try:
            dup_sql = """
                DELETE FROM bet_recommendations
                WHERE placed = 0
                  AND id NOT IN (
                      SELECT MAX(id)
                      FROM bet_recommendations
                      WHERE placed = 0
                      GROUP BY game_id, bet_type, side
                  )
            """
            result = db.session.execute(db.text(dup_sql))
            removed = result.rowcount
            db.session.commit()
            if removed:
                logger.info(f"[init_db] Removed {removed} duplicate unplaced recommendation(s)")
        except Exception as e:
            db.session.rollback()
            logger.warning(f"[init_db] Dedup cleanup skipped: {e}")

        # Seed initial bankroll if empty
        if BankrollLog.query.count() == 0:
            db.session.add(BankrollLog(amount=BANKROLL_START, change=0, note="Starting bankroll"))
            db.session.commit()
            logger.info(f"Initialized bankroll at ${BANKROLL_START}")


# ---------------------------------------------------------------------------
# Startup — runs under gunicorn (Railway) via INIT_ON_IMPORT=1, or under
# `python app.py` via __main__. Importers (backtest/import scripts) don't
# set INIT_ON_IMPORT, so they won't trigger DB init or launch the scheduler.
# ---------------------------------------------------------------------------
def _startup_once():
    """Idempotent startup: DB init + (optionally) scheduler."""
    init_db()
    if os.getenv("RUN_SCHEDULER", "1") == "1":
        # scheduler.py keeps a module-global _scheduler; guard against double-init
        if sched._scheduler is None:
            sched.init_scheduler(app)


if os.getenv("INIT_ON_IMPORT") == "1":
    _startup_once()


if __name__ == "__main__":
    _startup_once()
    app.run(debug=True, host="0.0.0.0", port=5001)
