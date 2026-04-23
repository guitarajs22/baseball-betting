"""
Kelly Criterion and Expected Value calculations.

Kelly formula: f* = (bp - q) / b
  where:
    b = decimal odds - 1  (the profit per unit wagered)
    p = our estimated win probability
    q = 1 - p (our estimated loss probability)

We use FRACTIONAL Kelly (half Kelly by default) to reduce variance.
"""


def american_to_decimal(american_odds: int) -> float:
    """Convert American odds to decimal odds."""
    if american_odds > 0:
        return (american_odds / 100) + 1
    else:
        return (100 / abs(american_odds)) + 1


def american_to_implied_prob(american_odds: int) -> float:
    """
    Convert American odds to implied probability.
    Removes the vig to get the fair implied probability.
    """
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def remove_vig(home_odds: int, away_odds: int) -> tuple:
    """
    Remove the bookmaker's vig from a two-sided market.
    Returns (fair_home_prob, fair_away_prob) that sum to 1.0.
    """
    raw_home = american_to_implied_prob(home_odds)
    raw_away = american_to_implied_prob(away_odds)
    total = raw_home + raw_away
    return raw_home / total, raw_away / total


def calculate_ev(our_prob: float, american_odds: int) -> float:
    """
    Calculate Expected Value as a percentage of amount wagered.

    EV% = (our_prob * profit_per_unit) - ((1 - our_prob) * 1)
    Positive EV = profitable bet in the long run.
    """
    decimal = american_to_decimal(american_odds)
    profit_per_unit = decimal - 1
    ev = (our_prob * profit_per_unit) - ((1 - our_prob) * 1)
    return round(ev * 100, 2)  # Return as percentage


def kelly_criterion(our_prob: float, american_odds: int, fraction: float = 0.25) -> float:
    """
    Calculate Kelly fraction of bankroll to wager.

    fraction: Kelly multiplier.
              0.25 = quarter Kelly (default — conservative, recommended).
              0.5  = half Kelly.

    Returns a float between 0 and 1 (fraction of bankroll).
    Returns 0 if no edge.
    Hard-capped at 5% of bankroll per bet regardless of Kelly output.
    """
    decimal = american_to_decimal(american_odds)
    b = decimal - 1  # profit per unit
    p = our_prob
    q = 1 - p

    kelly = (b * p - q) / b

    if kelly <= 0:
        return 0.0  # No edge — don't bet

    return round(min(kelly * fraction, 0.05), 4)  # Hard cap at 5% of bankroll per bet


def recommended_bet(bankroll: float, kelly_fraction: float, min_bet: float = 5.0) -> float:
    """
    Dollar amount to wager given bankroll and Kelly fraction.
    Rounded to nearest dollar. Minimum $5 bet.
    """
    amount = bankroll * kelly_fraction
    if amount < min_bet:
        return 0.0  # Skip if too small
    return round(amount, 0)


def edge_percentage(our_prob: float, american_odds: int) -> float:
    """
    Our edge over the book's implied probability.
    Positive = we have an edge.
    """
    implied = american_to_implied_prob(american_odds)
    return round((our_prob - implied) * 100, 2)


def analyze_bet(our_prob: float, american_odds: int, bankroll: float,
                kelly_fraction: float = 0.25, max_bet_pct: float = 0.05) -> dict:
    """
    Full bet analysis. Returns all relevant metrics in one dict.

    kelly_fraction: Quarter Kelly by default (0.25) — conservative sizing.
    max_bet_pct:    Hard cap as a fraction of bankroll (default 5%).
                    The recommended bet will never exceed bankroll * max_bet_pct.
    """
    implied = american_to_implied_prob(american_odds)
    ev = calculate_ev(our_prob, american_odds)
    edge = edge_percentage(our_prob, american_odds)
    kelly = kelly_criterion(our_prob, american_odds, kelly_fraction)
    bet_size = min(recommended_bet(bankroll, kelly), bankroll * max_bet_pct)

    # Bet grade based on edge
    if edge >= 5:
        grade = "A"
    elif edge >= 3:
        grade = "B"
    elif edge >= 1.5:
        grade = "C"
    elif edge > 0:
        grade = "D"
    else:
        grade = "F"  # No edge — skip

    return {
        "our_probability": round(our_prob * 100, 1),
        "implied_probability": round(implied * 100, 1),
        "edge_pct": edge,
        "ev_pct": ev,
        "kelly_fraction": kelly,
        "recommended_bet": bet_size,
        "grade": grade,
        "has_edge": edge > 0,
    }
