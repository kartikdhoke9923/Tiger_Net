"""
Ingestion: pulls new images from a camera's local drop folder (SD card copy
or local network share) into the database as 'unclassified' rows.

Deliberately does NOT require network access -- this step must work purely
off local storage, since cameras are retrieved physically or connect over
unreliable local links, not the open internet. See app/sync for the
separate step that later pushes confirmed data to a central system.

Expects images to be pre-sorted into per-camera folders:
    data/incoming/<camera_code>/*.jpg
<<<<<<< HEAD
Real deployment would pull EXIF captured_at; this stub uses file mtime as a
stand-in and logs a warning, since EXIF parsing needs a real image library
(Pillow) wired to real files, not simulated here.
"""

from pathlib import Path
from datetime import datetime
=======

captured_at is read from EXIF DateTimeOriginal/DateTime via Pillow (real
camera traps embed this). Falls back to file mtime ONLY when EXIF is absent
or undecodable -- mtime reflects when the SD card was copied, not when the
animal triggered the shutter, so treat fallback values as approximate.
"""

import sys
from pathlib import Path
from datetime import datetime
from PIL import Image
from PIL.ExifTags import Base as ExifBase

>>>>>>> b2727e2c528f5d462e2e467856663ce86d7f5a25
from app.db.schema import get_connection

INCOMING_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "incoming"
VALID_EXT = {".jpg", ".jpeg", ".png"}

<<<<<<< HEAD
=======
_EXIF_DATETIME_TAGS = ("DateTimeOriginal", "DateTime", "DateTimeDigitized")
_EXIF_FORMAT = "%Y:%m:%d %H:%M:%S"


def read_captured_at(img_path: Path) -> str:
    """
    Best-effort capture time for an image:
      1. EXIF DateTimeOriginal / DateTime / DateTimeDigitized (camera traps
         embed this -- it is the true shutter time)
      2. file mtime fallback (SD-card copy time; approximate, logged as such)
    Never raises: a corrupt/unreadable EXIF block must not stop ingestion.
    """
    try:
        exif = Image.open(img_path).getexif()
        for tag_name in _EXIF_DATETIME_TAGS:
            raw = exif.get(getattr(ExifBase, tag_name))
            if not raw:
                continue
            text = raw.strip() if isinstance(raw, str) else bytes(raw).decode("ascii", "ignore").strip()
            return datetime.strptime(text, _EXIF_FORMAT).isoformat()
    except Exception as e:
        print(f"[ingest] WARNING: no usable EXIF on {img_path.name} ({e}); "
              f"falling back to mtime -- treat captured_at as approximate", file=sys.stderr)
    else:
        print(f"[ingest] NOTE: no EXIF datetime on {img_path.name}; "
              f"using mtime fallback -- treat captured_at as approximate", file=sys.stderr)
    return datetime.fromtimestamp(img_path.stat().st_mtime).isoformat()

>>>>>>> b2727e2c528f5d462e2e467856663ce86d7f5a25

def get_or_create_camera(conn, camera_code: str, latitude=0.0, longitude=0.0, beat_id="UNKNOWN"):
    row = conn.execute("SELECT id FROM cameras WHERE camera_code = ?", (camera_code,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cameras (camera_code, beat_id, latitude, longitude) VALUES (?, ?, ?, ?)",
        (camera_code, beat_id, latitude, longitude),
    )
    conn.commit()
    return cur.lastrowid


def ingest_folder(conn=None):
    """
    Scans data/incoming/<camera_code>/ for new image files not already in
    the DB (matched by file_path) and inserts them as unclassified.
    Returns count of newly ingested images per camera.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    if not INCOMING_DIR.exists():
        print(f"[ingest] {INCOMING_DIR} does not exist yet — nothing to ingest.")
        if own_conn:
            conn.close()
        return {}

    summary = {}
    for camera_folder in INCOMING_DIR.iterdir():
        if not camera_folder.is_dir():
            continue
        camera_code = camera_folder.name
        camera_id = get_or_create_camera(conn, camera_code)
        new_count = 0

        for img_path in camera_folder.iterdir():
            if img_path.suffix.lower() not in VALID_EXT:
                continue
            existing = conn.execute(
                "SELECT id FROM images WHERE file_path = ?", (str(img_path),)
            ).fetchone()
            if existing:
                continue

<<<<<<< HEAD
            captured_at = datetime.fromtimestamp(img_path.stat().st_mtime).isoformat()
=======
            captured_at = read_captured_at(img_path)
>>>>>>> b2727e2c528f5d462e2e467856663ce86d7f5a25
            conn.execute(
                """INSERT INTO images (camera_id, file_path, captured_at, classification)
                   VALUES (?, ?, ?, 'unclassified')""",
                (camera_id, str(img_path), captured_at),
            )
            new_count += 1

        conn.execute(
            "UPDATE cameras SET last_seen_at = datetime('now') WHERE id = ?", (camera_id,)
        )
        summary[camera_code] = new_count

    conn.commit()
    if own_conn:
        conn.close()
    return summary


if __name__ == "__main__":
    result = ingest_folder()
    if not result:
        print("[ingest] nothing found. Add images under data/incoming/<camera_code>/")
    else:
        for cam, count in result.items():
            print(f"[ingest] {cam}: {count} new image(s)")
