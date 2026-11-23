"""User authentication, secure PBKDF2 password hashing, and session management."""
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import db

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HASH_ITERATIONS = 100_000


def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with a unique random salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), HASH_ITERATIONS)
    return f"pbkdf2:sha256:{HASH_ITERATIONS}${salt}${key.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against stored hash using constant-time comparison."""
    try:
        parts = hashed.split("$")
        if len(parts) != 3:
            return False
        meta, salt, expected_key = parts
        _, algo, iters_str = meta.split(":")
        iterations = int(iters_str)
        key = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return secrets.compare_digest(key.hex(), expected_key)
    except Exception:
        return False


def is_valid_email(email: str) -> bool:
    return bool(email and EMAIL_REGEX.match(email.strip()))


def create_user(name: str, email: str, password: str, confirm_password: str | None = None, role: str = "AP Reviewer") -> dict:
    name = (name or "").strip()
    email = (email or "").strip().lower()
    password = password or ""

    if len(name) < 2:
        raise ValueError("Name must be at least 2 characters")
    if not is_valid_email(email):
        raise ValueError("Please provide a valid email address")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    if confirm_password is not None and password != confirm_password:
        raise ValueError("Passwords do not match")

    existing = db.q1("SELECT id FROM users WHERE email=?", (email,))
    if existing:
        raise ValueError("An account with this email address already exists")

    user_id = f"U-{uuid.uuid4().hex[:10]}"
    p_hash = hash_password(password)

    db.execute(
        "INSERT INTO users (id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, email, p_hash, role),
    )
    return {"id": user_id, "name": name, "email": email, "role": role}


def authenticate_user(email: str, password: str) -> dict | None:
    email = (email or "").strip().lower()
    password = password or ""
    if not email or not password:
        return None

    user = db.q1("SELECT id, name, email, password_hash, role FROM users WHERE email=?", (email,))
    if not user:
        # Run dummy hash to prevent timing attack enumeration
        hash_password("dummy_password_for_timing")
        return None

    if not verify_password(password, user["password_hash"]):
        return None

    return {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}


def create_session(user_id: str, days: int = 7) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )
    return token


def get_user_by_session(token: str) -> dict | None:
    if not token:
        return None

    row = db.q1(
        "SELECT s.token, s.expires_at, u.id, u.name, u.email, u.role "
        "FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token=?",
        (token,),
    )
    if not row:
        return None

    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp < datetime.now(timezone.utc):
            db.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
    except Exception:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None

    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}


def revoke_session(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))


def seed_default_user():
    """Ensure a default demo reviewer exists for testing and instant access."""
    count_row = db.q1("SELECT COUNT(*) as cnt FROM users")
    if not count_row or count_row["cnt"] == 0:
        create_user(
            name="Demo Reviewer",
            email="demo@kestrel.com",
            password="Password@123",
            role="AP Lead",
        )
