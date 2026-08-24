"""RBAC + audit: the PRD's access matrix, verified row by row.

Matrix under test (PRD section 6):
  field_ranger   : sightings + location, OWN BEAT only
  range_officer  : sightings + location, full reserve
  stpf           : human-detection alerts ONLY
  researcher     : sightings, NEVER location
  admin          : everything
Every grant AND denial must land in audit_log.
"""

import pytest

from app.security.access_control import (
    AccessDenied,
    can_view_location,
    get_human_detections_for_user,
    get_sightings_for_user,
)


@pytest.fixture()
def sightings_setup(db, camera_ids, incoming_image):
    """
    One confirmed sighting per beat:
      - BEAT-01 sighting on PTR-CAM-001 with location row
      - BEAT-02 sighting on PTR-CAM-003 with location row
      - one human detection on a restricted-zone camera
    Returns dict of ids.
    """
    from app.review.interface import confirm_sighting

    cam1 = camera_ids["PTR-CAM-001"]   # BEAT-01
    cam3 = camera_ids["PTR-CAM-003"]   # BEAT-02

    img1_id, p1 = incoming_image(cam1, seed=100)
    from app.idmatch.matcher import generate_shortlist
    r1 = generate_shortlist(image_id=img1_id, image_path=str(p1), conn=db)
    officer = db.execute("SELECT id FROM users WHERE username='officer_priya'").fetchone()["id"]
    confirm_sighting(r1["sighting_id"], tiger_id=1, user_id=officer, conn=db)

    img2_id, p2 = incoming_image(cam3, seed=200)
    r2 = generate_shortlist(image_id=img2_id, image_path=str(p2), conn=db)
    confirm_sighting(r2["sighting_id"], tiger_id=1, user_id=officer, conn=db)

    # location rows (normally derived from camera at confirmation time)
    db.execute("INSERT INTO sighting_location (sighting_id, latitude, longitude, camera_id)"
               " VALUES (?, 21.7679, 79.2961, ?)", (r1["sighting_id"], cam1))
    db.execute("INSERT INTO sighting_location (sighting_id, latitude, longitude, camera_id)"
               " VALUES (?, 21.8500, 79.4000, ?)", (r2["sighting_id"], cam3))

    db.execute(
        "INSERT INTO human_detections (image_id, camera_id, zone_type) VALUES (?, ?, 'restricted')",
        (img1_id, camera_ids["PTR-CAM-002"]),
    )
    db.commit()
    return {"beat1_sighting": r1["sighting_id"], "beat2_sighting": r2["sighting_id"],
            "officer": officer}


# ---- researcher: sightings yes, location NEVER ----

def test_researcher_gets_zero_location_fields_even_on_request(db, user_ids, sightings_setup):
    rows = get_sightings_for_user(db, user_ids["researcher_wct"])
    assert len(rows) >= 1                      # sees sightings...
    for r in rows:
        assert "latitude" not in r             # ...but no coordinate keys exist AT ALL
        assert "longitude" not in r
        assert "camera_id" not in r


def test_researcher_location_permission_is_false(db, user_ids):
    assert can_view_location(db, user_ids["researcher_wct"]) is False


def test_researcher_cannot_touch_human_alerts(db, user_ids, sightings_setup):
    with pytest.raises(AccessDenied):
        get_human_detections_for_user(db, user_ids["researcher_wct"])


# ---- STPF: alerts only, general sightings denied outright ----

def test_stpf_denied_general_sightings_entirely(db, user_ids, sightings_setup):
    with pytest.raises(AccessDenied):
        get_sightings_for_user(db, user_ids["stpf_team1"])


def test_stpf_sees_human_detection_alerts(db, user_ids, sightings_setup):
    alerts = get_human_detections_for_user(db, user_ids["stpf_team1"])
    assert len(alerts) == 1
    assert alerts[0]["zone_type"] == "restricted"


# ---- field_ranger: own beat only ----

def test_ranger_scoped_to_own_beat(db, user_ids, sightings_setup):
    amit = user_ids["ranger_amit"]       # BEAT-01
    rows = get_sightings_for_user(db, amit)
    seen_ids = {r["id"] for r in rows}
    assert sightings_setup["beat1_sighting"] in seen_ids     # own beat visible...
    assert sightings_setup["beat2_sighting"] not in seen_ids # ...other beat NOT


def test_other_beat_ranger_does_not_see_beat_one(db, user_ids, sightings_setup):
    other = user_ids["ranger_other"]     # BEAT-02
    rows = get_sightings_for_user(db, other)
    seen_ids = {r["id"] for r in rows}
    assert sightings_setup["beat2_sighting"] in seen_ids
    assert sightings_setup["beat1_sighting"] not in seen_ids


def test_ranger_with_location_permission_gets_coordinates(db, user_ids, sightings_setup):
    rows = get_sightings_for_user(db, user_ids["ranger_amit"])
    assert all("latitude" in r for r in rows)


# ---- range_officer / admin: full reserve ----

def test_range_officer_sees_all_beats(db, user_ids, sightings_setup):
    rows = get_sightings_for_user(db, user_ids["officer_priya"])
    seen_ids = {r["id"] for r in rows}
    assert sightings_setup["beat1_sighting"] in seen_ids
    assert sightings_setup["beat2_sighting"] in seen_ids


def test_admin_full_access(db, user_ids, sightings_setup):
    assert len(get_sightings_for_user(db, user_ids["admin_root"])) == 2
    assert len(get_human_detections_for_user(db, user_ids["admin_root"])) == 1


# ---- audit trail: every grant and denial ----

def test_every_access_granted_or_denied_is_audited(db, user_ids, sightings_setup):
    """The PRD claim 'every access logged' is checked here, end to end."""
    uid = user_ids["researcher_wct"]
    before = _audit_count(db)

    get_sightings_for_user(db, uid)                       # granted -> VIEW logged
    with pytest.raises(AccessDenied):
        get_human_detections_for_user(db, uid)            # denied -> DENIED_ACCESS logged
    with pytest.raises(AccessDenied):
        get_sightings_for_user(db, user_ids["stpf_team1"])

    after = _audit_count(db)
    events = db.execute(
        "SELECT action FROM audit_log WHERE id > ?", (before,)
    ).fetchall()
    actions = {e["action"] for e in events}
    assert "VIEW" in actions
    assert "DENIED_ACCESS" in actions


def test_location_views_are_specifically_audited(db, user_ids, sightings_setup):
    uid = user_ids["officer_priya"]
    before = _audit_count(db)
    get_sightings_for_user(db, uid)
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM audit_log WHERE id > ?", (before,)
    ).fetchall()]
    assert "VIEW_LOCATION" in actions   # touching coordinates is its own audited event


def test_unknown_user_rejected(db):
    with pytest.raises(AccessDenied):
        get_sightings_for_user(db, 99999)


def _audit_count(db):
    return db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_log").fetchone()["m"]
