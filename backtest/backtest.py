"""
Backtesting framework.

Runs the simulation engine against historical games and compares our
predicted win probabilities to the actual results and historical odds.

Usage:
  python -m backtest.backtest --start 2024-04-01 --end 2024-10-01

What it does:
  1. Fetches historical schedule from MLB Stats API
  2. For each completed game, runs the simulation using that season's stats
  3. Compares our win probability to the implied probability from the line
  4. Calculates ROI if we had bet every game with positive edge
  5. Saves results to a CSV and prints a summary
"""
import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def american_to_decimal(american_odds: int) -> float:
    if american_odds > 0:
        return (american_odds / 100) + 1
    return (100 / abs(american_odds)) + 1


def american_to_implied(american_odds: int) -> float:
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def get_history_asof(history_model, as_of_date, **filters):
    """Return {player_id: row} — the most recent point-in-time snapshot row
    per player from a *History table (PlayerStatsHistory / PitchingStatsHistory)
    at or before as_of_date, matching the given equality filters (season,
    split, role, etc.).

    This is the fix for the backtest's look-ahead bias: PlayerStats/PitchingStats
    only ever hold the CURRENT stat snapshot (overwritten on every import), so a
    backtest of a March game and a September game both saw whatever the latest
    import happened to be. The History tables accumulate one row per snapshot
    date instead, and this picks the snapshot that would actually have been
    available on the date being simulated.

    Returns an empty dict if no history rows exist at or before as_of_date for
    these filters — callers should fall back to the live table in that case
    (this happens for games before the first snapshot was ever taken).
    """
    # Normalize datetime -> date so comparisons against the DB's Date column work.
    if hasattr(as_of_date, "hour"):
        as_of_date = as_of_date.date()

    rows = history_model.query.filter_by(**filters).filter(
        history_model.as_of_date <= as_of_date
    ).all()

    best = {}
    for r in rows:
        prev = best.get(r.player_id)
        if prev is None or r.as_of_date > prev.as_of_date:
            best[r.player_id] = r
    return best


def woo_rifi_prob(total: float) -> float:
    """Wizard of Odds regression: baseline P(run in first inning) from game total."""
    return min(0.99, max(0.01, 0.2554 + 0.0304 * total))


def implied_to_american(prob: float) -> int:
    """Convert win probability to American moneyline odds (rounded to nearest integer)."""
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        return -round(prob / (1.0 - prob) * 100)
    return round((1.0 - prob) / prob * 100)


def synthetic_rifi_odds(total: float, vig: float = 0.05):
    """
    Derive synthetic RIFI Yes / NRFI market odds using the Wizard of Odds formula + vig.
    Returns (rifi_yes_ml, nrfi_ml) as American odds integers.
    5% total vig (2.5% per side) is typical for MLB first-inning props.
    """
    p_rifi = woo_rifi_prob(total)
    p_nrfi = 1.0 - p_rifi
    rifi_ml = implied_to_american(p_rifi + vig / 2)
    nrfi_ml = implied_to_american(p_nrfi + vig / 2)
    return rifi_ml, nrfi_ml


def simulate_season_backtest(app_context, start_date: str, end_date: str,
                              min_edge: float = 2.0, max_edge: float = 10.0,
                              kelly_fraction: float = 0.25,
                              max_bet_pct: float = 0.05,
                              max_bet_dollars: float = 500.0,
                              flat_bet: Optional[float] = None,
                              daily_max_bets: Optional[int] = None,
                              skip_spring_training: bool = True,
                              starting_bankroll: float = 1000.0,
                              output_csv: str = "backtest/results.csv",
                              backtest_rifi: bool = False,
                              rifi_output_csv: str = "backtest/rifi_results.csv",
                              lineup_cache_file: str = "backtest/lineup_cache.json",
                              progress_callback=None,
                              underdog_mode: bool = False,
                              ml_permissive: bool = False,
                              ml_permissive_calibrated: bool = False,
                              seed: Optional[int] = None,
                              calibration_variant: str = "standard",
                              no_pitcher_splits: bool = False):
    """
    Run a full backtest over a date range.

    For each game in the range:
      - Pull player stats from DB (season stats up to that date if available,
        otherwise full-season stats — conservative approximation)
      - Run 10,000 simulations
      - Compare to closing moneyline from historical odds in DB (if available)
        or use a placeholder ±110 line for EV estimation
      - Track hypothetical profit/loss

    Realistic betting constraints applied:
      - max_bet_dollars: Hard dollar ceiling per bet (e.g. $500 mirrors real book limits)
      - flat_bet: If set, ignores Kelly and bets this fixed amount every time —
                  removes compounding to show true model win rate
      - daily_max_bets: Max bets placed per calendar day (books flag heavy action)
      - skip_spring_training: Skip games with game_type "S" (Spring Training). Uses the
                              MLB API game_type field so late-March regular season openers
                              (e.g. 2025 started March 27) are included correctly.
      - underdog_mode: Research mode — skips v2 calibration for underdogs (positive odds)
                       and uses the raw simulation probability directly. Min edge is also
                       lowered to 2% for underdogs so you can see all bets the model liked.
                       Use with --flat-bet so results aren't distorted by Kelly sizing.
                       Does NOT change live recommendations — backtest only.

    Returns a summary dict and saves a CSV.
    """
    from data.mlb_api import get_historical_schedule, get_game_result, get_first_inning_result
    from database.schema import (Team, Player, PlayerStats, PitchingStats,
                                  PlayerStatsHistory, PitchingStatsHistory,
                                  Game, Lineup, Odds, BankrollLog, HistoricalOdds)
    from models.simulation import (run_simulations, GameInputs, BatterProfile,
                                    PitcherProfile, BullpenProfile, ParkFactors,
                                    WeatherFactors, UmpireFactors,
                                    build_batter_profile, build_pitcher_profile,
                                    build_pitcher_split_rates,
                                    calculate_runline, calculate_over_under)
    from models.kelly import analyze_bet
    from models.calibration import calibrate_prob, calibrate_prob_scoped

    # "Permissive" mode flags:
    #   ml_permissive              — strip ML filters, use RAW sim probs (no calibration)
    #   ml_permissive_calibrated   — strip ML filters, use CALIBRATED probs
    # Either flag activates the filter-stripping behavior; only the raw variant
    # bypasses the calibration layer. Both skip totals.
    permissive_mode = ml_permissive or ml_permissive_calibrated
    use_raw_probs   = ml_permissive  # only the raw variant bypasses calibration

    # Calibration variant — "standard" uses calibrate_prob (production).
    # "scoped-bypass" uses calibrate_prob_scoped with favorites-only scoping.
    # Anything else is rejected early so typos don't silently fall back.
    if calibration_variant not in ("standard", "scoped-bypass"):
        raise ValueError(
            f"Unknown calibration_variant: {calibration_variant!r}. "
            f"Must be 'standard' or 'scoped-bypass'."
        )
    logger.info(f"Calibration variant: {calibration_variant}")

    # Per-game seed derivation. If `seed` is None the sim runs unseeded
    # (pre-existing behavior — every run is a fresh Monte Carlo draw).
    # If `seed` is set, every game gets a deterministic per-game seed
    # derived from (base_seed, date, away_abbr, home_abbr), so two runs
    # with the same `seed` see IDENTICAL sim outputs on identical games.
    # That isolates calibration changes from Monte Carlo noise.
    #
    # IMPORTANT: use hashlib, not Python's built-in hash(), because the
    # built-in is randomly salted per interpreter start (PYTHONHASHSEED),
    # which means the same inputs would produce different seeds between
    # invocations — defeating the entire point of seeding.
    import hashlib
    def _game_seed(base: Optional[int], game_date, away_abbr: str, home_abbr: str):
        if base is None:
            return None
        key = f"{base}|{game_date.isoformat()}|{away_abbr}|{home_abbr}"
        digest = hashlib.md5(key.encode("utf-8")).digest()
        # Take first 4 bytes as an unsigned int in [0, 2**31 - 1].
        return int.from_bytes(digest[:4], "big") % (2**31 - 1)

    if seed is not None:
        logger.info(f"Base seed: {seed} (per-game seeds derived from date+teams)")
    else:
        logger.info("No seed — Monte Carlo draws will differ between runs")

    # Preferred bookmaker order (sharpest first) for odds lookup
    PREFERRED_BOOKS = [
        "pinnacle", "draftkings", "fanduel", "betmgm",
        "caesars", "bet365", "betrivers", "bovada",
    ]

    def _get_hist_odds(gdate, home_abbr, away_abbr):
        """
        Return (home_ml, away_ml, total_line, over_odds, under_odds, home_rl_odds, away_rl_odds, bookmaker)
        from the HistoricalOdds table, choosing the sharpest available book.
        Returns a dict with keys 'home_ml', 'away_ml', etc. or None if not found.
        """
        rows = HistoricalOdds.query.filter_by(
            game_date=gdate,
            home_team_abbr=home_abbr,
            away_team_abbr=away_abbr,
        ).all()

        if not rows:
            return None

        # Pick by preferred book priority
        row_by_book = {r.bookmaker: r for r in rows}
        for book in PREFERRED_BOOKS:
            if book in row_by_book:
                r = row_by_book[book]
                if r.home_ml is not None and r.away_ml is not None:
                    return {
                        "home_ml":        r.home_ml,
                        "away_ml":        r.away_ml,
                        "total_line":     r.total_line,
                        "over_odds":      r.over_odds,
                        "under_odds":     r.under_odds,
                        "home_rl_odds":   r.home_rl_odds,
                        "away_rl_odds":   r.away_rl_odds,
                        "home_rl_spread": r.home_rl_spread,
                        "bookmaker":      book,
                    }

        # Fall back to first row with valid moneyline
        for r in rows:
            if r.home_ml is not None and r.away_ml is not None:
                return {
                    "home_ml":        r.home_ml,
                    "away_ml":        r.away_ml,
                    "total_line":     r.total_line,
                    "over_odds":      r.over_odds,
                    "under_odds":     r.under_odds,
                    "home_rl_odds":   r.home_rl_odds,
                    "away_rl_odds":   r.away_rl_odds,
                    "home_rl_spread": r.home_rl_spread,
                    "bookmaker":      r.bookmaker,
                }
        return None

    # ── Lineup cache (pre-fetched actual lineups / starters) ─────────────────
    lineup_cache: dict = {}
    if os.path.exists(lineup_cache_file):
        with open(lineup_cache_file) as _lf:
            lineup_cache = json.load(_lf)
        logger.info(f"Lineup cache loaded: {len(lineup_cache)} entries from {lineup_cache_file}")
    else:
        logger.info(f"No lineup cache found at {lineup_cache_file} — using rotation averages for all games. "
                    "Run 'python -m backtest.build_lineup_cache' to build it.")

    def _lookup_lineup_cache(date_str: str, home_abbr: str, away_abbr: str):
        """
        Look up a game in the lineup cache. Tries the primary key first, then
        falls back to fuzzy matching on team name suffixes.
        Returns the cache entry dict or None.
        """
        primary = f"{away_abbr}@{home_abbr}_{date_str}"
        if primary in lineup_cache:
            return lineup_cache[primary]
        # Fuzzy: scan all keys for matching date + team fragments
        for key, val in lineup_cache.items():
            if val.get("date") == date_str:
                if val.get("home_abbr") == home_abbr and val.get("away_abbr") == away_abbr:
                    return val
        return None

    logger.info(f"Loading historical schedule: {start_date} to {end_date}")
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    all_results = []
    bankroll = starting_bankroll
    bets_placed = 0
    bets_won = 0

    # First-inning cache — avoids re-fetching linescore from MLB API on repeated runs.
    # Keyed by str(game_pk). Loaded from disk, written back after each day.
    fi_cache_path = "backtest/fi_cache.json"
    fi_cache: dict = {}
    rifi_results = []
    rifi_bankroll = starting_bankroll
    rifi_bets_placed = 0
    rifi_bets_won = 0
    if backtest_rifi:
        if os.path.exists(fi_cache_path):
            with open(fi_cache_path) as _f:
                fi_cache = json.load(_f)

    total_days = (end - start).days + 1
    day_num = 0

    current = start
    while current <= end:
        day_num += 1
        if progress_callback:
            progress_callback(day_num, total_days)

        # Skip entire days that are clearly pre-season (before March 20 of any year).
        # Individual spring training games mixed in with regular-season openers
        # are filtered per-game below using game_type == "S".
        if skip_spring_training and (current.month < 3 or (current.month == 3 and current.day < 20)):
            logger.info(f"[{day_num}/{total_days}] {current.strftime('%Y-%m-%d')} — skipping (pre-season)")
            current += timedelta(days=1)
            continue

        logger.info(f"[{day_num}/{total_days}] {current.strftime('%Y-%m-%d')} ...")
        bets_today = 0  # track daily bet count
        season = current.year  # used by nested helper closures

        date_str = current.strftime("%m/%d/%Y")
        schedule = get_historical_schedule(date_str, date_str)

        for game_info in schedule:
            if game_info.get("status") != "Final":
                continue

            # Skip spring training games regardless of date — game_type "S" = Spring Training.
            # This correctly handles late-March regular season openers (e.g. 2025 season
            # started March 27) while still excluding any ST games in that window.
            if skip_spring_training and game_info.get("game_type") == "S":
                continue

            home_name = game_info.get("home_name", "")
            away_name = game_info.get("away_name", "")
            home_score = game_info.get("home_score")
            away_score = game_info.get("away_score")

            if home_score is None or away_score is None:
                continue

            # Match teams to DB
            home_team = Team.query.filter(
                Team.name.ilike(f"%{home_name.split()[-1]}%")
            ).first()
            away_team = Team.query.filter(
                Team.name.ilike(f"%{away_name.split()[-1]}%")
            ).first()

            if not home_team or not away_team:
                continue

            season = current.year

            # Build league-average profiles as fallback
            def league_avg_batter(name=""):
                return BatterProfile(
                    name=name, bats="R",
                    single_rate=0.149, double_rate=0.046, triple_rate=0.004,
                    hr_rate=0.034, walk_rate=0.083, strikeout_rate=0.225, out_rate=0.459,
                )

            def get_team_lineup(team_id, n=9, opponent_throws="R"):
                """
                Get n best batters by wOBA for a team, using the correct
                handedness split based on the opposing starter's throwing hand.
                Falls back to overall stats if the split has fewer than 5 players.

                Uses point-in-time stats (PlayerStatsHistory) as of `current`,
                the date being simulated, so a March game and a September game
                see the stats that actually existed on those dates. Falls back
                to the live PlayerStats table for dates before the first
                snapshot was ever taken (early-season gap in history coverage).
                """
                split = "vs_LHP" if opponent_throws == "L" else "vs_RHP"
                team_player_ids = {p.id for p in Player.query.filter_by(team_id=team_id).all()}

                def _ranked_asof(split_name):
                    hist = get_history_asof(PlayerStatsHistory, current,
                                             season=season, split=split_name)
                    rows = [r for pid, r in hist.items() if pid in team_player_ids]
                    if rows:
                        rows.sort(key=lambda r: r.woba or 0, reverse=True)
                        return rows[:n]
                    return (
                        PlayerStats.query
                        .join(Player)
                        .filter(Player.team_id == team_id,
                                PlayerStats.season == season,
                                PlayerStats.split == split_name)
                        .order_by(PlayerStats.woba.desc())
                        .limit(n).all()
                    )

                stats = _ranked_asof(split)

                # Fall back to overall if split doesn't have enough players
                if len(stats) < 5:
                    stats = _ranked_asof("overall")

                batters = []
                for s in stats:
                    batters.append(build_batter_profile(
                        name=s.player.name, bats=s.player.bats or "R",
                        pa=s.pa or 0,
                        single_rate=s.single_rate or 0.150,
                        double_rate=s.double_rate or 0.047,
                        triple_rate=s.triple_rate or 0.005,
                        hr_rate=s.hr_rate or 0.030,
                        walk_rate=s.walk_rate or 0.084,
                        strikeout_rate=s.strikeout_rate or 0.226,
                        out_rate=s.out_rate or 0.458,
                    ))
                while len(batters) < 9:
                    batters.append(league_avg_batter())
                return batters[:9]

            def get_team_starter(team_id):
                """
                Build a weighted-average starter profile across the full rotation.

                WHY NOT RANDOM SAMPLING:
                Random sampling caused the 60-70% calibration problem. When the
                sampler happened to draw the ace for the away team, their win
                probability jumped to 65%+. But the actual game used a different
                starter — so we were betting on a ghost. Backtesting showed those
                bets won at only 47% despite 60-70% model confidence.

                WHY WEIGHTED AVERAGE:
                Instead of picking one starter, we compute an innings-pitched
                weighted average across the full rotation. This produces a stable,
                consistent "expected quality of this team's starters" — which is
                what we actually care about when we don't know the day's starter.
                The same team always gets the same profile, no phantom edge from
                random draws.
                """
                team_player_ids_rot = {p.id for p in Player.query.filter_by(team_id=team_id).all()}
                hist_rot = get_history_asof(PitchingStatsHistory, current,
                                             season=season, role="SP", split="overall")
                rotation = [r for pid, r in hist_rot.items()
                            if pid in team_player_ids_rot and (r.games_started or 0) > 0]
                if rotation:
                    rotation.sort(key=lambda r: r.games_started or 0, reverse=True)
                    rotation = rotation[:6]
                else:
                    rotation = (
                        PitchingStats.query
                        .join(Player)
                        .filter(Player.team_id == team_id,
                                PitchingStats.season == season,
                                PitchingStats.role == "SP",
                                PitchingStats.split == "overall",
                                PitchingStats.games_started > 0)
                        .order_by(PitchingStats.games_started.desc())
                        .limit(6)  # top 6 by starts covers the realistic rotation depth
                        .all()
                    )
                if not rotation:
                    return PitcherProfile(
                        name="TBD", throws="R",
                        single_rate_allowed=0.150, double_rate_allowed=0.047,
                        triple_rate_allowed=0.005, hr_rate_allowed=0.030,
                        walk_rate_allowed=0.084, strikeout_rate=0.226, out_rate=0.458,
                        stamina=6.0,
                    )

                # Weight each starter's rates by IP — more innings = more
                # representative of their true contribution to the rotation
                total_ip = sum(s.ip or 0 for s in rotation) or 1.0
                def wavg(field):
                    return sum((getattr(s, field) or 0) * (s.ip or 0)
                               for s in rotation) / total_ip

                # Weighted average stamina (IP per start)
                total_starts = sum(s.games_started or 0 for s in rotation) or 1
                avg_stamina = total_ip / total_starts

                # Most rotations are majority right-handed; use R unless majority L
                lhp_ip = sum((s.ip or 0) for s in rotation
                             if s.player.throws == "L")
                avg_throws = "L" if lhp_ip > total_ip / 2 else "R"

                profile = build_pitcher_profile(
                    name="Rotation Avg", throws=avg_throws,
                    ip=total_ip,          # full rotation IP for regression purposes
                    single_rate_allowed=wavg("single_rate_allowed"),
                    double_rate_allowed=wavg("double_rate_allowed"),
                    triple_rate_allowed=wavg("triple_rate_allowed"),
                    hr_rate_allowed=wavg("hr_rate_allowed"),
                    walk_rate_allowed=wavg("walk_rate_allowed"),
                    strikeout_rate=wavg("strikeout_rate"),
                    out_rate=wavg("out_rate"),
                    stamina=avg_stamina,
                )
                # Attach platoon splits — IP-weighted across the rotation,
                # gated by --no-pitcher-splits flag for A/B testing.
                if not no_pitcher_splits:
                    rotation_player_ids = [s.player_id for s in rotation]
                    rotation_splits = {}
                    for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
                        hist_split = get_history_asof(PitchingStatsHistory, current,
                                                      season=season, role="SP", split=db_split)
                        split_rows = [r for pid, r in hist_split.items() if pid in rotation_player_ids]
                        if not split_rows:
                            split_rows = (PitchingStats.query
                                          .filter(PitchingStats.player_id.in_(rotation_player_ids),
                                                  PitchingStats.season == season,
                                                  PitchingStats.role == "SP",
                                                  PitchingStats.split == db_split)
                                          .all())
                        if not split_rows:
                            continue
                        split_tip = sum(r.ip or 0 for r in split_rows)
                        if split_tip < 50.0:  # ~200 BF — too noisy below this
                            continue
                        def s_wavg(f, rows=split_rows, tip=split_tip):
                            return sum((getattr(r, f) or 0) * (r.ip or 0) for r in rows) / tip
                        rotation_splits[bat_hand] = build_pitcher_split_rates({
                            "single_rate_allowed": s_wavg("single_rate_allowed"),
                            "double_rate_allowed": s_wavg("double_rate_allowed"),
                            "triple_rate_allowed": s_wavg("triple_rate_allowed"),
                            "hr_rate_allowed":     s_wavg("hr_rate_allowed"),
                            "walk_rate_allowed":   s_wavg("walk_rate_allowed"),
                            "strikeout_rate":      s_wavg("strikeout_rate"),
                            "out_rate":            s_wavg("out_rate"),
                        }, split_tip)
                    if rotation_splits:
                        profile.splits = rotation_splits
                return profile

            def get_team_bullpen(team_id):
                team_player_ids_rp = {p.id for p in Player.query.filter_by(team_id=team_id).all()}
                hist_rp = get_history_asof(PitchingStatsHistory, current,
                                            season=season, role="RP", split="overall")
                relievers = [r for pid, r in hist_rp.items() if pid in team_player_ids_rp]
                if not relievers:
                    relievers = (
                        PitchingStats.query.join(Player)
                        .filter(Player.team_id == team_id,
                                PitchingStats.season == season,
                                PitchingStats.role == "RP",
                                PitchingStats.split == "overall")
                        .all()
                    )
                if not relievers:
                    return BullpenProfile(
                        single_rate_allowed=0.155, double_rate_allowed=0.048,
                        triple_rate_allowed=0.004, hr_rate_allowed=0.038,
                        walk_rate_allowed=0.090, strikeout_rate=0.220, out_rate=0.445,
                    )
                # Weight each reliever's rates by their IP so high-usage
                # relievers contribute more to the bullpen profile than
                # guys who only threw 2 innings all season.
                total_ip = sum(r.ip or 0 for r in relievers) or 1.0
                def wavg(f):
                    return sum((getattr(r, f) or 0) * (r.ip or 0)
                               for r in relievers) / total_ip

                # Build a regressed aggregate bullpen profile using combined IP
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

            # Park factors
            park = ParkFactors()
            if home_team.ballpark:
                park = ParkFactors(
                    runs=home_team.ballpark.park_factor_runs or 1.0,
                    hr=home_team.ballpark.park_factor_hr or 1.0,
                    hits=home_team.ballpark.park_factor_hits or 1.0,
                )

            # ── Resolve starters and lineups ─────────────────────────────────
            # If the lineup cache is available, use the actual starters and
            # batting orders from that game's boxscore.
            # Falls back to rotation average / top-9-by-wOBA if not cached.

            cached = _lookup_lineup_cache(
                current.strftime("%Y-%m-%d"),
                home_team.abbreviation,
                away_team.abbreviation,
            )

            def _starter_from_cache(mlb_id, fallback_team_id):
                """Look up a starting pitcher by MLB ID; fall back to rotation avg."""
                if not mlb_id:
                    return get_team_starter(fallback_team_id)
                player = Player.query.filter_by(mlb_id=mlb_id).first()
                if not player:
                    return get_team_starter(fallback_team_id)
                hist_starter = get_history_asof(PitchingStatsHistory, current,
                                                 player_id=player.id, season=season,
                                                 role="SP", split="overall")
                stats = hist_starter.get(player.id)
                if not stats:
                    stats = (
                        PitchingStats.query
                        .filter_by(player_id=player.id, season=season, role="SP", split="overall")
                        .first()
                    )
                if not stats:
                    # Try prior season
                    stats = (
                        PitchingStats.query
                        .filter_by(player_id=player.id, season=season - 1, role="SP", split="overall")
                        .first()
                    )
                if not stats:
                    return get_team_starter(fallback_team_id)
                profile = build_pitcher_profile(
                    name=player.name,
                    throws=player.throws or "R",
                    ip=stats.ip or 1.0,
                    single_rate_allowed=stats.single_rate_allowed or 0.150,
                    double_rate_allowed=stats.double_rate_allowed or 0.047,
                    triple_rate_allowed=stats.triple_rate_allowed or 0.005,
                    hr_rate_allowed=stats.hr_rate_allowed or 0.030,
                    walk_rate_allowed=stats.walk_rate_allowed or 0.084,
                    strikeout_rate=stats.strikeout_rate or 0.226,
                    out_rate=stats.out_rate or 0.458,
                    stamina=stats.ip / max(stats.games_started, 1) if stats.games_started else 6.0,
                )
                # Attach platoon splits for this specific pitcher,
                # gated by --no-pitcher-splits flag for A/B testing.
                if not no_pitcher_splits:
                    splits = {}
                    for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
                        hist_sp_split = get_history_asof(PitchingStatsHistory, current,
                                                          player_id=player.id, season=stats.season,
                                                          role="SP", split=db_split)
                        split_row = hist_sp_split.get(player.id)
                        if not split_row:
                            split_row = (PitchingStats.query
                                         .filter_by(player_id=player.id,
                                                    season=stats.season,
                                                    role="SP",
                                                    split=db_split)
                                         .first())
                        if not split_row or (split_row.ip or 0) < 50.0:
                            continue
                        splits[bat_hand] = build_pitcher_split_rates({
                            "single_rate_allowed": split_row.single_rate_allowed or 0.150,
                            "double_rate_allowed": split_row.double_rate_allowed or 0.047,
                            "triple_rate_allowed": split_row.triple_rate_allowed or 0.005,
                            "hr_rate_allowed":     split_row.hr_rate_allowed or 0.030,
                            "walk_rate_allowed":   split_row.walk_rate_allowed or 0.084,
                            "strikeout_rate":      split_row.strikeout_rate or 0.226,
                            "out_rate":            split_row.out_rate or 0.458,
                        }, split_row.ip or 0)
                    if splits:
                        profile.splits = splits
                return profile

            def _lineup_from_cache(batter_entries, opponent_throws, fallback_team_id):
                """
                Build a 9-batter lineup from cache entries.
                Each entry is {"mlb_id": int, "name": str, "order": int}.
                Unmatched slots are filled with league average.
                Falls back to top-9-by-wOBA if entries are empty.
                """
                if not batter_entries:
                    return get_team_lineup(fallback_team_id, opponent_throws=opponent_throws)

                split = "vs_LHP" if opponent_throws == "L" else "vs_RHP"
                ordered = sorted(batter_entries, key=lambda x: x["order"])
                lineup = []
                for entry in ordered[:9]:
                    player = Player.query.filter_by(mlb_id=entry["mlb_id"]).first()
                    stats = None
                    if player:
                        hist_lineup = get_history_asof(PlayerStatsHistory, current,
                                                        player_id=player.id, season=season, split=split)
                        stats = hist_lineup.get(player.id)
                        if not stats:
                            stats = (PlayerStats.query
                                     .filter_by(player_id=player.id, season=season, split=split)
                                     .first())
                        if not stats:
                            hist_lineup_overall = get_history_asof(PlayerStatsHistory, current,
                                                                    player_id=player.id, season=season,
                                                                    split="overall")
                            stats = hist_lineup_overall.get(player.id)
                        if not stats:
                            stats = (PlayerStats.query
                                     .filter_by(player_id=player.id, season=season, split="overall")
                                     .first())
                        if not stats:
                            stats = (PlayerStats.query
                                     .filter_by(player_id=player.id, season=season - 1, split="overall")
                                     .first())
                    if stats:
                        lineup.append(build_batter_profile(
                            name=entry["name"], bats=player.bats or "R",
                            pa=stats.pa or 0,
                            single_rate=stats.single_rate or 0.150,
                            double_rate=stats.double_rate or 0.047,
                            triple_rate=stats.triple_rate or 0.005,
                            hr_rate=stats.hr_rate or 0.030,
                            walk_rate=stats.walk_rate or 0.084,
                            strikeout_rate=stats.strikeout_rate or 0.226,
                            out_rate=stats.out_rate or 0.458,
                        ))
                    else:
                        lineup.append(league_avg_batter(entry["name"]))

                # Pad to 9 if needed
                while len(lineup) < 9:
                    lineup.append(league_avg_batter())
                return lineup[:9]

            if cached:
                home_starter_profile = _starter_from_cache(
                    cached["home"]["sp_id"], home_team.id)
                away_starter_profile = _starter_from_cache(
                    cached["away"]["sp_id"], away_team.id)
                home_lineup = _lineup_from_cache(
                    cached["home"]["batters"], away_starter_profile.throws, home_team.id)
                away_lineup = _lineup_from_cache(
                    cached["away"]["batters"], home_starter_profile.throws, away_team.id)
            else:
                home_starter_profile = get_team_starter(home_team.id)
                away_starter_profile = get_team_starter(away_team.id)
                home_lineup = get_team_lineup(home_team.id, opponent_throws=away_starter_profile.throws)
                away_lineup = get_team_lineup(away_team.id, opponent_throws=home_starter_profile.throws)

            inputs = GameInputs(
                home_lineup=home_lineup,
                away_lineup=away_lineup,
                home_starter=home_starter_profile,
                away_starter=away_starter_profile,
                home_bullpen=get_team_bullpen(home_team.id),
                away_bullpen=get_team_bullpen(away_team.id),
                park=park,
            )

            gseed = _game_seed(seed, current.date(),
                               away_team.abbreviation, home_team.abbreviation)
            sim = run_simulations(inputs, n=10000, seed=gseed)
            home_win_prob = sim["home_win_pct"]
            away_win_prob = sim["away_win_pct"]
            total_avg_runs = sim.get("total_avg_runs", 9.0)
            actual_home_win = home_score > away_score

            # Try to get real historical closing line from DB first
            hist = _get_hist_odds(
                current.date(),
                home_team.abbreviation,
                away_team.abbreviation,
            )

            if not hist:
                # No real closing line available — skip this game entirely
                # so all backtest results are against real market prices
                continue

            home_ml       = hist["home_ml"]
            away_ml       = hist["away_ml"]
            odds_src      = hist["bookmaker"]
            real_total    = hist.get("total_line")
            real_over_ml  = hist.get("over_odds",  -110)
            real_under_ml = hist.get("under_odds", -110)
            real_home_rl  = hist.get("home_rl_odds")
            real_away_rl  = hist.get("away_rl_odds")

            # ── Moneyline bets — ML-permissive calibrated filters ─────────────
            # Derived from backtest/ml_permissive_cal (2024+2025 combined).
            # Rules (matches app.py live filter):
            #   1. Calibrated probabilities on both sides.
            #   2. Edge floor: 8%.
            #   3. Cut market-implied prob >= 65% (heavy favorites lost -2.46% ROI).

            ML_MAX_IMPLIED = 0.65

            def _get_prob(raw: float, bet_type: str, side_key: str, odds: int) -> float:
                # Route through the chosen calibration variant. "standard" is
                # the production path (no-op forward to calibrate_prob).
                # "scoped-bypass" applies the favorites-only away bypass.
                if calibration_variant == "scoped-bypass":
                    return calibrate_prob_scoped(raw, bet_type, side_key, odds)
                return calibrate_prob(raw, bet_type, side_key)

            def _ml_min_edge(raw_prob: float, odds: int) -> float:
                return max(min_edge, 8.0)

            if use_raw_probs:
                # Use raw simulation probabilities — no calibration routing,
                # no UDG sweet-spot short-circuit. "Edge" here is the pure
                # sim-vs-market edge before any adjustments.
                cal_home_prob = home_win_prob
                cal_away_prob = away_win_prob
            else:
                cal_home_prob = _get_prob(home_win_prob, "moneyline", "home", home_ml)
                cal_away_prob = _get_prob(away_win_prob, "moneyline", "away", away_ml)
            home_analysis = analyze_bet(cal_home_prob, home_ml, bankroll, kelly_fraction)
            away_analysis = analyze_bet(cal_away_prob, away_ml, bankroll, kelly_fraction)

            for side, analysis, won, ml, raw_prob in [
                ("home", home_analysis, actual_home_win,     home_ml, home_win_prob),
                ("away", away_analysis, not actual_home_win, away_ml, away_win_prob),
            ]:
                # ML_PERMISSIVE mode: strip ALL filter gates. Keeps ONLY the
                # min_edge floor. Use this to see what pure "edge ≥ X%"
                # looks like with no other safety rails.
                if not permissive_mode:
                    # New unified filter: cut market-implied prob >= 65%.
                    # Heavy favorites returned -2.46% ROI across 105 bets in the
                    # calibrated backtest — no odds range filter needed.
                    if american_to_implied(ml) >= ML_MAX_IMPLIED:
                        continue

                    # Cap at max_edge: very high claimed edge usually means model error
                    if analysis["edge_pct"] > max_edge:
                        continue

                effective_min = min_edge if permissive_mode else _ml_min_edge(raw_prob, ml)
                if analysis["edge_pct"] >= effective_min:
                    # Daily bet cap — skip if we've already hit today's limit
                    if daily_max_bets is not None and bets_today >= daily_max_bets:
                        continue

                    if flat_bet is not None:
                        # Flat bet mode: ignore Kelly, bet the same fixed amount every time
                        bet_size = flat_bet
                    else:
                        # Kelly with two hard caps:
                        #   1. % of current bankroll  2. absolute dollar ceiling
                        bet_size = min(
                            analysis["recommended_bet"],
                            bankroll * max_bet_pct,   # e.g. 5% of bankroll
                            max_bet_dollars,          # e.g. $500 hard ceiling (book limits)
                        )
                    if bet_size <= 0:
                        continue

                    dec_odds = american_to_decimal(ml)
                    profit = bet_size * (dec_odds - 1) if won else -bet_size
                    bankroll += profit
                    bets_placed += 1
                    bets_today += 1
                    if won:
                        bets_won += 1

                    all_results.append({
                        "date":         current.strftime("%Y-%m-%d"),
                        "matchup":      f"{away_name} @ {home_name}",
                        "bet_type":     "moneyline",
                        "side":         side,
                        "our_prob":     round(raw_prob, 4),
                        "implied_prob": round(american_to_implied(ml), 4),
                        "edge_pct":     analysis["edge_pct"],
                        "ev_pct":       analysis["ev_pct"],
                        "bet_size":     bet_size,
                        "odds":         ml,
                        "is_underdog":  ml > 0,
                        "is_favorite":  ml < 0,
                        "odds_source":  odds_src,
                        "won":          won,
                        "profit":       round(profit, 2),
                        "bankroll":     round(bankroll, 2),
                    })

            # ── Totals bets (only when we have a real line) ────────────────
            # Both overs and unders at 9% EV floor.
            # Framing-enabled 2024+2025 backtest threshold sweep:
            #   EV ≥ 6%: 430 bets, 55.8% WR,  +7.7% ROI
            #   EV ≥ 8%: 221 bets, 57.0% WR,  +8.6% ROI
            #   EV ≥ 9%: 104 bets, 64.4% WR, +22.7% ROI  ← sweet spot
            OVER_MIN_EDGE  = max(min_edge, 9.0)
            UNDER_MIN_EDGE = max(min_edge, 9.0)
            # permissive modes focus on moneyline only — skip totals
            if real_total is not None and not permissive_mode:
                ou = calculate_over_under(sim, real_total)
                over_prob  = ou["over"]
                under_prob = ou["under"]

                # Validated totals probability range from v4 2025 backtest (193 bets):
                #   54-58% raw: 0-48% WR, negative ROI  — skip
                #   58-64% raw: 54-63% WR, +6% to +24% ROI — sweet spot (139 bets)
                #   64-66% raw: 50% WR, -11% ROI          — skip
                #   66-68% raw: 67% WR, +16% ROI          — too small a sample (9 bets)
                # Floor at 0.58 (where model becomes predictive).
                # NOTE: 0.70 floor was based on v2 calibrated probs (inflated).
                #       With v4 identity calibration, raw probs never reach 0.70.
                TOTALS_MIN_RAW = 0.58
                if over_prob < TOTALS_MIN_RAW and under_prob < TOTALS_MIN_RAW:
                    continue

                cal_over_prob  = calibrate_prob(over_prob,  "totals", "over")
                cal_under_prob = calibrate_prob(under_prob, "totals", "under")
                over_analysis  = analyze_bet(cal_over_prob,  real_over_ml,  bankroll, kelly_fraction)
                under_analysis = analyze_bet(cal_under_prob, real_under_ml, bankroll, kelly_fraction)

                actual_total = home_score + away_score
                is_push      = (actual_total == real_total)
                actual_over  = (actual_total > real_total)
                actual_under = (actual_total < real_total)

                for side, analysis, won, ml, min_e in [
                    ("over",  over_analysis,  actual_over,  real_over_ml,  OVER_MIN_EDGE),
                    ("under", under_analysis, actual_under, real_under_ml, UNDER_MIN_EDGE),
                ]:
                    if analysis["edge_pct"] > max_edge:
                        continue
                    if analysis["edge_pct"] >= min_e:
                        if daily_max_bets is not None and bets_today >= daily_max_bets:
                            continue

                        if flat_bet is not None:
                            bet_size = flat_bet
                        else:
                            bet_size = min(
                                analysis["recommended_bet"],
                                bankroll * max_bet_pct,
                                max_bet_dollars,
                            )
                        if bet_size <= 0:
                            continue

                        if is_push:
                            # Push: money returned, bankroll unchanged
                            profit = 0.0
                        else:
                            dec_odds = american_to_decimal(ml)
                            profit = bet_size * (dec_odds - 1) if won else -bet_size
                            bankroll += profit

                        bets_placed += 1
                        bets_today += 1
                        if won:
                            bets_won += 1

                        all_results.append({
                            "date":         current.strftime("%Y-%m-%d"),
                            "matchup":      f"{away_name} @ {home_name}",
                            "bet_type":     "totals",
                            "side":         side,
                            "our_prob":     round(over_prob if side == "over" else under_prob, 4),
                            "implied_prob": round(american_to_implied(ml), 4),
                            "edge_pct":     analysis["edge_pct"],
                            "ev_pct":       analysis["ev_pct"],
                            "bet_size":     bet_size,
                            "odds":         ml,
                            "is_underdog":  False,
                            "is_favorite":  False,
                            "odds_source":  odds_src,
                            "won":          won if not is_push else None,  # None = push
                            "profit":       round(profit, 2),
                            "bankroll":     round(bankroll, 2),
                        })

            # ── Run line bets — DISABLED ────────────────────────────────────
            # Backtest: home RL -8.8% ROI across 77 bets. Model overestimates RL edge.
            if False and real_home_rl is not None and real_away_rl is not None:
                rl = calculate_runline(sim, line=1.5)
                margin = home_score - away_score

                # Determine which team is -1.5 from the stored spread.
                # home_rl_spread = -1.5 means home is favored (laying 1.5 runs).
                # home_rl_spread = +1.5 means home is the underdog (getting 1.5 runs).
                home_rl_spread = hist.get("home_rl_spread") or -1.5

                if home_rl_spread < 0:
                    # Home is -1.5: must win by 2+ to cover
                    home_rl_prob = rl["home_minus_cover"]           # P(home wins by 2+)
                    away_rl_prob = round(1.0 - home_rl_prob, 4)    # P(away +1.5 covers)
                    home_rl_won = margin >= 2                        # home wins by 2+
                    away_rl_won = margin < 2                         # away keeps it within 1 or wins
                else:
                    # Home is +1.5: away must win by 2+ to cover
                    away_rl_prob = rl["away_minus_cover"]           # P(away wins by 2+)
                    home_rl_prob = round(1.0 - away_rl_prob, 4)    # P(home +1.5 covers)
                    away_rl_won = margin <= -2                       # away wins by 2+
                    home_rl_won = margin > -2                        # home keeps it within 1 or wins

                home_rl_analysis = analyze_bet(home_rl_prob, real_home_rl, bankroll, kelly_fraction)
                away_rl_analysis = analyze_bet(away_rl_prob, real_away_rl, bankroll, kelly_fraction)

                # HOME run line only — away run line is -4.2% ROI even at 10%+ edge.
                # Away teams consistently under-perform model expectations on the road.
                # Floor at 6%: backtest shows 5-6% edge band loses -31.2% ROI.
                RL_HOME_MIN_EDGE = max(min_edge, 6.0)

                for side, analysis, won, ml in [
                    ("home", home_rl_analysis, home_rl_won, real_home_rl),
                ]:
                    if analysis["edge_pct"] > max_edge:
                        continue
                    if analysis["edge_pct"] >= RL_HOME_MIN_EDGE:
                        if daily_max_bets is not None and bets_today >= daily_max_bets:
                            continue

                        if flat_bet is not None:
                            bet_size = flat_bet
                        else:
                            bet_size = min(
                                analysis["recommended_bet"],
                                bankroll * max_bet_pct,
                                max_bet_dollars,
                            )
                        if bet_size <= 0:
                            continue
                        dec_odds = american_to_decimal(ml)
                        profit = bet_size * (dec_odds - 1) if won else -bet_size
                        bankroll += profit
                        bets_placed += 1
                        bets_today += 1
                        if won:
                            bets_won += 1

                        all_results.append({
                            "date":         current.strftime("%Y-%m-%d"),
                            "matchup":      f"{away_name} @ {home_name}",
                            "bet_type":     "runline",
                            "side":         side,
                            "our_prob":     round(home_rl_prob if side == "home" else away_rl_prob, 4),
                            "implied_prob": round(american_to_implied(ml), 4),
                            "edge_pct":     analysis["edge_pct"],
                            "ev_pct":       analysis["ev_pct"],
                            "bet_size":     bet_size,
                            "odds":         ml,
                            "odds_source":  odds_src,
                            "won":          won,
                            "profit":       round(profit, 2),
                            "bankroll":     round(bankroll, 2),
                        })

        # ── RIFI / NRFI bets ──────────────────────────────────────────────
        # Uses Wizard of Odds formula as synthetic market price + 5% vig.
        # Outcome fetched from MLB linescore API and cached to fi_cache.json.
        if backtest_rifi:
            for game_info in schedule:
                if game_info.get("status") != "Final":
                    continue
                game_pk   = game_info.get("game_id")
                home_name = game_info.get("home_name", "")
                away_name = game_info.get("away_name", "")
                if not game_pk:
                    continue

                home_team = Team.query.filter(Team.name.ilike(f"%{home_name.split()[-1]}%")).first()
                away_team = Team.query.filter(Team.name.ilike(f"%{away_name.split()[-1]}%")).first()
                if not home_team or not away_team:
                    continue

                hist = _get_hist_odds(current.date(), home_team.abbreviation, away_team.abbreviation)
                if not hist or not hist.get("total_line"):
                    continue
                real_total = hist["total_line"]

                # Fetch/cache first-inning outcome
                cache_key = str(game_pk)
                if cache_key not in fi_cache:
                    fi_data = get_first_inning_result(game_pk)
                    if fi_data is None:
                        continue
                    fi_cache[cache_key] = fi_data
                fi_data = fi_cache[cache_key]
                actual_rifi = fi_data["rifi"]

                # Run simulation to get model's rifi_pct
                home_sp = get_team_starter(home_team.id)
                away_sp = get_team_starter(away_team.id)
                fi_inputs = GameInputs(
                    home_lineup=get_team_lineup(home_team.id, opponent_throws=away_sp.throws),
                    away_lineup=get_team_lineup(away_team.id, opponent_throws=home_sp.throws),
                    home_starter=home_sp, away_starter=away_sp,
                    home_bullpen=get_team_bullpen(home_team.id),
                    away_bullpen=get_team_bullpen(away_team.id),
                )
                # Use a distinct seed for the first-inning sim so it doesn't
                # produce the same output as the full-game sim on the same game.
                fi_seed = None if gseed is None else (gseed ^ 0x5A5A5A5A) % (2**31 - 1)
                fi_sim = run_simulations(fi_inputs, n=10000, seed=fi_seed)
                model_rifi_pct = fi_sim.get("rifi_pct", 0.5)
                model_nrfi_pct = round(1.0 - model_rifi_pct, 4)

                # Synthetic market odds: WoO formula + 5% vig
                rifi_ml, nrfi_ml = synthetic_rifi_odds(real_total)

                for side, our_prob, ml, won in [
                    ("rifi", model_rifi_pct, rifi_ml, actual_rifi),
                    ("nrfi", model_nrfi_pct, nrfi_ml, not actual_rifi),
                ]:
                    analysis = analyze_bet(our_prob, ml, rifi_bankroll, kelly_fraction)
                    if analysis["edge_pct"] > max_edge or analysis["edge_pct"] < min_edge:
                        continue

                    if flat_bet is not None:
                        bet_size = flat_bet
                    else:
                        bet_size = min(analysis["recommended_bet"],
                                       rifi_bankroll * max_bet_pct, max_bet_dollars)
                    if bet_size <= 0:
                        continue

                    dec_odds = american_to_decimal(ml)
                    profit = bet_size * (dec_odds - 1) if won else -bet_size
                    rifi_bankroll += profit
                    rifi_bets_placed += 1
                    if won:
                        rifi_bets_won += 1

                    rifi_results.append({
                        "date":             current.strftime("%Y-%m-%d"),
                        "matchup":          f"{away_name} @ {home_name}",
                        "total_line":       real_total,
                        "woo_rifi_prob":    round(woo_rifi_prob(real_total), 4),
                        "model_rifi_pct":   round(model_rifi_pct, 4),
                        "side":             side,
                        "our_prob":         round(our_prob, 4),
                        "implied_prob":     round(american_to_implied(ml), 4),
                        "edge_pct":         analysis["edge_pct"],
                        "ev_pct":           analysis["ev_pct"],
                        "bet_size":         bet_size,
                        "odds":             ml,
                        "won":              won,
                        "profit":           round(profit, 2),
                        "bankroll":         round(rifi_bankroll, 2),
                    })

            # Flush cache to disk after each day so reruns are fast
            with open(fi_cache_path, "w") as _f:
                json.dump(fi_cache, _f)

        current += timedelta(days=1)

    # Save results
    df = pd.DataFrame(all_results)
    os.makedirs("backtest", exist_ok=True)
    df.to_csv(output_csv, index=False)

    # Save RIFI results (separate bankroll / CSV)
    if backtest_rifi and rifi_results:
        rifi_df = pd.DataFrame(rifi_results)
        rifi_df.to_csv(rifi_output_csv, index=False)
        rifi_wagered = rifi_df["bet_size"].sum()
        rifi_profit  = rifi_df["profit"].sum()
        rifi_roi     = (rifi_profit / rifi_wagered * 100) if rifi_wagered > 0 else 0
        rifi_win_rate = (rifi_bets_won / rifi_bets_placed * 100) if rifi_bets_placed > 0 else 0
        logger.info("=" * 50)
        logger.info("RIFI BACKTEST SUMMARY")
        logger.info("=" * 50)
        logger.info(f"  bets_placed:    {rifi_bets_placed}")
        logger.info(f"  bets_won:       {rifi_bets_won}")
        logger.info(f"  win_rate:       {round(rifi_win_rate, 1)}%")
        logger.info(f"  total_wagered:  ${round(rifi_wagered, 2)}")
        logger.info(f"  total_profit:   ${round(rifi_profit, 2)}")
        logger.info(f"  roi:            {round(rifi_roi, 2)}%")
        logger.info(f"  final_bankroll: ${round(rifi_bankroll, 2)}")
        logger.info(f"  output_csv:     {rifi_output_csv}")
        logger.info("=" * 50)
    else:
        rifi_wagered = rifi_profit = rifi_roi = rifi_win_rate = 0

    # Summary
    total_wagered = df["bet_size"].sum() if not df.empty else 0
    total_profit  = df["profit"].sum()   if not df.empty else 0
    roi      = (total_profit / total_wagered * 100) if total_wagered > 0 else 0
    # Pushes (won=None) are excluded from win rate — only count decided bets
    graded_bets = int(df["won"].notna().sum()) if not df.empty else bets_placed
    win_rate = (bets_won / graded_bets * 100) if graded_bets > 0 else 0

    pushes = int(df["won"].isna().sum()) if not df.empty else 0

    # Count how many bets used real historical odds vs. the flat -110 fallback
    real_odds_bets = int(df[df["odds_source"] != "flat_-110"].shape[0]) if not df.empty else 0
    flat_odds_bets = int(df[df["odds_source"] == "flat_-110"].shape[0]) if not df.empty else 0

    summary = {
        "start_date":           start_date,
        "end_date":             end_date,
        "games_evaluated":      len(all_results),
        "bets_placed":          bets_placed,
        "bets_won":             bets_won,
        "bets_pushed":          pushes,
        "win_rate_pct":         round(win_rate, 1),
        "total_wagered":        round(total_wagered, 2),
        "total_profit":         round(total_profit, 2),
        "roi_pct":              round(roi, 2),
        "final_bankroll":       round(bankroll, 2),
        "starting_bankroll":    starting_bankroll,
        "real_odds_bets":       real_odds_bets,
        "flat_odds_bets":       flat_odds_bets,
        "output_csv":           output_csv,
        # Constraints applied — so you always know exactly what ran
        "min_edge_pct":         min_edge,
        "kelly_fraction":       flat_bet if flat_bet else kelly_fraction,
        "bet_mode":             f"flat ${flat_bet}" if flat_bet else "kelly",
        "max_bet_dollars":      "n/a (flat)" if flat_bet else max_bet_dollars,
        "daily_bet_cap":        daily_max_bets if daily_max_bets else "unlimited",
        "spring_training_skip": skip_spring_training,
        # RIFI section (only populated when --rifi is passed)
        "rifi_bets_placed":     rifi_bets_placed if backtest_rifi else "n/a",
        "rifi_bets_won":        rifi_bets_won    if backtest_rifi else "n/a",
        "rifi_win_rate_pct":    round(rifi_win_rate, 1) if backtest_rifi else "n/a",
        "rifi_total_wagered":   round(rifi_wagered, 2)  if backtest_rifi else "n/a",
        "rifi_total_profit":    round(rifi_profit, 2)   if backtest_rifi else "n/a",
        "rifi_roi_pct":         round(rifi_roi, 2)      if backtest_rifi else "n/a",
        "rifi_final_bankroll":  round(rifi_bankroll, 2) if backtest_rifi else "n/a",
        "rifi_output_csv":      rifi_output_csv         if backtest_rifi else "n/a",
    }

    logger.info("=" * 50)
    logger.info("BACKTEST SUMMARY")
    logger.info("=" * 50)
    for k, v in summary.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 50)

    return summary


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Baseball betting backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Realistic Kelly run with book limits
  python -m backtest.backtest --start 2025-04-01 --end 2025-08-16 --min-edge 5 --max-bet-dollars 500

  # Flat $100/bet to see pure model win rate (no compounding)
  python -m backtest.backtest --start 2025-04-01 --end 2025-08-16 --flat-bet 100 --min-edge 5

  # Aggressive edge filter, max 3 bets per day
  python -m backtest.backtest --start 2025-04-01 --end 2025-08-16 --min-edge 7 --daily-limit 3
        """,
    )
    parser.add_argument("--start",            default="2024-04-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",              default="2024-06-30", help="End date YYYY-MM-DD")
    parser.add_argument("--min-edge",         type=float, default=6.0,    help="Min edge %% to place a bet (default: 6.0)")
    parser.add_argument("--max-edge",         type=float, default=10.0,   help="Max edge %% cap — bets above this are likely model errors (default: 10.0)")
    parser.add_argument("--kelly",            type=float, default=0.25,   help="Kelly fraction — 0.25 = quarter Kelly (default: 0.25)")
    parser.add_argument("--max-bet",          type=float, default=5.0,    help="Max bet as %% of bankroll per game (default: 5.0)")
    parser.add_argument("--max-bet-dollars",  type=float, default=500.0,  help="Hard dollar ceiling per bet — mirrors real book limits (default: 500)")
    parser.add_argument("--flat-bet",         type=float, default=None,   help="Flat dollar amount per bet instead of Kelly — shows pure model performance")
    parser.add_argument("--daily-limit",      type=int,   default=None,   help="Max bets per calendar day (default: unlimited)")
    parser.add_argument("--bankroll",         type=float, default=1000.0, help="Starting bankroll (default: 1000)")
    parser.add_argument("--include-spring",   action="store_true",        help="Include spring training games (excluded by default)")
    parser.add_argument("--rifi",             action="store_true",        help="Also backtest RIFI/NRFI first-inning bets (saves to backtest/rifi_results.csv)")
    parser.add_argument("--cache",            default="backtest/lineup_cache.json",  help="Path to lineup cache JSON (default: backtest/lineup_cache.json)")
    parser.add_argument("--output",           default="backtest/results.csv",        help="Output CSV path (default: backtest/results.csv)")
    parser.add_argument("--underdog-mode",    action="store_true",        help="Research mode: skip v2 calibration for underdogs (+odds), use raw sim prob. Use with --flat-bet for clean results.")
    parser.add_argument("--ml-permissive",    action="store_true",        help="Research mode: moneyline only, strip ALL ML filter gates (odds range, raw prob floors, max edge cap). Takes every ML bet where raw edge >= --min-edge. Use to see what pure edge-based betting looks like.")
    parser.add_argument("--ml-permissive-calibrated", action="store_true", help="Like --ml-permissive, but KEEPS the calibration layer active. Apples-to-apples comparison to the live app's calibrated probabilities.")
    parser.add_argument("--seed",             type=int,   default=None,   help="Base seed for the Monte Carlo sim. Per-game seeds are derived from (seed, date, teams), so two runs with the same seed see identical sim outputs. Use to isolate calibration/filter changes from MC noise.")
    parser.add_argument("--calibration-variant", default="standard", choices=["standard", "scoped-bypass"], help="Which calibration path to use. 'standard' is production. 'scoped-bypass' applies the away-favorite bypass only when the away side is a favorite (odds < 0). Research mode.")
    parser.add_argument("--no-pitcher-splits", action="store_true", help="Disable pitcher vs_LHB / vs_RHB splits in the simulation (use overall rates only). Default: splits are ON.  Use to A/B test the impact of splits via two seeded runs.")
    args = parser.parse_args()

    from app import app, init_db
    with app.app_context():
        init_db()
        simulate_season_backtest(
            app_context=None,
            start_date=args.start,
            end_date=args.end,
            min_edge=args.min_edge,
            max_edge=args.max_edge,
            kelly_fraction=args.kelly,
            max_bet_pct=args.max_bet / 100.0,
            max_bet_dollars=args.max_bet_dollars,
            flat_bet=args.flat_bet,
            daily_max_bets=args.daily_limit,
            skip_spring_training=not args.include_spring,
            starting_bankroll=args.bankroll,
            backtest_rifi=args.rifi,
            lineup_cache_file=args.cache,
            output_csv=args.output,
            underdog_mode=args.underdog_mode,
            ml_permissive=args.ml_permissive,
            ml_permissive_calibrated=args.ml_permissive_calibrated,
            seed=args.seed,
            calibration_variant=args.calibration_variant,
            no_pitcher_splits=args.no_pitcher_splits,
        )
