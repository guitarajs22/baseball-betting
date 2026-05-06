#!/bin/bash
# Seeded head-to-head: pitcher splits ON vs OFF, holding everything else fixed.
# Same seed (42) on both runs → identical sim outputs except for the
# pitcher-rates lookup per PA. Any ROI difference is purely from the splits.

set -e
cd "$(dirname "$0")/../.."
source venv/bin/activate

SEED=42
BASE="backtest/splits_seeded"

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

log "STARTING 4-run head-to-head, seed=$SEED, splits ON vs OFF"

# Run 1/4: 2024 WITH splits
log "Run 1/4: 2024 WITH splits"
python -m backtest.backtest \
  --start 2024-04-01 --end 2024-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated \
  --output "$BASE/with_splits/results_2024.csv" \
  > "$BASE/with_splits/run_2024.log" 2>&1

# Run 2/4: 2024 NO splits
log "Run 2/4: 2024 NO splits"
python -m backtest.backtest \
  --start 2024-04-01 --end 2024-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated --no-pitcher-splits \
  --output "$BASE/no_splits/results_2024.csv" \
  > "$BASE/no_splits/run_2024.log" 2>&1

# Run 3/4: 2025 WITH splits
log "Run 3/4: 2025 WITH splits"
python -m backtest.backtest \
  --start 2025-04-01 --end 2025-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated \
  --output "$BASE/with_splits/results_2025.csv" \
  > "$BASE/with_splits/run_2025.log" 2>&1

# Run 4/4: 2025 NO splits
log "Run 4/4: 2025 NO splits"
python -m backtest.backtest \
  --start 2025-04-01 --end 2025-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated --no-pitcher-splits \
  --output "$BASE/no_splits/results_2025.csv" \
  > "$BASE/no_splits/run_2025.log" 2>&1

log "ALL 4 RUNS COMPLETE"

# Drop a marker file for monitor scripts to poll
touch "$BASE/_done"

# Print the final comparison
log "Running compare.py..."
python "$BASE/compare.py" > "$BASE/final_report.txt" 2>&1
log "Report: $BASE/final_report.txt"
