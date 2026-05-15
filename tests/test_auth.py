"""Tests for authentication — password hashing and user accounts."""
import pytest

from app import auth, db


@pytest.fixture
def conn():
    c = db.get_conn(":memory:")
    db.init_db(c)
    yield c
    c.close()


def test_password_hash_round_trips():
    stored = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong password", stored)


def test_hash_is_not_the_plaintext():
    stored = auth.hash_password("supersecret123")
    assert "supersecret123" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_short_password_is_rejected():
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.hash_password("short")


def test_verify_rejects_a_malformed_stored_value():
    assert not auth.verify_password("anything", "not-a-real-hash")


def test_create_and_authenticate(conn):
    auth.create_user(conn, "David", "longenough1", display_name="David B",
                     role="admin")
    assert auth.user_count(conn) == 1
    # Username is case-insensitive.
    user = auth.authenticate(conn, "david", "longenough1")
    assert user is not None
    assert user["role"] == "admin"
    assert auth.authenticate(conn, "david", "wrongpassword") is None
    assert auth.authenticate(conn, "nobody", "longenough1") is None


def test_duplicate_username_is_rejected(conn):
    auth.create_user(conn, "david", "longenough1")
    with pytest.raises(auth.AuthError, match="already taken"):
        auth.create_user(conn, "David", "longenough2")


def test_inactive_user_cannot_authenticate(conn):
    auth.create_user(conn, "david", "longenough1")
    conn.execute("UPDATE users SET active = 0 WHERE username = 'david'")
    conn.commit()
    assert auth.authenticate(conn, "david", "longenough1") is None


def test_unknown_role_is_rejected(conn):
    with pytest.raises(auth.AuthError, match="role"):
        auth.create_user(conn, "david", "longenough1", role="superuser")


def _only_user(conn):
    return auth.list_users(conn)[0]["id"]


def test_change_password(conn):
    auth.create_user(conn, "david", "oldpassword1")
    uid = _only_user(conn)
    auth.change_password(conn, uid, "oldpassword1", "newpassword2")
    assert auth.authenticate(conn, "david", "newpassword2") is not None
    assert auth.authenticate(conn, "david", "oldpassword1") is None


def test_change_password_needs_correct_current_password(conn):
    auth.create_user(conn, "david", "oldpassword1")
    uid = _only_user(conn)
    with pytest.raises(auth.AuthError, match="current password"):
        auth.change_password(conn, uid, "wrongcurrent", "newpassword2")


def test_change_password_rejects_a_short_new_password(conn):
    auth.create_user(conn, "david", "oldpassword1")
    uid = _only_user(conn)
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.change_password(conn, uid, "oldpassword1", "short")


def test_change_username(conn):
    auth.create_user(conn, "david", "longenough1", display_name="Old Name")
    uid = _only_user(conn)
    auth.change_profile(conn, uid, "longenough1", "dbradshaw", "David B")
    assert auth.authenticate(conn, "dbradshaw", "longenough1") is not None
    assert auth.get_user(conn, "david") is None


def test_change_username_to_a_taken_name_is_rejected(conn):
    auth.create_user(conn, "david", "longenough1")
    auth.create_user(conn, "claire", "longenough2")
    david_id = auth.get_user(conn, "david")["id"]
    with pytest.raises(auth.AuthError, match="already taken"):
        auth.change_profile(conn, david_id, "longenough1", "claire", None)


def test_change_profile_keeping_the_same_username(conn):
    auth.create_user(conn, "david", "longenough1")
    uid = _only_user(conn)
    auth.change_profile(conn, uid, "longenough1", "david", "David B.")
    assert auth.get_user(conn, "david")["display_name"] == "David B."
