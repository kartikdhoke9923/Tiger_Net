"""Offline-first sync: nothing marked synced unless the push ACTUALLY succeeds."""

import json

from app.review.interface import confirm_sighting
from app.idmatch.matcher import generate_shortlist
from app.sync import sync_queue
from app.sync.sync_queue import build_sync_batch, run_sync


def _make_confirmed_sighting(db, camera_ids, incoming_image, user_id):
    image_id, p = incoming_image(camera_ids["PTR-CAM-001"], seed=100)
    r = generate_shortlist(image_id=image_id, image_path=str(p), conn=db)
    confirm_sighting(r["sighting_id"], tiger_id=1, user_id=user_id, conn=db)
    return image_id


def _make_alert(db, camera_ids, incoming_image):
    image_id, _ = incoming_image(camera_ids["PTR-CAM-002"], name="poacher.jpg")
    db.execute(
        "INSERT INTO human_detections (image_id, camera_id, zone_type) VALUES (?, ?, 'restricted')",
        (image_id, camera_ids["PTR-CAM-002"]),
    )
    return image_id


def test_failed_push_marks_nothing_synced(db, user_ids, camera_ids, incoming_image,
                                          monkeypatch):
    """THE fail-safe guarantee: offline == queued, never lost, never faked."""
    _make_alert(db, camera_ids, incoming_image)
    _make_confirmed_sighting(db, camera_ids, incoming_image, user_ids["officer_priya"])

    monkeypatch.setattr(sync_queue, "push_to_central", lambda payload: False)
    result = run_sync(conn=db)

    assert result == {"alerts_sent": 0, "sightings_sent": 0, "queued_for_retry": 2}
    unsynced = db.execute(
        "SELECT COUNT(*) AS n FROM images WHERE synced_to_central = 1"
    ).fetchone()["n"]
    assert unsynced == 0
    unsent = db.execute(
        "SELECT COUNT(*) AS n FROM human_detections WHERE alert_sent = 1"
    ).fetchone()["n"]
    assert unsent == 0


def test_successful_push_marks_everything_synced(db, user_ids, camera_ids,
                                                 incoming_image, monkeypatch):
    _make_alert(db, camera_ids, incoming_image)
    _make_confirmed_sighting(db, camera_ids, incoming_image, user_ids["officer_priya"])

    pushes = []

    def fake_push(payload):
        pushes.append(payload["type"])
        return True

    monkeypatch.setattr(sync_queue, "push_to_central", fake_push)
    result = run_sync(conn=db)

    assert result == {"alerts_sent": 1, "sightings_sent": 1, "queued_for_retry": 0}
    assert db.execute(
        "SELECT alert_sent FROM human_detections").fetchone()["alert_sent"] == 1
    assert db.execute(
        "SELECT synced_to_central FROM images WHERE id IN (SELECT image_id FROM sightings)"
    ).fetchone()["synced_to_central"] == 1


def test_alerts_push_first_priority_order(db, user_ids, camera_ids, incoming_image,
                                          monkeypatch):
    """PRD section 11: human-detection alerts leave the node before sightings."""
    _make_alert(db, camera_ids, incoming_image)
    _make_confirmed_sighting(db, camera_ids, incoming_image, user_ids["officer_priya"])

    order = []
    monkeypatch.setattr(sync_queue, "push_to_central",
                        lambda payload: (order.append(payload["type"]) or True))
    run_sync(conn=db)

    assert order == ["human_detections", "confirmed_sightings"]


def test_partial_failure_only_commits_what_succeeded(db, user_ids, camera_ids,
                                                     incoming_image, monkeypatch):
    """Alert succeeds, sighting push fails -> exactly the sighting stays queued."""
    _make_alert(db, camera_ids, incoming_image)
    _make_confirmed_sighting(db, camera_ids, incoming_image, user_ids["officer_priya"])

    def flaky_push(payload):
        return payload["type"] == "human_detections"

    monkeypatch.setattr(sync_queue, "push_to_central", flaky_push)
    result = run_sync(conn=db)

    assert result["alerts_sent"] == 1
    assert result["sightings_sent"] == 0
    assert result["queued_for_retry"] == 1


def test_batch_contents_exclude_unconfirmed_and_already_synced(db, user_ids, camera_ids,
                                                               incoming_image):
    # confirmed + pending mix; only the confirmed one is eligible
    _make_confirmed_sighting(db, camera_ids, incoming_image, user_ids["officer_priya"])
    image2, _ = incoming_image(camera_ids["PTR-CAM-001"], seed=555)
    generate_shortlist(image_id=image2, image_path="/tmp/never_confirmed.jpg", conn=db)  # left pending

    batch = build_sync_batch(db)
    assert len(batch["confirmed_sightings"]) == 1   # pending one excluded


def test_failed_attempts_are_logged_for_the_operator(tmp_path, monkeypatch):
    log_file = tmp_path / "sync_queue.jsonl"
    monkeypatch.setattr(sync_queue, "QUEUE_LOG", log_file)

    from app.sync.sync_queue import push_to_central
    push_to_central({"type": "test", "data": [1]})

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["payload"]["type"] == "test"     # operator can see what didn't go out
