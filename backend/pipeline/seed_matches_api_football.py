"""
seed_matches.py
---------------
Fetches the 2026 FIFA World Cup fixture list from API-Football
and inserts every match into the matches table in PostgreSQL.

This script is run ONCE before any scraping or pipeline work begins.
It is safe to run multiple times — it checks for existing records
and skips any fixture that is already in the database.

Usage (from project root):
    python backend/pipeline/seed_matches.py
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
# Add the project root to sys.path so we can import from backend/app.
# This works regardless of which directory you invoke the script from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /world-cup-near-me/
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
# load_dotenv() reads the .env file and loads each line into os.environ.
# After this call, os.getenv("API_FOOTBALL_KEY") will return your real key.
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# These imports come from our own codebase — created in Phase 1.
# ---------------------------------------------------------------------------
from backend.app.database import get_db_session  # yields a SQLAlchemy session
from backend.app.models import Match              # SQLAlchemy ORM model

# ---------------------------------------------------------------------------
# API-Football configuration
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_FOOTBALL_KEY")
API_BASE_URL = "https://v3.football.api-sports.io"

# FIFA World Cup = league ID 1 in API-Football
WORLD_CUP_LEAGUE_ID = 1
WORLD_CUP_SEASON = 2026
TOURNAMENT_NAME = "FIFA World Cup 2026"


def fetch_fixtures() -> list[dict]:
    """
    Call the API-Football /fixtures endpoint and return the raw list
    of fixture objects from the response.

    API-Football returns a response shaped like:
    {
        "response": [
            {
                "fixture": { "id": ..., "date": ..., "status": { ... } },
                "league":  { "round": ... },
                "teams":   { "home": { "name": ... }, "away": { "name": ... } },
                "venue":   { "name": ..., "city": ... }
            },
            ...
        ]
    }

    We return the raw "response" list and do all transformation in
    seed_matches() so this function stays focused on the network call.
    """
    if not API_KEY:
        raise ValueError(
            "API_FOOTBALL_KEY is not set. "
            "Add it to your .env file and try again."
        )

    # httpx.Client is a synchronous HTTP client — straightforward for a
    # one-off seed script. We don't need async here.
    with httpx.Client(timeout=30.0) as client:
        print(f"Fetching fixtures from API-Football...")
        print(f"  League : {WORLD_CUP_LEAGUE_ID} (FIFA World Cup)")
        print(f"  Season : {WORLD_CUP_SEASON}")

        response = client.get(
            f"{API_BASE_URL}/fixtures",
            params={
                "league": WORLD_CUP_LEAGUE_ID,
                "season": WORLD_CUP_SEASON,
            },
            headers={
                # API-Football requires the key in this header
                "x-apisports-key": API_KEY,
            },
        )

        # Raise an exception immediately if the HTTP status is 4xx or 5xx.
        # This gives a clear error rather than a confusing KeyError later
        # when we try to parse an error response as fixture data.
        response.raise_for_status()

        data = response.json()

        # API-Football includes an "errors" key when something goes wrong
        # at the application level (e.g. invalid key, quota exceeded) even
        # if the HTTP status was 200. We check for that explicitly.
        if data.get("errors"):
            raise RuntimeError(
                f"API-Football returned errors: {data['errors']}"
            )

        fixtures = data.get("response", [])
        print(f"  Received : {len(fixtures)} fixtures\n")
        return fixtures


def parse_stage(round_string: str) -> str:
    """
    Convert API-Football's round string into our stage vocabulary.

    API-Football returns strings like:
        "Group Stage - 1"
        "Round of 32"
        "Round of 16"
        "Quarter-finals"
        "Semi-finals"
        "3rd Place Final"
        "Final"

    We map these to the values defined in our architecture:
        group / round_of_32 / round_of_16 / quarterfinal /
        semifinal / third_place / final
    """
    r = round_string.lower()

    if "group" in r:
        return "group_stage"
    elif "round of 32" in r:
        return "round_of_32"
    elif "round of 16" in r:
        return "round_of_16"
    elif "quarter" in r:
        return "quarterfinal"
    elif "semi" in r:
        return "semifinal"
    elif "3rd" in r or "third" in r:
        return "third_place"
    elif "final" in r:
        return "final"
    else:
        # Keep the raw string rather than silently dropping it.
        # This makes unexpected rounds visible instead of hidden.
        return round_string


def parse_group(round_string: str) -> str | None:
    """
    Extract the group letter from a round string, or return None.

    API-Football does not reliably return a group letter per fixture.
    We return None for now — this is a known limitation, not a bug.
    The group column is not critical for match linking.
    """
    return None


def seed_matches() -> None:
    """
    Main seeding function.

    Steps:
    1. Fetch all fixtures from API-Football.
    2. Open a database session.
    3. For each fixture, check if it already exists (by api_football_id).
    4. If it does not exist, create a Match ORM object and stage it.
    5. Commit once — all inserts happen in a single transaction.
    """
    fixtures = fetch_fixtures()

    with get_db_session() as session:

        inserted = 0
        skipped = 0

        for fixture in fixtures:
            # ------------------------------------------------------------------
            # Pull the nested fields out of the API response structure.
            # Each fixture is a dict with keys: fixture, league, teams, venue.
            # ------------------------------------------------------------------
            fixture_data = fixture.get("fixture", {})
            league_data  = fixture.get("league", {})
            teams_data   = fixture.get("teams", {})
            venue_data   = fixture.get("venue", {})

            # The unique ID API-Football assigns to this fixture.
            # We store this so we can reliably detect duplicates on re-runs
            # and so we can fetch updated data (scores, etc.) later if needed.
            api_football_id = fixture_data.get("id")

            # ------------------------------------------------------------------
            # Idempotency check — skip if already seeded.
            # ------------------------------------------------------------------
            existing = (
                session.query(Match)
                .filter_by(api_football_id=api_football_id)
                .first()
            )

            if existing:
                skipped += 1
                continue

            # ------------------------------------------------------------------
            # Parse datetime.
            # API-Football returns ISO 8601 with timezone offset:
            # "2026-06-11T20:00:00+00:00"
            # datetime.fromisoformat() handles this in Python 3.11+.
            # ------------------------------------------------------------------
            raw_datetime = fixture_data.get("date")
            match_datetime = datetime.fromisoformat(raw_datetime) if raw_datetime else None

            # ------------------------------------------------------------------
            # Parse stage and group from the round string.
            # ------------------------------------------------------------------
            round_string = league_data.get("round", "")
            stage = parse_stage(round_string)
            group = parse_group(round_string)

            # ------------------------------------------------------------------
            # Team names — None for knockout rounds before teams are determined.
            # ------------------------------------------------------------------
            home_team = teams_data.get("home", {}).get("name")
            away_team = teams_data.get("away", {}).get("name")

            # ------------------------------------------------------------------
            # Build the ORM object.
            # ------------------------------------------------------------------
            match = Match(
                api_football_id=api_football_id,
                tournament=TOURNAMENT_NAME,
                stage=stage,
                home_team=home_team,
                away_team=away_team,
                match_datetime=match_datetime,
                venue=venue_data.get("name"),
                city=venue_data.get("city"),
            )

            session.add(match)
            inserted += 1

        # Commit once — all inserts in a single transaction.
        session.commit()

        print("Seeding complete.")
        print(f"  Inserted : {inserted}")
        print(f"  Skipped  : {skipped} (already existed)")
        print(f"  Total    : {inserted + skipped}")


if __name__ == "__main__":
    seed_matches()