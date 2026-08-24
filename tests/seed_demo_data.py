"""
Seeds the DB and data/incoming/ with demo data so the pipeline can be run
and checked end-to-end without real Pench data.

Images are now REAL, valid, decodable synthetic JPEGs (random stripe-like
patterns via OpenCV) -- not fake placeholder bytes. This means the real
ORB feature matcher (app/idmatch/feature_matcher.py) actually runs on them
and produces genuine similarity scores, instead of crashing or needing a
stub. Filenames still encode what the classifier stub expects (see
app/prefilter/classifier.py docstring for why classification is still a
filename-based stub, not a trained model) -- but the images themselves are
now real enough for the real matcher to exercise honestly.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np

from app.db.schema import init_db, get_connection


def _make_stripe_image(seed: int, size: int = 300):
    """Generates a real, decodable synthetic 'stripe pattern' image -- stands
    in for a tiger crop so the real ORB matcher has actual pixel content to
    compare, not corrupt placeholder bytes."""
    rng = np.random.RandomState(seed)
    img = np.ones((size, size), dtype=np.uint8) * 200
    for _ in range(25):
        x1, y1 = rng.randint(0, size, 2)
        x2, y2 = rng.randint(0, size, 2)
        thickness = int(rng.randint(3, 8))
        cv2.line(img, (x1, y1), (x2, y2), 30, thickness)
    noise = rng.randint(0, 30, (size, size)).astype(np.uint8)
    img = cv2.subtract(img, noise)
    return img

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "data" / "incoming"


def seed_users(conn):
    users = [
        ("ranger_amit", "field_ranger", "BEAT-01"),
        ("officer_priya", "range_officer", None),
        ("stpf_team1", "stpf", None),
        ("researcher_wct", "researcher", None),
    ]
    for username, role, beat in users:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, role, beat_id) VALUES (?, ?, ?)",
            (username, role, beat),
        )
    conn.commit()


def seed_cameras(conn):
    cameras = [
        ("PTR-CAM-001", "BEAT-01", 21.7679, 79.2961, "general"),
        ("PTR-CAM-002", "BEAT-01", 21.7700, 79.3000, "restricted"),
    ]
    for code, beat, lat, lon, zone in cameras:
        conn.execute(
            """INSERT OR IGNORE INTO cameras (camera_code, beat_id, latitude, longitude, zone_type, battery_pct, status)
               VALUES (?, ?, ?, ?, ?, 78.0, 'active')""",
            (code, beat, lat, lon, zone),
        )
    conn.commit()


def seed_reference_tiger(conn):
    """
    Reference tiger PTR-LOCAL-001: a real synthetic stripe-pattern image
    (seed=100), so the ORB matcher has real pixel content to compare against.
    """
    ref_dir = ROOT / "data" / "reference_tigers"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_img = ref_dir / "tiger_PTR-LOCAL-001_ref.jpg"
    cv2.imwrite(str(ref_img), _make_stripe_image(seed=100))
    conn.execute(
        """INSERT OR IGNORE INTO tigers (local_id, reference_image_path, status, first_seen_at, last_seen_at, source)
           VALUES ('PTR-LOCAL-001', ?, 'confirmed', datetime('now'), datetime('now'), 'pench_confirmed')""",
        (str(ref_img),),
    )
    conn.commit()


def seed_incoming_images():
    """
    img_tiger_0002.jpg is seeded from the SAME pattern seed as the reference
    tiger (100) with a small rotation -- simulating 'same tiger, different
    camera angle', so the real matcher should score it as a plausible match.
    img_tiger_0005.jpg uses a different seed (200) -- simulating a genuinely
    different, currently-unregistered tiger. Non-tiger images use distinct
    unrelated patterns; they only need to be valid, decodable images since
    classification is still filename-based (see classifier.py docstring).
    """
    cam1 = INCOMING / "PTR-CAM-001"
    cam2 = INCOMING / "PTR-CAM-002"
    cam1.mkdir(parents=True, exist_ok=True)
    cam2.mkdir(parents=True, exist_ok=True)

    # Same pattern as reference tiger, slightly rotated -- "recaptured" look
    ref_pattern = _make_stripe_image(seed=100)
    rot_matrix = cv2.getRotationMatrix2D((150, 150), 7, 1.0)
    tiger_recapture = cv2.warpAffine(ref_pattern, rot_matrix, (300, 300), borderValue=200)

    images = {
        cam1 / "img_blank_0001.jpg": np.ones((300, 300), dtype=np.uint8) * 210,  # near-blank frame
        cam1 / "img_tiger_0002.jpg": tiger_recapture,                            # same tiger as reference
        cam1 / "img_deer_0003.jpg": _make_stripe_image(seed=300),                # unrelated pattern
        cam2 / "img_human_poacher_0004.jpg": _make_stripe_image(seed=400),       # unrelated pattern
        cam2 / "img_tiger_0005.jpg": _make_stripe_image(seed=200),               # different, new tiger
    }
    for path, img_array in images.items():
        if not path.exists():
            cv2.imwrite(str(path), img_array)

    print(f"[seed] wrote {len(images)} real synthetic demo images across 2 cameras")


def main():
    init_db()
    conn = get_connection()
    seed_users(conn)
    seed_cameras(conn)
    seed_reference_tiger(conn)
    conn.close()
    seed_incoming_images()
    print("[seed] done. Run: python run_pipeline.py")


if __name__ == "__main__":
    main()
