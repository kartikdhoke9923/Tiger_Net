"""
Role-based access control + audit logging.

Why this exists as its own module (per PRD section 5.5):
Location data of tiger sightings is the single most sensitive field in this
system -- if it leaks, poachers get a hunting map. Every read of location
data MUST go through here, MUST check role, and MUST be logged. Do not query
sighting_location directly from anywhere else in the codebase.

Access matrix (matches PRD section on end users):
- field_ranger   : sightings + location, own beat only
- range_officer  : sightings + location, full reserve
- stpf           : human_detections only (not general tiger sightings)
- researcher     : sightings, NO location (aggregated/anonymized only)
- admin          : everything (system maintenance, not routine use)
"""

from app.db.schema import get_connection

PERMISSIONS = {
    "field_ranger":  {"sightings": "own_beat", "location": "own_beat", "human_detections": False},
    "range_officer": {"sightings": "all",      "location": "all",      "human_detections": True},
    "stpf":          {"sightings": False,      "location": False,      "human_detections": True},
    "researcher":    {"sightings": "all",      "location": False,      "human_detections": False},
    "admin":         {"sightings": "all",      "location": "all",      "human_detections": True},
}


class AccessDenied(Exception):
    pass


def _log(conn, user_id, action, resource, resource_id=None):
    conn.execute(
        "INSERT INTO audit_log (user_id, action, resource, resource_id) VALUES (?, ?, ?, ?)",
        (user_id, action, resource, resource_id),
    )
    conn.commit()


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise AccessDenied(f"Unknown user_id {user_id}")
    return row


def can_view_location(conn, user_id) -> bool:
    user = get_user(conn, user_id)
    perm = PERMISSIONS.get(user["role"], {})
    return bool(perm.get("location"))


def get_sightings_for_user(conn, user_id):
    """
    Returns sightings visible to this user, respecting role + beat scope.
    Location is joined in ONLY if the role permits it -- researchers and
    STPF never get lat/long back from this function, even if they ask.
    """
    user = get_user(conn, user_id)
    perm = PERMISSIONS.get(user["role"])
    if not perm or perm["sightings"] is False:
        _log(conn, user_id, "DENIED_ACCESS", "sightings")
        raise AccessDenied(f"Role '{user['role']}' cannot view sightings")

    include_location = bool(perm["location"])
    scope_clause = ""
    params = []

    if perm["sightings"] == "own_beat":
        scope_clause = """
            JOIN images im ON im.id = s.image_id
            JOIN cameras c ON c.id = im.camera_id
            WHERE c.beat_id = ?
        """
        params.append(user["beat_id"])

    if include_location:
        query = f"""
            SELECT s.id, s.tiger_id, s.confirmed_at, s.confidence_tier,
                   sl.latitude, sl.longitude, sl.camera_id
            FROM sightings s
            LEFT JOIN sighting_location sl ON sl.sighting_id = s.id
            {scope_clause}
        """
        _log(conn, user_id, "VIEW_LOCATION", "sighting_location")
    else:
        query = f"""
            SELECT s.id, s.tiger_id, s.confirmed_at, s.confidence_tier
            FROM sightings s
            {scope_clause}
        """

    _log(conn, user_id, "VIEW", "sightings")
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_human_detections_for_user(conn, user_id):
    user = get_user(conn, user_id)
    perm = PERMISSIONS.get(user["role"])
    if not perm or not perm["human_detections"]:
        _log(conn, user_id, "DENIED_ACCESS", "human_detections")
        raise AccessDenied(f"Role '{user['role']}' cannot view human detection alerts")

    _log(conn, user_id, "VIEW", "human_detections")
    rows = conn.execute("SELECT * FROM human_detections ORDER BY detected_at DESC").fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # Quick self-check demonstrating the access control actually restricts something
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, role, beat_id) VALUES (1, 'demo_ranger', 'field_ranger', 'BEAT-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, role) VALUES (2, 'demo_researcher', 'researcher')"
    )
    conn.commit()
    print("field_ranger sees location:", can_view_location(conn, 1))
    print("researcher sees location:", can_view_location(conn, 2))
    conn.close()
