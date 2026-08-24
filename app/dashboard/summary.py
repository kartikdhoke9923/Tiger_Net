"""
Local range-office dashboard. Text/CLI summary for now -- this is
deliberately the least-built module (per PRD build order, it's last and
lowest priority). Swap for a real web dashboard later; the data-gathering
functions below can be reused as-is by an API layer.

Every data pull goes through app/security/access_control.py -- this module
never queries sightings/location tables directly.
"""

from app.db.schema import get_connection
from app.security.access_control import get_sightings_for_user, get_human_detections_for_user, AccessDenied


def render_dashboard(user_id: int):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise ValueError(f"No such user_id {user_id}")

    print(f"\n=== Dashboard for {user['username']} ({user['role']}) ===\n")

    try:
        sightings = get_sightings_for_user(conn, user_id)
        print(f"Sightings visible to you: {len(sightings)}")
        for s in sightings[:10]:
            loc = f" @ ({s.get('latitude')}, {s.get('longitude')})" if "latitude" in s else ""
            print(f"  - sighting #{s['id']} tiger_id={s['tiger_id']} tier={s['confidence_tier']}{loc}")
    except AccessDenied as e:
        print(f"Sightings: access denied ({e})")

    try:
        alerts = get_human_detections_for_user(conn, user_id)
        print(f"\nHuman detection alerts: {len(alerts)}")
        for a in alerts[:10]:
            print(f"  - ALERT camera_id={a['camera_id']} zone={a['zone_type']} at {a['detected_at']} "
                  f"sent={bool(a['alert_sent'])}")
    except AccessDenied as e:
        print(f"Human detection alerts: access denied ({e})")

    cam_health = conn.execute(
        "SELECT camera_code, battery_pct, status, last_seen_at FROM cameras"
    ).fetchall()
    print(f"\nCamera health ({len(cam_health)} cameras):")
    for c in cam_health:
        print(f"  - {c['camera_code']}: {c['status']}, battery={c['battery_pct']}%, last_seen={c['last_seen_at']}")

    conn.close()


if __name__ == "__main__":
    import sys
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    render_dashboard(uid)
