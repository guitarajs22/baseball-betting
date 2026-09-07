# 2026 FanGraphs Stats Import Guide

This folder holds your 2026 season CSV exports from FanGraphs.
Once you've downloaded all the files and placed them here, run:

```
python import_data.py --season 2026
```

---

## When to Start Using 2026 Stats

Wait until teams have played **at least 30 games** (roughly mid-May) before importing.
Early season samples are too small and will hurt your model's accuracy.

---

## Files You Need (7 total)

Each file must be named **exactly** as shown below.

---

### 1. `batting_overall_2026.csv`
**What it is:** Full-season batting stats for all qualified hitters

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/major-league
2. Set **Season** to `2026`
3. Set **Split** to `Full Season`
4. Set **Min PA** to `50` (or "Qualified")
5. Set **Stat** to `Standard` — then also export `Advanced` (see note)
6. Click **Export Data** (bottom of page)
7. Rename the file to `batting_overall_2026.csv`

**Columns needed** (make sure these are included):
`Name, Team, G, PA, AB, H, 2B, 3B, HR, R, RBI, BB, SO, SB, AVG, OBP, SLG, OPS, wOBA, wRC+, WAR`

> Tip: Use the **Dashboard** or **Value** stat tabs — they include both standard and advanced columns in one export.

---

### 2. `batting_vs_lhp_2026.csv`
**What it is:** Batting splits vs. left-handed pitchers

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/splits-leaderboards
2. Set **Season** to `2026`
3. Set **Split** to `vs LHP`
4. Set **Player Type** to `Batters`
5. Set **Min PA** to `20`
6. Click **Export Data**
7. Rename to `batting_vs_lhp_2026.csv`

---

### 3. `batting_vs_rhp_2026.csv`
**What it is:** Batting splits vs. right-handed pitchers

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/splits-leaderboards
2. Same as above but set **Split** to `vs RHP`
3. Click **Export Data**
4. Rename to `batting_vs_rhp_2026.csv`

---

### 4. `pitching_sp_overall_2026.csv`
**What it is:** Full-season stats for starting pitchers

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/major-league
2. Set **Season** to `2026`
3. Set **Position** to `SP` (Starters)
4. Set **Min IP** to `20`
5. Click **Export Data**
6. Rename to `pitching_sp_overall_2026.csv`

**Columns needed:**
`Name, Team, W, L, G, GS, IP, H, ER, HR, BB, SO, ERA, FIP, xFIP, WHIP, K/9, BB/9, HR/9, BABIP, LOB%, WAR`

---

### 5. `pitching_rp_overall_2026.csv`
**What it is:** Full-season stats for relief pitchers

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/major-league
2. Set **Season** to `2026`
3. Set **Position** to `RP` (Relievers)
4. Set **Min IP** to `5`
5. Click **Export Data**
6. Rename to `pitching_rp_overall_2026.csv`

---

### 6. `pitching_sp_vs_lhb_2026.csv`
**What it is:** Starting pitcher splits vs. left-handed batters

**Steps:**
1. Go to: https://www.fangraphs.com/leaders/splits-leaderboards
2. Set **Season** to `2026`
3. Set **Split** to `vs LHB`
4. Set **Player Type** to `Pitchers`
5. Filter to **SP** only if possible, or export all and filter
6. Click **Export Data**
7. Rename to `pitching_sp_vs_lhb_2026.csv`

---

### 7. `pitching_sp_vs_rhb_2026.csv`
**What it is:** Starting pitcher splits vs. right-handed batters

**Steps:**
1. Same as above but set **Split** to `vs RHB`
2. Rename to `pitching_sp_vs_rhb_2026.csv`

---

## Running the Import

Once all 7 files are in this folder (`exports/2026/`), open Terminal and run:

```bash
cd "/Users/andrewsilva/App Building/baseball-betting"
source venv/bin/activate
python import_data.py --season 2026
```

To import stats **without** re-syncing rosters from the MLB API (faster):

```bash
python import_data.py --season 2026 --stats-only
```

---

## Updating Throughout the Season

You can re-run the import as often as you like — it **replaces** existing stats rather than duplicating them. Recommended schedule:
- **Weekly** during the season for the best model accuracy
- After any major trade deadline moves
- Never import mid-series if you want stable picks for a current series

---

## Troubleshooting

**"File not found" error:**
Make sure the filename matches exactly (no extra spaces, correct year, `.csv` extension).

**Import finishes but model still uses old stats:**
The simulation pulls stats fresh for each game calculation — no restart needed.

**A player is missing from the import:**
They may have fallen below the minimum PA/IP threshold. Lower the minimum filter on FanGraphs and re-export.
