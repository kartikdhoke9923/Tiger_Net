"""
Offline-first web dashboard for the range office.

Why Python stdlib http.server and NOT Flask/Django:
- PRD section 11: all core modules run with zero network dependency. A range
  office machine may not have (and must not need) a package manager session
  to run this. http.server is in the standard library; there are no CDN
  assets, webfonts, or external JS -- every byte of this UI is served from
  local disk. Swap for a proper framework later ONLY if you also accept the
  dependency-management burden on field hardware.

Security posture (prototype-honest):
- Login: username + PIN via app/security/auth.py (PBKDF2, salted). This
  demonstrates real per-user sessions driving RBAC; it is NOT production
  identity management. Rotate/replace before field deployment.
- Sessions: random 256-bit tokens held server-side, cookie HttpOnly +
  SameSite=Strict. In-memory store = restart logs everyone out; acceptable
  for a single range-office node.
- CSRF: per-session token required on every POST.
- RBAC: NO route queries sightings/location/human_detections directly --
  everything goes through app/security/access_control.py so role checks and
  audit logging stay in exactly one place. Web-specific denials (e.g. STPF
  opening the review queue) are logged here too.
- Location fields never appear in HTML for roles without location
  permission -- get_sightings_for_user() simply does not return them.

Roles -> what they get here (mirrors PRD access matrix):
  field_ranger   : dashboard w/ own-beat sightings + location, review queue
  range_officer  : reserve-wide sightings + location, review queue, alerts
  stpf           : human-detection alerts ONLY (review/dashboard data denied)
  researcher     : sightings WITHOUT location, no review actions
  admin          : everything incl. audit log viewer

Run:
    python3 -m app.dashboard.web            # http://127.0.0.1:8070
    python3 -m app.dashboard.web --host 0.0.0.0 --port 8080   # LAN-visible
"""

import argparse
import html as html_mod
import mimetypes
import secrets
import sqlite3
import sys
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.db.schema import init_db, get_connection
from app.security.access_control import (
    PERMISSIONS,
    AccessDenied,
    can_view_location,
    get_human_detections_for_user,
    get_sightings_for_user,
    _log,
)
from app.security.auth import authenticate
from app.review.interface import confirm_sighting, dismiss_sighting, mark_new_individual, pending_reviews

SESSION_TTL_SECONDS = 8 * 60 * 60  # one ranger shift
SESSIONS = {}  # sid -> {"user_id", "csrf", "expires"}; in-memory by design


# --------------------------------------------------------------------------
# small helpers


def esc(value) -> str:
    return html_mod.escape(str(value if value is not None else ""))


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


PAGE_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Pench Tiger Monitoring</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f1ea; color: #22261f; }}
 header {{ background: #2d4a22; color: #fff; padding: .7rem 1.2rem; display: flex;
           justify-content: space-between; align-items: center; flex-wrap: wrap; gap: .5rem; }}
 header nav a {{ color: #dbe7cf; margin-right: 1rem; text-decoration: none; }}
 header nav a:hover {{ text-decoration: underline; }}
 main {{ max-width: 980px; margin: 1.2rem auto; padding: 0 1rem; }}
 h1 {{ font-size: 1.25rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff; }}
 th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #ddd; font-size: .92rem; }}
 th {{ background: #ece9df; }}
 .card {{ background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 1rem; margin: .8rem 0; }}
 .tier-high {{ color: #1b5e20; font-weight: 600; }} .tier-medium {{ color: #a15c00; font-weight: 600; }}
 .tier-low {{ color: #8a1c1c; font-weight: 600; }}
 button, .btn {{ background: #2d4a22; color: #fff; border: none; padding: .45rem .9rem;
                 border-radius: 4px; cursor: pointer; font-size: .92rem; }}
 button.warn {{ background: #8a1c1c; }} button.muted {{ background: #777; }}
 form.inline {{ display: inline; }}
 img.trapshot {{ max-width: 420px; width: 100%; border: 1px solid #999; background: #111; }}
 .denied {{ background: #fbeaea; border: 1px solid #d99; padding: 1rem; border-radius: 6px; }}
 .note {{ color: #555; font-size: .85rem; }}
 select, input[type=text], input[type=password] {{ padding: .35rem; }}
</style></head>
<body>
<header>
  <strong>🐘 Pench — Tiger Monitoring (local)</strong>
  <nav>{nav}</nav>
  <span>{user_badge}</span>
</header>
<main>{body}</main>
</body></html>"""


def page(title: str, body: str, user_row=None) -> str:
    nav_links = []
    badge = ""
    if user_row is not None:
        role = user_row["role"]
        if role != "stpf":
            nav_links.append('<a href="/dashboard">Dashboard</a>')
        if role in ("field_ranger", "range_officer", "admin"):
            nav_links.append('<a href="/review">Review queue</a>')
        if PERMISSIONS.get(role, {}).get("human_detections"):
            nav_links.append('<a href="/alerts">Human alerts</a>')
        if role == "admin":
            nav_links.append('<a href="/audit">Audit log</a>')
        nav_links.append(
            f'<form class="inline" method="post" action="/logout">'
            f'<input type="hidden" name="csrf" value="{esc(user_row["_csrf"])}">'
            f'<button class="muted">Logout ({esc(user_row["username"])})</button></form>'
        )
        badge = f"{esc(user_row['username'])} · {esc(role)}"
    return PAGE_SHELL.format(
        title=esc(title), nav="".join(nav_links), user_badge=badge, body=body
    )


def tier_span(tier) -> str:
    return f'<span class="tier-{esc(tier)}">{esc(tier)}</span>'


# --------------------------------------------------------------------------
# view builders (all data through access_control)


def build_login_page(error: str = "") -> str:
    err = f'<div class="denied">{esc(error)}</div>' if error else ""
    body = f"""
<h1>Range-office login</h1>
<div class="card">
{err}
<form method="post" action="/login">
  <label>Username <input type="text" name="username" autocomplete="username" required></label><br><br>
  <label>PIN <input type="password" name="pin" autocomplete="current-pin" required></label><br><br>
  <button type="submit">Sign in</button>
</form>
<p class="note">Prototype note: demo users/PINs come from tests/seed_demo_data.py.
Replace with your department's credential policy before field use.</p>
</div>"""
    return page("Login", body)


def build_dashboard(conn, user_row) -> str:
    uid = user_row["id"]
    parts = [f"<h1>Sightings overview</h1>"]

    try:
        sightings = get_sightings_for_user(conn, uid)
        show_loc = bool(sightings) and "latitude" in sightings[0]

        def _loc_cell(s):
            # Pending sightings have no sighting_location row yet -> NULL coords.
            if s.get("latitude") is None:
                return "<td>—</td>"
            return f"<td>({s['latitude']:.4f}, {s['longitude']:.4f}) cam #{s['camera_id']}</td>"

        rows = "".join(
            "<tr>"
            f"<td>#{s['id']}</td>"
            f"<td>{'PTR-' + str(s['tiger_id']) if s['tiger_id'] else '<em>unresolved</em>'}</td>"
            f"<td>{tier_span(s['confidence_tier'])}</td>"
            f"<td>{esc(s['confirmed_at'] or 'pending')}</td>"
            + (_loc_cell(s) if show_loc else "")
            + "</tr>"
            for s in sightings[:50]
        )
        loc_header = "<th>Location (restricted)</th>" if show_loc else ""
        parts.append(f"""
<p>{len(sightings)} sighting record(s) visible to your role
{'— with location' if show_loc else '— <strong>location withheld by policy</strong>'}.</p>
<table><tr><th>ID</th><th>Tiger</th><th>Tier</th><th>Confirmed</th>{loc_header}</tr>
{rows or '<tr><td colspan="5">none yet</td></tr>'}</table>""")
        if not can_view_location(conn, uid):
            parts.append('<p class="note">Your role receives aggregated sighting data only; '
                         'coordinates are filtered out server-side (see audit log).</p>')
    except AccessDenied as e:
        parts.append(f'<div class="denied">Sightings: access denied — {esc(e)}</div>')

    try:
        alerts = get_human_detections_for_user(conn, uid)
        alert_rows = "".join(
            f"<tr><td>#{a['id']}</td><td>camera #{a['camera_id']}</td>"
            f"<td>{esc(a['zone_type'])}</td><td>{esc(a['detected_at'])}</td>"
            f"<td>{'sent' if a['alert_sent'] else 'queued'}</td></tr>"
            for a in alerts[:20]
        )
        parts.append(f"""
<h2>Human-detection alerts ({len(alerts)})</h2>
<table><tr><th>ID</th><th>Camera</th><th>Zone</th><th>Detected</th><th>Alert</th></tr>
{alert_rows or '<tr><td colspan="5">no human detections recorded</td></tr>'}</table>""")
    except AccessDenied:
        pass  # role simply has no alerts section rendered

    if user_row["role"] in ("range_officer", "admin"):
        cams = conn.execute(
            "SELECT camera_code, battery_pct, status, last_seen_at FROM cameras ORDER BY camera_code"
        ).fetchall()
        cam_rows = "".join(
            f"<tr><td>{esc(c['camera_code'])}</td><td>{esc(c['battery_pct'])}%</td>"
            f"<td>{esc(c['status'])}</td><td>{esc(c['last_seen_at'])}</td></tr>"
            for c in cams
        )
        unsynced = conn.execute(
            """SELECT COUNT(*) AS n FROM images WHERE synced_to_central = 0
               AND id IN (SELECT image_id FROM sightings WHERE confirmed_at IS NOT NULL)"""
        ).fetchone()["n"]
        parts.append(f"""
<h2>Camera health <span class="note">(coordinates deliberately not shown even to admins —
operational health only)</span></h2>
<table><tr><th>Camera</th><th>Battery</th><th>Status</th><th>Last seen</th></tr>{cam_rows}</table>
<h2>Sync status</h2><div class="card">{unsynced} confirmed sighting(s) queued for central push.
Nothing is marked synced unless the push actually succeeded (offline-first fail-safe).</div>""")

    pending_count = len(pending_reviews(conn)) if user_row["role"] in ("field_ranger", "range_officer", "admin") else None
    if pending_count is not None:
        parts.append(f'<h2>Review workload</h2><div class="card">'
                     f'<a href="/review">{pending_count} tiger candidate(s) awaiting human review</a></div>')
    return "\n".join(parts)


def build_review_queue(conn) -> str:
    queue = pending_reviews(conn)
    rows = "".join(
        f"""<tr>
<td><a href="/review/{s['sighting_id']}">#{s['sighting_id']}</a></td>
<td>{tier_span(s['confidence_tier'])}</td><td>{s['match_confidence']}</td>
<td>{esc(Path(s['file_path']).name)}</td><td>{esc(s['captured_at'])}</td></tr>"""
        for s in queue
    )
    return f"""
<h1>Tiger candidates awaiting human review</h1>
<p class="note">No ID is ever auto-assigned. Every row below needs a human decision
(confirm against an existing individual / register new / dismiss false positive).</p>
<table><tr><th>Sighting</th><th>Tier</th><th>Best score</th><th>Image</th><th>Captured</th></tr>
{rows or '<tr><td colspan="5">queue empty 🎉</td></tr>'}</table>"""


def build_review_detail(conn, sighting_id: int, csrf: str) -> str:
    from app.idmatch.matcher import get_matcher, _tier_for

    s = conn.execute(
        """SELECT s.id AS sighting_id, s.image_id, s.match_confidence, s.confidence_tier,
                  i.file_path, i.captured_at
           FROM sightings s JOIN images i ON i.id = s.image_id
           WHERE s.id = ? AND s.tiger_id IS NULL AND s.confirmed_at IS NULL""",
        (sighting_id,),
    ).fetchone()
    if not s:
        raise HttpError(404, f"Sighting {sighting_id} not found or already resolved")

    # Live re-match against current reference set -- shortlist shown at review
    # time, ranked, tiered; still just a suggestion for the human.
    matcher = get_matcher()
    refs = conn.execute(
        "SELECT id, local_id FROM tigers WHERE reference_image_path IS NOT NULL ORDER BY local_id"
    ).fetchall()
    scored = []
    for t in refs:
        ref_path = conn.execute("SELECT reference_image_path FROM tigers WHERE id=?", (t["id"],)).fetchone()["reference_image_path"]
        try:
            scored.append((t["local_id"], matcher.score(s["file_path"], ref_path)))
        except Exception as e:
            print(f"[web] match failed vs {t['local_id']}: {e}")
    scored.sort(key=lambda p: p[1], reverse=True)

    options = "".join(
        f'<option value="{lid}">{lid} — score {sc:.3f} ({_tier_for(sc)})</option>'
        for lid, sc in scored[:10]
    )
    body = f"""
<h1>Review sighting #{s['sighting_id']}</h1>
<div class="card">
<img class="trapshot" src="/media?image_id={s['image_id']}" alt="camera trap capture">
<p>Captured: {esc(s['captured_at'])} · pre-filter best score {s['match_confidence']} ·
tier {tier_span(s['confidence_tier'])}</p>

<h2>Ranked shortlist (suggestion only — you decide)</h2>
<form method="post" action="/review/confirm">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="sighting_id" value="{s['sighting_id']}">
  <label>Same individual as:<br>
  <select name="local_id" size="1" style="min-width:340px">{options or '<option value="">— no registered tigers —</option>'}</select></label>
  <br><br><button {'disabled' if not options else ''}>Confirm match to selected tiger</button>
</form>
<hr>
<form method="post" action="/review/new">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="sighting_id" value="{s['sighting_id']}">
  <label>New individual, assign provisional local ID:
  <input type="text" name="local_id" placeholder="PTR-LOCAL-002" pattern="[A-Za-z0-9\\-]+" required></label>
  <button>Register as NEW tiger</button>
</form>
<hr>
<form method="post" action="/review/dismiss">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="sighting_id" value="{s['sighting_id']}">
  <label>Not a tiger (false positive). Reason (optional):
  <input type="text" name="reason" maxlength="200"></label>
  <button class="warn">Dismiss</button>
</form>
</div>"""
    return body


def build_alerts(conn, user_row) -> str:
    alerts = get_human_detections_for_user(conn, user_row["id"])
    ack_form = lambda a: (
        f"""<form class="inline" method="post" action="/alerts/ack">
<input type="hidden" name="csrf" value="{esc(user_row['_csrf'])}">
<input type="hidden" name="alert_id" value="{a['id']}">
<button class="warn">Acknowledge</button></form>"""
        if not a["acknowledged_by_user_id"] else esc("acked")
    )
    rows = "".join(
        f"""<tr><td>#{a['id']}</td><td>camera #{a['camera_id']}</td>
<td>{'⚠️ RESTRICTED' if a['zone_type'] == 'restricted' else esc(a['zone_type'])}</td>
<td>{esc(a['detected_at'])}</td><td>{'sent' if a['alert_sent'] else 'queued'}</td>
<td>{ack_form(a)}</td></tr>"""
        for a in alerts
    )
    return f"""
<h1>Human-presence alerts</h1>
<p class="note">Routed to anti-poaching staff. Coordinates intentionally excluded from this view;
STPF sees that a camera tripped, not a tiger map. Sync priority: alerts leave the node first.</p>
<table><tr><th>ID</th><th>Camera</th><th>Zone</th><th>Detected</th><th>Sync</th><th>Action</th></tr>
{rows or '<tr><td colspan="6">no detections</td></tr>'}</table>"""


def build_audit(conn) -> str:
    rows_raw = conn.execute(
        """SELECT a.id, a.action, a.resource, a.resource_id, a.timestamp,
                  u.username, u.role
           FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
           ORDER BY a.id DESC LIMIT 300"""
    ).fetchall()
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{esc(r['timestamp'])}</td>"
        f"<td>{esc(r['username']) or '?'}</td><td>{esc(r['role']) or '?'}</td>"
        f"<td><strong>{esc(r['action'])}</strong></td>"
        f"<td>{esc(r['resource'])}{('#' + str(r['resource_id'])) if r['resource_id'] else ''}</td></tr>"
        for r in rows_raw
    )
    return f"""
<h1>Audit log (latest 300 events)</h1>
<p class="note">Every sensitive read, grant AND denial, lands here. Zero unauthorized
location-access events is a PRD success metric — this table is how you prove it.</p>
<table><tr><th>#</th><th>When</th><th>User</th><th>Role</th><th>Action</th><th>Resource</th></tr>
{rows}</table>"""


# --------------------------------------------------------------------------
# HTTP layer


class PenchWebHandler(BaseHTTPRequestHandler):
    server_version = "PenchLocalDashboard/0.3"

    # ---- plumbing ----
    def log_message(self, fmt, *args):  # quieter console, keep errors visible
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_html(self, body: str, status: int = 200, set_cookie: str | None = None):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")  # sighting data must not linger in shared caches
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, set_cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _cookie_sid(self):
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get("pench_session")
        return morsel.value if morsel else None

    def _session(self):
        sid = self._cookie_sid()
        if not sid:
            return None, None
        sess = SESSIONS.get(sid)
        if not sess or sess["expires"] < time.time():
            SESSIONS.pop(sid, None)
            return None, None
        return sess, sid

    def _current_user(self, conn):
        sess, _ = self._session()
        if not sess:
            return None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (sess["user_id"],)).fetchone()
        if row is None:
            return None
        # attach csrf token for template convenience (never stored in DB)
        d = dict(row)
        d["_csrf"] = sess["csrf"]
        return d

    def _require_csrf(self, sess, form):
        if not sess or form.get("csrf", [""])[0] != sess["csrf"]:
            raise HttpError(403, "CSRF token missing or invalid")

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:  # sanity cap
            raise HttpError(413, "Request body too large")
        return parse_qs(self.rfile.read(length).decode())

    def _deny_and_log(self, conn, user_row, action, resource, message):
        if user_row:
            _log(conn, user_row["id"], action, resource)
        raise HttpError(403, message)

    # ---- GET routes ----
    def do_GET(self):
        try:
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            conn = get_connection()
            try:
                user = self._current_user(conn)

                if path == "/":
                    return self._redirect("/dashboard" if user else "/login")

                if path == "/login":
                    if user:
                        return self._redirect("/dashboard")
                    return self._send_html(build_login_page())

                if not user:
                    return self._redirect("/login")

                if path == "/dashboard":
                    if user["role"] == "stpf":
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "dashboard",
                                           "STPF accounts use the Alerts tab only.")
                    return self._send_html(page("Dashboard", build_dashboard(conn, user), user))

                if path == "/review":
                    if user["role"] not in ("field_ranger", "range_officer", "admin"):
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "review_queue",
                                           "Review actions require ranger/officer/admin role.")
                    return self._send_html(page("Review queue", build_review_queue(conn), user))

                if path.startswith("/review/"):
                    if user["role"] not in ("field_ranger", "range_officer", "admin"):
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "review_queue",
                                           "Review actions require ranger/officer/admin role.")
                    try:
                        sid = int(path.rsplit("/", 1)[-1])
                    except ValueError:
                        raise HttpError(404, "Bad sighting id")
                    sess_csrf, _sid = self._session()
                    body = build_review_detail(conn, sid, sess_csrf["csrf"])
                    _log(conn, user["id"], "VIEW", "review_detail", sid)
                    conn.commit()
                    return self._send_html(page(f"Review #{sid}", body, user))

                if path == "/alerts":
                    try:
                        body = build_alerts(conn, user)
                    except AccessDenied as e:
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "human_detections",
                                           "Your role cannot view human-detection alerts.")
                    return self._send_html(page("Human alerts", body, user))

                if path == "/audit":
                    if user["role"] != "admin":
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "audit_log",
                                           "Audit log is admin-only.")
                    return self._send_html(page("Audit log", build_audit(conn), user))

                if path == "/media":
                    return self._serve_media(conn, user, parse_qs(url.query))

                raise HttpError(404, f"No route for {path}")
            finally:
                conn.close()
        except HttpError as e:
            body = f'<div class="denied"><strong>{e.status}</strong> — {esc(e.message)}</div>' \
                   f'<p><a href="/">Back</a></p>'
            self._send_html(page("Error", body), status=e.status)

    def _serve_media(self, conn, user, params):
        """
        Serves a camera-trap image by DB image_id -- never by arbitrary client
        path (that would be a directory-traversal hole). Access rule mirrors
        PRD matrix: any role that may see sightings may see imagery, EXCEPT
        STPF who may only open images tied to a human detection they're
        allowed to act on.
        """
        try:
            image_id = int(params.get("image_id", [""])[0])
        except ValueError:
            raise HttpError(400, "bad image_id")
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HttpError(404, "No such image")

        allowed = False
        perm = PERMISSIONS.get(user["role"], {})
        if perm.get("sightings"):
            allowed = True
        elif perm.get("human_detections"):
            linked = conn.execute(
                "SELECT 1 FROM human_detections WHERE image_id = ?", (image_id,)
            ).fetchone()
            allowed = linked is not None

        if not allowed:
            self._deny_and_log(conn, user, "DENIED_MEDIA_ACCESS", "images",
                               "Your role cannot view trap imagery.")

        file_path = Path(row["file_path"]).resolve()
        # Path-traversal note: the client can only pass a numeric image_id --
        # the path comes from OUR database, never from the request, so there
        # is no client-controlled path component to sanitize. We still verify
        # it exists as a regular file before reading.
        if not file_path.is_file():
            raise HttpError(404, "Image file missing on disk")

        _log(conn, user["id"], "VIEW_MEDIA", "images", image_id)
        conn.commit()
        ctype, _ = mimetypes.guess_type(str(file_path))
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # ---- POST routes ----
    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            form = self._read_form()
            conn = get_connection()
            try:
                if path == "/login":
                    user_row = authenticate(conn, form.get("username", [""])[0].strip(),
                                            form.get("pin", [""])[0])
                    conn.commit()
                    if not user_row:
                        time.sleep(0.6)  # cheap brute-force damper
                        return self._send_html(build_login_page("Invalid username or PIN."), status=401)
                    sid = secrets.token_urlsafe(32)
                    SESSIONS[sid] = {
                        "user_id": user_row["id"],
                        "csrf": secrets.token_urlsafe(24),
                        "expires": time.time() + SESSION_TTL_SECONDS,
                    }
                    return self._redirect("/dashboard", set_cookie=(
                        f"pench_session={sid}; Path=/; HttpOnly; SameSite=Strict"))

                sess, sid = self._session()
                if not sess:
                    return self._redirect("/login")
                self._require_csrf(sess, form)
                user = self._current_user(conn)

                if path == "/logout":
                    SESSIONS.pop(sid, None)
                    return self._redirect("/login",
                                          set_cookie="pench_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")

                if path == "/review/confirm":
                    self._require_review_role_web(conn, user)
                    tiger = conn.execute("SELECT id FROM tigers WHERE local_id = ?",
                                         (form.get("local_id", [""])[0],)).fetchone()
                    if not tiger:
                        raise HttpError(400, "Unknown local_id — pick a listed tiger or register new")
                    confirm_sighting(int(form["sighting_id"][0]), tiger["id"], user["id"], conn=conn)
                    return self._redirect("/review")

                if path == "/review/new":
                    self._require_review_role_web(conn, user)
                    mark_new_individual(int(form["sighting_id"][0]),
                                        form.get("local_id", [""])[0].strip(), user["id"], conn=conn)
                    return self._redirect("/review")

                if path == "/review/dismiss":
                    self._require_review_role_web(conn, user)
                    dismiss_sighting(int(form["sighting_id"][0]), user["id"],
                                     reason=form.get("reason", [""])[0][:200], conn=conn)
                    return self._redirect("/review")

                if path == "/alerts/ack":
                    try:
                        get_human_detections_for_user(conn, user["id"])
                    except AccessDenied:
                        self._deny_and_log(conn, user, "DENIED_ACCESS", "human_detections",
                                           "Your role cannot acknowledge human-detection alerts.")
                    aid = int(form["alert_id"][0])
                    conn.execute(
                        "UPDATE human_detections SET acknowledged_by_user_id = ? WHERE id = ?",
                        (user["id"], aid),
                    )
                    _log(conn, user["id"], "ACK_ALERT", "human_detections", aid)
                    conn.commit()
                    return self._redirect("/alerts")

                raise HttpError(404, f"No route for POST {path}")
            finally:
                conn.close()
        except HttpError as e:
            body = f'<div class="denied"><strong>{e.status}</strong> — {esc(e.message)}</div>' \
                   f'<p><a href="/">Back</a></p>'
            self._send_html(page("Error", body), status=e.status)

    @staticmethod
    def _require_review_role_web(conn, user):
        if user is None or user["role"] not in ("field_ranger", "range_officer", "admin"):
            _log(conn, user["id"] if user else None, "DENIED_ACCESS", "review_action")
            raise HttpError(403, "Review actions require ranger/officer/admin role.")


def run(host="127.0.0.1", port=8070):
    init_db()
    server = ThreadingHTTPServer((host, port), PenchWebHandler)
    print(f"[web] Pench dashboard serving on http://{host}:{port}  (Ctrl+C to stop)")
    print("[web] Fully offline: stdlib server, no CDN assets. Data stays on this machine;")
    print("[web] only app/sync pushes anything out, and only when connectivity exists.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] shutting down")
        server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pench range-office dashboard (offline)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; use 0.0.0.0 to expose on LAN)")
    ap.add_argument("--port", type=int, default=8070)
    args = ap.parse_args()
    run(args.host, args.port)
