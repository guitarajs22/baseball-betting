#!/bin/bash
# Build the historical lineup cache from the MLB API for the 2026 season.
# Run this ONCE before backtesting 2026 -- takes ~30-45 minutes for a full season.
# Safe to re-run -- it resumes from where it left off.
#
# NOTE: 2026 is the current season, so this only fetches lineups for
# completed ("Final") games. Bump END_DATE forward and re-run periodically
# as the season progresses -- it'll pick up new completed games and skip
# ones it's already cached.

cd "$(dirname "$0")"

START_DATE="2026-03-26"
END_DATE="2026-09-02"
OUTPUT="backtest/lineup_cache_2026.json"

echo "=================================================="
echo "  Building Historical Lineup Cache -- 2026 Season"
echo "  $START_DATE -> $END_DATE"
echo "  Output: $OUTPUT"
echo "  This will take ~30-45 minutes."
echo "  You can safely stop and restart -- it resumes."
echo "=================================================="
echo ""

source venv/bin/activate
caffeinate -i python -m backtest.build_lineup_cache --start "$START_DATE" --end "$END_DATE" --output "$OUTPUT"

echo ""
echo "Done. You can now run BACKTEST_2026.command."
echo "Press any key to close."
read -n 1
