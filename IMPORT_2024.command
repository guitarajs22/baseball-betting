#!/bin/bash
# Double-click this file to import your 2024 FanGraphs stats

cd "$(dirname "$0")"

echo "============================================"
echo "  Baseball Betting — 2024 Stats Import"
echo "============================================"
echo ""

source venv/bin/activate

CSV_COUNT=$(ls exports/2024/*.csv 2>/dev/null | wc -l | tr -d ' ')

echo "Found $CSV_COUNT CSV file(s) in exports/2024/"
echo ""

if [ "$CSV_COUNT" -eq 0 ]; then
  echo "No CSV files found!"
  echo ""
  echo "Please export your FanGraphs files first."
  echo "See: exports/2024/README.md for step-by-step instructions."
  echo ""
  echo "Press any key to close..."
  read -n 1
  exit 1
fi

echo "Starting import (--stats-only: roster sync skipped for historical data)..."
echo ""
python import_data.py --season 2024 --stats-only

echo ""
echo "============================================"
echo "  Import complete!"
echo "============================================"
echo ""
echo "The model will now blend 2024 + 2025 stats automatically."
echo "No restart required."
echo ""
echo "Press any key to close..."
read -n 1
