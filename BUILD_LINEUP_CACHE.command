#!/bin/bash
# Build the historical lineup cache from the MLB API.
# Run this ONCE before backtesting — takes ~30-45 minutes for a full season.
# Safe to re-run — it resumes from where it left off.

cd "$(dirname "$0")"

START_DATE="2024-04-01"
END_DATE="2024-09-30"

echo "=================================================="
echo "  Building Historical Lineup Cache"
echo "  $START_DATE → $END_DATE"
echo "  This will take ~30–45 minutes."
echo "  You can safely stop and restart — it resumes."
echo "=================================================="
echo ""

source venv/bin/activate
caffeinate -i python -m backtest.build_lineup_cache --start "$START_DATE" --end "$END_DATE"

echo ""
echo "Done. You can now run BACKTEST.command."
echo "Press any key to close."
read -n 1
