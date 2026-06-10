"""
models.py
---------
Defines all database tables as SQLAlchemy ORM models.

Each class here maps to one table in PostgreSQL. SQLAlchemy uses these
definitions to:
  1. Generate the SQL CREATE TABLE statements (via Alembic migrations)
  2. Map database rows to Python objects in application code

Import order matters: Match and PipelineRun have no foreign key dependencies,
so they are defined first. RawEvent depends on PipelineRun. Event depends on
both RawEvent and Match.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

# Base is defined in database.py — all models inherit from it.
# SQLAlchemy uses Base to track which classes are tables.
from backend.app.database import Base


# ===========================================================================
# Match
# ===========================================================================
class Match(Base):
    """
    Represents a single FIFA World Cup fixture.

    Seeded once from API-Football before the pipeline runs.
    Events discovered by the pipeline are linked back to this table
    in Phase 6 (match linking) via the match_id foreign key on Event.
    """

    __tablename__ = "matches"

    # Primary key — auto-incrementing integer managed by PostgreSQL.
    match_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The unique fixture ID from API-Football.
    # Stored so we can detect duplicates on re-runs and fetch
    # updated data (e.g. scores) in the future without re-seeding everything.
    # Nullable because future tournaments seeded from other sources
    # may not have an API-Football ID.
    api_football_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    balldontlie_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)

    # Which tournament this match belongs to.
    # "FIFA World Cup 2026" for our initial dataset.
    # This column is what makes the schema extensible to other tournaments.
    tournament: Mapped[str] = mapped_column(String(100), nullable=False)

    # Stage of the tournament.
    # Values: group / round_of_32 / round_of_16 / quarterfinal /
    #         semifinal / third_place / final
    stage: Mapped[str] = mapped_column(String(50), nullable=False)

    # Team names — nullable because knockout round matchups are unknown
    # until earlier rounds complete. NULL means "not yet determined".
    home_team: Mapped[str | None] = mapped_column(String(100), nullable=True)
    away_team: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Scheduled kickoff time. Stored with timezone awareness.
    match_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Where the match is being played.
    venue: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Relationship: one match can be linked to many events.
    # back_populates connects this to Event.match below.
    events: Mapped[list["Event"]] = relationship("Event", back_populates="match")

    def __repr__(self) -> str:
        return (
            f"<Match {self.match_id}: {self.home_team} vs {self.away_team} "
            f"({self.tournament}, {self.stage})>"
        )


# ===========================================================================
# PipelineRun
# ===========================================================================
class PipelineRun(Base):
    """
    Tracks a single execution of the scraping and processing pipeline.

    Every RawEvent is tagged with the pipeline_run_id of the run that
    created it. This lets us trace exactly which pipeline run produced
    which data, which is essential for debugging and reprocessing.

    Status lifecycle: pending → running → completed / failed
    """

    __tablename__ = "pipeline_runs"

    pipeline_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Timestamps — when the run started and when it finished.
    # server_default=func.now() means PostgreSQL sets this automatically
    # at insert time, so we never have to pass it manually.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Current state of the run.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )

    # If status = "failed", the error message is stored here.
    # NULL means no error occurred.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Counters — updated as the pipeline progresses.
    # These give us a quick summary of what each run did.
    events_scraped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_merged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationship: one pipeline run produces many raw events.
    raw_events: Mapped[list["RawEvent"]] = relationship("RawEvent", back_populates="pipeline_run")

    def __repr__(self) -> str:
        return f"<PipelineRun {self.pipeline_run_id}: {self.status}>"

# ===========================================================================
# RawEventContribution
# ===========================================================================
class RawEventContribution(Base):
    """
    Junction table mapping which raw events contributed to a final event.
    Represents the true many-to-one relationship between RawEvent and Event.
    
    A single Event can be produced from multiple RawEvents (e.g. an Instagram
    post and a Facebook post about the same watch party getting deduplicated
    into one Event). This table preserves the full audit trail.
    """

    __tablename__ = "raw_event_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    raw_event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("raw_events.raw_event_id"),
        nullable=False,
    )
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("events.event_id"),
        nullable=False,
    )

    raw_event: Mapped["RawEvent"] = relationship("RawEvent", back_populates="contributions")
    event: Mapped["Event"] = relationship("Event", back_populates="contributions")

# ===========================================================================
# RawEvent
# ===========================================================================
class RawEvent(Base):
    """
    Stores the raw, unprocessed output of a scraper or input adapter.

    Every record that enters the system lands here first, unchanged.
    We never modify raw data after it is inserted — this table is the
    audit log of exactly what we received from each source.

    Downstream pipeline stages (deterministic extraction, AI extraction)
    read from this table and write their output to the events table.
    """

    __tablename__ = "raw_events"

    raw_event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Which source produced this record.
    # Examples: "mock", "instagram", "facebook", "eventbrite"
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    # The URL of the original post or listing, if available.
    # Used for deduplication (Layer 1) and for linking back to the source.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    # When this record was scraped/fetched.
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # The raw text content of the post or listing.
    # For social media: the caption or post body.
    # For Eventbrite: the event title + description concatenated.
    # For image-only posts: the text extracted by GPT-4o Vision.
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The original structured JSON payload from the source, stored as-is.
    # JSONB is PostgreSQL's binary JSON type — it's indexed and queryable.
    # We store this so we can always reprocess from the original data
    # without re-scraping, even if our extraction logic changes later.
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Foreign key to pipeline_runs — which run produced this record.
    pipeline_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pipeline_runs.pipeline_run_id"),
        nullable=True,
    )

    processed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # Relationships
    contributions: Mapped[list["RawEventContribution"]] = relationship("RawEventContribution", 
                                                                       back_populates="raw_event"
    )
    pipeline_run: Mapped["PipelineRun"] = relationship("PipelineRun", back_populates="raw_events")

    def __repr__(self) -> str:
        return f"<RawEvent {self.raw_event_id}: {self.source}>"


# ===========================================================================
# Event
# ===========================================================================
class Event(Base):
    """
    The cleaned, extracted, enriched output of the full pipeline.

    Each Event is produced from one RawEvent. It contains everything
    we were able to extract — deterministically and via AI — plus
    geocoordinates, match linking, and deduplication flags.

    This is the table the FastAPI layer queries and the frontend displays.
    """

    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Foreign key to matches — which World Cup match this event is for.
    # NULLABLE by design — events are inserted before match linking runs
    # (Phase 6). NULL means "not yet linked", not "no match exists".
    # This must be nullable from migration 1 or Phase 3 inserts will fail.
    match_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("matches.match_id"),
        nullable=True,
        index=True
    )

    # --- Extracted fields ---------------------------------------------------

    # The name or title of the event.
    event_name: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Event type — extracted by AI classification.
    # Values: watch_party / fan_fest / viewing_venue / unknown
    event_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # Venue name and address as extracted from the raw text.
    venue: Mapped[str | None] = mapped_column(String(300), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Geocoordinates — populated in Phase 6 by Nominatim geocoding.
    # NULL until geocoding runs.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Date and time of the event — extracted deterministically where possible.
    event_date: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    event_end_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    event_time: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Teams mentioned or inferred — stored as a comma-separated string.
    # Example: "Argentina, France"
    # Simple and queryable without needing a junction table at this stage.
    teams: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    # The original source URL, copied from raw_events for convenience
    # so the frontend doesn't need to join raw_events for every query.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    # --- Pipeline metadata --------------------------------------------------

    # When this event record was created by the pipeline.
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Which AI model produced the extraction (e.g. "gpt-4o").
    # NULL if deterministic extraction was sufficient.
    model_name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Which prompt version was used — lets us correlate output quality
    # with prompt changes during evaluation in Phase 9.
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- AI confidence scores -----------------------------------------------
    # Stored as separate float columns so they are directly queryable
    # and sortable without parsing JSON. Values range from 0.0 to 1.0.

    event_type_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    venue_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    team_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Deduplication flag -------------------------------------------------
    # Set to True by the deduplication layer (Phase 6) when this event
    # matches another by location + date + time.
    # Does NOT mean the event is deleted — it is flagged for review.
    is_duplicate_candidate: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # --- Relationships ------------------------------------------------------
    contributions: Mapped[list["RawEventContribution"]] = relationship("RawEventContribution", back_populates="event")
    match: Mapped["Match | None"] = relationship("Match", back_populates="events")

    def __repr__(self) -> str:
        return f"<Event {self.event_id}: {self.event_name} ({self.event_type})>"