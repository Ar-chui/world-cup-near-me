"""
database.py
-----------
Establishes the SQLAlchemy connection to PostgreSQL and provides:

  - engine       : the low-level connection to the database
  - SessionLocal : a factory that creates new database sessions
  - Base         : the class all ORM models inherit from
  - get_db_session : a context manager for safe session handling

Every other file in the project that needs database access imports
from this module. Nothing else should create its own engine or session.
"""

import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from pathlib import Path

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
# We load .env here as well as in the seed script because database.py can
# be imported from many entry points (seed scripts, FastAPI, tests). Each
# entry point may be run from a different working directory, so we resolve
# the .env path relative to this file's location to be safe.
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /world-cup-near-me/
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Database URL
# ---------------------------------------------------------------------------
# The DATABASE_URL environment variable holds the full PostgreSQL connection
# string. It looks like:
#   postgresql+psycopg://user:password@localhost:5432/worldcup
#
# We use the "postgresql+psycopg" prefix (not "postgresql+psycopg2") because
# our stack uses psycopg v3, which SQLAlchemy addresses with that prefix.
#
# If this variable is missing, we raise immediately with a clear message
# rather than letting SQLAlchemy raise a confusing error later.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is not set. "
        "Add it to your .env file. Example:\n"
        "  DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/worldcup"
    )

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# create_engine() sets up the connection pool and dialect.
# It does NOT open a connection immediately — connections are opened lazily
# when the first query is made.
#
# echo=False means SQLAlchemy won't print every SQL statement it executes.
# Set echo=True temporarily if you want to see the raw SQL during debugging.
engine = create_engine(
    DATABASE_URL,
    echo=False,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
# sessionmaker() returns a class (not an instance) that produces new Session
# objects when called. Think of it as a configured template for sessions.
#
# autocommit=False : changes are NOT written to the database until you
#                    explicitly call session.commit(). This is the safe
#                    default — you control exactly when data is persisted.
#
# autoflush=False  : SQLAlchemy won't automatically sync pending changes to
#                    the database before queries. We flush manually, which
#                    gives us more predictable behavior.
#
# bind=engine      : tells the session factory which database to talk to.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
# All SQLAlchemy ORM models inherit from Base. This is how SQLAlchemy
# discovers which Python classes correspond to which database tables.
# We define it here so every model imports Base from the same place.
class Base(DeclarativeBase):
    pass

# ---------------------------------------------------------------------------
# get_db_session — context manager for safe session handling
# ---------------------------------------------------------------------------
@contextmanager
def get_db_session():
    """
    Provides a database session and guarantees it is properly closed.

    Usage:
        with get_db_session() as session:
            session.add(some_object)
            session.commit()

    How it works:
    - Opens a new session when the with-block is entered.
    - Yields the session to the calling code.
    - If an exception occurs inside the with-block, rolls back any
      uncommitted changes so the database is left in a clean state.
    - Always closes the session when the with-block exits, whether
      or not an exception occurred. Closing returns the connection
      back to the connection pool.

    Why a context manager instead of just returning a session?
    If we returned a raw session and the caller forgot to close it,
    we'd leak database connections. The context manager makes correct
    cleanup automatic and impossible to forget.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        # Something went wrong — undo any staged changes.
        session.rollback()
        # Re-raise the original exception so the caller sees the real error.
        raise
    finally:
        # Always runs — closes the session and returns the connection
        # to the pool regardless of success or failure.
        session.close()