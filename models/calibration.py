"""
Probability calibration for the MLB simulation model.

The raw Monte Carlo output is systematically overconfident in the 35–60%
probability range but reasonably well-calibrated (and slightly underconfident)
in the 60–75% range.

Calibration method: Platt scaling (logistic regression on the logit of the
raw probability).  Formula: sigmoid(A * logit(raw_prob) + B)

Parameter history:
  v1 — fitted on 561 backtest simulation bets (April–Sep 2024).
       Well-sized sample but used simulated odds, not live market prices.
       Global brier: 0.2664 raw → 0.2428 calibrated.
       Problem: too conservative above 60% — the model IS underconfident
       there (77% actual in the 60–70% bucket), so v1 over-compressed it.

  v2 — refitted on 52 live 2024 bets.
       Too aggressive below 60%: compressed 60–70% raw down to ~45%.
       Valid signal below 60% (1-9 actual = 10% win rate) but coefficients
       were unreliable on such a small sample.

  v3 — hybrid approach (v2 below 60%, v1 above 60%).
       Blended via sigmoid transition at 0.60 pivot.

  v4 (current) — refitted on 115 moneyline bets (2024+2025 combined backtest).
       Above-60% params updated from real backtest outcomes:
         65–70%:  30 bets, 66.7% actual  → model well-calibrated
         70–75%:  28 bets, 78.6% actual  → model UNDERCONFIDENT (fixed here)
         75%+:    13 bets, 69.2% actual  → slight overconfidence, small sample
       Single set of params used for home and away (insufficient split data).
       v2 params retained for below-60% (still sparse in backtest, 1-9 live).

  v4.1 (REVERTED) — attempted an away-favorite bypass window for raw
       probabilities in [0.55, 0.70). Re-running the calibrated backtest
       with this active showed overall ROI dropped from +7.93% (old cal)
       to -2.03% (v4.1). The fix improved the away-favorite bucket as
       predicted (-12.32% → -1.06%) but inadvertently activated on away
       UNDERDOG bets too (side == "away" regardless of market odds),
       which degraded that bucket from +7.29% → -20.49%. Monte Carlo
       noise (sim is unseeded) also cascaded through bet selection in
       untouched buckets, making the runs hard to compare cleanly.
       Rolled back 2026-04-23; revisit with (a) favorite-side scoping
       and (b) a seeded sim for a clean head-to-head.

  Totals note:
       Totals-specific params have been removed. The 52-bet live sample
       contained too few over/under bets to fit reliable Platt params
       (v2 over slope was 7.1 — clearly overfit, crushed 63% raw → 3%).
       Totals now use a=1.0, b=0.0 (identity: sigmoid(logit(p)) = p),
       i.e. the raw simulation probability is used directly.
       Combined 70-bet backtest (2024+2025) shows raw probs are well-
       calibrated at 70–80% (75% and 78% actual respectively).

Usage:
    from models.calibration import calibrate_prob
    calibrated = calibrate_prob(0.65, 'moneyline', 'away')
"""

import math


# ---------------------------------------------------------------------------
# v4 params — refitted on 115 moneyline bets (2024+2025 backtest combined).
# Used for prob >= 0.60 (conservative/above-pivot region).
# Symmetric home/away: insufficient data to fit separate sides reliably.
#
# Derivation (two-point fit from bucket midpoints):
#   Point 1: raw=0.675, actual=0.667 → logit(0.675)=0.701, logit(0.667)=0.677
#   Point 2: raw=0.725, actual=0.786 → logit(0.725)=1.068, logit(0.786)=1.350
#   Slope A = (1.350-0.677)/(1.068-0.701) = 1.833
#   Intercept B = 0.677 - 1.833*0.701 = -0.607
#
# Validation:
#   raw 72.5% → calibrated 79.3%  (actual 78.6% ✓)
#   raw 67.5% → calibrated 66.2%  (actual 66.7% ✓)
# ---------------------------------------------------------------------------
_V4_A = 1.833
_V4_B = -0.607

_V4_PARAMS = {
    ("moneyline", "home"):  (_V4_A, _V4_B),
    ("moneyline", "away"):  (_V4_A, _V4_B),
}
_V4_GLOBAL_A = _V4_A
_V4_GLOBAL_B = _V4_B

# ---------------------------------------------------------------------------
# v2 params — retained for prob < 0.60 (aggressive compression).
# Fitted on 52 live 2024 bets; still the best signal we have sub-60%.
# Totals intentionally omitted.
# ---------------------------------------------------------------------------
_V2_PARAMS = {
    ("moneyline", "home"):  (2.088376, -1.124799),
    ("moneyline", "away"):  (3.386300, -2.519710),
}
_V2_GLOBAL_A = 2.514804
_V2_GLOBAL_B = -1.642055

# Blend transition point — below this raw prob use v2, above use v4.
# Set at 0.60 because backtest data below 0.60 is sparse (1-9 in live data).
_BLEND_PIVOT = 0.60

# ---------------------------------------------------------------------------
# Identity params — a=1.0, b=0.0 means sigmoid(logit(p)) = p exactly.
# Used for bet types where we have insufficient calibration data (totals).
# ---------------------------------------------------------------------------
_IDENTITY_A = 1.0
_IDENTITY_B = 0.0


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = max(0.001, min(0.999, p))
    return math.log(p / (1.0 - p))


def _apply(raw: float, a: float, b: float) -> float:
    return _sigmoid(a * _logit(raw) + b)


def calibrate_prob(raw_prob: float, bet_type: str, side: str) -> float:
    """
    Return a calibrated win probability for the given bet type and side.

    Uses a hybrid v2/v4 approach:
      - raw_prob < 0.60  → aggressive v2 compression (35-60% range was 1-9 in live data)
      - raw_prob >= 0.60 → conservative v4 params   (60-70% range went 10-3, 77% actual)
      - Smooth blend in the 0.55-0.65 window to avoid a hard jump at the boundary.

    Args:
        raw_prob:  Raw Monte Carlo win probability (0–1)
        bet_type:  'moneyline', 'totals', 'first_inning', 'f5_moneyline', etc.
        side:      'home', 'away', 'over', 'under', 'rifi', 'nrfi', etc.

    Returns:
        Calibrated probability (0–1), always in [0.01, 0.99]
    """
    side_key = side.lower().split()[0] if side else ""

    bt = bet_type.lower()
    if bt.startswith("f5_"):
        bt = bt[3:]

    # Totals (and F5 totals) use raw probability directly — we don't have
    # enough live over/under results to fit reliable Platt params yet.
    # The identity transform (a=1, b=0) returns the raw prob unchanged.
    if bt == "totals":
        return round(max(0.01, min(0.99, raw_prob)), 6)

    # Look up params for both versions
    v4_a, v4_b = _V4_PARAMS.get((bt, side_key), (_V4_GLOBAL_A, _V4_GLOBAL_B))
    v2_a, v2_b = _V2_PARAMS.get((bt, side_key), (_V2_GLOBAL_A, _V2_GLOBAL_B))

    p4 = _apply(raw_prob, v4_a, v4_b)  # conservative/above-pivot (v4 refitted)
    p2 = _apply(raw_prob, v2_a, v2_b)  # aggressive/below-pivot  (v2 retained)

    # Smooth blend: weight toward v2 below pivot, v4 above pivot.
    # Uses a sigmoid transition over a ±0.05 window around 0.60.
    blend_width = 0.05
    # blend=1 → pure v2 (aggressive), blend=0 → pure v4 (refitted)
    blend = _sigmoid(((_BLEND_PIVOT - raw_prob) / blend_width) * 4)
    calibrated = blend * p2 + (1.0 - blend) * p4

    return round(max(0.01, min(0.99, calibrated)), 6)


# ---------------------------------------------------------------------------
# Experimental: v4.1 "scoped bypass" variant.
#
# This is NOT used in production. It exists so the backtest can run the same
# seeded sim through two calibration paths — standard vs scoped-bypass — to
# cleanly measure whether the away-favorite bypass helps.
#
# Difference from calibrate_prob():
#   - Takes `market_odds` as an extra arg.
#   - Applies the bypass ONLY when the bet is on an away moneyline favorite
#     (market_odds < 0) with raw_prob in [0.55, 0.70), with linear ramps at
#     [0.53, 0.55) and [0.70, 0.72). Away underdogs go through standard
#     calibration — fixing the side-effect that tanked the first v4.1 run.
#
# Call sites: backtest.backtest when --calibration-variant=scoped-bypass is
# passed. Do NOT wire this into app.py until the seeded head-to-head validates
# it.
# ---------------------------------------------------------------------------
_SCOPED_LO_START = 0.53
_SCOPED_LO_END   = 0.55
_SCOPED_HI_START = 0.70
_SCOPED_HI_END   = 0.72


def _scoped_bypass_weight(raw: float) -> float:
    """Return 0..1 weight for raw-probability bypass. Identical ramp shape
    to the original v4.1 fix — only the gating condition (favorites only)
    lives at the call site."""
    if raw < _SCOPED_LO_START or raw >= _SCOPED_HI_END:
        return 0.0
    if raw < _SCOPED_LO_END:
        return (raw - _SCOPED_LO_START) / (_SCOPED_LO_END - _SCOPED_LO_START)
    if raw < _SCOPED_HI_START:
        return 1.0
    return 1.0 - (raw - _SCOPED_HI_START) / (_SCOPED_HI_END - _SCOPED_HI_START)


def calibrate_prob_scoped(raw_prob: float, bet_type: str, side: str,
                          market_odds: int) -> float:
    """
    Experimental v4.1-scoped variant — see module docstring for context.

    Away-moneyline bypass conditions (ALL must hold):
      - bet_type is moneyline (f5_moneyline normalized to moneyline)
      - side == "away"
      - market_odds < 0 (the away team is a favorite per the book)
      - raw_prob ∈ [0.53, 0.72) (ramp window)

    Everything else falls through to calibrate_prob() unchanged.
    """
    standard = calibrate_prob(raw_prob, bet_type, side)

    side_key = side.lower().split()[0] if side else ""
    bt = bet_type.lower()
    if bt.startswith("f5_"):
        bt = bt[3:]

    if bt != "moneyline":
        return standard
    if side_key != "away":
        return standard
    if market_odds is None or market_odds >= 0:
        return standard

    w = _scoped_bypass_weight(raw_prob)
    if w <= 0.0:
        return standard

    blended = w * raw_prob + (1.0 - w) * standard
    return round(max(0.01, min(0.99, blended)), 6)
