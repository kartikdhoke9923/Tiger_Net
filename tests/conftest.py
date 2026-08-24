"""
Shared pytest fixtures.

Every test gets its OWN throwaway SQLite database in tmp_path -- tests never
touch data/pench.db. This works because app.db.schema.get_connection()
resolves DB_PATH at call time, so monkeypatching the module attribute
redirects every module that imports get_connection (they all go through
app.db.schema).
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import schema
from app.security.auth import set_user_pin


def make_stripe_image(seed: int, size: int = 300) -> np.ndarray:
    """Synthetic 'tiger stripe' pattern -- real decodable pixels for ORB."""
    rng = np.random.RandomState(seed)
    img = np.ones((size, size), dtype=np.uint8) * 200
    for _ in range(25):
        x1, y1 = rng.randint(0, size, 2)
        x2, y2 = rng.randint(0, size, 2)
        cv2.line(img, (x1, y1), (x2, y2), 30, int(rng.randint(3, 8)))
    noise = rng.randint(0, 30, (size, size)).astype(np.uint8)
    return cv2.subtract(img, noise)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh initialized DB + standard demo users/cameras/reference tiger."""
    monkeypatch.setattr(schema, "DB_PATH", tmp_path / "test.db")
    schema.init_db()
    conn = schema.get_connection()

    users = [
        ("ranger_amit", "field_ranger", "BEAT-01"),
        ("ranger_other", "field_ranger", "BEAT-02"),
        ("officer_priya", "range_officer", None),
        ("stpf_team1", "stpf", None),
        ("researcher_wct", "researcher", None),
        ("admin_root", "admin", None),
    ]
    for username, role, beat in users:
        conn.execute(
            "INSERT INTO users (username, role, beat_id) VALUES (?, ?, ?)",
            (username, role, beat),
        )

    cameras = [
        ("PTR-CAM-001", "BEAT-01", 21.7679, 79.2961, "general"),
        ("PTR-CAM-002", "BEAT-01", 21.7700, 79.3000, "restricted"),
        ("PTR-CAM-003", "BEAT-02", 21.8500, 79.4000, "general"),
    ]
    for code, beat, lat, lon, zone in cameras:
        conn.execute(
            "INSERT INTO cameras (camera_code, beat_id, latitude, longitude, zone_type, battery_pct, status)"
            " VALUES (?, ?, ?, ?, ?, 78.0, 'active')",
            (code, beat, lat, lon, zone),
        )

    ref_dir = tmp_path / "reference_tigers"
    ref_dir.mkdir()
    ref_img = ref_dir / "tiger_PTR-LOCAL-001_ref.jpg"
    cv2.imwrite(str(ref_img), make_stripe_image(seed=100))
    conn.execute(
        """INSERT INTO tigers (local_id, reference_image_path, status, first_seen_at,
                               last_seen_at, source)
           VALUES ('PTR-LOCAL-001', ?, 'confirmed', datetime('now'), datetime('now'),
                   'pench_confirmed')""",
        (str(ref_img),),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def user_ids(db):
    rows = db.execute("SELECT id, username FROM users").fetchall()
    return {r["username"]: r["id"] for r in rows}


@pytest.fixture()
def camera_ids(db):
    rows = db.execute("SELECT id, camera_code FROM cameras").fetchall()
    return {r["camera_code"]: r["id"] for r in rows}


@pytest.fixture()
def incoming_image(db, tmp_path):
    """
    Factory: writes a real synthetic JPEG into a per-camera folder and inserts
    an images row. Returns callable(camera_id, seed) -> (image_id, path).
    """
    folder = tmp_path / "incoming"
    folder.mkdir(exist_ok=True)

    def _make(camera_id: int, seed: int = 42, name: str | None = None):
        p = folder / (name or f"img_{seed}.jpg")
        cv2.imwrite(str(p), make_stripe_image(seed))
        cur = db.execute(
            "INSERT INTO images (camera_id, file_path, captured_at, classification)"
            " VALUES (?, ?, datetime('now'), 'unclassified')",
            (camera_id, str(p)),
        )
        db.commit()
        return cur.lastrowid, p

    return _make
