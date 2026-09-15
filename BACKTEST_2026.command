#!/bin/bash
# Double-click this file to run the 2026 season backtest.
# caffeinate -s keeps your Mac awake even with the lid closed (stay plugged in!).
# Edit the OPTIONS line below to change edge %, bet size, etc.
#
# NOTE: 2026 is the current season, so END_DATE only covers however far
# HistoricalOdds has been backfilled (see fetch_main_odds_history.py) --
# bump it forward as you fetch more odds data. Check current coverage with:
#   python -c "from app import app; from database.schema import HistoricalOdds
#   with app.app_context():
#       r = HistoricalOdds.query.filter(HistoricalOdds.game_date.like('2026-%')).order_by(HistoricalOdds.game_date.desc()).first()
#       print(r.game_date if r else 'no 2026 odds loaded')"

cd "$(dirname "$0")"
source venv/bin/activate

# ── Edit these to change your backtest settings ────────────────────────────
START_DATE="2026-03-26"
END_DATE="2026-09-02"   # <-- bump forward as you backfill more HistoricalOdds
MIN_EDGE=6            # Only bet when model edge is >= this % (6 = 6%)
MAX_EDGE=30           # Bets above this % are blocked as likely data errors
MAX_BET_DOLLARS=2000  # Hard ceiling per bet in dollars (mirrors BetMGM/Caesars limits)
BANKROLL=1000         # Starting bankroll in dollars
CACHE="backtest/lineup_cache_2026.json"
OUTPUT="backtest/results_2026_full.csv"
SEED=71828182          # Fixed seed -- keeps Monte Carlo draws identical between
                        # runs so you can isolate the effect of a real code/data
                        # change (like a filter or calibration tweak) instead of
                        # comparing against fresh random noise each time. Change
                        # this to a new number only if you deliberately want a
                        # different random draw to sanity-check for overfitting.
MC_DRAWS=50000          # Monte Carlo simulations per game (default in the live
                        # app is 10000). 5x more draws shrinks per-game
                        # probability noise, so fewer borderline bets flip in
                        # or out of the edge threshold between runs. Takes
                        # roughly 5x longer to run -- lower this back toward
                        # 10000-20000 if a full-season run gets too slow.
# Uncomment ONE of the two lines below:
# BET_MODE="--flat-bet 100"         # Flat $100/bet — best for seeing true model accuracy
BET_MODE="--kelly 0.25"             # Kelly sizing — realistic growth simulation
# ──────────────────────────────────────────────────────────────────────────

echo ""
echo "=============================================="
echo "  Baseball Betting Backtest — 2026 Season"
echo "=============================================="
echo "  Date range:   $START_DATE → $END_DATE"
echo "  Min edge:     $MIN_EDGE%  |  Max edge: $MAX_EDGE%"
echo "  Max bet:      \$$MAX_BET_DOLLARS"
echo "  Bankroll:     \$$BANKROLL"
echo "  Bet mode:     $BET_MODE"
echo "  Cache:        $CACHE"
echo "  Output:       $OUTPUT"
echo "  Seed:         $SEED (fixed -- reruns are reproducible)"
echo "  MC draws:     $MC_DRAWS per game"
echo "  caffeinate:   ON (lid-close safe)"
echo "=============================================="
echo ""
echo "Your Mac will stay awake until this finishes."
echo "You can close the lid — just stay plugged in!"
echo ""

caffeinate -s python -m backtest.backtest \
  --start "$START_DATE" \
  --end   "$END_DATE" \
  --min-edge "$MIN_EDGE" \
  --max-edge "$MAX_EDGE" \
  --max-bet-dollars "$MAX_BET_DOLLARS" \
  --bankroll "$BANKROLL" \
  --cache "$CACHE" \
  --output "$OUTPUT" \
  --seed "$SEED" \
  --mc-draws "$MC_DRAWS" \
  $BET_MODE

echo ""
echo "=============================================="
echo "  Backtest complete! Results saved to:"
echo "  $OUTPUT"
echo "=============================================="
echo ""
echo "Press any key to close this window."
read -n 1
