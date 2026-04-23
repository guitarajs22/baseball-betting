#!/bin/bash
# Build the historical lineup cache from the MLB API for the 2025 season.
# Run this ONCE before backtesting 2025 — takes ~30-45 minutes for a full season.
# Safe to re-run — it resumes from where it left off.

cd "$(dirname "$0")"

START_DATE="2025-03-27"
END_DATE="2025-09-28"
OUTPUT="backtest/lineup_cache_2025.json"

echo "=================================================="
echo "  Building Historical Lineup Cache — 2025 Season"
echo "  $START_DATE → $END_DATE"
echo "  Output: $OUTPUT"
echo "  This will take ~30–45 minutes."
echo "  You can safely stop and restart — it resumes."
echo "=================================================="
echo ""

source venv/bin/activate
caffeinate -i python -m backtest.build_lineup_cache --start "$START_DATE" --end "$END_DATE" --output "$OUTPUT"

echo ""
echo "Done. You can now run a 2025 backtest with:"
echo "  python -m backtest.backtest --start 2025-03-27 --end 2025-09-28 --cache backtest/lineup_cache_2025.json"
echo "Press any key to close."
read -n 1
