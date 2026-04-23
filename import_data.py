"""
Data import script.

Run this after exporting CSVs from FanGraphs:
  python import_data.py

Also seeds all 30 MLB teams, ballparks, and fetches rosters from the MLB API.
"""
import os
import sys
import logging
import pandas as pd
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, db, init_db
from database.schema import (Team, Player, PlayerStats, PitchingStats,
                               Ballpark, BankrollLog)
from data.fangraphs import load_batting_csv, load_pitching_csv
from data.mlb_api import get_all_teams, get_team_roster

# ---------------------------------------------------------------------------
# MLB Teams & Ballparks seed data
# ---------------------------------------------------------------------------

MLB_TEAMS = [
    # (mlb_id, name, abbr, city, league, division, ballpark_name, latitude, longitude, altitude_ft, park_runs, park_hr, park_hits, roof, cf_bearing_deg)
    # cf_bearing_deg = compass heading from home plate toward center field (0=N, 90=E, 180=S, 270=W)
    # Used to orient the wind direction arrow correctly on the field diagram.
    (108, "Los Angeles Angels",      "LAA", "Anaheim",      "AL", "AL West",  "Angel Stadium",          33.80,  -117.88, 160,  0.97, 0.93, 0.97, "open",         293),
    (109, "Arizona Diamondbacks",    "ARI", "Phoenix",      "NL", "NL West",  "Chase Field",            33.45,  -112.07, 1082, 1.03, 1.05, 1.02, "retractable",  305),
    (110, "Baltimore Orioles",       "BAL", "Baltimore",    "AL", "AL East",  "Oriole Park",            39.28,  -76.62,  46,   1.00, 1.02, 1.00, "open",         278),
    (111, "Boston Red Sox",          "BOS", "Boston",       "AL", "AL East",  "Fenway Park",            42.35,  -71.10,  20,   1.04, 0.92, 1.05, "open",         283),
    (112, "Chicago Cubs",            "CHC", "Chicago",      "NL", "NL Central","Wrigley Field",         41.95,  -87.66,  595,  1.01, 1.03, 1.01, "open",         305),
    (113, "Cincinnati Reds",         "CIN", "Cincinnati",   "NL", "NL Central","Great American Ball Park",39.10, -84.51,  869,  1.08, 1.15, 1.04, "open",         276),
    (114, "Cleveland Guardians",     "CLE", "Cleveland",    "AL", "AL Central","Progressive Field",     41.50,  -81.69,  653,  0.96, 0.93, 0.97, "open",         287),
    (115, "Colorado Rockies",        "COL", "Denver",       "NL", "NL West",  "Coors Field",            39.76,  -104.99, 5200, 1.28, 1.32, 1.21, "open",         292),
    (116, "Detroit Tigers",          "DET", "Detroit",      "AL", "AL Central","Comerica Park",         42.34,  -83.05,  585,  0.95, 0.88, 0.97, "open",         285),
    (117, "Houston Astros",          "HOU", "Houston",      "AL", "AL West",  "Minute Maid Park",       29.76,  -95.36,  43,   0.99, 1.00, 0.99, "retractable",  330),
    (118, "Kansas City Royals",      "KC",  "Kansas City",  "AL", "AL Central","Kauffman Stadium",      39.05,  -94.48,  750,  0.97, 0.95, 0.98, "open",         296),
    (119, "Los Angeles Dodgers",     "LAD", "Los Angeles",  "NL", "NL West",  "Dodger Stadium",         34.07,  -118.24, 512,  0.97, 0.94, 0.98, "open",         313),
    (120, "Washington Nationals",    "WSH", "Washington",   "NL", "NL East",  "Nationals Park",         38.87,  -77.01,  25,   1.01, 1.02, 1.01, "open",         297),
    (121, "New York Mets",           "NYM", "New York",     "NL", "NL East",  "Citi Field",             40.76,  -73.85,  20,   0.96, 0.91, 0.97, "open",         303),
    (133, "Oakland Athletics",       "OAK", "Oakland",      "AL", "AL West",  "Oakland Coliseum",       37.75,  -122.20, 25,   0.91, 0.84, 0.93, "open",         308),
    (134, "Pittsburgh Pirates",      "PIT", "Pittsburgh",   "NL", "NL Central","PNC Park",              40.45,  -80.01,  730,  0.96, 0.91, 0.97, "open",         299),
    (135, "San Diego Padres",        "SD",  "San Diego",    "NL", "NL West",  "Petco Park",             32.71,  -117.16, 20,   0.94, 0.88, 0.95, "open",         309),
    (136, "Seattle Mariners",        "SEA", "Seattle",      "AL", "AL West",  "T-Mobile Park",          47.59,  -122.33, 20,   0.93, 0.90, 0.94, "retractable",  340),
    (137, "San Francisco Giants",    "SF",  "San Francisco","NL", "NL West",  "Oracle Park",            37.78,  -122.39, 20,   0.91, 0.84, 0.93, "open",         307),
    (138, "St. Louis Cardinals",     "STL", "St. Louis",    "NL", "NL Central","Busch Stadium",         38.62,  -90.19,  465,  0.98, 0.96, 0.99, "open",         290),
    (139, "Tampa Bay Rays",          "TB",  "St. Petersburg","AL","AL East",  "Tropicana Field",         27.77,  -82.65,  20,   0.93, 0.88, 0.94, "dome",         None),
    (140, "Texas Rangers",           "TEX", "Arlington",    "AL", "AL West",  "Globe Life Field",       32.75,  -97.08,  551,  1.05, 1.08, 1.03, "retractable",  320),
    (141, "Toronto Blue Jays",       "TOR", "Toronto",      "AL", "AL East",  "Rogers Centre",          43.64,  -79.39,  276,  1.01, 1.04, 1.01, "retractable",  310),
    (142, "Minnesota Twins",         "MIN", "Minneapolis",  "AL", "AL Central","Target Field",          44.98,  -93.28,  830,  1.00, 1.00, 1.00, "open",         293),
    (143, "Philadelphia Phillies",   "PHI", "Philadelphia", "NL", "NL East",  "Citizens Bank Park",     39.91,  -75.17,  20,   1.06, 1.09, 1.04, "open",         282),
    (144, "Atlanta Braves",          "ATL", "Cumberland",   "NL", "NL East",  "Truist Park",            33.89,  -84.47,  1050, 1.02, 1.04, 1.02, "open",         298),
    (145, "Chicago White Sox",       "CWS", "Chicago",      "AL", "AL Central","Guaranteed Rate Field", 41.83,  -87.63,  595,  1.02, 1.05, 1.02, "open",         288),
    (146, "Miami Marlins",           "MIA", "Miami",        "NL", "NL East",  "loanDepot park",         25.78,  -80.22,  6,    0.92, 0.86, 0.93, "retractable",  300),
    (147, "New York Yankees",        "NYY", "New York",     "AL", "AL East",  "Yankee Stadium",         40.83,  -73.93,  55,   1.05, 1.10, 1.03, "open",         300),
    (158, "Milwaukee Brewers",       "MIL", "Milwaukee",    "NL", "NL Central","American Family Field", 43.03,  -87.97,  634,  1.01, 1.03, 1.01, "retractable",  305),
]


def seed_teams_and_parks():
    """Insert all 30 teams and their ballparks if not already present.
    Also updates cf_bearing_deg on existing parks so re-running this is safe."""
    inserted = 0
    for row in MLB_TEAMS:
        (mlb_id, name, abbr, city, league, division,
         park_name, lat, lon, alt, park_runs, park_hr, park_hits, roof, cf_bearing) = row

        park = Ballpark.query.filter_by(name=park_name).first()
        if not park:
            park = Ballpark(
                name=park_name, city=city, latitude=lat, longitude=lon,
                altitude_feet=alt, park_factor_runs=park_runs,
                park_factor_hr=park_hr, park_factor_hits=park_hits,
                roof_type=roof, cf_bearing_deg=cf_bearing,
            )
            db.session.add(park)
            db.session.flush()
        else:
            # Always sync cf_bearing_deg so re-running updates existing rows
            park.cf_bearing_deg = cf_bearing

        if Team.query.filter_by(mlb_id=mlb_id).first():
            continue

        team = Team(
            mlb_id=mlb_id, name=name, abbreviation=abbr, city=city,
            league=league, division=division, ballpark_id=park.id,
        )
        db.session.add(team)
        inserted += 1

    db.session.commit()
    logger.info(f"Seeded {inserted} new teams.")


def _find_player(row: dict) -> "Player | None":
    """
    Match a CSV row to a Player in the DB.
    Priority: MLBAMID → fangraphs PlayerId → name.
    """
    # 1. MLBAMID is the MLB Stats API ID — most reliable
    mlbam = row.get("MLBAMID") or row.get("mlbamid")
    if mlbam and str(mlbam).strip() not in ("", "nan", "None"):
        try:
            player = Player.query.filter_by(mlb_id=int(float(mlbam))).first()
            if player:
                return player
        except (ValueError, TypeError):
            pass

    # 2. FanGraphs PlayerId — store in fangraphs_id column
    fg_id = row.get("PlayerId") or row.get("playerId") or row.get("playerid")
    if fg_id and str(fg_id).strip() not in ("", "nan", "None"):
        fg_str = str(fg_id).strip()
        player = Player.query.filter_by(fangraphs_id=fg_str).first()
        if player:
            return player
        # Save this ID for future lookups
        name = str(row.get("Name", row.get("name", ""))).strip()
        p = Player.query.filter_by(name=name).first()
        if p:
            p.fangraphs_id = fg_str
            return p

    # 3. Name match
    name = str(row.get("Name", row.get("name", ""))).strip()
    if name:
        return Player.query.filter_by(name=name).first()

    return None


def _parse_pct(val) -> float:
    """Parse a FanGraphs percentage value like '12.5%' or 0.125 → 0.125."""
    if val is None:
        return 0.0
    s = str(val).replace("%", "").strip()
    try:
        f = float(s)
        return f / 100 if f > 1.5 else f  # already decimal if <= 1.5
    except (ValueError, TypeError):
        return 0.0


def _rates_from_dashboard(row: dict, pa: int) -> dict:
    """
    Derive per-PA outcome rates from FanGraphs Dashboard batting export.
    Dashboard columns available: BB%, K%, ISO, AVG, OBP, SLG, HR, PA.
    """
    if pa <= 0:
        return {}

    hr_count = float(row.get("HR", 0) or 0)
    avg = float(row.get("AVG", 0) or 0)
    obp = float(row.get("OBP", 0) or 0)
    slg = float(row.get("SLG", 0) or 0)
    bb_rate = _parse_pct(row.get("BB%", 0))
    k_rate = _parse_pct(row.get("K%", 0))

    hr_rate = hr_count / pa

    # Estimate AB (plate appearances minus walks/HBP)
    ab_est = pa * (1 - bb_rate)
    if ab_est <= 0:
        ab_est = pa

    # ISO = SLG - AVG = (extra bases) / AB
    # We can decompose:  ISO = (2B*1 + 3B*2 + HR*3) / AB
    # HR contribution to ISO: hr_per_ab * 3
    hr_per_ab = hr_count / ab_est if ab_est > 0 else 0
    xbh_iso = max(0, (slg - avg) - hr_per_ab * 3)  # ISO from 2B + 3B only
    # Assume 2B ≈ 90% of non-HR XBH, 3B ≈ 10%
    double_rate = (xbh_iso * 0.90) * (ab_est / pa)
    triple_rate = (xbh_iso * 0.10) * (ab_est / pa)

    # Total hits per PA = AVG * (AB/PA)
    hits_per_pa = avg * (ab_est / pa)
    # Subtract HR, 2B, 3B to get singles
    single_rate = max(0, hits_per_pa - hr_rate - double_rate - triple_rate)

    out_rate = max(0, 1.0 - single_rate - double_rate - triple_rate - hr_rate - bb_rate - k_rate)

    return {
        "single_rate": round(single_rate, 5),
        "double_rate": round(double_rate, 5),
        "triple_rate": round(triple_rate, 5),
        "hr_rate": round(hr_rate, 5),
        "walk_rate": round(bb_rate, 5),
        "strikeout_rate": round(k_rate, 5),
        "out_rate": round(out_rate, 5),
    }


def _rates_from_standard(row: dict, pa: int) -> dict:
    """
    Derive per-PA outcome rates from FanGraphs Standard/Splits batting export.
    Has raw counts: 1B, 2B, 3B, HR, BB, IBB, SO, HBP.
    """
    if pa <= 0:
        return {}

    single  = float(row.get("1B", 0) or 0)
    double  = float(row.get("2B", 0) or 0)
    triple  = float(row.get("3B", 0) or 0)
    hr      = float(row.get("HR", 0) or 0)
    bb      = float(row.get("BB", 0) or 0)
    ibb     = float(row.get("IBB", 0) or 0)
    hbp     = float(row.get("HBP", 0) or 0)
    so      = float(row.get("SO", 0) or 0)

    walk_total = bb + hbp  # include HBP as a "free base"
    out_count  = max(0, pa - single - double - triple - hr - walk_total - so)

    return {
        "single_rate":    round(single / pa, 5),
        "double_rate":    round(double / pa, 5),
        "triple_rate":    round(triple / pa, 5),
        "hr_rate":        round(hr / pa, 5),
        "walk_rate":      round(walk_total / pa, 5),
        "strikeout_rate": round(so / pa, 5),
        "out_rate":       round(out_count / pa, 5),
    }


def _find_csv(exports_dir: str, season: int, filename: str) -> str:
    """
    Look for a CSV in exports/{season}/ first, then fall back to exports/.
    Returns the full path if found, else None.
    """
    season_path = os.path.join(exports_dir, str(season), filename)
    if os.path.exists(season_path):
        return season_path
    legacy_path = os.path.join(exports_dir, filename)
    if os.path.exists(legacy_path):
        return legacy_path
    return None


def import_fangraphs_batting(season: int = None):
    """Import all FanGraphs batting CSVs from exports/{season}/ folder."""
    if season is None:
        season = date.today().year

    exports_dir = os.path.join(os.path.dirname(__file__), "exports")
    files = {
        "overall": f"batting_overall_{season}.csv",
        "vs_LHP":  f"batting_vs_lhp_{season}.csv",
        "vs_RHP":  f"batting_vs_rhp_{season}.csv",
    }

    for split, filename in files.items():
        filepath = _find_csv(exports_dir, season, filename)
        if not filepath:
            logger.warning(f"Not found: exports/{season}/{filename} — skipping {split} split")
            continue

        df = pd.read_csv(filepath)
        logger.info(f"Loaded {len(df)} rows from {filename}")

        # Detect format: Dashboard has BB%, Standard has 1B column
        is_dashboard = "BB%" in df.columns
        has_raw_counts = "1B" in df.columns

        imported = 0
        for _, raw_row in df.iterrows():
            row = raw_row.to_dict()
            player = _find_player(row)
            if not player:
                continue

            pa = int(float(row.get("PA", 0) or 0))
            ab = int(float(row.get("AB", 0) or 0))

            if is_dashboard:
                rates = _rates_from_dashboard(row, pa)
            elif has_raw_counts:
                rates = _rates_from_standard(row, pa)
            else:
                rates = {}

            if not rates:
                continue

            # Delete existing record for same season/split
            PlayerStats.query.filter_by(
                player_id=player.id, season=season, split=split
            ).delete()

            def _f(key, default=0.0):
                v = row.get(key, default)
                try:
                    return float(v) if v not in (None, "", "nan") else default
                except (ValueError, TypeError):
                    return default

            stats = PlayerStats(
                player_id=player.id, season=season, split=split,
                pa=pa, ab=ab,
                single_rate=rates.get("single_rate", 0),
                double_rate=rates.get("double_rate", 0),
                triple_rate=rates.get("triple_rate", 0),
                hr_rate=rates.get("hr_rate", 0),
                walk_rate=rates.get("walk_rate", 0),
                strikeout_rate=rates.get("strikeout_rate", 0),
                out_rate=rates.get("out_rate", 0),
                woba=_f("wOBA") or None,
                wrc_plus=int(_f("wRC+")) or None,
                babip=_f("BABIP") or None,
                avg=_f("AVG") or None,
                obp=_f("OBP") or None,
                slg=_f("SLG") or None,
            )
            db.session.add(stats)
            imported += 1

        db.session.commit()
        logger.info(f"Imported {imported} batting records for split: {split}")


def _pitcher_rates_from_dashboard(row: dict, ip: float) -> dict:
    """
    Derive per-batter-faced rates from Dashboard pitching format.
    Dashboard has: K/9, BB/9, HR/9, BABIP, GB%, ERA, FIP, xFIP, IP.
    Approximate batters faced: IP * 4.3 (league average BF/IP ≈ 4.3)
    """
    if ip <= 0:
        return {}

    bf_est = ip * 4.3
    k_per_9  = float(row.get("K/9", 0) or 0)
    bb_per_9 = float(row.get("BB/9", 0) or 0)
    hr_per_9 = float(row.get("HR/9", 0) or 0)

    k_rate  = k_per_9  / 27  # 27 outs per 9 innings → per PA approximation
    bb_rate = bb_per_9 / 27
    hr_rate = hr_per_9 / 27

    babip   = float(row.get("BABIP", 0.300) or 0.300)
    gb_rate = _parse_pct(row.get("GB%", 0.45))

    # Hits in play per PA = BABIP * (1 - k_rate - hr_rate)  (approx)
    bip_rate = max(0, 1 - k_rate - bb_rate - hr_rate)
    hits_in_play = babip * bip_rate

    # Distribute hits in play using league average ratios
    # ~72% singles, ~25% doubles, ~3% triples of all BIP hits
    single_rate  = hits_in_play * 0.72
    double_rate  = hits_in_play * 0.25
    triple_rate  = hits_in_play * 0.03
    out_rate     = max(0, 1 - single_rate - double_rate - triple_rate - hr_rate - bb_rate - k_rate)

    return {
        "single_rate_allowed":  round(single_rate, 5),
        "double_rate_allowed":  round(double_rate, 5),
        "triple_rate_allowed":  round(triple_rate, 5),
        "hr_rate_allowed":      round(hr_rate, 5),
        "walk_rate_allowed":    round(bb_rate, 5),
        "strikeout_rate":       round(k_rate, 5),
        "out_rate":             round(out_rate, 5),
    }


def _pitcher_rates_from_splits(row: dict) -> dict:
    """
    Derive per-BF rates from FanGraphs pitching splits format.
    Splits have: TBF, HR, BB, IBB, HBP, SO, H, 2B, 3B.
    """
    tbf = float(row.get("TBF", 0) or 0)
    if tbf <= 0:
        return {}

    hr  = float(row.get("HR", 0) or 0)
    bb  = float(row.get("BB", 0) or 0)
    ibb = float(row.get("IBB", 0) or 0)
    hbp = float(row.get("HBP", 0) or 0)
    so  = float(row.get("SO", 0) or 0)
    h   = float(row.get("H",  0) or 0)
    d2  = float(row.get("2B", 0) or 0)
    d3  = float(row.get("3B", 0) or 0)
    singles = max(0, h - d2 - d3 - hr)

    walk_total = bb + hbp
    out_count  = max(0, tbf - singles - d2 - d3 - hr - walk_total - so)

    return {
        "single_rate_allowed":  round(singles / tbf, 5),
        "double_rate_allowed":  round(d2 / tbf, 5),
        "triple_rate_allowed":  round(d3 / tbf, 5),
        "hr_rate_allowed":      round(hr / tbf, 5),
        "walk_rate_allowed":    round(walk_total / tbf, 5),
        "strikeout_rate":       round(so / tbf, 5),
        "out_rate":             round(out_count / tbf, 5),
    }


def import_fangraphs_pitching(season: int = None):
    """Import all FanGraphs pitching CSVs from exports/{season}/ folder."""
    if season is None:
        season = date.today().year

    exports_dir = os.path.join(os.path.dirname(__file__), "exports")
    files = {
        ("SP", "overall"): f"pitching_sp_overall_{season}.csv",
        ("RP", "overall"): f"pitching_rp_overall_{season}.csv",
        ("SP", "vs_LHB"):  f"pitching_sp_vs_lhb_{season}.csv",
        ("SP", "vs_RHB"):  f"pitching_sp_vs_rhb_{season}.csv",
    }

    for (role, split), filename in files.items():
        filepath = _find_csv(exports_dir, season, filename)
        if not filepath:
            logger.warning(f"Not found: exports/{season}/{filename} — skipping")
            continue

        df = pd.read_csv(filepath)
        logger.info(f"Loaded {len(df)} rows from {filename}")

        # Detect format
        is_dashboard = "K/9" in df.columns
        has_tbf      = "TBF" in df.columns

        imported = 0
        for _, raw_row in df.iterrows():
            row = raw_row.to_dict()
            player = _find_player(row)
            if not player:
                continue

            def _f(key, default=0.0):
                v = row.get(key, default)
                try:
                    return float(v) if v not in (None, "", "nan") else default
                except (ValueError, TypeError):
                    return default

            ip = _f("IP")
            # Splits exports don't include an IP column — estimate from TBF (BF ≈ IP × 4.3)
            if ip <= 0 and has_tbf:
                tbf = float(row.get("TBF", 0) or 0)
                ip = round(tbf / 4.3, 1)

            if is_dashboard:
                rates = _pitcher_rates_from_dashboard(row, ip)
            elif has_tbf:
                rates = _pitcher_rates_from_splits(row)
            else:
                rates = {}

            if not rates:
                continue

            PitchingStats.query.filter_by(
                player_id=player.id, season=season, role=role, split=split
            ).delete()

            # GB% cleanup
            gb_raw = row.get("GB%", 0)
            try:
                gb = _parse_pct(gb_raw) if gb_raw else None
            except Exception:
                gb = None

            ps = PitchingStats(
                player_id=player.id, season=season, role=role, split=split,
                ip=ip,
                games=int(_f("G")),
                games_started=int(_f("GS")),
                single_rate_allowed=rates.get("single_rate_allowed", 0),
                double_rate_allowed=rates.get("double_rate_allowed", 0),
                triple_rate_allowed=rates.get("triple_rate_allowed", 0),
                hr_rate_allowed=rates.get("hr_rate_allowed", 0),
                walk_rate_allowed=rates.get("walk_rate_allowed", 0),
                strikeout_rate=rates.get("strikeout_rate", 0),
                out_rate=rates.get("out_rate", 0),
                era=_f("ERA") or None,
                fip=_f("FIP") or None,
                xfip=_f("xFIP") or None,
                k_per_9=_f("K/9") or None,
                bb_per_9=_f("BB/9") or None,
                hr_per_9=_f("HR/9") or None,
                gb_rate=gb,
            )
            db.session.add(ps)
            imported += 1

        db.session.commit()
        logger.info(f"Imported {imported} pitching records for {role} {split}")


def seed_roster_from_mlb_api():
    """Pull rosters from MLB Stats API and add unknown players to DB."""
    teams = Team.query.all()
    added = 0
    for team in teams:
        if not team.mlb_id:
            continue
        try:
            roster_str = get_team_roster(team.mlb_id)
            # statsapi returns a string; parse it
            import statsapi
            roster_data = statsapi.get("team_roster", {"teamId": team.mlb_id, "rosterType": "40Man"})
            roster_entries = roster_data.get("roster", [])
            for entry in roster_entries:
                person = entry.get("person", {})
                mlb_id = person.get("id")
                name = person.get("fullName", "")
                pos = entry.get("position", {}).get("abbreviation", "")
                status = entry.get("status", {}).get("code", "A")

                if Player.query.filter_by(mlb_id=mlb_id).first():
                    continue

                player = Player(
                    mlb_id=mlb_id, name=name, team_id=team.id,
                    position=pos, active=(status == "A"),
                )
                db.session.add(player)
                added += 1
        except Exception as e:
            logger.warning(f"Could not fetch roster for {team.name}: {e}")

    db.session.commit()
    logger.info(f"Added {added} new players from MLB API rosters.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Import FanGraphs stats and roster data.")
    parser.add_argument(
        "--season", "-s",
        type=int,
        default=date.today().year,
        help="Season year to import (default: current year). CSVs must be in exports/{season}/",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Skip roster sync — only import FanGraphs CSVs (faster for mid-season updates).",
    )
    args = parser.parse_args()
    season = args.season

    logger.info(f"Starting data import for season {season}...")
    logger.info(f"Looking for CSVs in: exports/{season}/")

    with app.app_context():
        init_db()
        if not args.stats_only:
            logger.info("1/4 Seeding teams and ballparks...")
            seed_teams_and_parks()
            logger.info("2/4 Fetching rosters from MLB API...")
            seed_roster_from_mlb_api()
        else:
            logger.info("(--stats-only: skipping roster sync)")
        logger.info("3/4 Importing FanGraphs batting data...")
        import_fangraphs_batting(season)
        logger.info("4/4 Importing FanGraphs pitching data...")
        import_fangraphs_pitching(season)
        logger.info(f"Done! Season {season} stats imported.")
