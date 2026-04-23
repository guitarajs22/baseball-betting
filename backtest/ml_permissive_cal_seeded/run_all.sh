#!/bin/bash
# Seeded head-to-head: standard vs scoped-bypass calibration for moneyline.
# Same seed on both → identical sim outputs → any ROI difference is PURELY
# from the scoped-bypass calibration change for away favorites.

set -e
cd "$(dirname "$0")/../.."
source venv/bin/activate

SEED=42
BASE="backtest/ml_permissive_cal_seeded"

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

log "STARTING 4-run head-to-head, seed=$SEED"

# Run 1/4: 2024 standard
log "Run 1/4: 2024 STANDARD"
python -m backtest.backtest \
  --start 2024-04-01 --end 2024-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated \
  --output "$BASE/standard/results_2024.csv" \
  > "$BASE/standard/run_2024.log" 2>&1

# Run 2/4: 2024 scoped-bypass
log "Run 2/4: 2024 SCOPED-BYPASS"
python -m backtest.backtest \
  --start 2024-04-01 --end 2024-09-30 \
  --seed $SEED --calibration-variant scoped-bypass \
  --ml-permissive-calibrated \
  --output "$BASE/scoped_bypass/results_2024.csv" \
  > "$BASE/scoped_bypass/run_2024.log" 2>&1

# Run 3/4: 2025 standard
log "Run 3/4: 2025 STANDARD"
python -m backtest.backtest \
  --start 2025-04-01 --end 2025-09-30 \
  --seed $SEED --calibration-variant standard \
  --ml-permissive-calibrated \
  --output "$BASE/standard/results_2025.csv" \
  > "$BASE/standard/run_2025.log" 2>&1

# Run 4/4: 2025 scoped-bypass
log "Run 4/4: 2025 SCOPED-BYPASS"
python -m backtest.backtest \
  --start 2025-04-01 --end 2025-09-30 \
  --seed $SEED --calibration-variant scoped-bypass \
  --ml-permissive-calibrated \
  --output "$BASE/scoped_bypass/results_2025.csv" \
  > "$BASE/scoped_bypass/run_2025.log" 2>&1

log "ALL 4 RUNS COMPLETE"

# Drop a marker file for monitor scripts to poll
touch "$BASE/_done"

# And print the final comparison
log "Running compare.py..."
python "$BASE/compare.py" > "$BASE/final_report.txt" 2>&1
log "Report: $BASE/final_report.txt"
