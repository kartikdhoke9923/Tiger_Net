"""Schema integrity + the data-isolation rules that other modules rely on."""

import sqlite3

from app.db.schema import init_db, _migrate


def test_location_lives_in_its_own_table(db):
    """PRD section 10: location is isolated so access control can be table-level."""
    sighting_cols = {r["name"] for r in db.execute("PRAGMA table_info(sightings)").fetchall()}
    assert "latitude" not in sighting_cols
    assert "longitude" not in sighting_cols
    loc_cols = {r["name"] for r in db.execute("PRAGMA table_info(sighting_location)").fetchall()}
    assert {"sighting_id", "latitude", "longitude"} <= loc_cols


def test_tiger_source_constraint_blocks_unlabeled_rows(db):
    """The atrw_benchmark vs pench_confirmed split is enforced by CHECK, not convention."""
    with __import__("pytest").raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tigers (local_id, source) VALUES ('BAD-001', 'not_a_real_source')"
        )
        db.commit()


def test_migration_is_idempotent_and_adds_web_columns(tmp_path, monkeypatch):
    from app.db import schema as schema_mod

    monkeypatch.setattr(schema_mod, "DB_PATH", tmp_path / "mig.db")
    init_db()  # creates + migrates
    conn = sqlite3.connect(tmp_path / "mig.db")
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert {"pin_hash", "pin_salt"} <= cols
    _migrate(conn)  # second run must not raise / duplicate
    cols2 = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert cols == cols2
    s_cols = {r[1] for r in conn.execute("PRAGMA table_info(sightings)").fetchall()}
    assert "resolution" in s_cols
    conn.close()


def test_foreign_keys_enforced(db):
    """Images must reference a real camera -- garbage rows must not enter."""
    with __import__("pytest").raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO images (camera_id, file_path, captured_at) VALUES (9999, 'x.jpg', '2026-01-01')"
        )
        db.commit()
