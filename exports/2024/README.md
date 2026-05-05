# 2024 FanGraphs Stats Import Guide

This folder holds your **full-season 2024** CSV exports from FanGraphs.
2024 is a complete season (162 games per team), so these stats are the most
reliable historical anchor in the model — especially early in 2026 when
current-season samples are small.

Once you've downloaded all the files and placed them here, run:

```
python import_data.py --season 2024 --stats-only
```

(`--stats-only` skips the roster sync since rosters change every year — you
just want the historical stats.)

---

## How the Model Uses 2024 Data

The simulation blends stats across seasons using recency weights:

| Season | Weight |
|--------|--------|
| Most recent loaded (e.g. 2025) | 5× |
| One year back (e.g. 2024) | 4× |
| Two years back (e.g. 2023) | 3× |

A player with 500 PA in 2025 and 550 PA in 2024 gets an effective sample of
`500 + 550 = 1,050 PA`, blended with 2025 counting slightly more. This
dramatically reduces noise for early-season bets.

---

## Files You Need (same 7 as 2025, just different year)

Each file must be named **exactly** as shown.

### 1. `batting_overall_2024.csv`
1. Go to: https://www.fangraphs.com/leaders/major-league
2. Set **Season** to `2024`
3. Set **Split** to `Full Season`
4. Set **Min PA** to `50`
5. Use the **Dashboard** or **Value** tab
6. Click **Export Data** → rename to `batting_overall_2024.csv`

### 2. `batting_vs_lhp_2024.csv`
1. Go to: https://www.fangraphs.com/leaders/splits-leaderboards
2. **Season** = `2024`, **Split** = `vs LHP`, **Player Type** = `Batters`, **Min PA** = `20`
3. Export → rename to `batting_vs_lhp_2024.csv`

### 3. `batting_vs_rhp_2024.csv`
Same as above but **Split** = `vs RHP` → rename to `batting_vs_rhp_2024.csv`

### 4. `pitching_sp_overall_2024.csv`
1. Go to: https://www.fangraphs.com/leaders/major-league
2. **Season** = `2024`, **Position** = `SP`, **Min IP** = `20`
3. Export → rename to `pitching_sp_overall_2024.csv`

### 5. `pitching_rp_overall_2024.csv`
Same but **Position** = `RP`, **Min IP** = `5` → rename to `pitching_rp_overall_2024.csv`

### 6. `pitching_sp_vs_lhb_2024.csv`
1. Go to: https://www.fangraphs.com/leaders/splits-leaderboards
2. **Season** = `2024`, **Split** = `vs LHB`, **Player Type** = `Pitchers`, **Min TBF** = `20`
3. Export → rename to `pitching_sp_vs_lhb_2024.csv`

### 7. `pitching_sp_vs_rhb_2024.csv`
Same but **Split** = `vs RHB` → rename to `pitching_sp_vs_rhb_2024.csv`

---

## Running the Import

```bash
cd "/Users/andrewsilva/App Building/baseball-betting"
source venv/bin/activate
python import_data.py --season 2024 --stats-only
```

Or just double-click **IMPORT_2024.command**.

---

## Notes

- You only need to import 2024 **once** — it's a finished season that won't change.
- Players who retired or were released after 2024 are fine to import; the model
  ignores players not on a current roster when building lineups.
- If a player is missing (fell below the PA/IP threshold), lower the FanGraphs
  filter and re-export. Having incomplete data for a fringe player is fine —
  the model falls back to league average for anyone with no stats.
