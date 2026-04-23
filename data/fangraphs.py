"""
FanGraphs data import via CSV export.

How to export from FanGraphs:
  1. Go to fangraphs.com → Leaders → Batting or Pitching
  2. Set season and filters you want
  3. Click "Export Data" button (bottom of leaderboard)
  4. Save CSV to the /exports/ folder in this project

This module reads those CSVs and populates the database.
"""
import pandas as pd
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Expected column mappings from FanGraphs CSV to our field names
BATTING_COLUMNS = {
    "Name": "name",
    "Team": "team_abbr",
    "PA": "pa",
    "AB": "ab",
    "1B": "single_rate",    # We'll convert to rate after import
    "2B": "double_rate",
    "3B": "triple_rate",
    "HR": "hr_rate",
    "BB": "walk_rate",
    "IBB": "ibb",
    "HBP": "hbp",
    "SO": "strikeout_rate",
    "wOBA": "woba",
    "wRC+": "wrc_plus",
    "BABIP": "babip",
    "AVG": "avg",
    "OBP": "obp",
    "SLG": "slg",
    "OPS": "ops",
    "playerid": "fangraphs_id",
}

PITCHING_COLUMNS = {
    "Name": "name",
    "Team": "team_abbr",
    "IP": "ip",
    "G": "games",
    "GS": "games_started",
    "ERA": "era",
    "FIP": "fip",
    "xFIP": "xfip",
    "WHIP": "whip",
    "K/9": "k_per_9",
    "BB/9": "bb_per_9",
    "HR/9": "hr_per_9",
    "GB%": "gb_rate",
    "SO": "strikeout_raw",
    "BB": "walk_raw",
    "HR": "hr_raw",
    "H": "hits_allowed",
    "BF": "batters_faced",
    "FA%": "fastball_pct",
    "SL%": "slider_pct",
    "CU%": "curveball_pct",
    "CH%": "changeup_pct",
    "FC%": "cutter_pct",
    "SI%": "sinker_pct",
    "FS%": "splitter_pct",
    "wFA": "fastball_rv",
    "wSL": "slider_rv",
    "wCU": "curveball_rv",
    "wCH": "changeup_rv",
    "playerid": "fangraphs_id",
}


def load_batting_csv(filepath: str, season: int, split: str = "overall") -> pd.DataFrame:
    """
    Load a FanGraphs batting leaderboard CSV.

    split: "overall", "vs_LHP", "vs_RHP"
    Returns a cleaned DataFrame ready for database import.
    """
    if not os.path.exists(filepath):
        logger.error(f"File not found: {filepath}")
        return pd.DataFrame()

    try:
        df = pd.read_csv(filepath)
        logger.info(f"Loaded {len(df)} rows from {filepath}")
    except Exception as e:
        logger.error(f"Failed to read CSV: {e}")
        return pd.DataFrame()

    # Rename columns to our names (only columns that exist)
    rename_map = {k: v for k, v in BATTING_COLUMNS.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # Convert counting stats to per-PA rates
    if "pa" in df.columns and df["pa"].sum() > 0:
        hbp = df.get("hbp", pd.Series(0, index=df.index)).fillna(0)
        ibb = df.get("ibb", pd.Series(0, index=df.index)).fillna(0)

        for col in ["single_rate", "double_rate", "triple_rate", "hr_rate",
                    "strikeout_rate"]:
            if col in df.columns:
                df[col] = df[col].fillna(0) / df["pa"].replace(0, 1)

        # Walk rate includes HBP
        if "walk_rate" in df.columns:
            df["walk_rate"] = (df["walk_rate"].fillna(0) + hbp + ibb) / df["pa"].replace(0, 1)

        # Out rate = everything else
        on_base_cols = ["single_rate", "double_rate", "triple_rate",
                        "hr_rate", "walk_rate", "strikeout_rate"]
        existing = [c for c in on_base_cols if c in df.columns]
        df["out_rate"] = 1.0 - df[existing].sum(axis=1)
        df["out_rate"] = df["out_rate"].clip(lower=0)

    df["season"] = season
    df["split"] = split

    # Convert GB% from percentage string if needed
    if "gb_rate" in df.columns:
        df["gb_rate"] = pd.to_numeric(
            df["gb_rate"].astype(str).str.replace("%", ""), errors="coerce"
        ) / 100

    return df


def load_pitching_csv(filepath: str, season: int, split: str = "overall",
                       role: str = "SP") -> pd.DataFrame:
    """
    Load a FanGraphs pitching leaderboard CSV.

    role: "SP" or "RP"
    split: "overall", "vs_LHB", "vs_RHB"
    """
    if not os.path.exists(filepath):
        logger.error(f"File not found: {filepath}")
        return pd.DataFrame()

    try:
        df = pd.read_csv(filepath)
        logger.info(f"Loaded {len(df)} pitchers from {filepath}")
    except Exception as e:
        logger.error(f"Failed to read CSV: {e}")
        return pd.DataFrame()

    rename_map = {k: v for k, v in PITCHING_COLUMNS.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    # Convert counting stats to per-batter-faced rates
    if "batters_faced" in df.columns:
        bf = df["batters_faced"].replace(0, 1)
        for src, dst in [("strikeout_raw", "strikeout_rate"),
                          ("walk_raw", "walk_rate_allowed"),
                          ("hr_raw", "hr_rate_allowed")]:
            if src in df.columns:
                df[dst] = df[src].fillna(0) / bf

        if "hits_allowed" in df.columns:
            # Approximate single/double/triple breakdown
            # Singles ≈ H - 2B - 3B - HR
            hr = df.get("hr_raw", pd.Series(0, index=df.index)).fillna(0)
            doubles = df.get("2B", pd.Series(0, index=df.index)).fillna(0) if "2B" in df.columns else 0
            triples = df.get("3B", pd.Series(0, index=df.index)).fillna(0) if "3B" in df.columns else 0
            singles = (df["hits_allowed"].fillna(0) - doubles - triples - hr).clip(lower=0)
            df["single_rate_allowed"] = singles / bf
            df["double_rate_allowed"] = doubles / bf if not isinstance(doubles, int) else 0
            df["triple_rate_allowed"] = triples / bf if not isinstance(triples, int) else 0

        # Out rate
        rate_cols = ["strikeout_rate", "walk_rate_allowed", "hr_rate_allowed",
                     "single_rate_allowed", "double_rate_allowed", "triple_rate_allowed"]
        existing = [c for c in rate_cols if c in df.columns]
        df["out_rate"] = 1.0 - df[existing].sum(axis=1)
        df["out_rate"] = df["out_rate"].clip(lower=0)

    # Normalize pitch percentage columns (convert from 0-100 to 0-1)
    for col in ["fastball_pct", "slider_pct", "curveball_pct", "changeup_pct",
                "cutter_pct", "sinker_pct", "splitter_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace("%", ""), errors="coerce"
            ) / 100

    df["season"] = season
    df["split"] = split
    df["role"] = role

    return df


def get_import_instructions() -> str:
    """Return user-friendly instructions for exporting data from FanGraphs."""
    return """
HOW TO EXPORT FANGRAPHS DATA:

BATTING (repeat for each split):
  1. Go to: fangraphs.com/leaders.aspx?pos=all&stats=bat&lg=all&qual=50&type=8
  2. Set Season = 2025 (or current season)
  3. For splits: use the "Splits" tab → vs LHP / vs RHP
  4. Click "Export Data" at the bottom of the leaderboard
  5. Save file as:
     - exports/batting_overall_2025.csv
     - exports/batting_vs_lhp_2025.csv
     - exports/batting_vs_rhp_2025.csv

PITCHING (repeat for each role/split):
  1. Go to: fangraphs.com/leaders.aspx?pos=all&stats=pit&lg=all&qual=1&type=8
  2. Filter by SP or RP using the "Pos" dropdown
  3. Export and save as:
     - exports/pitching_sp_overall_2025.csv
     - exports/pitching_rp_overall_2025.csv
     - exports/pitching_sp_vs_lhb_2025.csv
     - exports/pitching_sp_vs_rhb_2025.csv

PITCH ARSENAL (for detailed pitch mix analysis):
  1. FanGraphs → Pitching → Pitch Type → Pitch Values
  2. Export and save as:
     - exports/pitch_arsenal_2025.csv
"""
