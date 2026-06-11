"""
ai_extractor.py
---------------
Phase 4 — AI Extraction.

Reads every event that has been through deterministic extraction and
fills in fields that require inference:
    - event_name
    - event_type (watch_party / watch_party_neutral / fan_fest / viewing_venue / unknown)
    - venue
    - address
    - event_end_date (for multi-day events)
    - teams (if deterministic found fewer than 2)
    - event_date (if deterministic found none)
    - event_time (if deterministic found none)
    - confidence scores

The AI only works on what is genuinely missing — fields already populated
by deterministic extraction are passed as context but not re-extracted,
except for teams where fewer than 2 were found.

Usage (from project root):
    python backend/pipeline/ai_extractor.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Imports from our codebase
# ---------------------------------------------------------------------------
from backend.app.database import get_db_session
from backend.app.models import Event, Match, RawEvent, RawEventContribution
from backend.pipeline.prompts.v2 import PROMPT_VERSION, build_prompt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL_NAME = "claude-sonnet-4-6"
TEAMS_PATH = PROJECT_ROOT / "data" / "teams.json"

# Rate limiting — be respectful to the API
# claude-sonnet allows generous rate limits but we add a small delay
# between calls to avoid bursting
DELAY_BETWEEN_CALLS = 0.5  # seconds


def load_valid_teams(path: Path) -> set[str]:
    """
    Load the set of valid official team names from teams.json.
    Used to validate AI-inferred teams after extraction.
    Returns a set of lowercase team names for case-insensitive comparison.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Team list not found at {path}. Run fetch_teams.py first."
        )
    with open(path, "r", encoding="utf-8") as f:
        teams = json.load(f)
    # Store as lowercase for case-insensitive validation
    return {t["name"].lower() for t in teams}


def get_relevant_matches(session, team_names: list[str] | str | None) -> list[dict]:
    """
    Query the matches table for fixtures involving the given teams.

    We look for matches where either home_team or away_team matches
    any of the provided team names. Returns a list of plain dicts
    so they can be safely passed to the prompt builder.

    This is Option A from our architecture discussion — only include
    match context for teams we already know about, keeping prompts focused.
    """
    if not team_names:
        return []

    from sqlalchemy import or_

    matches = (
        session.query(Match)
        .filter(
            or_(
                *[Match.home_team == team for team in team_names],
                *[Match.away_team == team for team in team_names],
            )
        )
        .all()
    )

    return [
        {
            "home_team": m.home_team,
            "away_team": m.away_team,
            "match_datetime": m.match_datetime,
            "venue": m.venue,
            "city": m.city,
        }
        for m in matches
    ]


def validate_teams(ai_teams_str: str | None, valid_teams: set[str]) -> str | None:
    """
    Validate AI-inferred teams against the official team list.

    Removes any team name the AI returned that isn't in the 2026
    World Cup roster. This prevents hallucinated team names from
    entering the database.

    Special case: "ALL" is always valid — it means fan fest.

    Returns a cleaned comma-separated string, or None if nothing valid remains.
    """
    if not ai_teams_str:
        return None

    # Fan fest special case
    if ai_teams_str.strip().upper() == "ALL":
        return "ALL"

    valid = []
    for team in ai_teams_str.split(","):
        team = team.strip()
        if team.lower() in valid_teams:
            valid.append(team)
        else:
            print(f"    ⚠ AI returned invalid team '{team}' — removed.")

    return ", ".join(valid) if valid else None


def call_claude(client: anthropic.Anthropic, prompt: str, image_url: str | None = None) -> dict:
    """
    Call the Claude API with the given prompt and optionally an image.

    When image_url is provided, we send it alongside the text using
    Claude's vision capability. This handles image-only social media
    posts where the flyer text was extracted via Vision AI.
    """
    # Build the user message content
    if image_url:
        # Multi-modal message — image + text
        content = [
            {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": image_url,
                },
            },
            {
                "type": "text",
                "text": prompt,
            },
        ]
    else:
        # Text only
        content = prompt

    message = client.messages.create(
        model=MODEL_NAME,
        max_tokens=1000,
        temperature=0.2,
        system=(
            "You are a precise data extraction assistant. "
            "You always respond with a valid JSON object and nothing else. "
            "No explanation, no markdown, no code blocks — only the JSON object."
        ),
        messages=[
            {"role": "user", "content": content}
        ],
    )

    # Extract text safely regardless of content block type
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text += block.text # type: ignore
    response_text = response_text.strip()

    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude returned invalid JSON: {e}\nResponse was: {response_text}"
        )


def merge_teams(existing: str | None, ai_extracted: str | None) -> str | None:
    """
    Merge teams found by deterministic extraction with teams found by AI.

    Rules:
    - If AI returns "ALL", that takes precedence — it's a fan fest
    - Otherwise combine both lists, deduplicate, preserve order
    - Deterministic teams come first (higher confidence)
    """
    if ai_extracted and ai_extracted.strip().upper() == "ALL":
        return "ALL"

    existing_list = [t.strip() for t in existing.split(",")] if existing else []
    ai_list = [t.strip() for t in ai_extracted.split(",")] if ai_extracted else []

    seen = set()
    merged = []
    for team in existing_list + ai_list:
        if team and team not in seen:
            seen.add(team)
            merged.append(team)

    return ", ".join(merged) if merged else None


def run_ai_extraction() -> None:
    """
    Main AI extraction function.

    For each Event that has not yet been through AI extraction
    (model_name is None), we:
    1. Load the corresponding RawEvent for the raw text
    2. Build a dynamic prompt based on what deterministic already found
    3. Call Claude API
    4. Validate and merge the results
    5. Update the Event record
    6. Commit after each event (not all at once) so partial progress
       is preserved if the script is interrupted mid-run
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    valid_teams = load_valid_teams(TEAMS_PATH)

    print(f"Loaded {len(valid_teams)} valid teams for validation.")
    print(f"Using model: {MODEL_NAME}")
    print(f"Prompt version: {PROMPT_VERSION}\n")

    with get_db_session() as session:

        # ------------------------------------------------------------------
        # Find all events that haven't been through AI extraction yet.
        # model_name being None means AI hasn't touched this event.
        # ------------------------------------------------------------------
        unprocessed = (
            session.query(Event)
            .filter(Event.model_name == None)
            .all()
        )

        print(f"Found {len(unprocessed)} events pending AI extraction.\n")

        processed = 0
        failed = 0

        for event in unprocessed:
            print(f"  Event {event.event_id}:")

            # ----------------------------------------------------------
            # Load the raw event text via the contributions table.
            # We take the first contributing raw event as the primary source.
            # ----------------------------------------------------------
            contribution = (
                session.query(RawEventContribution)
                .filter_by(event_id=event.event_id)
                .first()
            )

            if not contribution:
                print(f"    No raw event contribution found — skipping.")
                continue

            raw_event = (
                session.query(RawEvent)
                .filter_by(raw_event_id=contribution.raw_event_id)
                .first()
            )

            if not raw_event or not raw_event.raw_text:
                print(f"    No raw text found — skipping.")
                continue

            # ----------------------------------------------------------
            # Get relevant matches for teams already found.
            # ----------------------------------------------------------
            existing_team_list = (
                [t.strip() for t in event.teams.split(",")]
                if event.teams and event.teams != "ALL"
                else event.teams
            )

            relevant_matches = get_relevant_matches(session, existing_team_list)

            # ----------------------------------------------------------
            # Build the dynamic prompt.
            # ----------------------------------------------------------
            prompt = build_prompt(
                raw_text=raw_event.raw_text,
                existing_teams=event.teams,
                existing_date=event.event_date,
                existing_time=event.event_time,
                relevant_matches=relevant_matches if relevant_matches else None,
                has_image=raw_event.has_image,
                scraped_at=raw_event.scraped_at, 
            )

            # ----------------------------------------------------------
            # Call Claude API.
            # ----------------------------------------------------------
            try:
                result = call_claude(client, prompt, image_url=raw_event.image_url)
            except Exception as e:
                print(f"    ✗ API call failed: {e}")
                failed += 1
                continue

            # ----------------------------------------------------------
            # Validate and apply the results.
            # ----------------------------------------------------------

            # Teams — validate AI-inferred teams, then merge with existing
            if "teams" in result:
                validated_ai_teams = validate_teams(
                    result.get("teams"), valid_teams
                )
                event.teams = merge_teams(event.teams, validated_ai_teams)

            # Date — only apply if it was missing
            if "event_date" in result and result["event_date"]:
                event.event_date = result["event_date"]

            # Time — only apply if it was missing
            if "event_time" in result and result["event_time"]:
                event.event_time = result["event_time"]

            # Always apply these
            event.event_name = result.get("event_name")
            event.event_type = result.get("event_type")
            event.venue = result.get("venue")
            event.address = result.get("address")
            event.event_timezone = result.get("event_timezone")
            event.event_end_date = result.get("event_end_date")
            event.event_type_confidence = result.get("event_type_confidence")
            event.venue_confidence = result.get("venue_confidence")
            event.team_confidence = result.get("team_confidence")

            # Pipeline metadata
            event.model_name = MODEL_NAME
            event.prompt_version = PROMPT_VERSION

            print(f"    event_name  : {event.event_name}")
            print(f"    event_type  : {event.event_type}")
            print(f"    venue       : {event.venue}")
            print(f"    address     : {event.address}")
            print(f"    teams       : {event.teams}")
            print(f"    event_date  : {event.event_date}")
            print(f"    event_time  : {event.event_time}")
            print(f"    timezone    : {event.event_timezone}")
            print(f"    end_date    : {event.event_end_date}")
            print(f"    type_conf   : {event.event_type_confidence}")

            # ----------------------------------------------------------
            # Commit after each event so partial progress is preserved.
            # If the script is interrupted, already-processed events
            # won't be reprocessed on the next run (model_name is set).
            # ----------------------------------------------------------
            session.commit()
            processed += 1

            # Rate limiting
            time.sleep(DELAY_BETWEEN_CALLS)

        print(f"\nAI extraction complete.")
        print(f"  Processed : {processed}")
        print(f"  Failed    : {failed}")
        print(f"  Total     : {processed + failed}")


if __name__ == "__main__":
    run_ai_extraction()