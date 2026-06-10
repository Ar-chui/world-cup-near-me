"""
fetch_teams.py
--------------
Fetches the 2026 FIFA World Cup team list from the Balldontlie API
and saves it to data/teams.json.

This file is the authoritative source of teams for the deterministic
extraction layer. The extractor reads from this file instead of
maintaining a hardcoded list — keeping team data accurate and in sync
with the same source as our match data.

Run this once before running the extractor, or any time you want to
refresh the team list.

Usage (from project root):
    python backend/pipeline/fetch_teams.py
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_KEY = os.getenv("BALLDONTLIE_KEY")
TEAMS_URL = "https://api.balldontlie.io/fifa/worldcup/v1/teams"
OUTPUT_PATH = PROJECT_ROOT / "data" / "teams.json"


def fetch_teams() -> list[dict]:
    """
    Fetch all teams from the Balldontlie API.

    The teams endpoint returns all teams in a single response —
    no pagination needed. Each team object contains:
        id           : Balldontlie's internal team ID
        name         : official team name (may contain Unicode characters
                       e.g. "Côte d'Ivoire", "Curaçao", "Türkiye")
        abbreviation : 3-letter FIFA abbreviation e.g. "ARG", "FRA"
        country_code : same as abbreviation in most cases
        confederation: AFC / CAF / CONCACAF / CONMEBOL / OFC / UEFA
    """
    if not API_KEY:
        raise ValueError(
            "BALLDONTLIE_KEY is not set. "
            "Add it to your .env file and try again."
        )

    with httpx.Client(timeout=30.0) as client:
        print("Fetching teams from Balldontlie API...")

        response = client.get(
            TEAMS_URL,
            headers={"Authorization": API_KEY},
        )
        response.raise_for_status()
        data = response.json()

        teams = data.get("data", [])
        print(f"  Received {len(teams)} teams.")
        return teams


def build_team_records(raw_teams: list[dict]) -> list[dict]:
    """
    Transform the raw API response into our team record format.

    Each record contains:
        name         : official name as returned by the API (Unicode preserved)
        abbreviation : 3-letter FIFA code e.g. "ARG"
        country_code : FIFA country code (same as abbreviation in most cases)
        confederation: which confederation the team belongs to

    We also generate a normalized_name — a lowercase ASCII-safe version
    of the team name used for text matching in the extractor. This handles
    cases like "Côte d'Ivoire" → "cote d'ivoire" so we can match against
    text that may not have the correct accent characters.

    We deliberately do NOT include nicknames (Three Lions, La Roja, etc.)
    here. Nickname matching requires context to avoid false positives
    (e.g. "Three Lions" could be a pub name). That inference is handled
    by the AI extractor in Phase 4.
    """
    import unicodedata

    def normalize(text: str) -> str:
        """
        Convert a Unicode string to lowercase ASCII-safe form.

        "Côte d'Ivoire" → "cote d'ivoire"
        "Curaçao"       → "curacao"
        "Türkiye"       → "turkiye"

        This uses Unicode NFKD normalization which decomposes accented
        characters into base character + accent mark, then we encode to
        ASCII ignoring the accent marks.
        """
        return (
            unicodedata.normalize("NFKD", text)
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
            .strip()
        )

    records = []
    for team in raw_teams:
        name = team.get("name", "")
        abbreviation = team.get("abbreviation", "")

        record = {
            # Official name from the API — Unicode preserved
            "name": name,

            # Normalized name for text matching — ASCII lowercase
            # This is what the extractor uses to search raw text
            "normalized_name": normalize(name),

            # 3-letter FIFA abbreviation — also used for matching
            # e.g. a post saying "ARG vs FRA" can be matched
            "abbreviation": abbreviation.upper(),

            # Which confederation — useful for future filtering
            "confederation": team.get("confederation", ""),

            # Balldontlie internal ID — useful if we ever need to
            # cross-reference with match data from the same API
            "balldontlie_id": team.get("id"),
        }
        records.append(record)

    # Sort alphabetically by name for readability
    records.sort(key=lambda r: r["name"])
    return records


def save_teams(records: list[dict], path: Path) -> None:
    """
    Save the team records to a JSON file.

    ensure_ascii=False preserves Unicode characters in the output file
    so "Côte d'Ivoire" is stored as-is, not as "C\\u00f4te d'Ivoire".
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print(f"  Saved {len(records)} teams to {path}")


def main() -> None:
    raw_teams = fetch_teams()
    records = build_team_records(raw_teams)
    save_teams(records, OUTPUT_PATH)

    print("\nTeam list preview (first 5):")
    for team in records[:5]:
        print(
            f"  {team['name']:<25} "
            f"abbr={team['abbreviation']:<4} "
            f"normalized={team['normalized_name']}"
        )

    print("\nDone. Run extractor.py to use this team list.")


if __name__ == "__main__":
    main()