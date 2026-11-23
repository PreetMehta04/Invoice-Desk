"""Unit and integration tests for authentication, password hashing, sessions, and route protection."""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="invoice-auth-test-"))
os.environ["WATCH_INBOX"] = "0"

from app import auth, config, db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def setup_db():
    db.init_db(reset=True)
    auth.seed_default_user()
    config.reset_rules()


def test_password_hashing():
    pwd = "SecretPassword123"
    h1 = auth.hash_password(pwd)
    h2 = auth.hash_password(pwd)

    # Different salts produce different hashes
    assert h1 != h2
    assert auth.verify_password(pwd, h1) is True
    assert auth.verify_password(pwd, h2) is True
    assert auth.verify_password("WrongPassword", h1) is False


def test_create_user_and_validation():
    # Valid signup
    user = auth.create_user("Alice Reviewer", "alice@kestrel.com", "SecurePass456", "SecurePass456")
    assert user["name"] == "Alice Reviewer"
    assert user["email"] == "alice@kestrel.com"

    # Duplicate email rejection
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user("Alice 2", "alice@kestrel.com", "SecurePass456")

    # Mismatched passwords
    with pytest.raises(ValueError, match="do not match"):
        auth.create_user("Bob", "bob@kestrel.com", "Pass123", "Pass999")

    # Password too short
    with pytest.raises(ValueError, match="at least 6"):
        auth.create_user("Bob", "bob@kestrel.com", "123")


def test_authentication_and_session():
    # Demo reviewer was seeded
    user = auth.authenticate_user("demo@kestrel.com", "Password@123")
    assert user is not None
    assert user["name"] == "Demo Reviewer"

    # Wrong password
    assert auth.authenticate_user("demo@kestrel.com", "BadPass") is None

    # Nonexistent user
    assert auth.authenticate_user("ghost@kestrel.com", "Password@123") is None

    # Session lifecycle
    token = auth.create_session(user["id"], days=1)
    session_user = auth.get_user_by_session(token)
    assert session_user is not None
    assert session_user["email"] == "demo@kestrel.com"

    # Logout / revocation
    auth.revoke_session(token)
    assert auth.get_user_by_session(token) is None


def test_api_auth_endpoints_and_protected_routes():
    client = TestClient(app)

    # 1. Unauthenticated request to protected route is rejected with 401
    r = client.get("/api/runs")
    assert r.status_code == 401
    assert "Authentication required" in r.text

    # 2. Public endpoints remain accessible
    r_health = client.get("/api/health")
    assert r_health.status_code == 200

    r_cfg = client.get("/api/config")
    assert r_cfg.status_code == 200

    # 3. Sign up new user via REST API
    signup_payload = {
        "name": "Preet Mehta",
        "email": "preet@kestrel.com",
        "password": "Password@789",
        "confirm_password": "Password@789",
    }
    r_signup = client.post("/api/auth/signup", json=signup_payload)
    assert r_signup.status_code == 200
    data = r_signup.json()
    token = data["token"]
    assert token
    assert data["user"]["email"] == "preet@kestrel.com"

    # 4. Access protected route with Bearer token
    headers = {"Authorization": f"Bearer {token}"}
    r_runs = client.get("/api/runs", headers=headers)
    assert r_runs.status_code == 200

    r_me = client.get("/api/auth/me", headers=headers)
    assert r_me.status_code == 200
    assert r_me.json()["user"]["name"] == "Preet Mehta"

    # 5. Login via REST API
    r_login = client.post("/api/auth/login", json={"email": "preet@kestrel.com", "password": "Password@789"})
    assert r_login.status_code == 200
    new_token = r_login.json()["token"]

    # 6. Logout
    r_logout = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {new_token}"})
    assert r_logout.status_code == 200

    # Token should now be invalid
    r_after = client.get("/api/runs", headers={"Authorization": f"Bearer {new_token}"})
    assert r_after.status_code == 401
