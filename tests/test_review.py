"""Human review: mandatory confirmation, role checks, dismiss flow."""

import cv2
import pytest

from conftest import make_stripe_image
from app.idmatch.matcher import generate_shortlist
from app.review.interface import (
    confirm_sighting,
    dismiss_sighting,
    mark_new_individual,
    pending_reviews,
)
from app.security.access_control import AccessDenied


@pytest.fixture()
def pending_sighting(db, incoming_image):
    """A tiger-candidate image that has gone through shortlist generation."""
    cam = db.execute("SELECT id FROM cameras WHERE camera_code='PTR-CAM-001'").fetchone()
    image_id, path = incoming_image(cam["id"], seed=100)
    result = generate_shortlist(image_id=image_id, image_path=str(path), conn=db)
    return result["sighting_id"], image_id


def test_pending_queue_lists_unresolved_only(db, pending_sighting):
    sid, _ = pending_sighting
    queue = pending_reviews(conn=db)
    assert sid in [q["sighting_id"] for q in queue]


def test_confirm_requires_human_user_and_sets_everything(db, user_ids, pending_sighting):
    sid, image_id = pending_sighting

    confirm_sighting(sid, tiger_id=1, user_id=user_ids["officer_priya"], conn=db)

    s = db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert s["tiger_id"] == 1
    assert s["confirmed_by_user_id"] == user_ids["officer_priya"]
    assert s["confirmed_at"] is not None
    assert s["resolution"] == "confirmed"
    img = db.execute("SELECT reviewed FROM images WHERE id=?", (image_id,)).fetchone()
    assert img["reviewed"] == 1

    # audit entry exists for the confirmation action
    audit = db.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='CONFIRM_SIGHTING' AND resource_id=?",
        (sid,),
    ).fetchone()["n"]
    assert audit == 1

    assert sid not in [q["sighting_id"] for q in pending_reviews(conn=db)]


def test_researcher_cannot_confirm(db, user_ids, pending_sighting):
    sid, _ = pending_sighting
    with pytest.raises(AccessDenied):
        confirm_sighting(sid, tiger_id=1, user_id=user_ids["researcher_wct"], conn=db)
    # nothing was confirmed by the denied attempt
    s = db.execute("SELECT tiger_id FROM sightings WHERE id=?", (sid,)).fetchone()
    assert s["tiger_id"] is None


def test_stpf_cannot_confirm_or_register(db, user_ids, pending_sighting):
    sid, _ = pending_sighting
    with pytest.raises(AccessDenied):
        confirm_sighting(sid, tiger_id=1, user_id=user_ids["stpf_team1"], conn=db)
    with pytest.raises(AccessDenied):
        mark_new_individual(sid, "PTR-LOCAL-050", user_ids["stpf_team1"], conn=db)


def test_denied_confirmation_is_audited(db, user_ids, pending_sighting):
    sid, _ = pending_sighting
    with pytest.raises(AccessDenied):
        confirm_sighting(sid, tiger_id=1, user_id=user_ids["researcher_wct"], conn=db)
    n = db.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='DENIED_ACCESS'"
    ).fetchone()["n"]
    assert n >= 1


def test_mark_new_individual_creates_provisional_tiger_and_resolves(db, user_ids, pending_sighting):
    sid, image_id = pending_sighting

    tid = mark_new_individual(sid, "PTR-LOCAL-042", user_ids["ranger_amit"], conn=db)

    t = db.execute("SELECT * FROM tigers WHERE id=?", (tid,)).fetchone()
    assert t["local_id"] == "PTR-LOCAL-042"
    assert t["status"] == "provisional"
    s = db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert s["tiger_id"] == tid
    assert s["is_new_individual"] == 1
    assert s["resolution"] == "new_individual"
    assert sid not in [q["sighting_id"] for q in pending_reviews(conn=db)]


def test_dismiss_keeps_record_but_clears_queue(db, user_ids, pending_sighting):
    """False positives must be clearable without destroying the audit trail."""
    sid, image_id = pending_sighting

    dismiss_sighting(sid, user_id=user_ids["ranger_amit"],
                     reason="deer with odd lighting", conn=db)

    s = db.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    assert s["resolution"] == "dismissed"
    assert s["tiger_id"] is None              # never got an ID -- correct
    assert s["confirmed_by_user_id"] == user_ids["ranger_amit"]
    assert sid not in [q["sighting_id"] for q in pending_reviews(conn=db)]
    img = db.execute("SELECT reviewed FROM images WHERE id=?", (image_id,)).fetchone()
    assert img["reviewed"] == 1
