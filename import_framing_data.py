"""
import_framing_data.py — Download and import MLB catcher pitch-framing stats.

Data source: Baseball Savant catcher-framing leaderboard (direct CSV)
  Shadow-zone called strike rate — the industry-standard framing metric.
  The "shadow zone" is the band of pitches just outside the strike zone
  where catcher framing has the most impact on ball/strike calls.

What it imports per catcher per season:
  shadow_csr      — Called strike rate in the shadow zone (e.g. 0.491)
  csr_diff        — shadow_csr minus league avg (~0.466)
  shadow_pitches  — sample-size column ("pitches" in the CSV)
  runs_saved      — Savant's rv_tot value (run value of framing)

Usage:
    python import_framing_data.py                 # Imports current + prior season
    python import_framing_data.py --year 2024     # Only a specific year
    python import_framing_data.py --dry-run       # Preview without writing to DB
    python import_framing_data.py --force         # Overwrite recent records

Notes:
  - Early in a season there's not much framing data; script falls back to
    prior year via the app's `_load_catcher_factors()` helper at sim time.
  - Re-run weekly during the season; csr_diff stabilizes quickly but runs_saved
    accumulates.
  - Matching is by MLB player_id (unambiguous) with a last-name/first-name
    fallback for players whose mlb_id isn't populated in the DB yet.
"""

import argparse
import csv
import io
import logging
import sys
import urllib.request
from datetime import datetime, timedelta, date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Savant CSV endpoint. min=0 to get everyone who caught even one shadow pitch;
# we filter by shadow_pitches later in the script for quality control.
# NOTE: the season filter uses seasonStart/seasonEnd (NOT `year`) — an earlier
# version of this script used `year=` which was silently ignored by Savant.
SAVANT_CSV_URL = (
    "https://baseballsavant.mlb.com/leaderboard/catcher-framing"
    "?type=catcher&seasonStart={year}&seasonEnd={year}&team=&min=0"
    "&sortColumn=rv_tot&sortDirection=desc&csv=true"
)

# League-average shadow-zone CSR — used to compute csr_diff.
# 2024 MLB actual across all catchers. Stable to within ~0.002 year over year.
LEAGUE_AVG_SHADOW_CSR = 0.466

# Minimum sample size before a catcher's framing is considered stable enough
# to store. Below this, the noise dominates and league-average is better.
MIN_SHADOW_PITCHES = 200


def _fetch_catcher_framing(year: int):
    """Download Savant's catcher-framing CSV and return parsed rows."""
    url = SAVANT_CSV_URL.format(year=year)
    logger.info(f"Fetching {year} catcher framing from Baseball Savant...")
    # Savant blocks urllib's default UA — send a browser-style UA string.
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/csv,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8-sig")  # strip BOM if present
    except Exception as e:
        logger.error(f"Savant fetch failed for {year}: {e}")
        return None

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        logger.warning(f"  No rows returned for {year}")
        return None

    # Parse all valid rows first so we can compute a self-consistent
    # league-average CSR for THIS year. The baseline drifts year-over-year
    # (Savant has adjusted the shadow-zone definition / called-strike model),
    # so a hardcoded constant would put csr_diff off-center.
    parsed = []
    for row in rows:
        try:
            mlb_id         = int(row.get("id") or 0)
            name_raw       = (row.get("name") or "").strip().strip('"')
            shadow_pitches = int(row.get("pitches") or 0)
            shadow_csr     = float(row.get("pct_tot") or 0.0)
            runs_saved     = float(row.get("rv_tot") or 0.0)
        except (ValueError, TypeError):
            continue

        if not mlb_id or not name_raw or shadow_pitches < MIN_SHADOW_PITCHES:
            continue

        # "Last, First" → "First Last"
        if "," in name_raw:
            last, first = [p.strip() for p in name_raw.split(",", 1)]
            name = f"{first} {last}"
        else:
            name = name_raw

        parsed.append({
            "mlb_id":         mlb_id,
            "name":           name,
            "shadow_pitches": shadow_pitches,
            "shadow_csr":     shadow_csr,
            "runs_saved":     runs_saved,
        })

    # Weighted league-average CSR for THIS season (weighted by shadow pitches).
    # Fall back to the hardcoded constant if data is sparse (<1000 pitches total).
    total_pitches = sum(p["shadow_pitches"] for p in parsed)
    if total_pitches >= 1000:
        league_avg = sum(p["shadow_csr"] * p["shadow_pitches"] for p in parsed) / total_pitches
        logger.info(f"  Computed {year} league-avg shadow CSR = {league_avg:.4f} "
                    f"(from {len(parsed)} catchers, {total_pitches:,} pitches)")
    else:
        league_avg = LEAGUE_AVG_SHADOW_CSR
        logger.info(f"  Using hardcoded league-avg shadow CSR = {league_avg:.4f} "
                    f"(insufficient data: {total_pitches} pitches)")

    results = []
    for p in parsed:
        results.append({
            "mlb_id":         p["mlb_id"],
            "name":           p["name"],
            "shadow_pitches": p["shadow_pitches"],
            "shadow_csr":     round(p["shadow_csr"], 4),
            "csr_diff":       round(p["shadow_csr"] - league_avg, 4),
            "runs_saved":     round(p["runs_saved"], 3),
        })

    logger.info(f"  Retrieved {len(results)} catchers with ≥{MIN_SHADOW_PITCHES} shadow pitches")
    return results


def import_catcher_framing(years=None, dry_run=False, force=False) -> None:
    from app import app, db, init_db
    from database.schema import Player, CatcherFraming

    if years is None:
        # Default: current season + last completed season
        current = date.today().year
        years = [current, current - 1]

    with app.app_context():
        init_db()

        # Build lookup by mlb_id and last-name fallback
        all_players = Player.query.all()
        by_mlb_id = {p.mlb_id: p for p in all_players if p.mlb_id}
        # Build last-name, first-name fallback for players missing mlb_id
        by_name_lower = {}
        for p in all_players:
            if p.name:
                by_name_lower.setdefault(p.name.lower(), p)

        stale_cutoff = datetime.utcnow() - timedelta(days=7)
        total_updated = 0
        total_skipped = 0
        total_missed  = 0
        total_created_players = 0

        for year in years:
            data = _fetch_catcher_framing(year)
            if not data:
                logger.warning(f"  Skipping {year} — no data available")
                continue

            logger.info(f"\nApplying {year} catcher framing data...")
            logger.info("-" * 78)

            for row in data:
                # Match by MLB id first (unambiguous)
                player = by_mlb_id.get(row["mlb_id"])

                # Fallback: case-insensitive name match
                if not player:
                    player = by_name_lower.get(row["name"].lower())

                # If the catcher isn't in our Player table at all, create a
                # minimal record — they likely weren't caught by the roster
                # seed (e.g. a mid-season call-up).
                if not player:
                    if dry_run:
                        logger.info(f"  [DRY] Would create player: {row['name']} "
                                    f"(mlb_id={row['mlb_id']})")
                        total_missed += 1
                        continue
                    player = Player(
                        mlb_id=row["mlb_id"],
                        name=row["name"],
                        position="C",
                        active=True,
                    )
                    db.session.add(player)
                    db.session.flush()  # get an id
                    by_mlb_id[row["mlb_id"]] = player
                    by_name_lower[row["name"].lower()] = player
                    total_created_players += 1

                cf = CatcherFraming.query.filter_by(
                    player_id=player.id, season=year
                ).first()

                if cf and not force and cf.updated_at and cf.updated_at > stale_cutoff:
                    total_skipped += 1
                    continue

                if not cf:
                    cf = CatcherFraming(player_id=player.id, season=year)
                    if not dry_run:
                        db.session.add(cf)

                cf.shadow_csr     = row["shadow_csr"]
                cf.csr_diff       = row["csr_diff"]
                cf.shadow_pitches = row["shadow_pitches"]
                cf.runs_saved     = row["runs_saved"]
                cf.updated_at     = datetime.utcnow()

                total_updated += 1
                logger.info(
                    f"  {'[DRY]' if dry_run else 'OK   '} "
                    f"{year}  {player.name:<26}  "
                    f"CSR={row['shadow_csr']:.3f}  "
                    f"diff={row['csr_diff']:+.3f}  "
                    f"runs={row['runs_saved']:+6.2f}  "
                    f"({row['shadow_pitches']} shadow pitches)"
                )

            if not dry_run:
                db.session.commit()

        logger.info("-" * 78)
        logger.info(f"  Updated: {total_updated}  |  "
                    f"Skipped (recent): {total_skipped}  |  "
                    f"New players created: {total_created_players}  |  "
                    f"Missed (dry-run): {total_missed}")
        if dry_run:
            logger.info("  DRY RUN — no changes written to DB")
        else:
            logger.info("  Done. Catcher framing data is now current.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import MLB catcher pitch-framing stats from Baseball Savant."
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

    import_catcher_framing(
        years=args.year,
        dry_run=args.dry_run,
        force=args.force,
    )
