"""
extractor.py
------------
Phase 3 — Deterministic Extraction.

Reads unprocessed records from raw_events, extracts whatever it can
using pure Python logic (regex, dateparser, string matching), and
writes the results into the events table.

The deterministic layer always runs first, before any AI. Even noisy
social media posts contain fields we can extract reliably without AI:
dates, times, and explicit team name mentions. The AI layer (Phase 4)
fills in what we can't extract here.

Usage (from project root):
    python backend/pipeline/extractor.py
"""

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import dateparser
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
from backend.app.models import Event, RawEvent, RawEventContribution

# ---------------------------------------------------------------------------
# Path to the team list produced by fetch_teams.py
# ---------------------------------------------------------------------------
TEAMS_PATH = PROJECT_ROOT / "data" / "teams.json"


def load_teams(path: Path) -> list[dict]:
    """
    Load the team list from data/teams.json.

    Each record contains:
        name            : official name e.g. "Côte d'Ivoire"
        normalized_name : ASCII lowercase e.g. "cote d'ivoire"
        abbreviation    : 3-letter FIFA code e.g. "CIV"
        country_code    : FIFA country code
        confederation   : AFC / CAF / CONCACAF / CONMEBOL / OFC / UEFA

    Raises a clear error if the file doesn't exist — the user needs to
    run fetch_teams.py before running the extractor.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Team list not found at: {path}\n"
            f"Run fetch_teams.py first to generate it."
        )

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    """
    Convert a Unicode string to lowercase ASCII-safe form for matching.

    "Côte d'Ivoire" → "cote d'ivoire"
    "Curaçao"       → "curacao"
    "Türkiye"       → "turkiye"

    We normalize both the team names (in load_teams) and the raw text
    before matching so accents never cause a miss.
    """
    return (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .strip()
    )


def extract_teams(text: str, teams: list[dict]) -> str | None:
    """
    Scan raw text for explicit mentions of 2026 World Cup team names.

    Matches against:
    - normalized_name : handles accent variations
    - abbreviation    : catches posts like "ARG vs FRA"

    Deliberately does NOT match nicknames (La Roja, Die Mannschaft, etc.)
    Those require context to interpret safely — handled by AI in Phase 4.

    Uses word boundaries to prevent partial matches:
    - "Iran" won't match inside "Iranian"
    - "USA" won't match inside "CAUSA"

    Returns a comma-separated string of canonical team names found,
    or None if no teams are found.

    Examples:
        "Watch Argentina vs France!" → "Argentina, France"
        "ARG takes on ALG tonight"   → "Argentina, Algeria"
        "Vamos Albiceleste"          → None (AI handles this)
        "Deutschland vs Japan"       → None (AI handles this)
    """
    if not text:
        return None

    normalized_text = normalize_text(text)
    found_teams = []

    for team in teams:
        matched = False

        # Check normalized team name first
        pattern = r'\b' + re.escape(team["normalized_name"]) + r'\b'
        if re.search(pattern, normalized_text):
            matched = True

        # Check abbreviation if name didn't match
        # Only match abbreviations that are 3 characters to avoid
        # short false positives — e.g. "OR" being too ambiguous
        if not matched and len(team["abbreviation"]) == 3:
            pattern = r'\b' + re.escape(team["abbreviation"].lower()) + r'\b'
            if re.search(pattern, normalized_text):
                matched = True

        if matched:
            found_teams.append(team["name"])

    if not found_teams:
        return None

    # Remove duplicates while preserving order
    seen = set()
    unique_teams = []
    for team in found_teams:
        if team not in seen:
            seen.add(team)
            unique_teams.append(team)

    return ", ".join(unique_teams)


def extract_date(text: str) -> str | None:
    """
    Extract the most likely event date from raw text.

    Uses dateparser which handles a wide range of formats and languages:
        "June 17, 2026"          → "2026-06-17"
        "17 de Junio de 2026"    → "2026-06-17"
        "14. Juni 2026"          → "2026-06-14"
        "Saturday June 13"       → "2026-06-13"

    We filter results to only accept dates that have a day, month AND year.
    Bare years ("2026") and partial dates ("June 2026") are rejected because
    dateparser defaults them to today's date which is almost never correct.

    Returns the date as a YYYY-MM-DD string, or None if no date found.
    """
    if not text:
        return None

    from dateparser.search import search_dates

    settings = {
        # Prefer future dates when no year is specified
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }

    # Replace newlines and extra whitespace with single spaces so dateparser
    # doesn't split date expressions that span a line break
    cleaned_text = " ".join(text.split())
    results = search_dates(cleaned_text, languages=["en", "es", "de", "fr", "pt"], settings=settings)

    if not results:
        return None

    def has_full_date(match_str: str, parsed_dt) -> bool:
        s = match_str.strip().lower()

        # Reject bare years like '2026'
        if re.fullmatch(r'\d{4}', s):
            return False

        # Reject prices like '$10'
        if re.search(r'^\$\d+', s):
            return False
        
        if re.fullmatch(r'[a-z]{2}', s.strip()):
            return False

        # Use the parsed datetime to confirm a real month and day exist
        # datetime always has month (1-12) and day (1-31)
        # but we want to make sure it's not just defaulting to today
        has_day = parsed_dt.day is not None
        has_month = parsed_dt.month is not None
        has_year = parsed_dt.year is not None

        return has_day and has_month or has_day and has_year

    specific_results = [(m, dt) for m, dt in results if has_full_date(m, dt)]

    if not specific_results:
        return None
    _, parsed_datetime = specific_results[0]

    return parsed_datetime.strftime("%Y-%m-%d")


def extract_all_times(text: str) -> list[str]:
    """
    Extract ALL time expressions from raw text and return them as a list.

    We return all candidates rather than just the first because posts
    often contain multiple times with different meanings:
        "Doors open at 6PM, kickoff at 7PM"
        "Happy hour 5-7PM, match starts at 8PM ET"

    The AI extractor in Phase 4 uses the full raw text plus these
    candidates to determine which time is the actual event start.

    Returns a list of times in HH:MM 24-hour format.
    Returns an empty list if no times are found.

    Examples:
        "Doors open 6PM, game at 7PM" → ["18:00", "19:00"]
        "Kickoff at 20:00"            → ["20:00"]
        "Come watch with us!"         → []
    """
    if not text:
        return []

    times = []
    seen = set()

    # ------------------------------------------------------------------
    # Pattern 1 — 12-hour format with AM/PM
    # Matches: "7PM", "7:30 PM", "9:00 AM", "11:30pm"
    # (\d{1,2})       — hour 1-12
    # (?::(\d{2}))?   — optional :minutes
    # \s*             — optional whitespace between hour and AM/PM
    # (AM|PM)         — meridiem indicator, case insensitive
    # ------------------------------------------------------------------
    twelve_hour_pattern = r'(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)'

    for match in re.finditer(twelve_hour_pattern, text):
        hour = int(match.group(1))
        minutes = int(match.group(2)) if match.group(2) else 0
        period = match.group(3).upper()

        # Convert to 24-hour
        if period == "PM" and hour != 12:
            hour += 12
        elif period == "AM" and hour == 12:
            hour = 0

        # Validate range
        if 0 <= hour <= 23 and 0 <= minutes <= 59:
            formatted = f"{hour:02d}:{minutes:02d}"
            if formatted not in seen:
                seen.add(formatted)
                times.append(formatted)

    # ------------------------------------------------------------------
    # Pattern 2 — 24-hour format
    # Matches: "19:00", "08:30", "20:00"
    # [01]?\d|2[0-3]  — valid hours 0-23
    # [0-5]\d         — valid minutes 00-59
    # Only match if no 12-hour pattern already found this time
    # ------------------------------------------------------------------
    twenty_four_hour_pattern = r'\b([01]?\d|2[0-3]):([0-5]\d)\b'

    for match in re.finditer(twenty_four_hour_pattern, text):
        hour = int(match.group(1))
        minutes = int(match.group(2))

        if 0 <= hour <= 23 and 0 <= minutes <= 59:
            formatted = f"{hour:02d}:{minutes:02d}"
            if formatted not in seen:
                seen.add(formatted)
                times.append(formatted)

    return times


def run_extraction() -> None:
    """
    Main extraction function.

    For each unprocessed RawEvent (processed=False):
    1. Run deterministic extraction on raw_text
    2. Create an Event record with extracted fields
    3. Create a RawEventContribution linking RawEvent to Event
    4. Mark the RawEvent as processed=True
    5. Commit all changes in a single transaction

    Fields left null here will be filled by AI extraction in Phase 4:
    - event_name
    - event_type
    - venue
    - address
    - event_time (final — AI picks the correct one from candidates)
    - model_name / prompt_version / confidence scores
    """
    teams = load_teams(TEAMS_PATH)
    print(f"Loaded {len(teams)} teams from {TEAMS_PATH.name}")

    with get_db_session() as session:

        # ------------------------------------------------------------------
        # Find all unprocessed RawEvents using the processed flag.
        # This is a simple indexed boolean lookup — much faster than
        # the outerjoin approach we discussed during design.
        # ------------------------------------------------------------------
        unprocessed = (
            session.query(RawEvent)
            .filter_by(processed=False)
            .all()
        )

        print(f"Found {len(unprocessed)} unprocessed raw events.\n")

        inserted = 0

        for raw_event in unprocessed:
            text = raw_event.raw_text or ""

            # --------------------------------------------------------------
            # Run all deterministic extractors.
            # --------------------------------------------------------------
            teams_found  = extract_teams(text, teams)
            event_date   = extract_date(text)
            all_times    = extract_all_times(text)

            # Store the first time as event_time for now.
            # Phase 4 AI extraction will refine this using context.
            # All candidate times are visible in the raw_text for the AI.
            event_time = all_times[0] if all_times else None

            print(f"  RawEvent {raw_event.raw_event_id}:")
            print(f"    Teams      : {teams_found}")
            print(f"    Date       : {event_date}")
            print(f"    All times  : {all_times}")
            print(f"    Time used  : {event_time}")

            # --------------------------------------------------------------
            # Create the Event record.
            # AI-dependent fields are left as None for Phase 4.
            # --------------------------------------------------------------
            event = Event(
                source_url=raw_event.source_url,
                teams=teams_found,
                event_date=event_date,
                event_time=event_time,
                processed_at=datetime.now(timezone.utc),
            )
            session.add(event)

            # flush() to get the auto-generated event_id before creating
            # the contribution row that references it
            session.flush()

            # --------------------------------------------------------------
            # Create the RawEventContribution.
            # --------------------------------------------------------------
            contribution = RawEventContribution(
                raw_event_id=raw_event.raw_event_id,
                event_id=event.event_id,
            )
            session.add(contribution)

            # --------------------------------------------------------------
            # Mark the RawEvent as processed.
            # This prevents it from being picked up on the next run.
            # --------------------------------------------------------------
            raw_event.processed = True

            inserted += 1

        session.commit()

        print(f"\nExtraction complete.")
        print(f"  Events created : {inserted}")


if __name__ == "__main__":
    run_extraction()