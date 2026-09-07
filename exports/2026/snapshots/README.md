# Point-in-time stat snapshots (for backtesting)

This folder holds dated copies of the same 7 FanGraphs CSVs used for the live
app (see `exports/2026/README.md`), except each one reflects stats **as of a
specific past date** instead of "right now." `backtest/backtest.py` uses these
to look up what the model would actually have known on each historical game's
date, instead of applying today's stats retroactively (which is what it did
before — see the app-status doc in the Claude project for the full writeup of
why that was a problem).

This is purely a backtesting aid. It never touches the live app's database —
`import_data.py --snapshot-date` writes to `PlayerStatsHistory` /
`PitchingStatsHistory`, completely separate tables from the ones the live
site reads from.

## One-time backfill for the 2026 season

FanGraphs' main leaderboard page (`/leaders/major-league`) has a **Custom
Date Range** option under the Split menu — set the start date to opening day
and the end date to whatever historical date you want a snapshot "as of."
That covers the 3 non-split files (`batting_overall`, `pitching_sp_overall`,
`pitching_rp_overall`). Whether the splits-leaderboards page (used for the 4
`vs_LHP`/`vs_RHP`/`vs_LHB`/`vs_RHB` files) supports the same custom range
hasn't been confirmed yet — check next time you're there. If it doesn't,
those 4 can be skipped per-checkpoint; the backtest falls back gracefully to
whatever split data does exist.

Recommended checkpoints for 2026 (adjust as convenient):
- 2026-04-30
- 2026-05-31 (in addition to the 2026-05-04 checkpoint we already have from
  the original commit)
- 2026-06-30
- 2026-07-31
- 2026-08-31

For each date:
1. Export all 7 files the same way as `exports/2026/README.md` describes,
   but with the Custom Date Range set to season-start → that checkpoint date.
2. Put them in `exports/2026/snapshots/YYYY-MM-DD/` (create the folder),
   named exactly as usual (`batting_overall_2026.csv`, etc.).
3. Run:
   ```
   python import_data.py --season 2026 --snapshot-date YYYY-MM-DD \
       --snapshot-dir exports/2026/snapshots/YYYY-MM-DD
   ```
   This is safe to re-run for the same date — it replaces just that
   snapshot, nothing else.

## Going forward

Every time you do a regular stat refresh (weekly, per the main README's
recommendation), also drop a copy into
`exports/2026/snapshots/<today's date>/` and run the snapshot import with
today's date. That's one extra `import_data.py` call on top of what you're
already doing, and it means every future backtest gets more precise —
no separate retroactive effort needed once this becomes routine.

## Checking what's loaded

```python
from app import app
from database.schema import PlayerStatsHistory
with app.app_context():
    dates = sorted({r.as_of_date for r in PlayerStatsHistory.query.all()})
    print(dates)
```
