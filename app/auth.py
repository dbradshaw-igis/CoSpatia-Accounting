"""Authentication — user accounts and password hashing.

Passwords are never stored in the clear. Each is salted and run through
PBKDF2-HMAC-SHA256; the stored value records the algorithm, iteration count,
salt, and derived key, so it can be verified later and re-hashed if the cost is
ever raised.

Per-user accounts are a deliberate requirement (CLAUDE.md, FCPS lesson 3): a
shared login does not pass district procurement, and the audit trail is only
worth anything if each action ties to a person.
"""
import hashlib
import hmac
import os
from datetime import datetime, timezone

ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8


class AuthError(ValueError):
    """Raised when an account operation fails a rule."""


def hash_password(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"A password must be at least {MIN_PASSWORD_LENGTH} characters.")
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password, stored):
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(derived.hex(), hash_hex)


def user_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def get_user(conn, username):
    return conn.execute(
        "SELECT * FROM users WHERE username = ?",
        ((username or "").strip().lower(),),
    ).fetchone()


def create_user(conn, username, password, display_name=None, role="standard"):
    username = (username or "").strip().lower()
    if not username:
        raise AuthError("A username is required.")
    if role not in ("admin", "standard"):
        raise AuthError("Unknown role.")
    if get_user(conn, username) is not None:
        raise AuthError(f"The username '{username}' is already taken.")
    conn.execute(
        """INSERT INTO users
           (username, password_hash, display_name, role, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (username, hash_password(password),
         (display_name or "").strip() or None, role,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def authenticate(conn, username, password):
    """Return the user row for a correct username/password, otherwise None."""
    user = get_user(conn, username)
    if user is None or not user["active"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def get_user_by_id(conn, user_id):
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def change_password(conn, user_id, current_password, new_password):
    """Change a user's own password. The current password must be supplied —
    a logged-in session alone is not enough to set a new one."""
    user = get_user_by_id(conn, user_id)
    if user is None:
        raise AuthError("Account not found.")
    if not verify_password(current_password, user["password_hash"]):
        raise AuthError("Your current password is not correct.")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(new_password), user_id))
    conn.commit()


def change_profile(conn, user_id, current_password, new_username,
                   display_name):
    """Change a user's own username and display name, gated by the current
    password."""
    user = get_user_by_id(conn, user_id)
    if user is None:
        raise AuthError("Account not found.")
    if not verify_password(current_password, user["password_hash"]):
        raise AuthError("Your current password is not correct.")
    new_username = (new_username or "").strip().lower()
    if not new_username:
        raise AuthError("A username is required.")
    clash = get_user(conn, new_username)
    if clash is not None and clash["id"] != user_id:
        raise AuthError(f"The username '{new_username}' is already taken.")
    conn.execute(
        "UPDATE users SET username = ?, display_name = ? WHERE id = ?",
        (new_username, (display_name or "").strip() or None, user_id))
    conn.commit()
