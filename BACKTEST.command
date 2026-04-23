#!/bin/bash
# Double-click this file to run the 2024 season backtest.
# caffeinate -s keeps your Mac awake even with the lid closed (stay plugged in!).
# Edit the OPTIONS line below to change edge %, bet size, etc.

cd "$(dirname "$0")"
source venv/bin/activate

# ── Edit these to change your backtest settings ────────────────────────────
START_DATE="2024-04-01"
END_DATE="2024-09-30"
MIN_EDGE=6            # Only bet when model edge is >= this % (6 = 6%)
MAX_EDGE=30           # Bets above this % are blocked as likely data errors
MAX_BET_DOLLARS=2000  # Hard ceiling per bet in dollars (mirrors BetMGM/Caesars limits)
BANKROLL=1000         # Starting bankroll in dollars
CACHE="backtest/lineup_cache.json"
OUTPUT="backtest/results_2024.csv"
# Uncomment ONE of the two lines below:
# BET_MODE="--flat-bet 100"         # Flat $100/bet — best for seeing true model accuracy
BET_MODE="--kelly 0.25"             # Kelly sizing — realistic growth simulation
# ──────────────────────────────────────────────────────────────────────────

echo ""
echo "=============================================="
echo "  Baseball Betting Backtest — 2024 Season"
echo "=============================================="
echo "  Date range:   $START_DATE → $END_DATE"
echo "  Min edge:     $MIN_EDGE%  |  Max edge: $MAX_EDGE%"
echo "  Max bet:      \$$MAX_BET_DOLLARS"
echo "  Bankroll:     \$$BANKROLL"
echo "  Bet mode:     $BET_MODE"
echo "  Cache:        $CACHE"
echo "  Output:       $OUTPUT"
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
  $BET_MODE

echo ""
echo "=============================================="
echo "  Backtest complete! Results saved to:"
echo "  $OUTPUT"
echo "=============================================="
echo ""
echo "Press any key to close this window."
read -n 1
