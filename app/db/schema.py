"""
Database schema for Pench Tiger Monitoring Augmentation System.

Design notes (why it's built this way):
- SQLite for local/offline-first operation at range-office level. No internet
  dependency for core functioning. Swap for Postgres+PostGIS later if this
  needs to run as a shared central server instead of per-range local nodes.
- Location fields are ALWAYS stored in a separate table (sighting_location)
  from the general sighting record, so access control can be applied at the
  table level -- not everyone who can see "a tiger was seen" should be able
  to see "exactly where".
- tiger_id here is PROVISIONAL / LOCAL. It is explicitly not the same as the
  national ExtractCompare ID until reconciled. See `national_id` column,
  which stays NULL until sync confirms a match.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "pench.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Users / roles. Roles drive access control in app/security/access_control.py
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('field_ranger', 'range_officer', 'stpf', 'researcher', 'admin')),
    beat_id TEXT,              -- restricts field_ranger to their own beat
    created_at TEXT DEFAULT (datetime('now'))
);

-- Physical camera trap devices
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_code TEXT UNIQUE NOT NULL,   -- e.g. PTR-CAM-014
    beat_id TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    zone_type TEXT DEFAULT 'general' CHECK(zone_type IN ('general', 'restricted')),
    battery_pct REAL,
    last_seen_at TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'offline', 'maintenance'))
);

-- Every raw image ingested, before or after classification
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    file_path TEXT NOT NULL,
    captured_at TEXT NOT NULL,          -- from EXIF/camera metadata, not upload time
    ingested_at TEXT DEFAULT (datetime('now')),
    classification TEXT CHECK(classification IN
        ('unclassified', 'blank', 'animal_other', 'human', 'tiger_candidate')),
    classification_confidence REAL,
    reviewed INTEGER DEFAULT 0,          -- 0/1, has a human looked at this
    synced_to_central INTEGER DEFAULT 0  -- 0/1, offline queue status
);

-- Provisional/local tiger identities. national_id filled in only after
-- reconciliation with ExtractCompare / WII database.
CREATE TABLE IF NOT EXISTS tigers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_id TEXT UNIQUE NOT NULL,       -- e.g. PTR-LOCAL-014
    national_id TEXT,                    -- NULL until synced/confirmed nationally
    sex TEXT CHECK(sex IN ('M', 'F', 'unknown')) DEFAULT 'unknown',
    first_seen_at TEXT,
    last_seen_at TEXT,
    reference_image_path TEXT,           -- best confirmed image for matching against
    status TEXT DEFAULT 'provisional' CHECK(status IN ('provisional', 'confirmed', 'merged')),
    -- IMPORTANT: distinguishes real Pench sightings from benchmark/test data
    -- (e.g. tigers loaded from ATRW for pipeline testing). Never let
    -- 'atrw_benchmark' rows appear in a real population count -- they are
    -- Amur tigers from zoos, not Bengal tigers from Pench. See
    -- app/idmatch/atrw_loader.py docstring.
    source TEXT DEFAULT 'pench_confirmed' CHECK(source IN ('pench_confirmed', 'pench_provisional', 'atrw_benchmark'))
);

-- A confirmed sighting: links an image to a tiger ID, after human review.
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id INTEGER NOT NULL REFERENCES images(id),
    tiger_id INTEGER REFERENCES tigers(id),   -- NULL if still unresolved
    match_confidence REAL,               -- from idmatch module, before human confirm
    confidence_tier TEXT CHECK(confidence_tier IN ('high', 'medium', 'low')),
    confirmed_by_user_id INTEGER REFERENCES users(id),
    confirmed_at TEXT,
    is_new_individual INTEGER DEFAULT 0
);

-- Location kept separate from sightings for access-control reasons (see module docstring)
CREATE TABLE IF NOT EXISTS sighting_location (
    sighting_id INTEGER PRIMARY KEY REFERENCES sightings(id),
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    camera_id INTEGER NOT NULL REFERENCES cameras(id)
);

-- Human detection events -> routed to STPF, not general dashboard
CREATE TABLE IF NOT EXISTS human_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id INTEGER NOT NULL REFERENCES images(id),
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    detected_at TEXT DEFAULT (datetime('now')),
    zone_type TEXT,
    alert_sent INTEGER DEFAULT 0,
    alert_sent_at TEXT,
    acknowledged_by_user_id INTEGER REFERENCES users(id)
);

-- Every access to sensitive data (esp. location) gets logged here.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    resource_id INTEGER,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


<<<<<<< HEAD
def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
=======
def _migrate(conn):
    """
    Idempotent column migrations. CREATE TABLE IF NOT EXISTS does not touch
    pre-existing tables, so added-later columns need guarded ALTER TABLE here.
    pin_salt / pin_hash back the web dashboard login (app/dashboard/web.py +
    app/security/auth.py). NULL pin = no web login yet for that user.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "pin_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")
    if "pin_salt" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN pin_salt TEXT")
    sighting_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)").fetchall()}
    if "resolution" not in sighting_cols:
        # 'confirmed' | 'new_individual' | 'dismissed'; NULL = still pending.
        # 'dismissed' lets reviewers clear false-positive tiger candidates
        # (e.g. a deer the pre-filter over-promoted) instead of leaving them
        # stuck in the queue forever -- the row is kept for the record,
        # never deleted.
        conn.execute("ALTER TABLE sightings ADD COLUMN resolution TEXT")
    conn.commit()


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    _migrate(conn)
>>>>>>> b2727e2c528f5d462e2e467856663ce86d7f5a25
    conn.commit()
    conn.close()
    print(f"[db] initialized at {DB_PATH}")


if __name__ == "__main__":
    init_db()
