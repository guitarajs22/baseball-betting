#!/bin/bash
# Double-click to sync current rosters and pitching stats from the MLB API.
# Run this any time during the season to keep bullpen data up to date.
# Takes ~2-3 minutes. Safe to run repeatedly.

cd "$(dirname "$0")"
source venv/bin/activate

echo ""
echo "=============================================="
echo "  Updating Rosters & Pitching Stats"
echo "  Source: MLB Stats API (free, no key needed)"
echo "=============================================="
echo ""

python update_rosters.py

echo ""
echo "Press any key to close."
read -n 1
