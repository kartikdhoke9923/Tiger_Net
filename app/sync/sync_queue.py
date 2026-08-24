"""
Offline-first sync layer.

Why this exists (per DFO-flagged connectivity constraint):
Range offices at Pench cannot assume continuous internet. Every module above
this one (ingestion, prefilter, idmatch, review) works entirely on local
SQLite with zero network calls. This module is the ONLY place that attempts
network I/O, and it is designed to fail safe: if the push fails, nothing is
lost, nothing is marked synced, and it retries next run.

What gets synced:
- Confirmed sightings (tiger_id, confidence, confirmed_by, confirmed_at)
- Local tiger records marked 'provisional' -> sent for national reconciliation
- Human detection alerts (highest priority, sent first)

What does NOT get synced blindly:
- Raw images are not pushed wholesale in this stub (bandwidth-heavy; real
  deployment should sync metadata first, images on a lower-priority queue
  or via physical media, per PRD note on retrieval logistics)

This is a stub HTTP push (`push_to_central`) -- swap the URL / auth for your
actual central server before field use. The retry/queue logic around it is
real and does not need to change.
"""

import json
from pathlib import Path
from datetime import datetime
from app.db.schema import get_connection

QUEUE_LOG = Path(__file__).resolve().parent.parent.parent / "logs" / "sync_queue.jsonl"
CENTRAL_ENDPOINT = "https://REPLACE-ME.pench-central.example/api/sync"  # placeholder, not live


class SyncError(Exception):
    pass


def push_to_central(payload: dict) -> bool:
    """
    STUB. Replace with a real request (e.g. `requests.post(CENTRAL_ENDPOINT,
    json=payload, timeout=10)`) once a central endpoint exists. Returns
    False here unconditionally so the queue logic can be tested without a
    live network dependency -- this makes 'no internet' the default state,
    which matches the real field constraint more honestly than pretending
    success.
    """
    QUEUE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_LOG, "a") as f:
        f.write(json.dumps({"attempted_at": datetime.now().isoformat(), "payload": payload}) + "\n")
    return False  # simulate "no connectivity" until wired to a real endpoint


def build_sync_batch(conn):
    """Gathers everything eligible for sync that hasn't been sent yet."""
    alerts = conn.execute(
        "SELECT * FROM human_detections WHERE alert_sent = 0"
    ).fetchall()

    sightings = conn.execute(
        """SELECT s.id, s.tiger_id, s.confidence_tier, s.confirmed_at, t.local_id, t.national_id
           FROM sightings s
           JOIN tigers t ON t.id = s.tiger_id
           JOIN images i ON i.id = s.image_id
           WHERE s.confirmed_at IS NOT NULL AND i.synced_to_central = 0"""
    ).fetchall()

    return {
        "human_detections": [dict(r) for r in alerts],
        "confirmed_sightings": [dict(r) for r in sightings],
    }


def run_sync(conn=None):
    """
    Attempts one sync cycle. Priority order: human detection alerts first
    (time-sensitive, security-relevant), then confirmed sightings.
    Nothing is marked synced unless push_to_central() actually succeeds.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    batch = build_sync_batch(conn)
    result = {"alerts_sent": 0, "sightings_sent": 0, "queued_for_retry": 0}

    if batch["human_detections"]:
        ok = push_to_central({"type": "human_detections", "data": batch["human_detections"]})
        if ok:
            for alert in batch["human_detections"]:
                conn.execute(
                    "UPDATE human_detections SET alert_sent = 1, alert_sent_at = datetime('now') WHERE id = ?",
                    (alert["id"],),
                )
            result["alerts_sent"] = len(batch["human_detections"])
        else:
            result["queued_for_retry"] += len(batch["human_detections"])

    if batch["confirmed_sightings"]:
        ok = push_to_central({"type": "confirmed_sightings", "data": batch["confirmed_sightings"]})
        if ok:
            for s in batch["confirmed_sightings"]:
                conn.execute(
                    """UPDATE images SET synced_to_central = 1
                       WHERE id = (SELECT image_id FROM sightings WHERE id = ?)""",
                    (s["id"],),
                )
            result["sightings_sent"] = len(batch["confirmed_sightings"])
        else:
            result["queued_for_retry"] += len(batch["confirmed_sightings"])

    conn.commit()
    if own_conn:
        conn.close()
    return result


if __name__ == "__main__":
    outcome = run_sync()
    print(f"[sync] {outcome}")
    print(f"[sync] Note: CENTRAL_ENDPOINT is a placeholder — nothing actually syncs yet. "
          f"Failed attempts logged to {QUEUE_LOG}")
