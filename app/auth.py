"""User authentication: registration, login, session management, password hashing.

Uses stdlib PBKDF2-HMAC-SHA256 for password hashing (no extra dependencies) and
signed session tokens stored in SQLite and sent as HTTP-only cookies."""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import db

# ------------------------------------------------------------------------------- password hashing
_ITERATIONS = 260_000  # OWASP-recommended for PBKDF2-SHA256

def hash_password(password: str) -> str:
    """Return 'salt$hash' using PBKDF2-HMAC-SHA256."""
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"{salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    salt, expected = stored.split("$", 1)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return secrets.compare_digest(h, expected)


# ------------------------------------------------------------------------------- user CRUD
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

def validate_signup(name: str, email: str, password: str, confirm: str) -> str | None:
    """Return an error message, or None if valid."""
    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not name or len(name) < 2:
        return "Name must be at least 2 characters."
    if not email or not _EMAIL_RE.match(email):
        return "Enter a valid email address."
    if len(password) < 6:
        return "Password must be at least 6 characters."
    if password != confirm:
        return "Passwords do not match."
    if get_user_by_email(email):
        return "An account with this email already exists."
    return None


def create_user(name: str, email: str, password: str) -> dict:
    email = email.strip().lower()
    pw_hash = hash_password(password)
    uid = "U" + secrets.token_hex(6).upper()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    db.execute(
        "INSERT INTO users (id, name, email, pw_hash, created_at) VALUES (?,?,?,?,?)",
        (uid, name.strip(), email, pw_hash, now),
    )
    return {"id": uid, "name": name.strip(), "email": email}


def get_user_by_email(email: str) -> dict | None:
    return db.q1("SELECT * FROM users WHERE email=?", (email.strip().lower(),))


def get_user_by_id(uid: str) -> dict | None:
    return db.q1("SELECT id, name, email, created_at FROM users WHERE id=?", (uid,))


# ------------------------------------------------------------------------------- session management
SESSION_TTL = timedelta(days=7)

def create_session(user_id: str) -> str:
    """Create a new session token and persist it. Returns the token."""
    token = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + SESSION_TTL).isoformat(timespec="milliseconds")
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
        (token, user_id, expires),
    )
    return token


def validate_session(token: str | None) -> dict | None:
    """Return the user dict if token is valid and not expired, else None."""
    if not token:
        return None
    row = db.q1(
        "SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)
    )
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return get_user_by_id(row["user_id"])


def delete_session(token: str):
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def delete_user_sessions(user_id: str):
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
