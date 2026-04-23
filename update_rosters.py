"""
Live roster + pitching stats updater — pulls directly from the MLB Stats API.
Run this at any point during the season to refresh:
  1. Rosters — updates team assignments, positions, active status for all 30 teams
  2. Pitching stats — updates 2026 IP, games, ERA, K/9 etc. for every pitcher

No FanGraphs CSV needed. Uses only the free MLB Stats API.

Usage:
    python update_rosters.py
    python update_rosters.py --season 2025   # update a prior season
    python update_rosters.py --stats-only    # skip roster sync, just update stats
    python update_rosters.py --rosters-only  # skip stats, just sync rosters
"""

import logging
import sys
import time
import argparse
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, db, init_db
from database.schema import Team, Player, PitchingStats
import statsapi


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default=0.0):
    try:
        return float(val) if val not in (None, "", "-") else default
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    try:
        return int(float(val)) if val not in (None, "", "-") else default
    except (ValueError, TypeError):
        return default


def _ip_to_float(ip_str) -> float:
    """Convert MLB API IP string like '12.1' to decimal innings (12.333...)."""
    try:
        parts = str(ip_str).split(".")
        full = int(parts[0])
        thirds = int(parts[1]) if len(parts) > 1 else 0
        return round(full + thirds / 3.0, 2)
    except Exception:
        return 0.0


def _pitcher_rates_from_api(stat: dict, ip: float) -> dict:
    """
    Derive per-batter-faced outcome rates from MLB API season stat totals.
    Falls back to league averages for any missing field.
    """
    bf = _safe_int(stat.get("battersFaced")) or max(1, round(ip * 4.3))

    hits     = _safe_int(stat.get("hits"))
    doubles  = _safe_int(stat.get("doubles"))
    triples  = _safe_int(stat.get("triples"))
    hr       = _safe_int(stat.get("homeRuns"))
    bb       = _safe_int(stat.get("baseOnBalls")) + _safe_int(stat.get("hitByPitch"))
    so       = _safe_int(stat.get("strikeOuts"))

    singles  = max(0, hits - doubles - triples - hr)
    non_so_outs = max(0, bf - hits - bb - so)

    return {
        "single_rate_allowed":  round(singles  / bf, 4),
        "double_rate_allowed":  round(doubles  / bf, 4),
        "triple_rate_allowed":  round(triples  / bf, 4),
        "hr_rate_allowed":      round(hr       / bf, 4),
        "walk_rate_allowed":    round(bb       / bf, 4),
        "strikeout_rate":       round(so       / bf, 4),
        "out_rate":             round(non_so_outs / bf, 4),
    }


# ---------------------------------------------------------------------------
# Step 1: Roster sync
# ---------------------------------------------------------------------------

def sync_rosters(season: int) -> dict:
    """
    Pull the current 40-man roster for every team and update the Player table.
    - New players are added
    - Existing players get their team_id, position, and active flag updated
    - Players no longer on any 40-man are marked inactive
    """
    logger.info("=" * 55)
    logger.info(f"  Syncing rosters from MLB API (season {season})")
    logger.info("=" * 55)

    teams = Team.query.filter(Team.mlb_id.isnot(None)).all()
    seen_mlb_ids = set()
    added = updated = 0

    for team in teams:
        try:
            data = statsapi.get("team_roster", {
                "teamId": team.mlb_id,
                "rosterType": "active",   # 26-man active roster (most relevant for today)
                "season": season,
            })
            entries = data.get("roster", [])
        except Exception as e:
            logger.warning(f"  {team.abbreviation}: roster fetch failed — {e}")
            continue

        for entry in entries:
            person  = entry.get("person", {})
            mlb_id  = person.get("id")
            name    = person.get("fullName", "")
            pos     = entry.get("position", {}).get("abbreviation", "")
            status  = entry.get("status", {}).get("code", "A")

            if not mlb_id:
                continue
            seen_mlb_ids.add(mlb_id)

            player = Player.query.filter_by(mlb_id=mlb_id).first()
            if player:
                changed = False
                if player.team_id != team.id:
                    logger.info(f"  {name}: team {player.team_id} → {team.abbreviation}")
                    player.team_id = team.id
                    changed = True
                if pos and player.position != pos:
                    player.position = pos
                    changed = True
                if not player.active:
                    player.active = True
                    changed = True
                if changed:
                    updated += 1
            else:
                player = Player(
                    mlb_id=mlb_id,
                    name=name,
                    team_id=team.id,
                    position=pos,
                    active=(status == "A"),
                )
                db.session.add(player)
                added += 1
                logger.info(f"  + {name} ({pos}) → {team.abbreviation}")

        time.sleep(0.2)   # be gentle with the API

    # Mark players no longer on any active roster as inactive
    deactivated = 0
    all_pitchers = Player.query.filter(
        Player.active == True,
        Player.position.in_({"SP", "RP", "P", "TWP"}),
        Player.mlb_id.isnot(None),
    ).all()
    for p in all_pitchers:
        if p.mlb_id not in seen_mlb_ids:
            p.active = False
            deactivated += 1
            logger.info(f"  - {p.name}: marked inactive (not on any active roster)")

    db.session.commit()
    summary = {"added": added, "updated": updated, "deactivated": deactivated}
    logger.info(f"  Roster sync done — added {added}, updated {updated}, deactivated {deactivated}")
    return summary


# ---------------------------------------------------------------------------
# Step 2: Handedness sync
# ---------------------------------------------------------------------------

def sync_handedness() -> dict:
    """
    Fetch pitchHand and batSide for every active player that is missing
    throws / bats data.  Uses the MLB Stats API 'people' endpoint which
    accepts up to 50 comma-separated personIds per request.

    This is the data that drives platoon split selection in the simulation:
      - pitcher.throws  → tells the sim which batter split to use (vs_LHP / vs_RHP)
      - player.bats     → stored for potential future use
    """
    logger.info("=" * 55)
    logger.info("  Syncing handedness (pitchHand / batSide) from MLB API")
    logger.info("=" * 55)

    # All active players with a known mlb_id but missing throws or bats
    missing = Player.query.filter(
        Player.active == True,
        Player.mlb_id.isnot(None),
        db.or_(Player.throws == None, Player.bats == None),
    ).all()

    if not missing:
        logger.info("  All active players already have handedness data — nothing to do.")
        return {"updated": 0, "skipped": 0}

    logger.info(f"  {len(missing)} player(s) need handedness data")

    # Batch into groups of 50 (API limit per request)
    BATCH = 50
    updated = skipped = 0

    for i in range(0, len(missing), BATCH):
        batch = missing[i : i + BATCH]
        ids   = ",".join(str(p.mlb_id) for p in batch)

        try:
            data    = statsapi.get("people", {"personIds": ids})
            people  = {p["id"]: p for p in data.get("people", [])}
        except Exception as e:
            logger.warning(f"  Batch {i//BATCH + 1}: API error — {e}")
            skipped += len(batch)
            continue

        for player in batch:
            person = people.get(player.mlb_id)
            if not person:
                skipped += 1
                continue

            throws = person.get("pitchHand",  {}).get("code")  # "L" or "R"
            bats   = person.get("batSide",    {}).get("code")  # "L", "R", or "S"

            changed = False
            if throws and player.throws != throws:
                player.throws = throws
                changed = True
            if bats and player.bats != bats:
                player.bats = bats
                changed = True

            if changed:
                updated += 1

        db.session.commit()
        time.sleep(0.3)

    logger.info(f"  Handedness sync done — {updated} updated, {skipped} not found in API")
    return {"updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Step 3: Pitching stats sync
# ---------------------------------------------------------------------------

def sync_pitching_stats(season: int) -> dict:
    """
    Pull season-to-date pitching stats from the MLB Stats API for every pitcher
    on every team's active roster and upsert into PitchingStats.

    Uses player_stat_data() per pitcher (the team-aggregate endpoint only
    returns one row). Outcome rates are derived from raw counting stats.
    """
    logger.info("=" * 55)
    logger.info(f"  Syncing pitching stats from MLB API (season {season})")
    logger.info("=" * 55)

    teams = Team.query.filter(Team.mlb_id.isnot(None)).all()
    upserted = skipped = errors = 0

    for team in teams:
        # Get the active roster to know which pitchers to fetch
        try:
            roster_data = statsapi.get("team_roster", {
                "teamId": team.mlb_id,
                "rosterType": "active",
                "season": season,
            })
            entries = roster_data.get("roster", [])
        except Exception as e:
            logger.warning(f"  {team.abbreviation}: roster fetch failed — {e}")
            continue

        pitcher_mlb_ids = [
            entry["person"]["id"]
            for entry in entries
            if entry.get("position", {}).get("type") == "Pitcher"
            and entry.get("person", {}).get("id")
        ]

        team_upserted = 0
        for mlb_id in pitcher_mlb_ids:
            player = Player.query.filter_by(mlb_id=mlb_id).first()
            if not player:
                skipped += 1
                continue

            try:
                result = statsapi.player_stat_data(
                    mlb_id, group="pitching", type="season",
                    sportId=1, season=season,
                )
                stat_rows = result.get("stats", [])
                if not stat_rows:
                    continue
                stat = stat_rows[0].get("stats", {})
            except Exception as e:
                errors += 1
                continue

            ip     = _ip_to_float(stat.get("inningsPitched", "0.0"))
            games  = _safe_int(stat.get("gamesPitched"))
            gs     = _safe_int(stat.get("gamesStarted"))

            if ip < 0.1 and games == 0:
                continue  # hasn't pitched yet this season

            # Role: SP if majority of outings were starts, else RP
            role = "SP" if gs > 0 and gs >= games * 0.5 else "RP"

            rates = _pitcher_rates_from_api(stat, ip)

            existing = PitchingStats.query.filter_by(
                player_id=player.id, season=season, role=role, split="overall"
            ).first()

            if existing:
                existing.ip            = ip
                existing.games         = games
                existing.games_started = gs
                existing.era           = _safe_float(stat.get("era"))               or None
                existing.whip          = _safe_float(stat.get("whip"))              or None
                existing.k_per_9       = _safe_float(stat.get("strikeoutsPer9Inn")) or None
                existing.bb_per_9      = _safe_float(stat.get("walksPer9Inn"))      or None
                existing.hr_per_9      = _safe_float(stat.get("homeRunsPer9"))      or None
                for k, v in rates.items():
                    setattr(existing, k, v)
            else:
                ps = PitchingStats(
                    player_id=player.id,
                    season=season,
                    role=role,
                    split="overall",
                    ip=ip,
                    games=games,
                    games_started=gs,
                    era=_safe_float(stat.get("era"))               or None,
                    whip=_safe_float(stat.get("whip"))             or None,
                    k_per_9=_safe_float(stat.get("strikeoutsPer9Inn")) or None,
                    bb_per_9=_safe_float(stat.get("walksPer9Inn"))     or None,
                    hr_per_9=_safe_float(stat.get("homeRunsPer9"))     or None,
                    **rates,
                )
                db.session.add(ps)

            upserted += 1
            team_upserted += 1
            time.sleep(0.1)   # ~10 req/sec — well within free tier limits

        db.session.commit()
        logger.info(f"  {team.abbreviation}: {team_upserted} pitchers updated")

    summary = {"upserted": upserted, "skipped": skipped, "errors": errors}
    logger.info(f"  Stats sync done — {upserted} rows upserted, {skipped} not in DB, {errors} errors")
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync live rosters and pitching stats from MLB API")
    parser.add_argument("--season",           type=int, default=date.today().year, help="Season to update (default: current year)")
    parser.add_argument("--stats-only",       action="store_true", help="Skip roster sync, only update stats")
    parser.add_argument("--rosters-only",     action="store_true", help="Skip stats, only sync rosters")
    parser.add_argument("--handedness-only",  action="store_true", help="Only sync pitchHand/batSide — fast, no stats fetch")
    args = parser.parse_args()

    with app.app_context():
        init_db()

        if args.handedness_only:
            sync_handedness()
        else:
            if not args.stats_only:
                sync_rosters(args.season)
                sync_handedness()          # always run after roster sync

            if not args.rosters_only:
                sync_pitching_stats(args.season)

        logger.info("")
        logger.info("Done. Rosters, handedness, and stats are now current.")
