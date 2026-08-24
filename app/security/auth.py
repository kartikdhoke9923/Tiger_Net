"""
Credential hashing + verification for the web dashboard login.

Scope honesty (read before deploying):
- This is prototype-grade auth: a per-user salted PIN, PBKDF2-HMAC-SHA256
  via the Python stdlib. It exists so the web UI can demonstrate that RBAC
  is enforced per real user session -- NOT a production identity system.
- Before field use: replace with proper credentials policy (rotation,
  lockout, per-device tokens), ideally tied to how the forest department
  already manages staff accounts.
- The PIN never leaves this module as plaintext; only the hash is stored.
  Failed logins are written to audit_log like any other access attempt.
"""

import hashlib
import secrets

from app.db.schema import get_connection
from app.security.access_control import _log

PBKDF2_ITERATIONS = 100_000


def hash_pin(pin: str) -> tuple[str, str]:
    """Returns (salt_hex, pin_hash_hex)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def set_user_pin(conn, user_id: int, pin: str):
    """Sets/rotates a user's web login PIN."""
    if not pin or len(pin) < 4:
        raise ValueError("PIN must be at least 4 characters")
    salt_hex, hash_hex = hash_pin(pin)
    conn.execute(
        "UPDATE users SET pin_salt = ?, pin_hash = ? WHERE id = ?",
        (salt_hex, hash_hex, user_id),
    )
    conn.commit()


def verify_pin(user_row, pin: str) -> bool:
    if not user_row["pin_hash"] or not user_row["pin_salt"]:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode(),
        bytes.fromhex(user_row["pin_salt"]),
        PBKDF2_ITERATIONS,
    )
    return secrets.compare_digest(digest.hex(), user_row["pin_hash"])


def authenticate(conn, username: str, pin: str):
    """
    Returns the users row on success, None on failure. Every attempt --
    success or failure -- is written to audit_log so the security claim in
    PRD section 10 ('every access attempt, granted or denied') holds for
    logins too.
    """
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row and verify_pin(row, pin):
        _log(conn, row["id"], "LOGIN_OK", "web_session")
        return row
    user_id_for_log = row["id"] if row else None
    _log(conn, user_id_for_log, "LOGIN_FAILED", "web_session")
    return None
