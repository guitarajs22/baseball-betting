"""
Monte Carlo simulation engine for MLB games.

For each simulation:
  - We step through each half-inning
  - Each batter faces the current pitcher
  - Outcome is drawn from a probability distribution blending
    the batter's rates and the pitcher's rates against that handedness
  - Park factors and weather adjust the probabilities
  - Pitcher is pulled based on pitch count / fatigue model
  - Bullpen takes over with its own rated stats
"""
import numpy as np
import json
from dataclasses import dataclass, field
from typing import List, Optional, Dict


# ---------------------------------------------------------------------------
# Data containers (passed in — no DB calls inside the simulation loop)
# ---------------------------------------------------------------------------

@dataclass
class BatterProfile:
    name: str
    bats: str                # "L", "R", "S"
    single_rate: float
    double_rate: float
    triple_rate: float
    hr_rate: float
    walk_rate: float
    strikeout_rate: float
    out_rate: float


@dataclass
class PitcherProfile:
    name: str
    throws: str              # "L", "R"
    single_rate_allowed: float
    double_rate_allowed: float
    triple_rate_allowed: float
    hr_rate_allowed: float
    walk_rate_allowed: float
    strikeout_rate: float
    out_rate: float
    stamina: float = 6.0     # Expected innings before bullpen (SP default)
    is_reliever: bool = False
    # Optional platoon splits — populated when vs_LHB / vs_RHB sample is large
    # enough to trust. Keys are batter handedness ("L" or "R"); values are the
    # same outcome rate dict used in blend_rates ({"single": ..., "double": ...,
    # "triple": ..., "hr": ..., "walk": ..., "strikeout": ..., "out": ...}).
    # When None or missing for a batter's hand, blend_rates falls back to the
    # overall *_allowed fields above. Switch hitters are mapped to the opposite
    # of the pitcher's throwing hand inside blend_rates.
    splits: Optional[Dict[str, Dict[str, float]]] = None


@dataclass
class BullpenProfile:
    """Aggregate bullpen stats — averaged across all relievers."""
    single_rate_allowed: float
    double_rate_allowed: float
    triple_rate_allowed: float
    hr_rate_allowed: float
    walk_rate_allowed: float
    strikeout_rate: float
    out_rate: float
    stamina: float = 999.0  # Effectively unlimited; used when bullpen serves as "starter"


@dataclass
class ParkFactors:
    runs: float = 1.0
    hr: float = 1.0
    hits: float = 1.0


@dataclass
class WeatherFactors:
    """
    Fractional adjustments to ball-in-play rates derived from conditions.
    Positive = more offense (more HRs / hits). All values are additive on top
    of park factors (which are centered at 1.0).

    Coefficients are research-backed (Alan Nathan / Robert Adair physics):
      temp_adj     — applied to HR + all hits;  +0.003 per °F above 72°F
                     (~3% more HRs per 10°F, consistent with game-data studies)
      wind_adj     — applied to HR only;  out_component × mph × 0.0011
                     (~11% more HRs per 10 mph blowing directly out to CF)
      wind_hit_adj — applied to doubles/triples only;  out_component × mph × 0.0005
                     (wind affects fly balls most; line drives / gaps less so)
      pressure_adj — applied to HR only;  (29.92 - inHg) × 0.017
                     (~1.7% per inHg below standard; altitude already in park factors,
                      this captures only intraday weather-system variation ±0.5 inHg)
      humidity_adj — applied to HR + all hits;  (rh_pct - 50) × 0.00005
                     (humid air is less dense → ball travels slightly farther;
                      ~0.5% per 10% RH — very small but correctly signed)
    """
    temp_adj:     float = 0.0
    wind_adj:     float = 0.0   # HR boost from wind out
    wind_hit_adj: float = 0.0   # doubles/triples boost from wind out
    pressure_adj: float = 0.0
    humidity_adj: float = 0.0


@dataclass
class UmpireFactors:
    runs_per_game_impact: float = 0.0
    walk_rate_impact: float = 0.0
    k_rate_impact: float = 0.0


@dataclass
class DefenseFactors:
    """
    Team defensive quality in the field.

    oaa: Outs Above Average — total season fielding outs vs. expectation.
         Positive = better-than-average defense → converts more balls-in-play to outs.
         Range typically ±45 per season. League average = 0 by construction.

    Scaling: top-to-bottom defensive spread (~80 OAA) is worth ~0.35–0.40 runs per game.
    Per-PA hit-rate adjustment = 1.0 - (oaa / 800), bounded to [0.90, 1.10].
    Applied to singles/doubles/triples (balls-in-play); not HRs (out-of-play).
    """
    oaa: float = 0.0


@dataclass
class CatcherFactors:
    """
    Catcher pitch framing — stealing strikes on borderline pitches.

    csr_diff: shadow-zone called strike rate above/below league average (0.466).
              Range is roughly ±0.07 (elite framers like Patrick Bailey ~+0.025,
              poor framers like Shea Langeliers ~-0.062).

    A framer who converts extra pitches to strikes helps the pitcher:
      - More Ks (more 2-strike counts reach three)
      - Fewer walks (fewer 3-ball counts reach four)

    Scaling: csr_diff of ±0.05 maps to roughly ±2% shift in K and BB rates.
    Multiplier applied to the pitcher's side of the PA, when the fielding team's
    catcher is behind the plate (home catcher → away batting, vice versa).
    """
    csr_diff: float = 0.0


@dataclass
class GameInputs:
    home_lineup: List[BatterProfile]
    away_lineup: List[BatterProfile]
    home_starter: PitcherProfile
    away_starter: PitcherProfile
    home_bullpen: BullpenProfile
    away_bullpen: BullpenProfile
    park: ParkFactors = field(default_factory=ParkFactors)
    weather: WeatherFactors = field(default_factory=WeatherFactors)
    umpire: UmpireFactors = field(default_factory=UmpireFactors)
    # Team defense: home_defense applies when home team is fielding (away batting),
    # away_defense applies when away team is fielding (home batting).
    home_defense: DefenseFactors = field(default_factory=DefenseFactors)
    away_defense: DefenseFactors = field(default_factory=DefenseFactors)
    # Starting catcher framing: home_catcher applies when away bats, vice versa.
    home_catcher: CatcherFactors = field(default_factory=CatcherFactors)
    away_catcher: CatcherFactors = field(default_factory=CatcherFactors)


# ---------------------------------------------------------------------------
# Regression to the mean
#
# Why this matters: a player with 50 PA in April should NOT be treated
# the same as a player with 600 PA in September. Small samples are noisy —
# blending observed stats with league average (weighted by sample size)
# prevents the model from over-reacting to hot/cold early-season stretches.
#
# Formula: regressed = (observed * sample + league_avg * k) / (sample + k)
#   k = the "regression constant" — at exactly k PA, we trust observed 50%.
#   Below k PA → pulled heavily toward league avg.
#   Above k PA → mostly own stats.
#
# k values are based on how quickly each rate stabilizes in real MLB data.
# Strikeout rate stabilizes fast (~150 PA); BABIP-driven hit rates are noisy
# and need 800+ PA before they're reliable.
# ---------------------------------------------------------------------------

LEAGUE_AVG_BATTING = {
    "single_rate":    0.150,
    "double_rate":    0.047,
    "triple_rate":    0.005,
    "hr_rate":        0.030,
    "walk_rate":      0.084,
    "strikeout_rate": 0.226,
    "out_rate":       0.458,
}

BATTING_REGRESSION_K = {
    # k = "equivalent PA" of the league-average prior.
    # At k PA of data, observed and prior are weighted equally (50/50).
    # Below k → pulled toward league avg. Above k → mostly own stats.
    #
    # These values are calibrated for FULL-SEASON FanGraphs data (~383 median PA).
    # At 383 PA each stat is trusted roughly:
    #   strikeout_rate : 76%  walk_rate : 72%  hr_rate : 58%
    #   single/double  : 52%  out_rate  : 61%  triple  : 39%
    #
    # Previous values (150/200/550/800/800/400/1200) were for mid-season
    # partial samples and compressed every team toward league average,
    # causing all games to simulate as near-coinflips (~55% home win).
    "strikeout_rate": 120,   # K% stabilizes fast — very consistent skill
    "walk_rate":      150,   # BB% also very consistent
    "hr_rate":        280,   # Power — 58% trusted at 383 PA
    "double_rate":    350,   # Gap power — 52% trusted at 383 PA
    "single_rate":    350,   # Singles — 52% trusted at 383 PA
    "out_rate":       245,   # Derived from the others — 61% trusted
    "triple_rate":    600,   # Rarest outcome — still quite noisy
}

LEAGUE_AVG_PITCHING = {
    "single_rate_allowed":    0.150,
    "double_rate_allowed":    0.047,
    "triple_rate_allowed":    0.005,
    "hr_rate_allowed":        0.030,
    "walk_rate_allowed":      0.084,
    "strikeout_rate":         0.226,
    "out_rate":               0.458,
}

PITCHING_REGRESSION_K = {
    # Pitching uses batters-faced (BF ≈ IP × 4.3).
    # A typical SP with 150 IP has faced ~645 batters.
    # At 645 BF each stat is trusted roughly:
    #   strikeout : 81%  walk : 76%  hr_allowed : 67%
    #   single/double_allowed : 62%  triple_allowed : 48%
    #
    # Previous values compressed pitcher quality toward league average in
    # the same way the batting constants did — all starters looked similar.
    "strikeout_rate":         150,   # Pitcher K rate — very reliable
    "walk_rate_allowed":      200,   # BB rate stabilizes fairly quickly
    "hr_rate_allowed":        320,   # HR/FB varies, 67% trusted at 645 BF
    "out_rate":               300,
    "single_rate_allowed":    400,   # BABIP-driven — 62% trusted at 645 BF
    "double_rate_allowed":    400,
    "triple_rate_allowed":    700,   # Still noisy even for full season
}


def regress_rate(observed: float, league_avg: float, sample_size: int, k: int) -> float:
    """
    Bayesian shrinkage — pull observed rate toward league average.

    At sample_size = k, the result is exactly halfway between observed and
    league_avg. At sample_size = 0, we return league_avg entirely.
    At sample_size >> k, the result is close to the observed rate.
    """
    if sample_size <= 0:
        return league_avg
    return (observed * sample_size + league_avg * k) / (sample_size + k)


def build_batter_profile(name: str, bats: str, pa: int,
                          single_rate: float, double_rate: float,
                          triple_rate: float, hr_rate: float,
                          walk_rate: float, strikeout_rate: float,
                          out_rate: float) -> "BatterProfile":
    """
    Build a BatterProfile with regression to the mean applied.

    Players with fewer plate appearances (PA) are pulled more toward
    league average. A full-season player (~650 PA) retains most of
    their observed stats; an April player with 50 PA is mostly league avg.
    """
    return BatterProfile(
        name=name,
        bats=bats,
        single_rate=regress_rate(
            single_rate, LEAGUE_AVG_BATTING["single_rate"], pa,
            BATTING_REGRESSION_K["single_rate"]),
        double_rate=regress_rate(
            double_rate, LEAGUE_AVG_BATTING["double_rate"], pa,
            BATTING_REGRESSION_K["double_rate"]),
        triple_rate=regress_rate(
            triple_rate, LEAGUE_AVG_BATTING["triple_rate"], pa,
            BATTING_REGRESSION_K["triple_rate"]),
        hr_rate=regress_rate(
            hr_rate, LEAGUE_AVG_BATTING["hr_rate"], pa,
            BATTING_REGRESSION_K["hr_rate"]),
        walk_rate=regress_rate(
            walk_rate, LEAGUE_AVG_BATTING["walk_rate"], pa,
            BATTING_REGRESSION_K["walk_rate"]),
        strikeout_rate=regress_rate(
            strikeout_rate, LEAGUE_AVG_BATTING["strikeout_rate"], pa,
            BATTING_REGRESSION_K["strikeout_rate"]),
        out_rate=regress_rate(
            out_rate, LEAGUE_AVG_BATTING["out_rate"], pa,
            BATTING_REGRESSION_K["out_rate"]),
    )


def build_pitcher_split_rates(rates_dict: dict, ip: float) -> Optional[Dict[str, float]]:
    """
    Build a regressed pitcher-rates dict for a single platoon split (vs_LHB or
    vs_RHB). Returns the same shape as the `pitcher_rates` dict used inside
    blend_rates: {"single", "double", "triple", "hr", "walk", "strikeout", "out"}.

    Returns None if `rates_dict` is empty (caller should fall back to overall).
    Sample size is capped — splits naturally have fewer batters faced (typically
    ~50% of overall), so each rate is regressed with the same K constants but
    against the BF count derived from IP in *that* split only.
    """
    if not rates_dict:
        return None
    bf = max(int(ip * 4.3), 1)
    return {
        "single": regress_rate(
            rates_dict.get("single_rate_allowed", LEAGUE_AVG_PITCHING["single_rate_allowed"]),
            LEAGUE_AVG_PITCHING["single_rate_allowed"], bf, PITCHING_REGRESSION_K["single_rate_allowed"]),
        "double": regress_rate(
            rates_dict.get("double_rate_allowed", LEAGUE_AVG_PITCHING["double_rate_allowed"]),
            LEAGUE_AVG_PITCHING["double_rate_allowed"], bf, PITCHING_REGRESSION_K["double_rate_allowed"]),
        "triple": regress_rate(
            rates_dict.get("triple_rate_allowed", LEAGUE_AVG_PITCHING["triple_rate_allowed"]),
            LEAGUE_AVG_PITCHING["triple_rate_allowed"], bf, PITCHING_REGRESSION_K["triple_rate_allowed"]),
        "hr": regress_rate(
            rates_dict.get("hr_rate_allowed", LEAGUE_AVG_PITCHING["hr_rate_allowed"]),
            LEAGUE_AVG_PITCHING["hr_rate_allowed"], bf, PITCHING_REGRESSION_K["hr_rate_allowed"]),
        "walk": regress_rate(
            rates_dict.get("walk_rate_allowed", LEAGUE_AVG_PITCHING["walk_rate_allowed"]),
            LEAGUE_AVG_PITCHING["walk_rate_allowed"], bf, PITCHING_REGRESSION_K["walk_rate_allowed"]),
        "strikeout": regress_rate(
            rates_dict.get("strikeout_rate", LEAGUE_AVG_PITCHING["strikeout_rate"]),
            LEAGUE_AVG_PITCHING["strikeout_rate"], bf, PITCHING_REGRESSION_K["strikeout_rate"]),
        "out": regress_rate(
            rates_dict.get("out_rate", LEAGUE_AVG_PITCHING["out_rate"]),
            LEAGUE_AVG_PITCHING["out_rate"], bf, PITCHING_REGRESSION_K["out_rate"]),
    }


def build_pitcher_profile(name: str, throws: str, ip: float,
                           single_rate_allowed: float, double_rate_allowed: float,
                           triple_rate_allowed: float, hr_rate_allowed: float,
                           walk_rate_allowed: float, strikeout_rate: float,
                           out_rate: float, stamina: float = 6.0,
                           is_reliever: bool = False) -> "PitcherProfile":
    """
    Build a PitcherProfile with regression to the mean applied.

    IP is converted to estimated batters faced (BF ≈ IP × 4.3).
    A starter with 150 IP has faced ~645 batters — enough to trust K and BB
    rates but still regressed for HR and hit rates.
    """
    bf = int(ip * 4.3)  # approximate batters faced from innings pitched
    return PitcherProfile(
        name=name,
        throws=throws,
        single_rate_allowed=regress_rate(
            single_rate_allowed, LEAGUE_AVG_PITCHING["single_rate_allowed"], bf,
            PITCHING_REGRESSION_K["single_rate_allowed"]),
        double_rate_allowed=regress_rate(
            double_rate_allowed, LEAGUE_AVG_PITCHING["double_rate_allowed"], bf,
            PITCHING_REGRESSION_K["double_rate_allowed"]),
        triple_rate_allowed=regress_rate(
            triple_rate_allowed, LEAGUE_AVG_PITCHING["triple_rate_allowed"], bf,
            PITCHING_REGRESSION_K["triple_rate_allowed"]),
        hr_rate_allowed=regress_rate(
            hr_rate_allowed, LEAGUE_AVG_PITCHING["hr_rate_allowed"], bf,
            PITCHING_REGRESSION_K["hr_rate_allowed"]),
        walk_rate_allowed=regress_rate(
            walk_rate_allowed, LEAGUE_AVG_PITCHING["walk_rate_allowed"], bf,
            PITCHING_REGRESSION_K["walk_rate_allowed"]),
        strikeout_rate=regress_rate(
            strikeout_rate, LEAGUE_AVG_PITCHING["strikeout_rate"], bf,
            PITCHING_REGRESSION_K["strikeout_rate"]),
        out_rate=regress_rate(
            out_rate, LEAGUE_AVG_PITCHING["out_rate"], bf,
            PITCHING_REGRESSION_K["out_rate"]),
        stamina=stamina,
        is_reliever=is_reliever,
    )


# ---------------------------------------------------------------------------
# Outcome probabilities
# ---------------------------------------------------------------------------

OUTCOMES = ["single", "double", "triple", "hr", "walk", "strikeout", "out"]


def blend_rates(batter: BatterProfile, pitcher: PitcherProfile, park: ParkFactors,
                weather: WeatherFactors, umpire: UmpireFactors,
                defense: Optional['DefenseFactors'] = None,
                catcher: Optional['CatcherFactors'] = None) -> dict:
    """
    Blend batter and pitcher rates using a log-5 style approach.

    Log-5 formula: P(A beats B) = (A - A*B) / (A + B - 2*A*B)
    We apply this per outcome category then normalize to sum to 1.0.

    Environmental adjustments are multiplicative on top.
    """
    # League average rates — 2024 MLB actuals (per plate appearance)
    # BA .243, OBP .313, SLG .399, K% 22.6%, BB% 8.4%
    LEAGUE_AVG = {
        "single": 0.150,
        "double": 0.047,
        "triple": 0.005,
        "hr": 0.030,
        "walk": 0.084,
        "strikeout": 0.226,
        "out": 0.458,
    }

    batter_rates = {
        "single": batter.single_rate,
        "double": batter.double_rate,
        "triple": batter.triple_rate,
        "hr": batter.hr_rate,
        "walk": batter.walk_rate,
        "strikeout": batter.strikeout_rate,
        "out": batter.out_rate,
    }
    # Pick the right pitcher rates for THIS plate appearance.
    # If splits are populated AND the matching split exists for this batter's
    # handedness, use those. Otherwise fall back to the pitcher's overall rates.
    # Switch hitters bat opposite the pitcher's throwing arm, so we resolve
    # bats="S" → "R" vs LHP, "L" vs RHP.
    pitcher_overall_rates = {
        "single": pitcher.single_rate_allowed,
        "double": pitcher.double_rate_allowed,
        "triple": pitcher.triple_rate_allowed,
        "hr": pitcher.hr_rate_allowed,
        "walk": pitcher.walk_rate_allowed,
        "strikeout": pitcher.strikeout_rate,
        "out": pitcher.out_rate,
    }
    if getattr(pitcher, "splits", None):
        effective_bat = batter.bats or "R"
        if effective_bat == "S":
            effective_bat = "R" if (pitcher.throws == "L") else "L"
        pitcher_rates = pitcher.splits.get(effective_bat) or pitcher_overall_rates
    else:
        pitcher_rates = pitcher_overall_rates

    blended = {}
    for outcome in OUTCOMES:
        b = batter_rates[outcome]
        p = pitcher_rates[outcome]
        lg = LEAGUE_AVG[outcome]
        # Multiplicative odds-ratio: scales each outcome by how batter and
        # pitcher each deviate from league average, then re-normalize.
        # Formula: b * p / lg  (standard baseball simulation approach)
        blended[outcome] = (b * p / lg) if lg > 0 else (b + p) / 2

    # Environmental adjustments (only affect ball-in-play outcomes)
    # park factors are centered at 1.0 (neutral); weather adjustments add to that.
    # HR: affected by temperature, wind (fly balls), pressure, and humidity.
    # Doubles/triples: affected by temperature, wind (less than HRs), humidity.
    # Singles: small effect from temperature / humidity only (mostly grounders/liners).
    hr_adj   = (park.hr    + weather.temp_adj + weather.wind_adj
                + weather.pressure_adj + weather.humidity_adj)
    hit_adj  = (park.hits  + weather.temp_adj + weather.wind_hit_adj
                + weather.humidity_adj)
    sing_adj = (park.hits  + weather.temp_adj * 0.5 + weather.humidity_adj * 0.5)

    blended["hr"]     *= max(0.5, hr_adj)
    blended["double"] *= max(0.5, hit_adj)
    blended["triple"] *= max(0.5, hit_adj)
    blended["single"] *= max(0.5, sing_adj)

    # Umpire adjustments
    blended["walk"]     *= max(0.1, 1.0 + umpire.walk_rate_impact)
    blended["strikeout"] *= max(0.1, 1.0 + umpire.k_rate_impact)

    # runs_per_game_impact is the residual RPG effect not fully captured by
    # walk/K adjustments (e.g. contact quality, situational zone tendencies).
    # Scale to a per-PA contact rate: divide by league-avg total RPG (9.2) and
    # apply a 0.40 weight to avoid double-counting with the walk/K adjustments above.
    if umpire.runs_per_game_impact:
        contact_adj = (umpire.runs_per_game_impact / 9.2) * 0.40
        for _outcome in ("single", "double", "triple", "hr"):
            blended[_outcome] *= max(0.5, 1.0 + contact_adj)

    # Team defense (Outs Above Average) — suppresses balls-in-play hits.
    # +40 OAA (elite defense, ~Royals 2024) reduces BIP hits by ~5%.
    # -40 OAA (poor defense, ~Athletics 2024) boosts BIP hits by ~5%.
    # Applied ONLY to singles/doubles/triples; HRs are out of play.
    if defense and defense.oaa:
        bip_mult = 1.0 - (defense.oaa / 800.0)
        bip_mult = max(0.90, min(1.10, bip_mult))  # hard cap at ±10%
        blended["single"] *= bip_mult
        blended["double"] *= bip_mult
        blended["triple"] *= bip_mult

    # Catcher pitch framing — steals strikes on borderline pitches.
    # Elite framer (csr_diff ≈ +0.025) → ~+1% Ks, ~-1% walks
    # Poor framer  (csr_diff ≈ -0.062) → ~-2.5% Ks, ~+2.5% walks
    # Capped at ±5% so no single catcher dominates the PA outcome.
    if catcher and catcher.csr_diff:
        k_mult  = 1.0 + catcher.csr_diff * 0.40
        bb_mult = 1.0 - catcher.csr_diff * 0.40
        k_mult  = max(0.95, min(1.05, k_mult))
        bb_mult = max(0.95, min(1.05, bb_mult))
        blended["strikeout"] *= k_mult
        blended["walk"]      *= bb_mult

    # Calibration boost: the model omits errors (~0.6/game), stolen bases
    # (~0.7 SB/game per team), and wild pitches that advance runners.
    # These events contribute roughly 5% of real MLB run production.
    # Reduced from 1.08 → 1.05 after backtesting showed the model was
    # generating too many runs per game (inflating offense equally for
    # both sides, but tipping games the wrong way when one team had a
    # significantly better or worse pitcher).
    #
    # TODO — FUTURE FEATURE: Team Baserunning Aggressiveness
    # Replace the flat 1.05 with a per-team multiplier derived from:
    #   - Team stolen base rate (SB/opportunities from FanGraphs)
    #   - Team extra bases taken rate (XBT% — how often a runner goes 1st→3rd
    #     on a single, 1st→home on a double, etc.)
    #   - Sources: FanGraphs Team Baserunning (BsR), Baseball Savant Sprint Speed
    # Aggressive teams (e.g. Royals, Cardinals) should use ~1.07-1.09
    # Conservative teams (e.g. Red Sox, Yankees) should use ~1.03-1.05
    # Add a `baserunning_factor: float = 1.05` field to GameInputs and pass it in.
    OFFENSE_CALIBRATION = 1.05  # bumped from 1.03 — 2025 backtest showed strong under-bias
    for outcome in ("single", "double", "triple", "hr"):
        blended[outcome] *= OFFENSE_CALIBRATION

    # Clamp negatives then normalize so all outcomes sum to exactly 1.0
    blended = {k: max(0.0, v) for k, v in blended.items()}
    total = sum(blended.values())
    return {k: v / total for k, v in blended.items()} if total > 0 else {k: 1/7 for k in OUTCOMES}


def draw_outcome(rates: dict, rng: np.random.Generator) -> str:
    """Draw a single plate appearance outcome from the probability dict."""
    outcomes = list(rates.keys())
    probs = [rates[o] for o in outcomes]
    return rng.choice(outcomes, p=probs)


# ---------------------------------------------------------------------------
# Single inning simulation
# ---------------------------------------------------------------------------

def simulate_half_inning(lineup: List[BatterProfile],
                          lineup_pos: int,
                          pitcher: PitcherProfile,
                          bullpen: BullpenProfile,
                          park: ParkFactors,
                          weather: WeatherFactors,
                          umpire: UmpireFactors,
                          pitch_count: int,
                          rng: np.random.Generator,
                          defense: Optional[DefenseFactors] = None,
                          catcher: Optional[CatcherFactors] = None) -> tuple:
    """
    Simulate one half inning. Returns (runs_scored, new_lineup_pos, new_pitch_count).
    """
    runs = 0
    outs = 0
    bases = [False, False, False]  # 1st, 2nd, 3rd

    current_pitcher = pitcher
    using_bullpen = pitch_count >= pitcher.stamina * 15  # ~15 pitches/inning

    while outs < 3:
        batter = lineup[lineup_pos % len(lineup)]
        lineup_pos += 1

        # Switch to bullpen if starter is tired
        if not using_bullpen and pitch_count >= pitcher.stamina * 15:
            using_bullpen = True
            # Convert bullpen to pitcher profile
            current_pitcher = PitcherProfile(
                name="Bullpen",
                throws="R",
                single_rate_allowed=bullpen.single_rate_allowed,
                double_rate_allowed=bullpen.double_rate_allowed,
                triple_rate_allowed=bullpen.triple_rate_allowed,
                hr_rate_allowed=bullpen.hr_rate_allowed,
                walk_rate_allowed=bullpen.walk_rate_allowed,
                strikeout_rate=bullpen.strikeout_rate,
                out_rate=bullpen.out_rate,
                is_reliever=True,
            )

        rates = blend_rates(batter, current_pitcher, park, weather, umpire,
                            defense=defense, catcher=catcher)
        outcome = draw_outcome(rates, rng)

        # Estimate pitches per PA
        if outcome == "strikeout":
            pitch_count += 5
        elif outcome == "walk":
            pitch_count += 6
        else:
            pitch_count += 3

        # Advance runners
        if outcome == "out":
            outs += 1
        elif outcome == "strikeout":
            outs += 1
        elif outcome == "walk":
            if bases[0] and bases[1] and bases[2]:
                runs += 1
            elif bases[0] and bases[1]:
                bases[2] = True
            elif bases[0]:
                bases[1] = True
            else:
                bases[0] = True
        elif outcome == "single":
            # Use a new_bases list to avoid overwrite bugs when advancing runners.
            # Advancement rates based on 2024 MLB averages.
            #
            # TODO — FUTURE FEATURE: Team Baserunning Aggressiveness
            # The two probabilities below (0.63 and 0.28) are MLB averages.
            # Replace with per-team values from FanGraphs XBT% (extra bases taken):
            #   score_from_2nd_rate  → FanGraphs "2nd→Home on Single" %  (avg ~63%)
            #   first_to_third_rate  → FanGraphs "1st→3rd on Single" %   (avg ~28%)
            # Pass these in via a TeamBaserunning dataclass on GameInputs.
            score_from_2nd_rate = 0.63
            first_to_third_rate = 0.28

            new_bases = [False, False, False]
            # Runner on 3rd: always scores on a single
            if bases[2]:
                runs += 1
            # Runner on 2nd: scores or advances to 3rd
            if bases[1]:
                if rng.random() < score_from_2nd_rate:
                    runs += 1
                else:
                    new_bases[2] = True
            # Runner on 1st: if 2nd was empty, sometimes takes extra base to 3rd
            if bases[0]:
                if not bases[1] and rng.random() < first_to_third_rate:
                    # 3rd might already have runner from 2nd who didn't score
                    if new_bases[2]:
                        new_bases[1] = True   # can't both be on 3rd; stays at 2nd
                    else:
                        new_bases[2] = True   # 1st to 3rd
                else:
                    new_bases[1] = True       # 1st to 2nd (standard)
            # Batter always reaches 1st on a single
            new_bases[0] = True
            bases[:] = new_bases
        elif outcome == "double":
            runs += sum([bases[2], bases[1]])
            bases[2] = bases[0]
            bases[1] = True
            bases[0] = False
        elif outcome == "triple":
            runs += sum(bases)
            bases = [False, False, True]
        elif outcome == "hr":
            runs += 1 + sum(bases)
            bases = [False, False, False]

    return runs, lineup_pos, pitch_count


# ---------------------------------------------------------------------------
# Full game simulation
# ---------------------------------------------------------------------------

def simulate_game(inputs: GameInputs, rng: np.random.Generator) -> dict:
    """Simulate one complete 9-inning game. Returns score dict including F5 snapshot."""
    home_runs = 0
    away_runs = 0
    home_lineup_pos = 0
    away_lineup_pos = 0
    home_pitch_count = 0
    away_pitch_count = 0
    away_fi = 0  # away team runs in first inning
    home_fi = 0  # home team runs in first inning
    home_after_5 = None  # snapshot at end of 5th inning
    away_after_5 = None

    for inning in range(9):
        # Away team bats first — home team is fielding, so pass home defense + catcher
        runs, away_lineup_pos, away_pitch_count = simulate_half_inning(
            inputs.away_lineup, away_lineup_pos,
            inputs.home_starter, inputs.home_bullpen,
            inputs.park, inputs.weather, inputs.umpire,
            away_pitch_count, rng,
            defense=inputs.home_defense,
            catcher=inputs.home_catcher,
        )
        away_runs += runs
        if inning == 0:
            away_fi = runs

        # Home team bats (walk-off: skip bottom of 9th if home leads)
        if inning == 8 and home_runs > away_runs:
            break
        # Away team is fielding, so pass away defense + catcher
        runs, home_lineup_pos, home_pitch_count = simulate_half_inning(
            inputs.home_lineup, home_lineup_pos,
            inputs.away_starter, inputs.away_bullpen,
            inputs.park, inputs.weather, inputs.umpire,
            home_pitch_count, rng,
            defense=inputs.away_defense,
            catcher=inputs.away_catcher,
        )
        home_runs += runs
        if inning == 0:
            home_fi = runs

        # Snapshot scores after the 5th inning (index 4)
        if inning == 4:
            away_after_5 = away_runs
            home_after_5 = home_runs

    # Extra innings if tied
    extra = 0
    while home_runs == away_runs and extra < 6:
        extra += 1
        runs, away_lineup_pos, away_pitch_count = simulate_half_inning(
            inputs.away_lineup, away_lineup_pos,
            inputs.home_bullpen,  # type: ignore — bullpen pitching
            inputs.home_bullpen,
            inputs.park, inputs.weather, inputs.umpire,
            away_pitch_count, rng,
            defense=inputs.home_defense,
        )
        away_runs += runs

        if away_runs > home_runs:
            break

        runs, home_lineup_pos, home_pitch_count = simulate_half_inning(
            inputs.home_lineup, home_lineup_pos,
            inputs.away_bullpen,  # type: ignore
            inputs.away_bullpen,
            inputs.park, inputs.weather, inputs.umpire,
            home_pitch_count, rng,
            defense=inputs.away_defense,
        )
        home_runs += runs

    return {
        "home": home_runs,
        "away": away_runs,
        "away_fi": away_fi,
        "home_fi": home_fi,
        "home_after_5": home_after_5 if home_after_5 is not None else home_runs,
        "away_after_5": away_after_5 if away_after_5 is not None else away_runs,
    }


# ---------------------------------------------------------------------------
# Run N simulations and aggregate results
# ---------------------------------------------------------------------------

def run_simulations(inputs: GameInputs, n: int = 10000, seed: Optional[int] = None) -> dict:
    """
    Run N simulations and return aggregated statistics.

    Returns a dict ready to store in SimulationResult.
    """
    rng = np.random.default_rng(seed)
    home_scores = []
    away_scores = []
    home_f5_scores = []
    away_f5_scores = []
    away_fi_scored = 0  # count of sims where away team scored in 1st
    home_fi_scored = 0  # count of sims where home team scored in 1st

    for _ in range(n):
        result = simulate_game(inputs, rng)
        home_scores.append(result["home"])
        away_scores.append(result["away"])
        home_f5_scores.append(result["home_after_5"])
        away_f5_scores.append(result["away_after_5"])
        if result["away_fi"] > 0:
            away_fi_scored += 1
        if result["home_fi"] > 0:
            home_fi_scored += 1

    home_arr = np.array(home_scores)
    away_arr = np.array(away_scores)
    total_arr = home_arr + away_arr

    home_f5_arr = np.array(home_f5_scores)
    away_f5_arr = np.array(away_f5_scores)

    home_wins = int(np.sum(home_arr > away_arr))
    away_wins = int(np.sum(away_arr > home_arr))
    ties = n - home_wins - away_wins
    # Split ties 50/50
    home_win_pct = (home_wins + ties / 2) / n
    away_win_pct = (away_wins + ties / 2) / n

    # Home field advantage — MLB home teams win ~54% historically.
    # The simulation engine has no knowledge of which team is home vs away
    # beyond the lineup order, so we apply a small post-sim adjustment.
    # 0.04 = 4 percentage point boost to home team (calibrated to MLB avg ~54%).
    # Clamped to [0.05, 0.95] so we never output nonsensical probabilities.
    HOME_FIELD_ADV = 0.04
    home_win_pct = min(0.95, max(0.05, home_win_pct + HOME_FIELD_ADV))
    away_win_pct = round(1.0 - home_win_pct, 4)
    home_win_pct = round(home_win_pct, 4)

    # ── F5 win probabilities ─────────────────────────────────────────────────
    # Ties after 5 innings are a push at most books — split them evenly.
    f5_home_wins = int(np.sum(home_f5_arr > away_f5_arr))
    f5_away_wins = int(np.sum(away_f5_arr > home_f5_arr))
    f5_ties = n - f5_home_wins - f5_away_wins
    f5_home_win_pct = (f5_home_wins + f5_ties / 2) / n
    # Apply smaller home-field boost for F5 (starter matchup dilutes HFA)
    F5_HOME_FIELD_ADV = 0.02
    f5_home_win_pct = min(0.95, max(0.05, f5_home_win_pct + F5_HOME_FIELD_ADV))
    f5_away_win_pct = round(1.0 - f5_home_win_pct, 4)
    f5_home_win_pct = round(f5_home_win_pct, 4)
    f5_tie_pct = round(f5_ties / n, 4)

    # Score distribution for charts (cap at 20 runs each side)
    score_dist = {
        "home_scores":    home_scores[:2000],
        "away_scores":    away_scores[:2000],
        "home_f5_scores": home_f5_scores[:2000],
        "away_f5_scores": away_f5_scores[:2000],
    }

    away_fi_pct = round(away_fi_scored / n, 4)
    home_fi_pct = round(home_fi_scored / n, 4)
    # RIFI = at least one team scores in the first inning
    # P(RIFI) = 1 - P(away doesn't score) * P(home doesn't score)
    rifi_pct = round(1.0 - (1.0 - away_fi_pct) * (1.0 - home_fi_pct), 4)

    return {
        "num_simulations": n,
        "home_win_pct": round(home_win_pct, 4),
        "away_win_pct": round(away_win_pct, 4),
        "home_avg_runs": round(float(np.mean(home_arr)), 3),
        "away_avg_runs": round(float(np.mean(away_arr)), 3),
        "total_avg_runs": round(float(np.mean(total_arr)), 3),
        "total_std_dev": round(float(np.std(total_arr)), 3),
        "score_distribution": json.dumps(score_dist),
        "away_fi_score_pct": away_fi_pct,
        "home_fi_score_pct": home_fi_pct,
        "rifi_pct": rifi_pct,
        # First 5 innings
        "f5_home_win_pct":  f5_home_win_pct,
        "f5_away_win_pct":  f5_away_win_pct,
        "f5_tie_pct":       f5_tie_pct,
        "f5_home_avg_runs": round(float(np.mean(home_f5_arr)), 3),
        "f5_away_avg_runs": round(float(np.mean(away_f5_arr)), 3),
    }


def calculate_over_under(sim_results: dict, total_line: float) -> dict:
    """Given simulation results, calculate O/U probabilities for a specific line."""
    import json
    dist = json.loads(sim_results["score_distribution"])
    home = np.array(dist["home_scores"])
    away = np.array(dist["away_scores"])
    totals = home + away
    n = len(totals)
    over_pct = round(float(np.sum(totals > total_line) / n), 4)
    under_pct = round(float(np.sum(totals < total_line) / n), 4)
    push_pct = round(1.0 - over_pct - under_pct, 4)
    return {"over": over_pct, "under": under_pct, "push": push_pct}


def calculate_f5_over_under(sim_results: dict, f5_total_line: float) -> dict:
    """
    Calculate F5 over/under probabilities using the first-5-innings score
    distribution stored in sim_results["score_distribution"].

    Returns {"over": float, "under": float, "push": float}
    """
    import json
    dist = json.loads(sim_results["score_distribution"])
    home_f5 = np.array(dist.get("home_f5_scores", []))
    away_f5 = np.array(dist.get("away_f5_scores", []))
    if len(home_f5) == 0 or len(away_f5) == 0:
        return {"over": 0.5, "under": 0.5, "push": 0.0}
    totals_f5 = home_f5 + away_f5
    n = len(totals_f5)
    over_pct  = round(float(np.sum(totals_f5 > f5_total_line) / n), 4)
    under_pct = round(float(np.sum(totals_f5 < f5_total_line) / n), 4)
    push_pct  = round(1.0 - over_pct - under_pct, 4)
    return {"over": over_pct, "under": under_pct, "push": push_pct}


def calculate_runline(sim_results: dict, line: float = 1.5) -> dict:
    """Calculate run line cover probabilities.

    Returns the probability that each team wins by more than `line` runs.
    These are raw directional values — the caller must apply the correct
    spread direction (+/-) from the sportsbook data:
      - home_minus_cover: use when home team is -1.5 (must win by 2+)
      - away_minus_cover: use when away team is -1.5 (must win by 2+)
    When home is +1.5, home cover prob = 1 - away_minus_cover (no push possible).
    """
    import json
    dist = json.loads(sim_results["score_distribution"])
    home = np.array(dist["home_scores"])
    away = np.array(dist["away_scores"])
    n = len(home)
    home_minus_cover = round(float(np.sum((home - away) > line) / n), 4)
    away_minus_cover = round(float(np.sum((away - home) > line) / n), 4)
    return {
        "home_minus_cover": home_minus_cover,   # P(home wins by 2+)
        "away_minus_cover": away_minus_cover,    # P(away wins by 2+)
    }
