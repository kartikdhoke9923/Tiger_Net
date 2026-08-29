"""
Tiger ID shortlist / matching.

Core rule from PRD (non-negotiable, do not "optimize" this away later):
NO AUTOMATIC ID ASSIGNMENT, EVER. This module only ever produces a ranked
shortlist of candidates + a confidence tier. A human always makes the final
call via app/review. This is a population-of-an-endangered-species dataset;
a wrong silent auto-ID corrupts the record permanently.

Confidence tiers (as agreed — NOT a flat 50% cutoff):
  high    (>= 0.90) -> shown as "likely match", one-click human confirm
  medium  (0.50-0.90) -> ranked shortlist of top N, human picks
  low     (< 0.50) -> flagged "possible new individual", full manual review

Matching backend:
Now wired to a REAL matcher (app/idmatch/feature_matcher.py, ORB keypoint
matching via OpenCV) instead of a random stub. This is a working classical
computer-vision baseline, tested against real image comparisons -- it is
NOT yet a trained deep re-ID model, and will not match state-of-the-art
accuracy from papers using pose-guided deep learning on ATRW. Good enough
to validate the pipeline and get real (if rough) similarity signal now;
swap for a trained embedding model later without changing anything below
this line -- see `Matcher` interface.

Also critical: any ID this module creates is LOCAL/PROVISIONAL. It is
tagged as such in the DB (tigers.status = 'provisional') until reconciled
with the national ExtractCompare database during sync. See app/sync.
"""

from dataclasses import dataclass
from pathlib import Path
import random

from app.db.schema import get_connection


@dataclass
class Candidate:
    tiger_id: int
    local_id: str
    score: float  # 0.0 - 1.0, higher = more likely same individual


class Matcher:
    def score(self, query_image_path: str, reference_image_path: str) -> float:
        raise NotImplementedError


class StubMatcher(Matcher):
    """
    Fallback only, used if OpenCV isn't installed. Uses a seeded
    pseudo-random score -- NOT real matching. Kept so the pipeline doesn't
    hard-crash in an environment without opencv, but get_matcher() below
    prefers the real ORBMatcher whenever it's available.
    """
    def score(self, query_image_path: str, reference_image_path: str) -> float:
        seed = hash((Path(query_image_path).stem, Path(reference_image_path).stem)) % (10**8)
        rng = random.Random(seed)
        return round(rng.uniform(0.2, 0.98), 3)


def get_matcher() -> Matcher:
    try:
        from app.idmatch.feature_matcher import ORBMatcher
        return ORBMatcher()
    except ImportError:
        print("[idmatch] WARNING: opencv-python not installed -- falling back to "
              "StubMatcher (random scores, not real matching). "
              "Run: pip install opencv-python-headless numpy --break-system-packages")
        return StubMatcher()


def _tier_for(score: float) -> str:
    if score >= 0.90:
        return "high"
    if score >= 0.50:
        return "medium"
    return "low"


def generate_shortlist(image_id: int, image_path: str, top_n: int = 5, conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    # Don't re-shortlist an image that already has an unresolved sighting --
    # this is what was silently duplicating rows on every pipeline re-run.
    existing = conn.execute(
        "SELECT id, confidence_tier FROM sightings WHERE image_id = ? AND tiger_id IS NULL AND confirmed_at IS NULL",
        (image_id,),
    ).fetchone()
    if existing:
        if own_conn:
            conn.close()
        return {"sighting_id": existing["id"], "candidates": [], "tier": existing["confidence_tier"],
                "no_reference_tigers": False, "already_pending": True}

    matcher = get_matcher()
    # ... rest of the function stays exactly the same from here


def register_new_tiger(local_id: str, reference_image_path: str, conn=None):
    """
    Called by the review module when a human confirms 'this is a new
    individual, not in the reference set'. Status is 'provisional' until
    reconciled with the national ID database.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    conn.execute(
        """INSERT INTO tigers (local_id, reference_image_path, status, first_seen_at, last_seen_at)
           VALUES (?, ?, 'provisional', datetime('now'), datetime('now'))""",
        (local_id, reference_image_path),
    )
    conn.commit()
    tiger_id = conn.execute("SELECT id FROM tigers WHERE local_id = ?", (local_id,)).fetchone()["id"]

    if own_conn:
        conn.close()
    return tiger_id


if __name__ == "__main__":
    conn = get_connection()
    candidates = conn.execute(
        "SELECT id, file_path FROM images WHERE classification = 'tiger_candidate' AND reviewed = 0"
    ).fetchall()
    if not candidates:
        print("[idmatch] no unreviewed tiger-candidate images found. Run prefilter first.")
    for row in candidates:
        result = generate_shortlist(row["id"], row["file_path"], conn=conn)
        print(f"[idmatch] image {row['id']}: tier={result['tier']}, "
              f"top candidates={[(c.local_id, c.score) for c in result['candidates']]}")
    conn.close()
