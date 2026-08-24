"""Tiger ID shortlist: real ORB scoring, tier boundaries, and the
non-negotiable rule -- no automatic ID assignment, ever."""

import cv2

import pytest

from conftest import make_stripe_image
from app.idmatch.feature_matcher import ORBMatcher, ImageLoadError
from app.idmatch.matcher import _tier_for, generate_shortlist, register_new_tiger


def test_orb_identical_image_scores_perfect():
    p1 = "/tmp/identical_a.jpg"
    cv2.imwrite(p1, make_stripe_image(seed=55))
    m = ORBMatcher()
    assert m.score(p1, p1) == 1.0


def test_orb_same_subject_different_angle_scores_higher_than_different_subject():
    ref = "/tmp/match_ref.jpg"
    cv2.imwrite(ref, make_stripe_image(seed=100))

    recapture = cv2.warpAffine(
        make_stripe_image(seed=100),
        cv2.getRotationMatrix2D((150, 150), 7, 1.0),
        (300, 300), borderValue=200,
    )
    same_path = "/tmp/match_recapture.jpg"
    cv2.imwrite(same_path, recapture)

    other = "/tmp/match_other.jpg"
    cv2.imwrite(other, make_stripe_image(seed=999))

    m = ORBMatcher()
    s_same = m.score(same_path, ref)
    s_other = m.score(other, ref)

    assert 0.3 <= s_same <= 1.0   # rotated copy of the same pattern: clearly related
    assert s_other < 0.15         # unrelated pattern: near-zero signal
    assert s_same > s_other * 3   # separation must be decisive, not marginal


def test_orb_missing_image_raises(tmp_path):
    with pytest.raises(ImageLoadError):
        ORBMatcher().score(str(tmp_path / "nope.jpg"), str(tmp_path / "also_nope.jpg"))


def test_tier_boundaries_match_prd():
    assert _tier_for(0.95) == "high"
    assert _tier_for(0.90) == "high"      # >= 0.90 inclusive
    assert _tier_for(0.89) == "medium"
    assert _tier_for(0.50) == "medium"    # >= 0.50 inclusive
    assert _tier_for(0.49) == "low"
    assert _tier_for(0.02) == "low"


def test_shortlist_ranks_candidates_and_never_assigns_an_id(db, camera_ids, incoming_image):
    """The core PRD guarantee: shortlist rows stay unresolved until a human acts."""
    # second reference tiger with an unrelated pattern
    other_img = "/tmp/ref_other.jpg"
    cv2.imwrite(other_img, make_stripe_image(seed=777))
    db.execute(
        "INSERT INTO tigers (local_id, reference_image_path, status, source)"
        " VALUES ('PTR-LOCAL-002', ?, 'confirmed', 'pench_confirmed')",
        (other_img,),
    )
    db.commit()

    cam = db.execute("SELECT id FROM cameras WHERE camera_code='PTR-CAM-001'").fetchone()
    image_id, path = incoming_image(cam["id"], seed=100)
    query = str(path)  # matches PTR-LOCAL-001's pattern

    result = generate_shortlist(image_id=image_id, image_path=query, conn=db)

    assert not result["no_reference_tigers"]
    assert len(result["candidates"]) <= 5
    scores = [c.score for c in result["candidates"]]
    assert scores == sorted(scores, reverse=True)          # ranked descending
    assert result["candidates"][0].local_id == "PTR-LOCAL-001"

    row = db.execute(
        "SELECT tiger_id, confirmed_at FROM sightings WHERE id=?",
        (result["sighting_id"],),
    ).fetchone()
    # NO AUTO-ID, ever: the shortlist writes a PENDING row only
    assert row["tiger_id"] is None
    assert row["confirmed_at"] is None


def test_shortlist_with_no_reference_tigers_flags_manual_review(db, camera_ids, incoming_image):
    db.execute("DELETE FROM tigers")
    db.commit()
    cam = db.execute("SELECT id FROM cameras WHERE camera_code='PTR-CAM-001'").fetchone()
    image_id, path = incoming_image(cam["id"], seed=11)

    result = generate_shortlist(image_id=image_id, image_path=str(path), conn=db)
    assert result["no_reference_tigers"]
    assert result["candidates"] == []
    assert result["tier"] == "low"   # safest default: full manual review


def test_register_new_tiger_is_provisional(db):
    img = "/tmp/new_tiger.jpg"
    cv2.imwrite(img, make_stripe_image(seed=1234))
    tid = register_new_tiger("PTR-LOCAL-009", img, conn=db)

    row = db.execute("SELECT status, national_id FROM tigers WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "provisional"  # local IDs are provisional by design
    assert row["national_id"] is None      # filled only after ExtractCompare reconciliation
