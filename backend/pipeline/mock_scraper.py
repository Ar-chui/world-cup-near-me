"""
mock_scraper.py
---------------
Reads the mock raw event JSON file and inserts every record into the
raw_events table in PostgreSQL, tagged to a new pipeline run.

This is the Phase 2 scraper. It simulates what real scrapers (Instagram,
Facebook, Eventbrite via Apify) will do in Phase 5 — the only difference
is that real scrapers fetch data from the network instead of a local file.
Everything downstream of this script is identical regardless of source.

Usage (from project root):
    python backend/pipeline/mock_scraper.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

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
from backend.app.models import PipelineRun, RawEvent

# ---------------------------------------------------------------------------
# Where is the mock data file?
# ---------------------------------------------------------------------------
MOCK_DATA_PATH = PROJECT_ROOT / "data" / "mock_raw_events.json"


def load_mock_data(path: Path) -> list[dict]:
    """
    Read the mock events JSON file and return a list of raw dictionaries.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Mock data file not found at: {path}\n"
            f"Expected location: data/mock_raw_events.json"
        )

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Loaded {len(data)} mock records from {path}")
    return data


def run_mock_scraper() -> None:
    """
    Main scraper function.

    Steps:
    1. Create a new PipelineRun record with status "running".
    2. Load mock records from the JSON file.
    3. Insert each record into raw_events, linked to the pipeline run.
    4. Update the PipelineRun status to "completed" with final counts.
    5. If anything fails, mark the PipelineRun as "failed" and re-raise.

    Why create a PipelineRun first?
    Every raw event is tagged with a pipeline_run_id. This lets us trace
    exactly which run produced which data. If we run the scraper 10 times,
    we can see exactly when each record entered the system and which run
    it came from. This is essential for debugging and reprocessing.
    """
    mock_records = load_mock_data(MOCK_DATA_PATH)

    with get_db_session() as session:

        # ------------------------------------------------------------------
        # Step 1 — Create a new PipelineRun record.
        # We set status to "running" immediately so if the script crashes
        # mid-run, the database reflects that something went wrong rather
        # than showing no record at all.
        # ------------------------------------------------------------------
        pipeline_run = PipelineRun(
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        session.add(pipeline_run)

        # flush() sends the INSERT to the database within the current
        # transaction without committing. This gives us the auto-generated
        # pipeline_run_id we need to tag each raw event below.
        # Without flush(), pipeline_run.pipeline_run_id would still be None.
        session.flush()

        print(f"Created pipeline run: ID {pipeline_run.pipeline_run_id}")

        inserted = 0
        skipped = 0

        try:
            for record in mock_records:
                # --------------------------------------------------------------
                # Idempotency check — skip if this source_url already exists.
                # This is deduplication Layer 1 from the architecture:
                # same source_url = definite duplicate across runs.
                # --------------------------------------------------------------
                source_url = record.get("source_url")

                if source_url:
                    existing = (
                        session.query(RawEvent)
                        .filter_by(source_url=source_url)
                        .first()
                    )
                    if existing:
                        print(f"  Skipping duplicate: {source_url}")
                        skipped += 1
                        continue

                # --------------------------------------------------------------
                # Build the RawEvent ORM object.
                # raw_payload stores the original structured JSON exactly
                # as received. raw_text is the text the pipeline will process.
                # --------------------------------------------------------------
                raw_event = RawEvent(
                    source=record.get("source", "mock"),
                    source_url=source_url,
                    raw_text=record.get("raw_text"),
                    raw_payload=record.get("raw_payload"),
                    pipeline_run_id=pipeline_run.pipeline_run_id,
                    scraped_at=datetime.now(timezone.utc),
                )

                session.add(raw_event)
                inserted += 1

            # ------------------------------------------------------------------
            # Update the PipelineRun with final counts and mark as completed.
            # ------------------------------------------------------------------
            pipeline_run.status = "completed"
            pipeline_run.completed_at = datetime.now(timezone.utc)
            pipeline_run.events_scraped = inserted

            session.commit()

            print(f"\nMock scraper complete.")
            print(f"  Pipeline run ID : {pipeline_run.pipeline_run_id}")
            print(f"  Inserted        : {inserted}")
            print(f"  Skipped         : {skipped} (duplicate source_url)")
            print(f"  Total           : {inserted + skipped}")

        except Exception as e:
            # ------------------------------------------------------------------
            # If anything goes wrong, mark the pipeline run as failed and
            # store the error message. Then re-raise so the caller sees it.
            # We don't silently swallow errors — the architecture doc is
            # explicit: "failures are logged, not silently swallowed."
            # ------------------------------------------------------------------
            pipeline_run.status = "failed"
            pipeline_run.completed_at = datetime.now(timezone.utc)
            pipeline_run.error_message = str(e)
            session.commit()

            print(f"\nMock scraper FAILED: {e}")
            raise


if __name__ == "__main__":
    run_mock_scraper()