"""
Loader for the ATRW (Amur Tiger Re-identification in the Wild) dataset,
to populate this system's DB with real tiger images for pipeline testing.

READ THIS BEFORE RUNNING -- I have not seen your actual downloaded files,
so I cannot guarantee the exact column names / folder layout below match
what you have. ATRW re-ID releases are typically structured as:
    <root>/
        train/  (or reid_train, atrw_reid_train, etc.)
            <image files>.jpg
        test/
            <image files>.jpg
        reid_list_train.csv   (or similar -- maps filename -> identity id)
        reid_list_test.csv

CHECK YOUR ACTUAL FILES FIRST:
    - Open the CSV/annotation file in a text editor
    - Confirm: does each row look like "0001.jpg,42" (filename, identity_id)?
    - If it's JSON instead of CSV, or the columns are named differently,
      adjust `parse_identity_list()` below accordingly -- that function is
      the only place you should need to change.

CRITICAL -- what this loader does NOT do, on purpose:
- Does not write these tigers into the normal Pench population data path.
  Every tiger loaded from ATRW gets source='atrw_benchmark' (see schema.py).
  This keeps Amur-tiger zoo data from ever being counted as a real Pench
  Bengal tiger sighting. Do not remove or bypass this tagging.
- Does not run these through the human-review confirm flow -- ATRW identity
  labels are already ground truth from the dataset, so we insert them
  directly as 'confirmed' status, tagged 'atrw_benchmark'.

What this is actually useful for:
- Feeding real (if wrong-subspecies) tiger images through generate_shortlist()
  to sanity-check the matcher end-to-end on real photographic content, not
  just synthetic test patterns
- A future step: use these embeddings/matches to evaluate matcher accuracy
  with a real train/test split (ATRW ships one) before trusting it on
  actual Pench images
"""

import csv
from pathlib import Path
from app.db.schema import get_connection


def parse_identity_list(csv_path: str):
    """
    ADJUST THIS FUNCTION to match your actual file's format.
    Assumed format (verify against your file): each row is
        <image_filename>,<identity_id>
    with no header row. If your file has a header, or different column
    order, or is tab-separated, fix the reading logic below -- don't guess
    downstream, fix it here so everything after this function can rely on
    a clean (filename, identity_id) pair.
    """
    pairs = []
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            filename, identity_id = row[0].strip(), row[1].strip()
            if filename.lower() in ("filename", "image", "image_id"):
                continue  # skip a possible header row
            pairs.append((filename, identity_id))
    return pairs


def load_atrw_as_reference_tigers(images_dir: str, identity_csv: str, limit_individuals: int = None, conn=None):
    """
    Reads identity pairs, picks ONE representative image per individual
    (the first one encountered) to act as that individual's reference image,
    and registers each as a tiger with source='atrw_benchmark'.

    limit_individuals: cap how many distinct tigers to load, useful for a
    quick test run instead of loading all 92.

    Returns: dict of {atrw_identity_id: db_tiger_id}
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    images_dir = Path(images_dir)
    pairs = parse_identity_list(identity_csv)

    seen_identities = {}
    for filename, identity_id in pairs:
        if identity_id in seen_identities:
            continue
        img_path = images_dir / filename
        if not img_path.exists():
            print(f"[atrw_loader] WARNING: {img_path} listed in CSV but not found on disk -- skipping")
            continue
        seen_identities[identity_id] = str(img_path)
        if limit_individuals and len(seen_identities) >= limit_individuals:
            break

    id_map = {}
    for identity_id, img_path in seen_identities.items():
        local_id = f"ATRW-{identity_id}"
        conn.execute(
            """INSERT OR IGNORE INTO tigers
               (local_id, reference_image_path, status, first_seen_at, last_seen_at, source)
               VALUES (?, ?, 'confirmed', datetime('now'), datetime('now'), 'atrw_benchmark')""",
            (local_id, img_path),
        )
        row = conn.execute("SELECT id FROM tigers WHERE local_id = ?", (local_id,)).fetchone()
        id_map[identity_id] = row["id"]

    conn.commit()
    if own_conn:
        conn.close()

    print(f"[atrw_loader] loaded {len(id_map)} reference individuals (source=atrw_benchmark)")
    return id_map


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python -m app.idmatch.atrw_loader <images_dir> <identity_csv>")
        print("Example: python -m app.idmatch.atrw_loader /path/to/atrw/train /path/to/reid_list_train.csv")
        print("\nNOTE: verify parse_identity_list() matches your actual CSV format first --")
        print("see module docstring.")
        sys.exit(1)

    result = load_atrw_as_reference_tigers(sys.argv[1], sys.argv[2], limit_individuals=10)
    print(f"Loaded: {result}")
