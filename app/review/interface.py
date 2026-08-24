"""
Human review interface. CLI for now -- swap for a web UI later without
changing the logic below, since all the real work happens in confirm_sighting()
and mark_new_individual(), not in the I/O.

Rule enforced here (matches idmatch/matcher.py docstring): a sighting is
NEVER written as confirmed without a human user_id attached. There is no
code path that sets sightings.tiger_id without going through confirm_sighting().
"""

from app.db.schema import get_connection
from app.security.access_control import get_user, _log, AccessDenied


def pending_reviews(conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    rows = conn.execute(
        """SELECT s.id as sighting_id, s.image_id, s.match_confidence, s.confidence_tier,
                  i.file_path, i.captured_at
           FROM sightings s
           JOIN images i ON i.id = s.image_id
           WHERE s.tiger_id IS NULL AND s.confirmed_at IS NULL
             AND (s.resolution IS NULL OR s.resolution = '')
           ORDER BY s.confidence_tier DESC, s.match_confidence DESC"""
    ).fetchall()
    if own_conn:
        conn.close()
    return [dict(r) for r in rows]


def _require_review_role(conn, user_id, action_label):
    user = get_user(conn, user_id)
    if user["role"] not in ("field_ranger", "range_officer", "admin"):
        _log(conn, user_id, "DENIED_ACCESS", action_label)
        raise AccessDenied(f"Role '{user['role']}' is not permitted to perform review actions")
    return user


def confirm_sighting(sighting_id: int, tiger_id: int, user_id: int, conn=None):
    """
    Human confirms this image is a specific known tiger.
    Requires a real user with review permission -- role check kept simple
    here (any non-'stpf', non-'researcher' role can confirm); tighten as needed.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    _require_review_role(conn, user_id, "sightings")

    conn.execute(
        """UPDATE sightings
           SET tiger_id = ?, confirmed_by_user_id = ?, confirmed_at = datetime('now'),
               resolution = 'confirmed'
           WHERE id = ?""",
        (tiger_id, user_id, sighting_id),
    )
    row = conn.execute("SELECT image_id FROM sightings WHERE id = ?", (sighting_id,)).fetchone()
    conn.execute("UPDATE images SET reviewed = 1 WHERE id = ?", (row["image_id"],))
    conn.execute(
        "UPDATE tigers SET last_seen_at = datetime('now') WHERE id = ?", (tiger_id,)
    )
    conn.execute(
        "INSERT INTO audit_log (user_id, action, resource, resource_id) VALUES (?, 'CONFIRM_SIGHTING', 'sightings', ?)",
        (user_id, sighting_id),
    )
    conn.commit()
    if own_conn:
        conn.close()


def mark_new_individual(sighting_id: int, local_id: str, user_id: int, conn=None):
    """Human confirms this is a tiger not currently in the reference database."""
    from app.idmatch.matcher import register_new_tiger

    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    _require_review_role(conn, user_id, "sightings")

    image_row = conn.execute(
        "SELECT i.file_path FROM sightings s JOIN images i ON i.id = s.image_id WHERE s.id = ?",
        (sighting_id,),
    ).fetchone()

    tiger_id = register_new_tiger(local_id, image_row["file_path"], conn=conn)

    conn.execute(
        """UPDATE sightings
           SET tiger_id = ?, confirmed_by_user_id = ?, confirmed_at = datetime('now'),
               is_new_individual = 1, resolution = 'new_individual'
           WHERE id = ?""",
        (tiger_id, user_id, sighting_id),
    )
    row = conn.execute("SELECT image_id FROM sightings WHERE id = ?", (sighting_id,)).fetchone()
    conn.execute("UPDATE images SET reviewed = 1 WHERE id = ?", (row["image_id"],))
    _log(conn, user_id, "CONFIRM_NEW_TIGER", "sightings", sighting_id)
    conn.commit()
    if own_conn:
        conn.close()
    return tiger_id


def dismiss_sighting(sighting_id: int, user_id: int, reason: str = "", conn=None):
    """
    Reviewer marks this 'tiger candidate' as a false positive (e.g. a deer
    the pre-filter over-promoted). The sighting row is kept with
    resolution='dismissed' for the audit trail -- never deleted -- and the
    image leaves the review queue.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    _require_review_role(conn, user_id, "sightings")

    row = conn.execute("SELECT image_id FROM sightings WHERE id = ?", (sighting_id,)).fetchone()
    if not row:
        raise ValueError(f"No sighting {sighting_id}")

    conn.execute(
        """UPDATE sightings
           SET confirmed_by_user_id = ?, confirmed_at = datetime('now'),
               resolution = 'dismissed'
           WHERE id = ?""",
        (user_id, sighting_id),
    )
    conn.execute("UPDATE images SET reviewed = 1 WHERE id = ?", (row["image_id"],))
    _log(conn, user_id, "DISMISS_SIGHTING", "sightings", sighting_id)
    conn.commit()
    if own_conn:
        conn.close()


def run_cli():
    """Simple terminal review loop for local testing / demo. Replace with web UI later."""
    conn = get_connection()
    queue = pending_reviews(conn)
    if not queue:
        print("[review] no pending reviews.")
        return

    print(f"[review] {len(queue)} sightings awaiting review.\n")
    for item in queue:
        print(f"Sighting #{item['sighting_id']} | tier={item['confidence_tier']} "
              f"| score={item['match_confidence']} | image={item['file_path']} "
              f"| captured={item['captured_at']}")
    conn.close()


if __name__ == "__main__":
    run_cli()
