"""
F5 (First 5 Innings) Backtest
==============================
Tests first-5-innings moneyline and totals bets using:
  - Simulation engine's native F5 win probabilities
  - Historical F5 closing odds from backtest/f5_odds_cache.json
  - Actual F5 results from MLB Stats API linescore (cached locally)

The F5 market is graded after 5 complete innings:
  Moneyline: leading team wins — ties are a PUSH (money returned)
  Totals:    combined runs in innings 1-5 vs the posted F5 line

Usage:
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15 --flat-bet 100
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15 --min-edge 4

Output:
  backtest/f5_results_<year>_v1.csv  — one row per bet placed
  Console summary printed at end
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger(__name__)

# F5 odds cache built by fetch_f5_odds_history.py
F5_CACHE_FILE = os.path.join(os.path.dirname(__file__), "f5_odds_cache.json")

# Actual F5 linescore results — keyed by str(game_pk), built as we go
F5_RESULT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "f5_result_cache.json")

# Known team name differences between MLB Stats API and The Odds API.
# Add entries here only when exact matching fails.
TEAM_NAME_ALIASES = {
    "Arizona D-backs":      "Arizona Diamondbacks",
    "D-backs":              "Arizona Diamondbacks",
}


def _normalize_team(name: str) -> str:
    return TEAM_NAME_ALIASES.get(name, name)


def _f5_cache_lookup(cache: dict, away_name: str, home_name: str, date_str: str):
    """
    Look up F5 odds in the cache.
    Tries exact match first, then falls back to last-word (nickname) matching
    in case MLB API and Odds API use slightly different city names.
    """
    away_n = _normalize_team(away_name)
    home_n = _normalize_team(home_name)

    key = f"{away_n} @ {home_n} {date_str}"
    if key in cache:
        return cache[key]

    # Fuzzy fallback: match by last word of team name (e.g. "Dodgers")
    away_nick = away_n.split()[-1]
    home_nick = home_n.split()[-1]
    for k, v in cache.items():
        if not k.endswith(date_str):
            continue
        parts = k.split(" @ ", 1)
        if len(parts) != 2:
            continue
        k_away = parts[0].strip()
        # Remove trailing date from home side
        k_home = parts[1].replace(f" {date_str}", "").strip()
        if k_away.split()[-1] == away_nick and k_home.split()[-1] == home_nick:
            return v

    return None


def american_to_decimal(odds: int) -> float:
    if odds > 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1


def american_to_implied(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def run_f5_backtest(
    app_context,
    start_date:         str,
    end_date:           str,
    min_edge:           float = 4.0,
    max_edge:           float = 15.0,
    kelly_fraction:     float = 0.25,
    max_bet_pct:        float = 0.05,
    max_bet_dollars:    float = 500.0,
    flat_bet:           Optional[float] = None,
    starting_bankroll:  float = 1000.0,
    output_csv:         str = "backtest/f5_results.csv",
    lineup_cache_file:  str = "backtest/lineup_cache.json",
    skip_spring_training: bool = True,
    no_pitcher_splits:  bool = False,
):
    from data.mlb_api import get_historical_schedule, get_f5_result
    from database.schema import Team, Player, PlayerStats, PitchingStats
    from models.simulation import (
        run_simulations, GameInputs, BatterProfile, PitcherProfile,
        BullpenProfile, ParkFactors, build_batter_profile, build_pitcher_profile,
        build_pitcher_split_rates,
        calculate_f5_over_under,
    )
    from models.kelly import analyze_bet

    # ── Load caches ───────────────────────────────────────────────────────
    if not os.path.exists(F5_CACHE_FILE):
        logger.error(f"F5 odds cache not found: {F5_CACHE_FILE}")
        logger.error("Run:  python fetch_f5_odds_history.py --season 2025")
        sys.exit(1)

    with open(F5_CACHE_FILE) as f:
        f5_odds_cache = json.load(f)
    logger.info(f"F5 odds cache loaded: {len(f5_odds_cache)} games")

    f5_result_cache: dict = {}
    if os.path.exists(F5_RESULT_CACHE_FILE):
        with open(F5_RESULT_CACHE_FILE) as f:
            f5_result_cache = json.load(f)
        logger.info(f"F5 result cache loaded: {len(f5_result_cache)} games")

    lineup_cache: dict = {}
    if os.path.exists(lineup_cache_file):
        with open(lineup_cache_file) as f:
            lineup_cache = json.load(f)

    # ── Date range ────────────────────────────────────────────────────────
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")
    total_days = (end - start).days + 1

    all_results  = []
    bankroll     = starting_bankroll
    bets_placed  = 0
    bets_won     = 0
    games_no_f5  = 0
    games_no_result = 0

    # ── Helper: build profiles ────────────────────────────────────────────
    def league_avg_batter(name=""):
        return BatterProfile(
            name=name, bats="R",
            single_rate=0.149, double_rate=0.046, triple_rate=0.004,
            hr_rate=0.034, walk_rate=0.083, strikeout_rate=0.225, out_rate=0.459,
        )

    def get_team_lineup(team_id, n=9, opponent_throws="R"):
        split = "vs_LHP" if opponent_throws == "L" else "vs_RHP"
        stats = (
            PlayerStats.query.join(Player)
            .filter(Player.team_id == team_id,
                    PlayerStats.season == season,
                    PlayerStats.split  == split)
            .order_by(PlayerStats.woba.desc())
            .limit(n).all()
        )
        if len(stats) < 5:
            stats = (
                PlayerStats.query.join(Player)
                .filter(Player.team_id == team_id,
                        PlayerStats.season == season,
                        PlayerStats.split  == "overall")
                .order_by(PlayerStats.woba.desc())
                .limit(n).all()
            )
        batters = [
            build_batter_profile(
                name=s.player.name, bats=s.player.bats or "R",
                pa=s.pa or 0,
                single_rate=s.single_rate or 0.150,
                double_rate=s.double_rate or 0.047,
                triple_rate=s.triple_rate or 0.005,
                hr_rate=s.hr_rate or 0.030,
                walk_rate=s.walk_rate or 0.084,
                strikeout_rate=s.strikeout_rate or 0.226,
                out_rate=s.out_rate or 0.458,
            )
            for s in stats
        ]
        while len(batters) < 9:
            batters.append(league_avg_batter())
        return batters[:9]

    def get_team_starter(team_id):
        rotation = (
            PitchingStats.query.join(Player)
            .filter(Player.team_id == team_id,
                    PitchingStats.season == season,
                    PitchingStats.role   == "SP",
                    PitchingStats.split  == "overall",
                    PitchingStats.games_started > 0)
            .order_by(PitchingStats.games_started.desc())
            .limit(6).all()
        )
        if not rotation:
            return PitcherProfile(
                name="TBD", throws="R",
                single_rate_allowed=0.150, double_rate_allowed=0.047,
                triple_rate_allowed=0.005, hr_rate_allowed=0.030,
                walk_rate_allowed=0.084, strikeout_rate=0.226, out_rate=0.458,
                stamina=6.0,
            )
        total_ip = sum(s.ip or 0 for s in rotation) or 1.0
        def wavg(field):
            return sum((getattr(s, field) or 0) * (s.ip or 0) for s in rotation) / total_ip
        total_starts = sum(s.games_started or 0 for s in rotation) or 1
        lhp_ip = sum((s.ip or 0) for s in rotation if s.player.throws == "L")
        profile = build_pitcher_profile(
            name="Rotation Avg", throws="L" if lhp_ip > total_ip / 2 else "R",
            ip=total_ip,
            single_rate_allowed=wavg("single_rate_allowed"),
            double_rate_allowed=wavg("double_rate_allowed"),
            triple_rate_allowed=wavg("triple_rate_allowed"),
            hr_rate_allowed=wavg("hr_rate_allowed"),
            walk_rate_allowed=wavg("walk_rate_allowed"),
            strikeout_rate=wavg("strikeout_rate"),
            out_rate=wavg("out_rate"),
            stamina=total_ip / total_starts,
        )
        # Attach IP-weighted platoon splits from the same rotation members.
        # Mirrors the production app behavior — the simulator picks vs_LHB
        # or vs_RHB rates per PA based on the batter's hand.
        # Gated by --no-pitcher-splits flag for A/B testing.
        if no_pitcher_splits:
            return profile
        rotation_player_ids = [s.player_id for s in rotation]
        rotation_splits = {}
        for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
            split_rows = (PitchingStats.query
                          .filter(PitchingStats.player_id.in_(rotation_player_ids),
                                  PitchingStats.season == season,
                                  PitchingStats.role == "SP",
                                  PitchingStats.split == db_split)
                          .all())
            if not split_rows:
                continue
            split_tip = sum(r.ip or 0 for r in split_rows)
            if split_tip < 50.0:
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
        relievers = (
            PitchingStats.query.join(Player)
            .filter(Player.team_id == team_id,
                    PitchingStats.season == season,
                    PitchingStats.role   == "RP",
                    PitchingStats.split  == "overall")
            .all()
        )
        if not relievers:
            return BullpenProfile(
                single_rate_allowed=0.155, double_rate_allowed=0.048,
                triple_rate_allowed=0.004, hr_rate_allowed=0.038,
                walk_rate_allowed=0.090, strikeout_rate=0.220, out_rate=0.445,
            )
        total_ip = sum(r.ip or 0 for r in relievers) or 1.0
        def wavg(f):
            return sum((getattr(r, f) or 0) * (r.ip or 0) for r in relievers) / total_ip
        bp = build_pitcher_profile(
            name="Bullpen", throws="R", ip=total_ip,
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

    def _lookup_lineup_cache(date_str, home_abbr, away_abbr):
        primary = f"{away_abbr}@{home_abbr}_{date_str}"
        if primary in lineup_cache:
            return lineup_cache[primary]
        for key, val in lineup_cache.items():
            if val.get("date") == date_str:
                if val.get("home_abbr") == home_abbr and val.get("away_abbr") == away_abbr:
                    return val
        return None

    def _starter_from_cache(mlb_id, fallback_team_id):
        if not mlb_id:
            return get_team_starter(fallback_team_id)
        player = Player.query.filter_by(mlb_id=mlb_id).first()
        if not player:
            return get_team_starter(fallback_team_id)
        stats = (PitchingStats.query
                 .filter_by(player_id=player.id, season=season, role="SP", split="overall")
                 .first())
        if not stats:
            stats = (PitchingStats.query
                     .filter_by(player_id=player.id, season=season - 1, role="SP", split="overall")
                     .first())
        if not stats:
            return get_team_starter(fallback_team_id)
        profile = build_pitcher_profile(
            name=player.name, throws=player.throws or "R",
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
        # Attach platoon splits for this specific pitcher (matches production behavior).
        # Gated by --no-pitcher-splits flag for A/B testing.
        if no_pitcher_splits:
            return profile
        splits = {}
        for bat_hand, db_split in [("L", "vs_LHB"), ("R", "vs_RHB")]:
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
        if not batter_entries:
            return get_team_lineup(fallback_team_id, opponent_throws=opponent_throws)
        split   = "vs_LHP" if opponent_throws == "L" else "vs_RHP"
        ordered = sorted(batter_entries, key=lambda x: x["order"])
        lineup  = []
        for entry in ordered[:9]:
            player = Player.query.filter_by(mlb_id=entry["mlb_id"]).first()
            stats  = None
            if player:
                stats = (PlayerStats.query
                         .filter_by(player_id=player.id, season=season, split=split).first())
                if not stats:
                    stats = (PlayerStats.query
                             .filter_by(player_id=player.id, season=season, split="overall").first())
                if not stats:
                    stats = (PlayerStats.query
                             .filter_by(player_id=player.id, season=season - 1, split="overall").first())
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
        while len(lineup) < 9:
            lineup.append(league_avg_batter())
        return lineup[:9]

    # ── Main loop ─────────────────────────────────────────────────────────
    current = start
    day_num = 0
    result_cache_dirty = False

    while current <= end:
        day_num += 1

        if skip_spring_training and (
            current.month < 3 or (current.month == 3 and current.day < 20)
        ):
            current += timedelta(days=1)
            continue

        date_str = current.strftime("%Y-%m-%d")
        season   = current.year
        logger.info(f"[{day_num}/{total_days}] {date_str} ...")

        schedule = get_historical_schedule(
            current.strftime("%m/%d/%Y"), current.strftime("%m/%d/%Y")
        )

        for game_info in schedule:
            if game_info.get("status") != "Final":
                continue
            if skip_spring_training and game_info.get("game_type") == "S":
                continue

            home_name  = game_info.get("home_name", "")
            away_name  = game_info.get("away_name", "")
            game_pk    = game_info.get("game_id")
            home_score = game_info.get("home_score")
            away_score = game_info.get("away_score")

            if home_score is None or away_score is None or not game_pk:
                continue

            # ── Look up F5 odds ───────────────────────────────────────────
            f5_odds = _f5_cache_lookup(f5_odds_cache, away_name, home_name, date_str)
            if not f5_odds:
                games_no_f5 += 1
                continue

            f5_home_ml    = f5_odds.get("f5_home_ml")
            f5_away_ml    = f5_odds.get("f5_away_ml")
            f5_total_line = f5_odds.get("f5_total_line")
            f5_over_price = f5_odds.get("f5_over_price")
            f5_under_price= f5_odds.get("f5_under_price")
            odds_src      = f5_odds.get("bookmaker", "unknown")

            if f5_home_ml is None or f5_away_ml is None:
                games_no_f5 += 1
                continue

            # ── Look up actual F5 result ──────────────────────────────────
            pk_str = str(game_pk)
            if pk_str not in f5_result_cache:
                f5_actual = get_f5_result(game_pk)
                if f5_actual is None:
                    games_no_result += 1
                    continue
                f5_result_cache[pk_str] = f5_actual
                result_cache_dirty = True
            f5_actual = f5_result_cache[pk_str]

            # ── Match teams to DB for simulation ──────────────────────────
            from database.schema import Team
            home_team = Team.query.filter(
                Team.name.ilike(f"%{home_name.split()[-1]}%")
            ).first()
            away_team = Team.query.filter(
                Team.name.ilike(f"%{away_name.split()[-1]}%")
            ).first()
            if not home_team or not away_team:
                continue

            # ── Build simulation inputs ───────────────────────────────────
            cached = _lookup_lineup_cache(date_str, home_team.abbreviation, away_team.abbreviation)
            if cached:
                home_sp  = _starter_from_cache(cached["home"]["sp_id"], home_team.id)
                away_sp  = _starter_from_cache(cached["away"]["sp_id"], away_team.id)
                home_bat = _lineup_from_cache(cached["home"]["batters"], away_sp.throws, home_team.id)
                away_bat = _lineup_from_cache(cached["away"]["batters"], home_sp.throws,  away_team.id)
            else:
                home_sp  = get_team_starter(home_team.id)
                away_sp  = get_team_starter(away_team.id)
                home_bat = get_team_lineup(home_team.id, opponent_throws=away_sp.throws)
                away_bat = get_team_lineup(away_team.id, opponent_throws=home_sp.throws)

            park = ParkFactors()
            if home_team.ballpark:
                park = ParkFactors(
                    runs=home_team.ballpark.park_factor_runs or 1.0,
                    hr=home_team.ballpark.park_factor_hr   or 1.0,
                    hits=home_team.ballpark.park_factor_hits or 1.0,
                )

            inputs = GameInputs(
                home_lineup=home_bat, away_lineup=away_bat,
                home_starter=home_sp, away_starter=away_sp,
                home_bullpen=get_team_bullpen(home_team.id),
                away_bullpen=get_team_bullpen(away_team.id),
                park=park,
            )
            sim = run_simulations(inputs, n=10000)

            # ── F5 win probabilities (raw, no calibration yet) ────────────
            f5_home_prob = sim["f5_home_win_pct"]
            f5_away_prob = sim["f5_away_win_pct"]
            f5_tie_prob  = sim["f5_tie_pct"]

            # ── F5 MONEYLINE bets ─────────────────────────────────────────
            # Ties after 5 = push (money returned, not a loss)
            f5_home_won = f5_actual["home_leads"]  # True = home won F5
            f5_away_won = f5_actual["away_leads"]  # True = away won F5
            f5_tied     = f5_actual["tied"]         # True = push

            for side, our_prob, ml, won in [
                ("home", f5_home_prob, f5_home_ml, f5_home_won),
                ("away", f5_away_prob, f5_away_ml, f5_away_won),
            ]:
                if ml is None:
                    continue
                analysis = analyze_bet(our_prob, ml, bankroll, kelly_fraction)
                if analysis["edge_pct"] > max_edge:
                    continue
                if analysis["edge_pct"] >= min_edge:
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

                    if f5_tied:
                        # Push — return stake
                        profit = 0.0
                        won_flag = None
                    else:
                        dec_odds = american_to_decimal(ml)
                        profit = bet_size * (dec_odds - 1) if won else -bet_size
                        bankroll += profit
                        won_flag = won

                    bets_placed += 1
                    if won_flag:
                        bets_won += 1

                    all_results.append({
                        "date":          date_str,
                        "matchup":       f"{away_name} @ {home_name}",
                        "bet_type":      "f5_moneyline",
                        "side":          side,
                        "our_prob":      round(our_prob, 4),
                        "f5_tie_prob":   round(f5_tie_prob, 4),
                        "implied_prob":  round(american_to_implied(ml), 4),
                        "edge_pct":      analysis["edge_pct"],
                        "ev_pct":        analysis["ev_pct"],
                        "bet_size":      round(bet_size, 2),
                        "odds":          ml,
                        "is_underdog":   ml > 0,
                        "is_favorite":   ml < 0,
                        "odds_source":   odds_src,
                        "won":           won_flag,
                        "profit":        round(profit, 2),
                        "bankroll":      round(bankroll, 2),
                        # Actual result
                        "f5_home_runs":  f5_actual["home"],
                        "f5_away_runs":  f5_actual["away"],
                        "game_total":    home_score + away_score,
                    })

            # ── F5 TOTALS bets ────────────────────────────────────────────
            if (f5_total_line is not None
                    and f5_over_price is not None
                    and f5_under_price is not None):

                f5_ou = calculate_f5_over_under(sim, f5_total_line)
                over_prob  = f5_ou["over"]
                under_prob = f5_ou["under"]

                # Use same raw floor as full-game totals (0.58 sweet spot)
                F5_TOTALS_MIN_RAW = 0.55   # slightly lower — F5 sample is noisier
                if over_prob < F5_TOTALS_MIN_RAW and under_prob < F5_TOTALS_MIN_RAW:
                    continue

                actual_f5_total = f5_actual["total"]
                is_push = (actual_f5_total == f5_total_line)
                actual_over  = (actual_f5_total > f5_total_line)
                actual_under = (actual_f5_total < f5_total_line)

                for side, our_prob, ml, won in [
                    ("over",  over_prob,  f5_over_price,  actual_over),
                    ("under", under_prob, f5_under_price, actual_under),
                ]:
                    analysis = analyze_bet(our_prob, ml, bankroll, kelly_fraction)
                    if analysis["edge_pct"] > max_edge:
                        continue
                    if analysis["edge_pct"] >= min_edge:
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
                            profit   = 0.0
                            won_flag = None
                        else:
                            dec_odds = american_to_decimal(ml)
                            profit   = bet_size * (dec_odds - 1) if won else -bet_size
                            bankroll += profit
                            won_flag  = won

                        bets_placed += 1
                        if won_flag:
                            bets_won += 1

                        all_results.append({
                            "date":          date_str,
                            "matchup":       f"{away_name} @ {home_name}",
                            "bet_type":      "f5_totals",
                            "side":          side,
                            "our_prob":      round(our_prob, 4),
                            "f5_tie_prob":   None,
                            "implied_prob":  round(american_to_implied(ml), 4),
                            "edge_pct":      analysis["edge_pct"],
                            "ev_pct":        analysis["ev_pct"],
                            "bet_size":      round(bet_size, 2),
                            "odds":          ml,
                            "is_underdog":   False,
                            "is_favorite":   False,
                            "f5_total_line": f5_total_line,
                            "odds_source":   odds_src,
                            "won":           won_flag,
                            "profit":        round(profit, 2),
                            "bankroll":      round(bankroll, 2),
                            "f5_home_runs":  f5_actual["home"],
                            "f5_away_runs":  f5_actual["away"],
                            "game_total":    home_score + away_score,
                        })

        # Flush result cache to disk every day so reruns are fast
        if result_cache_dirty:
            with open(F5_RESULT_CACHE_FILE, "w") as f:
                json.dump(f5_result_cache, f)
            result_cache_dirty = False

        current += timedelta(days=1)

    # Final flush
    with open(F5_RESULT_CACHE_FILE, "w") as f:
        json.dump(f5_result_cache, f)

    # ── Save results ──────────────────────────────────────────────────────
    df = pd.DataFrame(all_results)
    os.makedirs("backtest", exist_ok=True)
    df.to_csv(output_csv, index=False)

    total_wagered = df["bet_size"].sum()    if not df.empty else 0
    total_profit  = df["profit"].sum()      if not df.empty else 0
    roi = (total_profit / total_wagered * 100) if total_wagered > 0 else 0
    graded = int(df["won"].notna().sum())   if not df.empty else 0
    pushes = int(df["won"].isna().sum())    if not df.empty else 0
    win_rate = (bets_won / graded * 100)    if graded > 0 else 0

    # Per-market breakdown
    if not df.empty:
        for bt in ["f5_moneyline", "f5_totals"]:
            sub = df[df["bet_type"] == bt]
            if sub.empty:
                continue
            sub_graded = int(sub["won"].notna().sum())
            sub_won    = int(sub["won"].eq(True).sum())
            sub_wr     = (sub_won / sub_graded * 100) if sub_graded > 0 else 0
            sub_wag    = sub["bet_size"].sum()
            sub_pnl    = sub["profit"].sum()
            sub_roi    = (sub_pnl / sub_wag * 100) if sub_wag > 0 else 0
            logger.info(f"  {bt}: {sub_graded} bets | {sub_wr:.1f}% WR | "
                        f"${sub_pnl:+.0f} P&L | {sub_roi:+.1f}% ROI")

    summary = {
        "start_date":        start_date,
        "end_date":          end_date,
        "bets_placed":       bets_placed,
        "bets_won":          bets_won,
        "bets_pushed":       pushes,
        "win_rate_pct":      round(win_rate, 1),
        "total_wagered":     round(total_wagered, 2),
        "total_profit":      round(total_profit, 2),
        "roi_pct":           round(roi, 2),
        "final_bankroll":    round(bankroll, 2),
        "starting_bankroll": starting_bankroll,
        "games_no_f5_odds":  games_no_f5,
        "games_no_result":   games_no_result,
        "output_csv":        output_csv,
        "min_edge_pct":      min_edge,
        "bet_mode":          f"flat ${flat_bet}" if flat_bet else "kelly",
    }

    logger.info("=" * 55)
    logger.info("F5 BACKTEST SUMMARY")
    logger.info("=" * 55)
    for k, v in summary.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 55)

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s %(message)s",
                        datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(
        description="F5 (First 5 Innings) backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Kelly run — explore all bets at 4%+ edge
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15

  # Flat $100/bet — pure model win rate, no compounding
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15 --flat-bet 100

  # Tighter filter
  python -m backtest.backtest_f5 --start 2025-04-01 --end 2025-09-15 --min-edge 6
        """,
    )
    parser.add_argument("--start",           default="2025-04-01")
    parser.add_argument("--end",             default="2025-09-15")
    parser.add_argument("--min-edge",        type=float, default=4.0)
    parser.add_argument("--max-edge",        type=float, default=15.0)
    parser.add_argument("--kelly",           type=float, default=0.25)
    parser.add_argument("--max-bet",         type=float, default=5.0)
    parser.add_argument("--max-bet-dollars", type=float, default=500.0)
    parser.add_argument("--flat-bet",        type=float, default=None)
    parser.add_argument("--bankroll",        type=float, default=1000.0)
    parser.add_argument("--output",          default="backtest/f5_results_2025_v1.csv")
    parser.add_argument("--cache",           default="backtest/lineup_cache.json")
    parser.add_argument("--include-spring",  action="store_true")
    parser.add_argument("--no-pitcher-splits", action="store_true",
                        help="Disable pitcher vs_LHB / vs_RHB splits. Use for A/B testing the hybrid setup.")
    args = parser.parse_args()

    from app import app, init_db
    with app.app_context():
        init_db()
        run_f5_backtest(
            app_context=None,
            start_date=args.start,
            end_date=args.end,
            min_edge=args.min_edge,
            max_edge=args.max_edge,
            kelly_fraction=args.kelly,
            max_bet_pct=args.max_bet / 100.0,
            max_bet_dollars=args.max_bet_dollars,
            flat_bet=args.flat_bet,
            starting_bankroll=args.bankroll,
            output_csv=args.output,
            lineup_cache_file=args.cache,
            skip_spring_training=not args.include_spring,
            no_pitcher_splits=args.no_pitcher_splits,
        )
