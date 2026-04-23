"""
Database models for the baseball betting app.
Uses SQLAlchemy with SQLite — no separate database server needed.
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Team(db.Model):
    __tablename__ = "teams"
    id = db.Column(db.Integer, primary_key=True)
    mlb_id = db.Column(db.Integer, unique=True)
    name = db.Column(db.String(100), nullable=False)
    abbreviation = db.Column(db.String(10), nullable=False)
    city = db.Column(db.String(100))
    ballpark_id = db.Column(db.Integer, db.ForeignKey("ballparks.id"))
    division = db.Column(db.String(20))  # e.g. "AL East"
    league = db.Column(db.String(5))     # "AL" or "NL"
    players = db.relationship("Player", backref="team", lazy=True)


class CatcherFraming(db.Model):
    """
    Catcher pitch-framing metrics per player per season.
    Source: Baseball Savant shadow-zone called strike rate.

    Framing is the skill of "stealing" strikes on borderline pitches — the
    catcher's glove position and receiving technique influence the ump's call.
    Separate from the umpire's own tendency.

    shadow_csr: Called strike rate in the shadow zone (just outside the
                strike zone).  League average ~0.466.  Top framers ~0.52.
    csr_diff:   shadow_csr minus league-avg (convenience column).  This is
                the number actually used in the simulation.
    """
    __tablename__ = "catcher_framing"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    season = db.Column(db.Integer, nullable=False)
    shadow_csr = db.Column(db.Float, default=0.0)
    csr_diff = db.Column(db.Float, default=0.0)       # shadow_csr - league_avg
    shadow_pitches = db.Column(db.Integer, default=0)
    runs_saved = db.Column(db.Float, default=0.0)     # Savant's rv_tot
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("player_id", "season", name="uq_catcher_framing_season"),)


class TeamDefense(db.Model):
    """
    Team-level defensive metrics per season.
    Source: Baseball Savant Statcast team-level Outs Above Average (OAA).

    oaa:  Season total OAA (fielding outs vs. expectation).
          Positive = better-than-average defense, suppresses hits on balls-in-play.
          Range typically ±45 per season; league average is 0 by construction.
    """
    __tablename__ = "team_defense"
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    season = db.Column(db.Integer, nullable=False)
    oaa = db.Column(db.Float, default=0.0)               # total OAA for the season
    oaa_rhh = db.Column(db.Float, default=0.0)           # OAA vs. right-handed hitters
    oaa_lhh = db.Column(db.Float, default=0.0)           # OAA vs. left-handed hitters
    success_rate = db.Column(db.Float)                   # actual fielding success rate (0-1)
    games_tracked = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("team_id", "season", name="uq_team_defense_season"),)


class Ballpark(db.Model):
    __tablename__ = "ballparks"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    city = db.Column(db.String(100))
    state = db.Column(db.String(50))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    altitude_feet = db.Column(db.Float)  # Affects ball travel distance
    # Park factors (1.0 = neutral, >1.0 = hitter friendly)
    park_factor_runs = db.Column(db.Float, default=1.0)
    park_factor_hr = db.Column(db.Float, default=1.0)
    park_factor_hits = db.Column(db.Float, default=1.0)
    roof_type = db.Column(db.String(20))  # "open", "retractable", "dome"
    # Compass bearing from home plate to center field (0=N, 90=E, 180=S, 270=W).
    # Used to orient the wind arrow correctly relative to the field layout.
    cf_bearing_deg = db.Column(db.Float)
    teams = db.relationship("Team", backref="ballpark", lazy=True)


class Player(db.Model):
    __tablename__ = "players"
    id = db.Column(db.Integer, primary_key=True)
    mlb_id = db.Column(db.Integer, unique=True)
    fangraphs_id = db.Column(db.String(20))
    name = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))
    position = db.Column(db.String(10))   # "SP", "RP", "C", "1B", etc.
    bats = db.Column(db.String(5))        # "L", "R", "S" (switch)
    throws = db.Column(db.String(5))      # "L", "R"
    active = db.Column(db.Boolean, default=True)
    stats = db.relationship("PlayerStats", backref="player", lazy=True)
    pitching_stats = db.relationship("PitchingStats", backref="player", lazy=True)


class PlayerStats(db.Model):
    """Batting stats with LHP/RHP splits."""
    __tablename__ = "player_stats"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    season = db.Column(db.Integer, nullable=False)
    split = db.Column(db.String(10), nullable=False)  # "vs_LHP", "vs_RHP", "overall"

    # Plate appearances
    pa = db.Column(db.Integer, default=0)
    ab = db.Column(db.Integer, default=0)

    # Results per plate appearance (our "success rate" model)
    single_rate = db.Column(db.Float, default=0.0)
    double_rate = db.Column(db.Float, default=0.0)
    triple_rate = db.Column(db.Float, default=0.0)
    hr_rate = db.Column(db.Float, default=0.0)
    walk_rate = db.Column(db.Float, default=0.0)   # BB + HBP
    strikeout_rate = db.Column(db.Float, default=0.0)
    out_rate = db.Column(db.Float, default=0.0)    # Non-strikeout outs

    # Advanced stats from FanGraphs
    woba = db.Column(db.Float)
    wrc_plus = db.Column(db.Integer)
    babip = db.Column(db.Float)
    avg = db.Column(db.Float)
    obp = db.Column(db.Float)
    slg = db.Column(db.Float)
    ops = db.Column(db.Float)

    # Recent form (last 15 days)
    woba_last_15 = db.Column(db.Float)
    wrc_plus_last_15 = db.Column(db.Integer)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class PitchingStats(db.Model):
    """Pitching stats including arsenal breakdown."""
    __tablename__ = "pitching_stats"
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    season = db.Column(db.Integer, nullable=False)
    role = db.Column(db.String(10), nullable=False)  # "SP" or "RP"
    split = db.Column(db.String(10), nullable=False)  # "vs_LHB", "vs_RHB", "overall"

    # Volume
    ip = db.Column(db.Float, default=0.0)
    games = db.Column(db.Integer, default=0)
    games_started = db.Column(db.Integer, default=0)

    # Outcome rates (per batter faced)
    single_rate_allowed = db.Column(db.Float, default=0.0)
    double_rate_allowed = db.Column(db.Float, default=0.0)
    triple_rate_allowed = db.Column(db.Float, default=0.0)
    hr_rate_allowed = db.Column(db.Float, default=0.0)
    walk_rate_allowed = db.Column(db.Float, default=0.0)
    strikeout_rate = db.Column(db.Float, default=0.0)
    out_rate = db.Column(db.Float, default=0.0)

    # Advanced pitching
    era = db.Column(db.Float)
    fip = db.Column(db.Float)
    xfip = db.Column(db.Float)
    whip = db.Column(db.Float)
    k_per_9 = db.Column(db.Float)
    bb_per_9 = db.Column(db.Float)
    hr_per_9 = db.Column(db.Float)
    gb_rate = db.Column(db.Float)  # Ground ball rate

    # Recent form (last 3 starts for SP, last 7 days for RP)
    era_last_3 = db.Column(db.Float)
    fip_last_3 = db.Column(db.Float)
    ip_last_3 = db.Column(db.Float)

    # Pitch mix (% usage)
    fastball_pct = db.Column(db.Float)
    slider_pct = db.Column(db.Float)
    curveball_pct = db.Column(db.Float)
    changeup_pct = db.Column(db.Float)
    cutter_pct = db.Column(db.Float)
    sinker_pct = db.Column(db.Float)
    splitter_pct = db.Column(db.Float)

    # Pitch effectiveness (run value per 100 pitches)
    fastball_rv = db.Column(db.Float)
    slider_rv = db.Column(db.Float)
    curveball_rv = db.Column(db.Float)
    changeup_rv = db.Column(db.Float)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Umpire(db.Model):
    __tablename__ = "umpires"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mlb_id = db.Column(db.Integer, unique=True)
    # Impact on totals — positive means more runs than average
    runs_per_game_impact = db.Column(db.Float, default=0.0)
    # Strike zone tendencies
    called_strike_rate = db.Column(db.Float)
    walk_rate_impact = db.Column(db.Float, default=0.0)
    k_rate_impact = db.Column(db.Float, default=0.0)
    home_team_favor = db.Column(db.Float, default=0.0)  # slight home team bias
    games_tracked = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Game(db.Model):
    __tablename__ = "games"
    id = db.Column(db.Integer, primary_key=True)
    mlb_game_id = db.Column(db.Integer, unique=True)
    game_date = db.Column(db.Date, nullable=False)
    home_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))
    away_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))
    home_starter_id = db.Column(db.Integer, db.ForeignKey("players.id"))
    away_starter_id = db.Column(db.Integer, db.ForeignKey("players.id"))
    umpire_id = db.Column(db.Integer, db.ForeignKey("umpires.id"))
    ballpark_id = db.Column(db.Integer, db.ForeignKey("ballparks.id"))

    # Weather at game time
    temperature_f = db.Column(db.Float)
    wind_speed_mph = db.Column(db.Float)
    wind_direction = db.Column(db.String(20))  # Compass label, e.g. "NW", "SSE"
    wind_deg = db.Column(db.Integer)            # Raw compass degrees wind comes FROM (0=N, 90=E)
    humidity_pct = db.Column(db.Float)
    pressure_inhg = db.Column(db.Float)  # Barometric pressure

    # Actual results (filled in after game)
    home_score = db.Column(db.Integer)
    away_score = db.Column(db.Integer)
    total_runs = db.Column(db.Integer)
    home_win = db.Column(db.Boolean)

    game_time_utc = db.Column(db.DateTime)  # first-pitch time in UTC from MLB API

    status = db.Column(db.String(20), default="scheduled")  # scheduled/live/final
    odds_api_id = db.Column(db.String(50))  # The Odds API event ID for F5 lookups

    # Staleness tracking — used to warn when data may be outdated
    weather_fetched_at      = db.Column(db.DateTime)   # last time weather was pulled from API
    lineup_confirmed_at     = db.Column(db.DateTime)   # last time a lineup was saved
    closing_odds_locked_at  = db.Column(db.DateTime)   # set when game goes live; CLV is snapshotted at this point

    # Pitch count overrides for starters
    home_starter_max_pitches = db.Column(db.Integer)   # pitch count limit override for home starter
    away_starter_max_pitches = db.Column(db.Integer)   # pitch count limit override for away starter

    # Doubleheader tracking
    game_number     = db.Column(db.Integer, default=1)   # 1 or 2
    is_doubleheader = db.Column(db.Boolean, default=False)

    # Game type: R=Regular Season, S=Spring Training, F=Wild Card, D=Division,
    #            L=Championship Series, W=World Series
    game_type       = db.Column(db.String(5), default="R")

    simulations = db.relationship("SimulationResult", backref="game", lazy=True)
    home_team   = db.relationship("Team", foreign_keys=[home_team_id], lazy="joined")
    away_team   = db.relationship("Team", foreign_keys=[away_team_id], lazy="joined")
    home_starter = db.relationship("Player", foreign_keys=[home_starter_id], lazy="joined")
    away_starter = db.relationship("Player", foreign_keys=[away_starter_id], lazy="joined")
    ballpark    = db.relationship("Ballpark", foreign_keys=[ballpark_id], lazy="joined")


class Lineup(db.Model):
    """Manually entered or scraped lineup for a game."""
    __tablename__ = "lineups"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    batting_order = db.Column(db.Integer)   # 1-9 for starters, 10+ for bullpen
    position = db.Column(db.String(10))
    is_starter = db.Column(db.Boolean, default=True)
    player = db.relationship("Player", lazy="joined")


class SimulationResult(db.Model):
    __tablename__ = "simulation_results"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    num_simulations = db.Column(db.Integer, default=10000)

    # Win probabilities
    home_win_pct = db.Column(db.Float)
    away_win_pct = db.Column(db.Float)

    # Scoring
    home_avg_runs = db.Column(db.Float)
    away_avg_runs = db.Column(db.Float)
    total_avg_runs = db.Column(db.Float)
    total_std_dev = db.Column(db.Float)

    # Over/Under probability at given line
    over_pct = db.Column(db.Float)
    under_pct = db.Column(db.Float)
    simulated_total_line = db.Column(db.Float)

    # Run line (1.5)
    home_cover_runline_pct = db.Column(db.Float)
    away_cover_runline_pct = db.Column(db.Float)

    # First inning scoring probabilities
    away_fi_score_pct = db.Column(db.Float)   # P(away team scores in 1st)
    home_fi_score_pct = db.Column(db.Float)   # P(home team scores in 1st)
    rifi_pct          = db.Column(db.Float)   # P(at least one run in 1st — RIFI)

    # First 5 innings (F5) probabilities
    f5_home_win_pct  = db.Column(db.Float)    # P(home leads after 5)
    f5_away_win_pct  = db.Column(db.Float)    # P(away leads after 5)
    f5_tie_pct       = db.Column(db.Float)    # P(tied after 5 — push on ML)
    f5_home_avg_runs = db.Column(db.Float)    # avg home runs through 5
    f5_away_avg_runs = db.Column(db.Float)    # avg away runs through 5
    f5_over_pct      = db.Column(db.Float)    # P(F5 total > f5_total_line)
    f5_under_pct     = db.Column(db.Float)    # P(F5 total < f5_total_line)
    f5_simulated_total_line = db.Column(db.Float)  # the line used for F5 O/U calc
    f5_home_cover_pct = db.Column(db.Float)   # P(home wins F5 outright, -0.5 RL)
    f5_away_cover_pct = db.Column(db.Float)   # P(away wins F5 outright, -0.5 RL)

    # Raw simulation data (JSON) for histogram display
    score_distribution = db.Column(db.Text)


class Odds(db.Model):
    """Live odds pulled from The Odds API."""
    __tablename__ = "odds"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"))
    bookmaker = db.Column(db.String(50))
    market = db.Column(db.String(30))  # "h2h", "spreads", "totals"
    home_price = db.Column(db.Integer)   # American odds e.g. -150
    away_price = db.Column(db.Integer)
    total_line = db.Column(db.Float)     # e.g. 8.5
    over_price = db.Column(db.Integer)
    under_price = db.Column(db.Integer)
    home_rl_spread = db.Column(db.Float)  # e.g. -1.5 (home) or +1.5 (home underdog)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)


class BetRecommendation(db.Model):
    __tablename__ = "bet_recommendations"
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False)
    simulation_id = db.Column(db.Integer, db.ForeignKey("simulation_results.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    game = db.relationship("Game", lazy="joined")

    bet_type = db.Column(db.String(30))   # "moneyline", "total_over", "runline", etc.
    side = db.Column(db.String(20))       # "home", "away", "over", "under"
    bookmaker = db.Column(db.String(50))
    price = db.Column(db.Integer)         # American odds

    model_probability = db.Column(db.Float)  # Our simulated win %
    implied_probability = db.Column(db.Float) # What the book implies
    edge_pct = db.Column(db.Float)           # Our edge
    ev_pct = db.Column(db.Float)             # Expected value %

    kelly_fraction = db.Column(db.Float)     # Kelly recommended fraction
    recommended_bet = db.Column(db.Float)    # Dollar amount based on bankroll
    bankroll_at_time = db.Column(db.Float)

    # Outcome tracking
    placed = db.Column(db.Boolean, default=False)
    actual_bet_size = db.Column(db.Float)   # amount user actually placed (may differ from recommended)
    won = db.Column(db.Boolean)
    profit_loss = db.Column(db.Float)

    # Threshold odds: the worst American price to accept for min_edge_pct edge.
    # "Or better" means book_price >= threshold_odds (higher integer = more favorable).
    threshold_odds = db.Column(db.Integer)

    # Manual entry support
    is_manual = db.Column(db.Boolean, default=False)   # True = user logged this manually (no model rec)
    notes     = db.Column(db.String(500))              # optional free-text note

    # Closing Line Value (CLV)
    # closing_price: pre-game closing market price, snapshotted when game goes live
    #   (before books switch to in-game odds)
    # clv_cents: how many "cents" better your price was vs the closing line
    #   Positive = you beat the closing line (sharp signal)
    #   Negative = line moved against you
    #   e.g. bet at +120, closed at +105 → clv_cents = +15
    #   e.g. bet at -110, closed at -125 → clv_cents = +15 (you got a better number)
    closing_price = db.Column(db.Integer)
    clv_cents     = db.Column(db.Float)


class AppSettings(db.Model):
    """Simple key-value store for user-configurable app settings."""
    __tablename__ = "app_settings"
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(50), unique=True, nullable=False)
    value      = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class BankrollLog(db.Model):
    __tablename__ = "bankroll_log"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    amount = db.Column(db.Float)
    change = db.Column(db.Float)
    note = db.Column(db.String(200))


class HistoricalOdds(db.Model):
    """
    Closing-line odds imported from the ArnavSaraogi mlb-odds-scraper dataset
    (or any compatible source).  One row per game × bookmaker.
    """
    __tablename__ = "historical_odds"
    id               = db.Column(db.Integer, primary_key=True)
    game_date        = db.Column(db.Date,    nullable=False, index=True)
    home_team_abbr   = db.Column(db.String(5), nullable=False)
    away_team_abbr   = db.Column(db.String(5), nullable=False)
    bookmaker        = db.Column(db.String(50), nullable=False)
    # Moneyline (American odds)
    home_ml          = db.Column(db.Integer)
    away_ml          = db.Column(db.Integer)
    # Run line
    home_rl_odds     = db.Column(db.Integer)
    away_rl_odds     = db.Column(db.Integer)
    home_rl_spread   = db.Column(db.Float)   # e.g. -1.5
    # Totals
    total_line       = db.Column(db.Float)
    over_odds        = db.Column(db.Integer)
    under_odds       = db.Column(db.Integer)

    __table_args__ = (
        db.UniqueConstraint(
            "game_date", "home_team_abbr", "away_team_abbr", "bookmaker",
            name="uq_hist_odds_game_book",
        ),
    )


class BullpenAvailability(db.Model):
    """Tracks which relievers are available for a given game."""
    __tablename__ = "bullpen_availability"
    id            = db.Column(db.Integer, primary_key=True)
    game_id       = db.Column(db.Integer, db.ForeignKey("games.id"), nullable=False)
    player_id     = db.Column(db.Integer, db.ForeignKey("players.id"), nullable=False)
    team_id       = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    available     = db.Column(db.Boolean, default=True)
    max_pitches   = db.Column(db.Integer)          # None = use role default
    sort_order    = db.Column(db.Integer, default=0)  # 0 = most likely to pitch
    pitched_yesterday   = db.Column(db.Boolean, default=False)
    pitches_yesterday   = db.Column(db.Integer)    # how many they threw
    player        = db.relationship("Player", lazy="joined")
    __table_args__ = (db.UniqueConstraint("game_id", "player_id", name="uq_bullpen_avail"),)
