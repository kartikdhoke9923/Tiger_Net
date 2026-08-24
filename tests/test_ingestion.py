"""Ingestion: dedup by path, per-camera folders, EXIF-first timestamps."""

import os
import time
from pathlib import Path

import cv2
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from conftest import make_stripe_image
from app.ingestion.ingest import ingest_folder, read_captured_at


def _write_jpeg(path: Path, seed=7, exif_datetime: str | None = None):
    img = Image.fromarray(make_stripe_image(seed))
    if exif_datetime:
        exif = img.getexif()
        exif[306] = exif_datetime  # 306 = DateTime
        img.save(path, exif=exif)
    else:
        img.save(path)


def test_ingest_creates_cameras_and_images(db, tmp_path, monkeypatch):
    from app.db import schema as schema_mod

    incoming = tmp_path / "incoming" / "PTR-CAM-TEST"
    incoming.mkdir(parents=True)
    _write_jpeg(incoming / "a.jpg", seed=1)
    _write_jpeg(incoming / "b.jpg", seed=2)

    monkeypatch.setattr("app.ingestion.ingest.INCOMING_DIR", tmp_path / "incoming")
    summary = ingest_folder(conn=db)

    assert summary == {"PTR-CAM-TEST": 2}
    rows = db.execute("SELECT classification FROM images").fetchall()
    assert all(r["classification"] == "unclassified" for r in rows)
    cam = db.execute(
        "SELECT id FROM cameras WHERE camera_code='PTR-CAM-TEST'"
    ).fetchone()
    assert cam is not None  # camera auto-registered


def test_ingest_is_idempotent_no_duplicates(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.ingestion.ingest.INCOMING_DIR", tmp_path / "incoming")
    folder = tmp_path / "incoming" / "PTR-CAM-IDEM"
    folder.mkdir(parents=True)
    _write_jpeg(folder / "same.jpg", seed=3)

    first = ingest_folder(conn=db)
    second = ingest_folder(conn=db)

    assert first == {"PTR-CAM_IDEM": 1} or first == {"PTR-CAM-IDEM": 1}
    assert second["PTR-CAM-IDEM"] == 0  # already ingested -> nothing new
    count = db.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"]
    assert count == 1


def test_non_image_files_skipped(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.ingestion.ingest.INCOMING_DIR", tmp_path / "incoming")
    folder = tmp_path / "incoming" / "PTR-CAM-SKIP"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("not an image")
    (folder / "data.csv").write_text("a,b\n1,2")

    summary = ingest_folder(conn=db)
    assert summary["PTR-CAM-SKIP"] == 0


def test_exif_datetime_preferred_over_mtime(tmp_path):
    """EXIF DateTime must win: mtime is the SD-card copy time, not shutter time."""
    p = tmp_path / "with_exif.jpg"
    _write_jpeg(p, seed=5, exif_datetime="2026:03:14 05:41:00")
    got = read_captured_at(p)
    assert got.startswith("2026-03-14T05:41:00")


def test_falls_back_to_mtime_when_no_exif(tmp_path, capsys):
    p = tmp_path / "no_exif.png"
    Image.new("L", (64, 64), 128).save(p)  # plain PNG, no DateTime tag
    before = time.time()
    got = read_captured_at(p)

    assert "T" in got  # still a valid ISO timestamp
    # stderr warning tells the operator this value is approximate
    err = capsys.readouterr().err.lower()
    assert "exif" in err and ("fallback" in err or "approximate" in err)
