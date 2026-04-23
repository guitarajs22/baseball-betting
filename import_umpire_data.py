"""
import_umpire_data.py — Download and import MLB umpire tendency stats.

Data source: SCORE Network (data.scorenetwork.org)
  Cumulative 2008–2024 home-plate umpire stats derived from game logs.
  Covers 156 umpires. Updated periodically by the SCORE project.

What it imports per umpire:
  walk_rate_impact     — fractional walk rate deviation (+0.08 = 8% more walks)
  k_rate_impact        — fractional K rate deviation    (+0.16 = 16% more Ks)
  runs_per_game_impact — absolute RPG deviation from mean (e.g. -0.32 = 0.32 fewer R/G)
  games_tracked        — number of games in the dataset for this umpire

Usage:
    python import_umpire_data.py
    python import_umpire_data.py --dry-run      # preview without writing to DB
    python import_umpire_data.py --force         # overwrite even if recently updated
"""

import argparse
import csv
import io
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_URL = "https://data.scorenetwork.org/data/mlb_umpires.csv"

# ---------------------------------------------------------------------------
# Fuzzy name matching helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    import re
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _best_match(target: str, candidates: list[str]) -> tuple[str, int]:
    """
    Return (best_candidate, match_score) using simple token overlap.
    Higher score = better match.
    """
    t_tokens = set(_normalize(target).split())
    best, best_score = "", 0
    for c in candidates:
        c_tokens = set(_normalize(c).split())
        score = len(t_tokens & c_tokens)
        if score > best_score:
            best, best_score = c, score
    return best, best_score


# ---------------------------------------------------------------------------
# Download + parse CSV
# ---------------------------------------------------------------------------

def _fetch_umpire_csv() -> dict:
    """
    Download the SCORE Network CSV and return a dict of:
      { umpire_name: { 'k_rate_impact': float, 'walk_rate_impact': float,
                       'runs_per_game_impact': float, 'games': int } }
    """
    logger.info(f"Fetching umpire data from {DATA_URL} ...")
    try:
        with urllib.request.urlopen(DATA_URL, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        sys.exit(1)

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    logger.info(f"  Downloaded {len(rows)} rows")

    # Collect per-umpire base stats and boost values
    umpire_base: dict[str, dict] = {}
    umpire_boosts: dict[str, dict] = {}

    for row in rows:
        name = row["Umpire"].strip()
        if name not in umpire_base:
            umpire_base[name] = {
                "games":   int(float(row["Games"])),
                "rpg":     float(row["RPG"]),
                "k_pct":   float(row["k_pct"]),
                "bb_pct":  float(row["bb_pct"]),
            }
            umpire_boosts[name] = {}

        stat = row["boost_stat"].strip().upper()
        try:
            umpire_boosts[name][stat] = float(row["boost_pct"])
        except ValueError:
            pass

    # Compute the average RPG across all umpires (our league-average baseline)
    avg_rpg = sum(v["rpg"] for v in umpire_base.values()) / len(umpire_base)
    logger.info(f"  {len(umpire_base)} unique umpires, avg RPG = {avg_rpg:.2f}")

    # Build final dict
    result = {}
    for name, base in umpire_base.items():
        boosts = umpire_boosts[name]
        result[name] = {
            "walk_rate_impact":     round(boosts.get("BB", 0.0) / 100.0, 4),
            "k_rate_impact":        round(boosts.get("K",  0.0) / 100.0, 4),
            "runs_per_game_impact": round(base["rpg"] - avg_rpg,         3),
            "games_tracked":        base["games"],
        }

    return result


# ---------------------------------------------------------------------------
# Main import
# ---------------------------------------------------------------------------

def import_umpires(dry_run: bool = False, force: bool = False) -> None:
    from app import app, db, init_db
    from database.schema import Umpire

    csv_data = _fetch_umpire_csv()

    with app.app_context():
        init_db()

        db_umpires = Umpire.query.all()
        db_names   = [u.name for u in db_umpires]
        db_by_name = {u.name: u for u in db_umpires}

        updated = skipped = new_found = not_found = 0
        stale_cutoff = datetime.utcnow() - timedelta(days=30)

        logger.info(f"\nMatching {len(csv_data)} CSV umpires → {len(db_umpires)} DB umpires ...")
        logger.info("-" * 60)

        for csv_name, stats in sorted(csv_data.items()):
            # Try exact match first
            if csv_name in db_by_name:
                ump = db_by_name[csv_name]
            else:
                # Fuzzy match
                best, score = _best_match(csv_name, db_names)
                if score >= 2:
                    ump = db_by_name[best]
                    logger.info(f"  Fuzzy match: '{csv_name}' → '{best}' (score={score})")
                else:
                    # Not in DB at all — create a neutral entry so it can be assigned
                    logger.info(f"  NEW umpire (not in DB): {csv_name}")
                    new_found += 1
                    if not dry_run:
                        ump = Umpire(name=csv_name)
                        db.session.add(ump)
                        db.session.flush()  # get an id
                    else:
                        continue

            # Skip recently updated unless --force
            if not force and ump.updated_at and ump.updated_at > stale_cutoff:
                skipped += 1
                continue

            if not dry_run:
                ump.walk_rate_impact     = stats["walk_rate_impact"]
                ump.k_rate_impact        = stats["k_rate_impact"]
                ump.runs_per_game_impact = stats["runs_per_game_impact"]
                ump.games_tracked        = stats["games_tracked"]
                ump.updated_at           = datetime.utcnow()
            updated += 1

            logger.info(
                f"  {'[DRY]' if dry_run else 'OK   '} {ump.name:<30}"
                f"  walk={stats['walk_rate_impact']:+.3f}"
                f"  k={stats['k_rate_impact']:+.3f}"
                f"  rpg={stats['runs_per_game_impact']:+.3f}"
                f"  ({stats['games_tracked']} games)"
            )

        # Umpires in DB but not in CSV — log but leave untouched
        csv_normalized = {_normalize(n) for n in csv_data}
        for db_name in db_names:
            if _normalize(db_name) not in csv_normalized:
                # Quick fuzzy check
                _, score = _best_match(db_name, list(csv_data.keys()))
                if score < 2:
                    logger.info(f"  NO CSV DATA for DB umpire: {db_name} — keeping existing values")
                    not_found += 1

        if not dry_run:
            db.session.commit()

        logger.info("-" * 60)
        logger.info(f"  Updated: {updated}  |  New: {new_found}  |  Skipped (recent): {skipped}  |  No CSV match: {not_found}")
        if dry_run:
            logger.info("  DRY RUN — no changes written to DB")
        else:
            logger.info("  Done. Umpire tendency data is now current.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import MLB umpire tendency stats from the SCORE Network dataset."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing to the database."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite data even if the record was updated in the last 30 days."
    )
    args = parser.parse_args()

    import_umpires(dry_run=args.dry_run, force=args.force)
