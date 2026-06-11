"""
prompts/v2.py
-------------
Version 2 of the AI extraction prompt.

UPDATE: Better guidance when picking if a watch party is neutral or supporter led and added time zone check.

This file contains only the prompt construction logic — no API calls,
no database access. It takes structured inputs and returns a formatted
prompt string ready to send to Claude.

Keeping prompts separate from extraction logic means:
- Prompts can be updated without touching pipeline code
- Old versions are preserved for Phase 9 evaluation
- Prompt changes are visible in version control as isolated diffs

To create a new prompt version:
1. Copy this file to v2.py
2. Make your changes
3. Update PROMPT_VERSION in ai_extractor.py to "v2"
"""

from datetime import datetime

PROMPT_VERSION = "v1"

# ---------------------------------------------------------------------------
# Event type definitions
# ---------------------------------------------------------------------------
EVENT_TYPES = {
    "watch_party": "A gathering organized specifically to support one team. "
                   "Predominantly one team's supporters. Not welcoming to opposing fans.",
    "watch_party_neutral": "A gathering open to fans of both teams or all fans. "
                           "Neutral atmosphere, both teams represented.",
    "fan_fest": "An official or large-scale multi-day event showing all World Cup matches. "
                "Open to all fans regardless of team.",
    "viewing_venue": "A bar, restaurant, or public space that is simply showing the match "
                     "on their screens. Not organized as a specific watch party.",
    "unknown": "Cannot be determined from the available information.",
}


def build_prompt(
    raw_text: str,
    existing_teams: str | None,
    existing_date: str | None,
    existing_time: str | None,
    relevant_matches: list[dict] | None,
    has_image: bool,
    scraped_at: datetime | None = None,
) -> str:
    """
    Build the extraction prompt dynamically based on what is already known.

    Parameters:
        raw_text        : the raw post or listing text
        existing_teams  : comma-separated teams found by deterministic extraction,
                          or None if none were found
        existing_date   : date found by deterministic extraction (YYYY-MM-DD), or None
        existing_time   : time found by deterministic extraction (HH:MM), or None
        relevant_matches: list of match dicts for teams already found, or None
        has_image       : whether the post has an image (vision was used)

    Returns a complete prompt string ready to send to Claude.
    """

    # ------------------------------------------------------------------
    # Determine what needs to be extracted based on existing values.
    # ------------------------------------------------------------------
    team_count = len(existing_teams.split(",")) if existing_teams else 0
    needs_teams = team_count < 2
    needs_date = existing_date is None
    needs_time = existing_time is None

    # ------------------------------------------------------------------
    # Build the match context section if we have relevant matches.
    # ------------------------------------------------------------------
    match_context = ""
    if relevant_matches:
        match_lines = []
        for m in relevant_matches:
            home = m.get("home_team") or "TBD"
            away = m.get("away_team") or "TBD"
            dt = m.get("match_datetime", "")
            venue = m.get("venue") or ""
            city = m.get("city") or ""
            if dt:
                from datetime import datetime
                try:
                    parsed = datetime.fromisoformat(str(dt))
                    dt_str = parsed.strftime("%b %d %Y %H:%M UTC")
                except ValueError:
                    dt_str = str(dt)
            else:
                dt_str = "TBD"
            match_lines.append(f"{home} vs {away} | {dt_str} | {venue}, {city}")
        match_context = "\n".join(match_lines)

    # ------------------------------------------------------------------
    # Build the extraction instructions dynamically.
    # ------------------------------------------------------------------
    extraction_instructions = []

    # Always extract these
    extraction_instructions.append(
        "- event_name: A clear, descriptive name for this event. If one is not provided already. "
        "If it is a single-supporter watch party, make that clear in the name "
        "(e.g. 'Argentina Supporters Watch Party'). "
        "If both teams are welcome, include both team names "
        "(e.g. 'Argentina vs France Watch Party'). "
        "If it is a fan fest, use an appropriate name like 'FIFA Fan Fest - Los Angeles'."
    )

    extraction_instructions.append(
        f"- event_type: One of the following values only:\n"
        + "\n".join([f"  * {k}: {v}" for k, v in EVENT_TYPES.items()])
        + "\n"
        + "If the event is organized by a national supporters group, cultural organization, "
          "or fan club associated with a specific country, classify it as 'watch_party' "
          "even if the text mentions both fans are welcome."
    )

    extraction_instructions.append(
        "- venue: The name of the venue where the event is taking place. "
        "null if not mentioned."
    )

    extraction_instructions.append(
        "- address: The full street address if explicitly mentioned. "
        "null if not mentioned."
    )

    extraction_instructions.append(
        "- event_end_date: If this is a multi-day event (e.g. a fan fest running "
        "for several weeks), extract the end date in YYYY-MM-DD format. "
        "null for single-day events."
    )

    # Conditionally extract teams
    if needs_teams:
        already_found = f" Already found: {existing_teams}." if existing_teams else ""
        extraction_instructions.append(
            f"- teams: Identify ANY 2026 FIFA World Cup teams mentioned or strongly implied "
            f"in the text, including references in other languages "
            f"(e.g. 'Deutschland' = Germany, 'Vamos Albiceleste' = Argentina, "
            f"'La Roja' = Spain).{already_found} "
            f"Return as a comma-separated string of official team names. "
            f"If this is a fan fest showing all matches or event is showing on multiple days, return exactly 'ALL'. "
            f"Return null if no teams can be identified."
        )

    # Conditionally extract date
    if needs_date:
        extraction_instructions.append(
            "- event_date: The date of the event in YYYY-MM-DD format. "
            "null if cannot be determined."
        )

    # Conditionally extract time
    if needs_time:
        extraction_instructions.append(
            "- event_time: The START time of the event in HH:MM 24-hour format. "
            "If multiple times are mentioned (e.g. doors open vs kickoff), "
            "use the event/match start time, not doors open. "
            "null if cannot be determined."
        )
    
    # Time Zone
    extraction_instructions.append(
        "- event_timezone: The timezone of the event as an IANA timezone string "
        "(e.g. 'America/New_York', 'America/Chicago', 'America/Los_Angeles'). "
        "Extract from the text if explicitly mentioned (e.g. 'ET', 'EST', 'CDT', 'PT'). "
        "If not mentioned but a city is identifiable, infer from the city "
        "(e.g. Miami → 'America/New_York', Chicago → 'America/Chicago', "
        "Los Angeles → 'America/Los_Angeles'). "
        "null if cannot be determined."
)

    # Confidence scores
    extraction_instructions.append(
        "- event_type_confidence: A float between 0.0 and 1.0 indicating "
        "how confident you are in the event_type classification."
    )
    extraction_instructions.append(
        "- venue_confidence: A float between 0.0 and 1.0 indicating "
        "how confident you are in the venue extraction. "
        "0.0 if venue is null."
    )
    extraction_instructions.append(
        "- team_confidence: A float between 0.0 and 1.0 indicating "
        "how confident you are in the teams extraction. "
        "0.0 if teams is null."
    )

    # ------------------------------------------------------------------
    # Assemble the full prompt.
    # ------------------------------------------------------------------
    prompt_parts = []

    prompt_parts.append(
        "You are an AI assistant extracting structured event information from "
        "social media posts and event listings about FIFA World Cup 2026 watch parties."
    )

    if match_context:
        prompt_parts.append(
            f"The following matches are relevant context based on teams already identified:\n"
            f"{match_context}"
        )

    if has_image:
        prompt_parts.append(
            "Note: This post originally contained an image. "
            "The text below may have been extracted from that image."
        )

        if scraped_at:
            prompt_parts.append(
                f"This post was scraped on {scraped_at.strftime('%A %B %d %Y at %H:%M UTC')}. "
                f"Use this as the anchor for relative date/time references like "
                f"'tonight', 'this Sunday', 'tomorrow', etc."
            )

    prompt_parts.append(
        "Extract the following fields from the post below:\n"
        + "\n".join(extraction_instructions)
    )

    prompt_parts.append(
        "Return your response as a JSON object with exactly these keys. "
        "Use null for any field that cannot be determined. "
        "Do not include any explanation or text outside the JSON object."
    )

    required_keys = [
        "event_name", "event_type", "venue", "address", "event_end_date", "event_timezone"
        "event_type_confidence", "venue_confidence", "team_confidence"
    ]
    if needs_teams:
        required_keys.append("teams")
    if needs_date:
        required_keys.append("event_date")
    if needs_time:
        required_keys.append("event_time")

    prompt_parts.append(f"Required JSON keys: {', '.join(required_keys)}")
    prompt_parts.append(f"Post to analyze:\n{raw_text}")

    return "\n\n".join(prompt_parts)