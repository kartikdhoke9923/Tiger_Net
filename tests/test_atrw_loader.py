"""ATRW loader: benchmark data must NEVER be able to contaminate real counts."""

import csv

import cv2

from conftest import make_stripe_image
from app.idmatch.atrw_loader import load_atrw_as_reference_tigers, parse_identity_list


def _write_atrw_fixture(tmp_path):
    """Fake-but-valid ATRW layout: images dir + identity CSV."""
    img_dir = tmp_path / "train"
    img_dir.mkdir()
    identities = [("0001.jpg", "1"), ("0002.jpg", "1"), ("0003.jpg", "2"), ("0004.jpg", "3")]
    for i, (fname, _) in enumerate(identities):
        cv2.imwrite(str(img_dir / fname), make_stripe_image(seed=500 + i))
    csv_path = tmp_path / "reid_list_train.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        for fname, ident in identities:
            w.writerow([fname, ident])
    return img_dir, csv_path


def test_parse_identity_list_basic_and_header_skip(tmp_path):
    p = tmp_path / "list.csv"
    p.write_text("filename,identity\n0001.jpg,7\n\n0002.jpg,8\n")
    pairs = parse_identity_list(str(p))
    assert ("0001.jpg", "7") in pairs
    assert ("0002.jpg", "8") in pairs
    assert all("identity" not in f for f, _ in pairs)   # header skipped


def test_loaded_tigers_are_tagged_atrw_benchmark_and_confirmed(db, tmp_path):
    img_dir, csv_path = _write_atrw_fixture(tmp_path)
    id_map = load_atrw_as_reference_tigers(str(img_dir), str(csv_path), conn=db)

    assert len(id_map) == 3  # 4 files, 3 distinct individuals (one rep image each)
    rows = db.execute(
        "SELECT local_id, status, source FROM tigers WHERE source='atrw_benchmark'"
    ).fetchall()
    assert {r["local_id"] for r in rows} == {"ATRW-1", "ATRW-2", "ATRW-3"}
    assert all(r["status"] == "confirmed" for r in rows)
    assert all(r["source"] == "atrw_benchmark" for r in rows)


def test_real_population_count_query_excludes_benchmark_data(db, tmp_path):
    """THE hard rule from PRD section 9: pench_confirmed filter is what keeps the
    Amur-zoo benchmark tigers out of the real Bengal population count."""
    img_dir, csv_path = _write_atrw_fixture(tmp_path)
    load_atrw_as_reference_tigers(str(img_dir), str(csv_path), conn=db)

    # the db fixture already has one real Pench tiger alongside them
    db.execute(
        "INSERT INTO tigers (local_id, status, source) VALUES ('PTR-LOCAL-077', 'confirmed', 'pench_confirmed')"
    )
    db.commit()

    total = db.execute("SELECT COUNT(*) AS n FROM tigers").fetchone()["n"]
    real = db.execute(
        "SELECT COUNT(*) AS n FROM tigers WHERE source='pench_confirmed'"
    ).fetchone()["n"]
    benchmark = db.execute(
        "SELECT COUNT(*) AS n FROM tigers WHERE source='atrw_benchmark'"
    ).fetchone()["n"]
    assert total == 5          # everything loaded...
    assert benchmark == 3      # ...benchmark rows exist...
    assert real == 2           # ...but only Pench tigers count for the population


def test_missing_files_skipped_not_fatal(db, tmp_path):
    img_dir = tmp_path / "train"
    img_dir.mkdir()
    csv_path = tmp_path / "reid_list_train.csv"
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerows([["ghost.jpg", "9"], ["real.jpg", "10"]])
    cv2.imwrite(str(img_dir / "real.jpg"), make_stripe_image(seed=600))

    id_map = load_atrw_as_reference_tigers(str(img_dir), str(csv_path), conn=db)
    assert set(id_map.keys()) == {"10"}


def test_limit_individuals_caps_load(db, tmp_path):
    img_dir, csv_path = _write_atrw_fixture(tmp_path)
    id_map = load_atrw_as_reference_tigers(str(img_dir), str(csv_path),
                                           limit_individuals=1, conn=db)
    assert len(id_map) == 1
