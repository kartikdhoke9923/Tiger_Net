"""Credential hashing + login auditing."""

import pytest

from app.security.auth import authenticate, set_user_pin, verify_pin, hash_pin


def test_set_and_verify_pin_roundtrip(db, user_ids):
    set_user_pin(db, user_ids["ranger_amit"], "9876")
    row = db.execute("SELECT * FROM users WHERE id=?", (user_ids["ranger_amit"],)).fetchone()
    assert verify_pin(row, "9876")
    assert not verify_pin(row, "0000")


def test_pin_is_never_stored_in_plaintext(db, user_ids):
    set_user_pin(db, user_ids["ranger_amit"], "s3cret-pin!")
    row = db.execute("SELECT * FROM users WHERE id=?", (user_ids["ranger_amit"],)).fetchone()
    assert row["pin_hash"] != "s3cret-pin!"
    assert "s3cret" not in (row["pin_hash"] or "")
    assert len(row["pin_salt"]) >= 32 // 2   # hex salt present


def test_same_pin_twice_yields_different_hashes(db, user_ids):
    """Per-user random salts -> identical PINs must not produce identical hashes."""
    set_user_pin(db, user_ids["ranger_amit"], "1234")
    set_user_pin(db, user_ids["officer_priya"], "1234")
    a = db.execute("SELECT pin_hash FROM users WHERE id=?", (user_ids["ranger_amit"],)).fetchone()
    b = db.execute("SELECT pin_hash FROM users WHERE id=?", (user_ids["officer_priya"],)).fetchone()
    assert a["pin_hash"] != b["pin_hash"]


def test_short_pin_rejected(db, user_ids):
    with pytest.raises(ValueError):
        set_user_pin(db, user_ids["admin_root"], "123")


def test_authenticate_success_and_failure_are_both_audited(db, user_ids):
    set_user_pin(db, user_ids["researcher_wct"], "4321")

    assert authenticate(db, "researcher_wct", "4321") is not None
    assert authenticate(db, "researcher_wct", "wrong") is None
    assert authenticate(db, "ghost_user", "4321") is None

    actions = [r["action"] for r in db.execute(
        "SELECT action FROM audit_log WHERE resource='web_session'"
    ).fetchall()]
    assert actions.count("LOGIN_OK") == 1
    assert actions.count("LOGIN_FAILED") == 2


def test_verify_without_pin_set_is_false(db, user_ids):
    row = db.execute("SELECT * FROM users WHERE id=?", (user_ids["stpf_team1"],)).fetchone()
    assert not verify_pin(row, "anything")
