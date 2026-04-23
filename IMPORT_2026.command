#!/bin/bash
# Double-click this file to import your 2026 FanGraphs stats

cd "$(dirname "$0")"

echo "============================================"
echo "  Baseball Betting — 2026 Stats Import"
echo "============================================"
echo ""

# Activate virtual environment
source venv/bin/activate

# Count how many 2026 CSVs are present
CSV_COUNT=$(ls exports/2026/*.csv 2>/dev/null | wc -l | tr -d ' ')

echo "Found $CSV_COUNT CSV file(s) in exports/2026/"
echo ""

if [ "$CSV_COUNT" -eq 0 ]; then
  echo "No CSV files found!"
  echo ""
  echo "Please export your FanGraphs files first."
  echo "See: exports/2026/README.md for step-by-step instructions."
  echo ""
  echo "Press any key to close..."
  read -n 1
  exit 1
fi

echo "Starting import..."
echo ""
python import_data.py --season 2026

echo ""
echo "============================================"
echo "  Import complete!"
echo "============================================"
echo ""
echo "Press any key to close..."
read -n 1
