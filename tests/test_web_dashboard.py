"""
Web dashboard end-to-end tests: real HTTP against the real server.

Covers the PRD guarantees in their user-facing form:
- login required, wrong PIN rejected
- role-aware pages: researcher never sees coordinates; STPF locked out of
  sightings/review; ranger scoped to own beat; admin sees audit log
- CSRF tokens enforced on every POST
- media served by DB id only, with role checks (STPF only via alert linkage)
"""

import threading
import urllib.request
import urllib.parse
import urllib.error
from http.cookiejar import CookieJar

import pytest

from app.dashboard import web as webmod
from app.security.auth import set_user_pin


@pytest.fixture()
def server(db):
    """Real HTTP server on an ephemeral port, sharing the test DB."""
    webmod.SESSIONS.clear()

    class QuietHandler(webmod.PenchWebHandler):
        def log_message(self, *a):  # silence per-request logging in tests
            pass

    httpd = webmod.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    webmod.SESSIONS.clear()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Blocks auto-follow so we can observe raw 303s (e.g. login responses)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """urllib client with a cookie jar + form helpers."""

    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.no_redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
        )

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            resp = self.opener.open(req)
            # media endpoints return raw JPEG bytes -- don't crash decoding them
            return resp.status, resp.read().decode(errors="replace"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), dict(e.headers)

    def post(self, path, data: dict):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            resp = self.opener.open(req)
            return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def post_no_follow(self, path, data: dict):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            resp = self.no_redirect_opener.open(req)
            return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            # 303 redirect responses arrive here under _NoRedirect -- that's
            # exactly what we want to observe for login
            if e.code in (301, 302, 303, 307):
                return e.code, "", dict(e.headers)
            return e.code, e.read().decode(), dict(e.headers)

    def login(self, username, pin):
        status, _, _ = self.post_no_follow("/login", {"username": username, "pin": pin})
        assert status == 303, f"login expected 303 redirect, got {status}"
        return status

    def csrf_from(self, html_text):
        marker = 'name="csrf" value="'
        i = html_text.find(marker)
        if i == -1:
            return None
        j = html_text.find('"', i + len(marker))
        return html_text[i + len(marker):j]


@pytest.fixture(autouse=True)
def demo_pins(db):
    for uid_row in db.execute("SELECT id FROM users").fetchall():
        set_user_pin(db, uid_row["id"], "1234")


# ---- login / sessions ----

def test_dashboard_requires_login(server):
    client = Client(server)
    status, text, headers = client.get("/dashboard")   # redirected to /login
    assert "Range-office login" in text


def _page(client, path):
    status, text, _ = client.get(path)
    return status, text


def test_login_wrong_pin_rejected(server):
    client = Client(server)
    status, text, _ = client.post("/login", {"username": "ranger_amit", "pin": "0000"})
    assert status == 401
    assert "Invalid username or PIN" in text


def test_login_success_lands_on_role_dashboard(server):
    client = Client(server)
    client.login("officer_priya", "1234")
    _, text = _page(client, "/dashboard")
    assert "Sightings overview" in text


# ---- RBAC through the UI ----

@pytest.fixture()
def confirmed_sighting_with_location(db, camera_ids, incoming_image, user_ids):
    from app.idmatch.matcher import generate_shortlist
    from app.review.interface import confirm_sighting

    image_id, p = incoming_image(camera_ids["PTR-CAM-001"], seed=100)
    r = generate_shortlist(image_id=image_id, image_path=str(p), conn=db)
    confirm_sighting(r["sighting_id"], tiger_id=1, user_id=user_ids["officer_priya"], conn=db)
    db.execute(
        "INSERT INTO sighting_location (sighting_id, latitude, longitude, camera_id)"
        " VALUES (?, 21.7679, 79.2961, ?)",
        (r["sighting_id"], camera_ids["PTR-CAM-001"]),
    )
    db.commit()
    return r["sighting_id"], image_id


def test_dashboard_handles_pending_sightings_without_location_rows(server, db,
                                                                   camera_ids,
                                                                   incoming_image):
    """Regression: a pending sighting (no sighting_location row yet) must render
    as '—', not crash the dashboard with NULL coordinates."""
    from app.idmatch.matcher import generate_shortlist
    img_id, p = incoming_image(camera_ids["PTR-CAM-001"], seed=100)
    generate_shortlist(image_id=img_id, image_path=str(p), conn=db)  # stays pending

    officer = Client(server)
    officer.login("officer_priya", "1234")
    status, text, _ = officer.get("/dashboard")
    assert status == 200
    assert "unresolved" in text and "<td>—</td>" in text


def test_researcher_sees_no_coordinates_anywhere_in_html(server, confirmed_sighting_with_location):
    client = Client(server)
    client.login("researcher_wct", "1234")
    _, text = _page(client, "/dashboard")
    assert "21.7679" not in text          # the actual coordinate must not appear
    assert "location withheld" in text    # policy is stated instead
    assert "/review\"" not in text        # no review link rendered either


def test_stpf_gets_403_on_dashboard_and_review_but_alerts_ok(
        server, confirmed_sighting_with_location):
    client = Client(server)
    client.login("stpf_team1", "1234")

    status, text, _ = client.get("/dashboard")
    assert status == 403 and "denied" in text.lower()

    status, text, _ = client.get("/review")
    assert status == 403

    status, text, _ = client.get("/alerts")
    assert status == 200
    assert "Human-presence alerts" in text


def test_ranger_sees_own_beat_only_in_ui(server, db, confirmed_sighting_with_location,
                                         camera_ids, incoming_image, user_ids):
    from app.idmatch.matcher import generate_shortlist
    from app.review.interface import confirm_sighting

    # second sighting on BEAT-02
    img2, p2 = incoming_image(camera_ids["PTR-CAM-003"], seed=300)
    r2 = generate_shortlist(image_id=img2, image_path=str(p2), conn=db)
    confirm_sighting(r2["sighting_id"], tiger_id=1, user_id=user_ids["officer_priya"], conn=db)

    client = Client(server)
    client.login("ranger_amit", "1234")     # BEAT-01 ranger
    _, text = _page(client, "/dashboard")
    assert "own beat" in text.lower() or "sighting record(s)" in text
    # sighting rows render as <td>#<id></td> -- the BEAT-02 sighting must not appear
    assert f"<td>#{r2['sighting_id']}</td>" not in text


def test_admin_audit_page_shows_denials(server, confirmed_sighting_with_location):
    # generate a denial first
    stpf = Client(server)
    stpf.login("stpf_team1", "1234")
    stpf.get("/dashboard")   # denied -> audited

    admin = Client(server)
    admin.login("admin_root", "1234")
    status, text = _page(admin, "/audit")
    assert status == 200
    assert "DENIED_ACCESS" in text
    assert "LOGIN_OK" in text


def test_non_admin_cannot_open_audit_log(server):
    client = Client(server)
    client.login("ranger_amit", "1234")
    status, text, _ = client.get("/audit")
    assert status == 403


# ---- review flow through the UI ----

def test_review_queue_lists_pending_and_confirm_flow_works(server, db, camera_ids,
                                                           incoming_image):
    from app.idmatch.matcher import generate_shortlist

    img_id, p = incoming_image(camera_ids["PTR-CAM-001"], seed=100)
    r = generate_shortlist(image_id=img_id, image_path=str(p), conn=db)

    officer = Client(server)
    officer.login("officer_priya", "1234")

    status, text = _page(officer, "/review")
    assert status == 200
    assert f"/review/{r['sighting_id']}" in text

    status, detail = _page(officer, f"/review/{r['sighting_id']}")
    assert "Ranked shortlist" in detail
    csrf = officer.csrf_from(detail)
    assert csrf  # forms are CSRF-protected

    status, _, _ = officer.post("/review/confirm", {
        "csrf": csrf, "sighting_id": str(r["sighting_id"]), "local_id": "PTR-LOCAL-001",
    })
    assert status == 200   # POST succeeded and redirected back to the queue
    row = db.execute("SELECT tiger_id, resolution FROM sightings WHERE id=?",
                     (r["sighting_id"],)).fetchone()
    assert row["tiger_id"] is not None       # now confirmed by a human, via the web
    assert row["resolution"] == "confirmed"


def test_review_actions_blocked_without_valid_csrf(server, db, camera_ids, incoming_image):
    from app.idmatch.matcher import generate_shortlist

    img_id, p = incoming_image(camera_ids["PTR-CAM-001"])
    r = generate_shortlist(image_id=img_id, image_path=str(p), conn=db)

    officer = Client(server)
    officer.login("officer_priya", "1234")
    status, text, _ = officer.post("/review/confirm", {
        "csrf": "forged-token", "sighting_id": str(r["sighting_id"]),
        "local_id": "PTR-LOCAL-001",
    })
    assert status == 403
    row = db.execute("SELECT tiger_id FROM sightings WHERE id=?", (r["sighting_id"],)).fetchone()
    assert row["tiger_id"] is None   # forged POST changed nothing


def test_dismiss_via_web_clears_queue_keeps_record(server, db, camera_ids, incoming_image):
    from app.idmatch.matcher import generate_shortlist

    img_id, p = incoming_image(camera_ids["PTR-CAM-001"], name="deer_mislabel.jpg")
    r = generate_shortlist(image_id=img_id, image_path=str(p), conn=db)

    ranger = Client(server)
    ranger.login("ranger_amit", "1234")
    _, detail = _page(ranger, f"/review/{r['sighting_id']}")
    csrf = ranger.csrf_from(detail)
    ranger.post("/review/dismiss", {
        "csrf": csrf, "sighting_id": str(r["sighting_id"]),
        "reason": "spotted deer, bad light",
    })
    row = db.execute("SELECT resolution, tiger_id FROM sightings WHERE id=?",
                     (r["sighting_id"],)).fetchone()
    assert row["resolution"] == "dismissed"
    assert row["tiger_id"] is None


# ---- media access control ----

def test_media_requires_permission_stpf_only_via_alert_linkage(server, db, camera_ids,
                                                               incoming_image):
    alert_img, p_alert = incoming_image(camera_ids["PTR-CAM-002"], name="human_cam2.jpg")
    plain_img, p_plain = incoming_image(camera_ids["PTR-CAM-001"], name="tiger_cam1.jpg")
    db.execute(
        "INSERT INTO human_detections (image_id, camera_id, zone_type) VALUES (?, ?, 'restricted')",
        (alert_img, camera_ids["PTR-CAM-002"]),
    )
    db.commit()

    stpf = Client(server)
    stpf.login("stpf_team1", "1234")

    # alert-linked image: allowed (this is what STPF needs to triage)
    status, body, _ = stpf.get(f"/media?image_id={alert_img}")
    assert status == 200
    # general sighting imagery: denied outright
    status, body, _ = stpf.get(f"/media?image_id={plain_img}")
    assert status == 403


def test_media_never_accepts_client_paths(server):
    client = Client(server)
    client.login("admin_root", "1234")
    status, text, _ = client.get("/media?image_id=../../../../etc/passwd")
    assert status in (400, 403, 404)   # path traversal attempt rejected, not served


def test_unknown_route_is_404_not_crash(server):
    client = Client(server)
    client.login("admin_root", "1234")
    status, _, _ = client.get("/no/such/route")
    assert status == 404
