"""
seed_matches_balldontlie.py
---------------------------
Fetches the 2026 FIFA World Cup fixture list from the Balldontlie API
and inserts every match into the matches table in PostgreSQL.

This script is run ONCE before any scraping or pipeline work begins.
It is safe to run multiple times — it checks for existing records
and skips any fixture that is already in the database.

The API returns paginated results using a cursor. We loop until
there is no next cursor, collecting all fixtures before inserting.

Usage (from project root):
    python backend/pipeline/seed_matches_balldontlie.py
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /world-cup-near-me/
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# These imports come from our own codebase — created in Phase 1.
# ---------------------------------------------------------------------------
from backend.app.database import get_db_session
from backend.app.models import Match

# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------
API_KEY = os.getenv("BALLDONTLIE_KEY")
BASE_URL = "https://api.balldontlie.io/fifa/worldcup/v1/matches"
TOURNAMENT_NAME = "FIFA World Cup 2026"


def fetch_all_fixtures() -> list[dict]:
    """
    Fetch every fixture from the Balldontlie API, handling pagination.

    The API returns results in pages. Each response includes a
    meta.next_cursor value — if it exists, there are more pages.
    We keep requesting until next_cursor is None.

    Returns a flat list of all fixture dicts across all pages.
    """
    if not API_KEY:
        raise ValueError(
            "BALLDONTLIE_KEY is not set. "
            "Add it to your .env file and try again."
        )

    all_fixtures = []
    cursor = None  # None means start from the first page

    with httpx.Client(timeout=30.0) as client:
        page = 1
        while True:
            print(f"  Fetching page {page}...", end=" ")

            # Build query params — only include cursor if we have one
            params = {"per_page": 100}
            if cursor is not None:
                params["cursor"] = cursor

            response = client.get(
                BASE_URL,
                headers={"Authorization": API_KEY},
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            fixtures = data.get("data", [])
            all_fixtures.extend(fixtures)
            print(f"{len(fixtures)} fixtures received.")

            # Check if there is another page
            # next_cursor is None when we are on the last page
            cursor = data.get("meta", {}).get("next_cursor")
            if cursor is None:
                break

            page += 1

    print(f"\n  Total fixtures fetched: {len(all_fixtures)}")
    return all_fixtures


def parse_stage(stage_name: str) -> str:
    """
    Convert Balldontlie's stage name into our stage vocabulary.

    Balldontlie returns strings like:
        "Group Stage"
        "Round of 32"
        "Round of 16"
        "Quarter-finals"
        "Semi-finals"
        "Third Place"
        "Final"

    We map these to our defined values:
        group / round_of_32 / round_of_16 / quarterfinal /
        semifinal / third_place / final
    """
    s = stage_name.lower()

    if "group" in s:
        return "group_stages"
    elif "round of 32" in s:
        return "round_of_32"
    elif "round of 16" in s:
        return "round_of_16"
    elif "quarter" in s:
        return "quarterfinal"
    elif "semi" in s:
        return "semifinal"
    elif "third" in s or "3rd" in s:
        return "third_place"
    elif "final" in s:
        return "final"
    else:
        # Preserve unknown stages visibly rather than silently losing them
        return stage_name


def seed_matches() -> None:
    """
    Main seeding function.

    Steps:
    1. Fetch all fixtures from Balldontlie (handles pagination internally).
    2. Open a database session.
    3. For each fixture, check if it already exists by the unique api id.
       We reuse this column for the Balldontlie fixture ID since it serves
       the same purpose — a unique external identifier.
    4. If it does not exist, create a Match ORM object and stage it.
    5. Commit once — all inserts in a single transaction.
    """
    print("Fetching fixtures from Balldontlie API...")
    all_fixtures = fetch_all_fixtures()

    with get_db_session() as session:
        inserted = 0
        skipped = 0

        for fixture in all_fixtures:
            # ------------------------------------------------------------------
            # The unique ID Balldontlie assigns to this fixture.
            # ------------------------------------------------------------------
            fixture_id = fixture.get("id")

            # ------------------------------------------------------------------
            # Idempotency check — skip if already seeded.
            # ------------------------------------------------------------------
            existing = (
                session.query(Match)
                .filter_by(balldontlie_id=fixture_id)
                .first()
            )

            if existing:
                skipped += 1
                continue

            # ------------------------------------------------------------------
            # Parse datetime.
            # Balldontlie returns UTC ISO 8601: "2026-06-11T19:00:00.000Z"
            # Python's fromisoformat doesn't handle the trailing Z in older
            # versions, so we replace it with +00:00 to be safe.
            # ------------------------------------------------------------------
            raw_datetime = fixture.get("datetime", "")
            if raw_datetime:
                match_datetime = datetime.fromisoformat(
                    raw_datetime.replace("Z", "+00:00")
                )
            else:
                match_datetime = None

            # ------------------------------------------------------------------
            # Parse stage from the nested stage object.
            # ------------------------------------------------------------------
            stage_data = fixture.get("stage") or {}
            stage = parse_stage(stage_data.get("name", "unknown"))

            # ------------------------------------------------------------------
            # Stadium data — Balldontlie nests this under "stadium".
            # ------------------------------------------------------------------
            stadium = fixture.get("stadium") or {}
            venue = stadium.get("name")
            city = stadium.get("city")

            # ------------------------------------------------------------------
            # Team names — null for unplayed knockout rounds.
            # ------------------------------------------------------------------
            home_team_data = fixture.get("home_team")
            away_team_data = fixture.get("away_team")
            home_team = home_team_data.get("name") if home_team_data else None
            away_team = away_team_data.get("name") if away_team_data else None

            # ------------------------------------------------------------------
            # Build the ORM object.
            # ------------------------------------------------------------------
            match = Match(
                balldontlie_id=fixture_id,
                tournament=TOURNAMENT_NAME,
                stage=stage,
                home_team=home_team,
                away_team=away_team,
                match_datetime=match_datetime,
                venue=venue,
                city=city,
            )

            session.add(match)
            inserted += 1

        # Commit once — all inserts in a single transaction.
        session.commit()

        print("\nSeeding complete.")
        print(f"  Inserted : {inserted}")
        print(f"  Skipped  : {skipped} (already existed)")
        print(f"  Total    : {inserted + skipped}")


if __name__ == "__main__":
    seed_matches()