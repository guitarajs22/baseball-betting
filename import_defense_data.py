"""
import_defense_data.py — Download and import team-level defensive stats.

Data source: Baseball Savant (Statcast) via pybaseball
  Team-level Outs Above Average (OAA) — the gold-standard fielding metric.
  Captures fielders' range, arm, and positioning relative to expected outs.

What it imports per team per season:
  oaa           — Total Outs Above Average (positive = better defense)
  oaa_rhh       — OAA vs. right-handed hitters
  oaa_lhh       — OAA vs. left-handed hitters
  success_rate  — Actual fielding success rate on tracked plays

Usage:
    python import_defense_data.py                 # Imports current + prior season
    python import_defense_data.py --year 2024     # Only a specific year
    python import_defense_data.py --dry-run       # Preview without writing to DB
    python import_defense_data.py --force         # Overwrite recent records

Notes:
  - Early in a season there's not much OAA data; script falls back to prior year.
  - OAA is cumulative, so it's reasonable to re-run weekly during the season.
  - Team name matching is by MLB team_id (reliable) with an abbreviation fallback.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# Map Savant "team_name" (short form) to MLB abbreviation for fallback matching.
# Primary matching is always by team_id (MLB ID), which is unambiguous.
SAVANT_NAME_TO_ABBREV = {
    "Angels": "LAA", "Astros": "HOU", "Athletics": "OAK", "Blue Jays": "TOR",
    "Braves": "ATL", "Brewers": "MIL", "Cardinals": "STL", "Cubs": "CHC",
    "D-backs": "ARI", "Dodgers": "LAD", "Giants": "SF", "Guardians": "CLE",
    "Mariners": "SEA", "Marlins": "MIA", "Mets": "NYM", "Nationals": "WSH",
    "Orioles": "BAL", "Padres": "SD", "Phillies": "PHI", "Pirates": "PIT",
    "Rangers": "TEX", "Rays": "TB", "Red Sox": "BOS", "Reds": "CIN",
    "Rockies": "COL", "Royals": "KC", "Tigers": "DET", "Twins": "MIN",
    "White Sox": "CWS", "Yankees": "NYY",
}


def _fetch_team_oaa(year: int):
    """Fetch team-level OAA from Baseball Savant via pybaseball."""
    try:
        import pybaseball as pb
    except ImportError:
        logger.error("pybaseball not installed. Run: pip install pybaseball")
        sys.exit(1)

    logger.info(f"Fetching {year} team-level OAA from Baseball Savant...")
    try:
        df = pb.statcast_outs_above_average(
            year=year, pos="all", min_att=1, view="Fielding_Team"
        )
    except Exception as e:
        logger.error(f"Savant fetch failed for {year}: {e}")
        return None

    if df is None or df.empty:
        logger.warning(f"No data returned for {year}")
        return None

    # Parse success-rate string ("79%") to float (0.79)
    def _parse_pct(s):
        try:
            return round(float(str(s).rstrip("%")) / 100.0, 4)
        except (ValueError, TypeError):
            return None

    results = []
    for _, row in df.iterrows():
        results.append({
            "team_name": row.get("team_name", ""),
            "team_mlb_id": int(row.get("team_id", 0)),
            "oaa":     float(row.get("outs_above_average", 0) or 0),
            "oaa_rhh": float(row.get("outs_above_average_rhh", 0) or 0),
            "oaa_lhh": float(row.get("outs_above_average_lhh", 0) or 0),
            "success_rate": _parse_pct(row.get("actual_success_rate_formatted", "")),
        })

    logger.info(f"  Retrieved {len(results)} teams")
    return results


def import_team_defense(years=None, dry_run=False, force=False) -> None:
    from app import app, db, init_db
    from database.schema import Team, TeamDefense

    if years is None:
        # Default: current season + last completed season
        current = date.today().year
        years = [current, current - 1]

    with app.app_context():
        init_db()

        # Build team lookup by MLB id and by abbreviation
        teams = Team.query.all()
        by_mlb_id = {t.mlb_id: t for t in teams if t.mlb_id}
        by_abbrev = {t.abbreviation.upper(): t for t in teams if t.abbreviation}

        stale_cutoff = datetime.utcnow() - timedelta(days=7)
        total_updated = 0
        total_skipped = 0
        total_missed  = 0

        for year in years:
            data = _fetch_team_oaa(year)
            if not data:
                logger.warning(f"  Skipping {year} — no data available")
                continue

            logger.info(f"\nApplying {year} defense data...")
            logger.info("-" * 72)

            for row in data:
                # Match by MLB team_id first (unambiguous)
                team = by_mlb_id.get(row["team_mlb_id"])
                # Fall back to abbreviation via the name map
                if not team:
                    abbrev = SAVANT_NAME_TO_ABBREV.get(row["team_name"])
                    if abbrev:
                        team = by_abbrev.get(abbrev.upper())

                if not team:
                    logger.warning(f"  MISS  {row['team_name']} "
                                   f"(mlb_id={row['team_mlb_id']}) — no matching DB team")
                    total_missed += 1
                    continue

                td = TeamDefense.query.filter_by(team_id=team.id, season=year).first()

                if td and not force and td.updated_at and td.updated_at > stale_cutoff:
                    total_skipped += 1
                    continue

                if not td:
                    td = TeamDefense(team_id=team.id, season=year)
                    if not dry_run:
                        db.session.add(td)

                td.oaa = row["oaa"]
                td.oaa_rhh = row["oaa_rhh"]
                td.oaa_lhh = row["oaa_lhh"]
                td.success_rate = row["success_rate"]
                td.updated_at = datetime.utcnow()

                total_updated += 1
                logger.info(
                    f"  {'[DRY]' if dry_run else 'OK   '} "
                    f"{year}  {team.abbreviation:<4} {team.name:<25}  "
                    f"OAA={row['oaa']:+6.1f}  (RHH={row['oaa_rhh']:+5.1f}, "
                    f"LHH={row['oaa_lhh']:+5.1f})  success={row['success_rate']}"
                )

            if not dry_run:
                db.session.commit()

        logger.info("-" * 72)
        logger.info(f"  Updated: {total_updated}  |  "
                    f"Skipped (recent): {total_skipped}  |  "
                    f"No DB match: {total_missed}")
        if dry_run:
            logger.info("  DRY RUN — no changes written to DB")
        else:
            logger.info("  Done. Team defense data is now current.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import team-level Outs Above Average from Baseball Savant."
    )
    parser.add_argument(
        "--year", type=int, action="append",
        help="Specific year(s) to import. May be passed multiple times. "
             "Default: current season + prior season."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing to the database."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite data even if the record was updated in the last 7 days."
    )
    args = parser.parse_args()

    import_team_defense(
        years=args.year,
        dry_run=args.dry_run,
        force=args.force,
    )
