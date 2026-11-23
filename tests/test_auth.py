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


def test_user_data_isolation():
    client = TestClient(app)

    # 1. Register User A (Alice)
    r_alice = client.post("/api/auth/signup", json={
        "name": "Alice", "email": "alice.isolated@kestrel.com", "password": "Password@123", "confirm_password": "Password@123"
    })
    token_alice = r_alice.json()["token"]
    h_alice = {"Authorization": f"Bearer {token_alice}"}

    # 2. Register User B (Bob) - a completely new user
    r_bob = client.post("/api/auth/signup", json={
        "name": "Bob", "email": "bob.isolated@kestrel.com", "password": "Password@123", "confirm_password": "Password@123"
    })
    token_bob = r_bob.json()["token"]
    h_bob = {"Authorization": f"Bearer {token_bob}"}

    # New user Bob starts completely from 0
    stats_bob = client.get("/api/stats", headers=h_bob).json()
    assert stats_bob["total"] == 0
    assert stats_bob["completed"] == 0
    assert stats_bob["value_processed"] == 0
    assert client.get("/api/runs", headers=h_bob).json() == []

    # Alice runs a sample invoice
    r_run = client.post("/api/samples/01_happy_apex_furniture.pdf/run", headers=h_alice)
    assert r_run.status_code == 200
    alice_run_id = r_run.json()["run_id"]

    # Alice now has 1 run
    stats_alice = client.get("/api/stats", headers=h_alice).json()
    assert stats_alice["total"] == 1
    alice_runs = client.get("/api/runs", headers=h_alice).json()
    assert len(alice_runs) == 1
    assert alice_runs[0]["id"] == alice_run_id

    # Bob STILL has 0 runs — completely isolated!
    stats_bob_after = client.get("/api/stats", headers=h_bob).json()
    assert stats_bob_after["total"] == 0
    assert client.get("/api/runs", headers=h_bob).json() == []

    # Bob cannot view or fetch Alice's run
    r_unauthorized = client.get(f"/api/runs/{alice_run_id}", headers=h_bob)
    assert r_unauthorized.status_code == 404


def test_delete_run_and_auto_adjust():
    client = TestClient(app)

    # 1. Register Alice and run an invoice
    r_alice = client.post("/api/auth/signup", json={
        "name": "Alice Delete", "email": "alice.del@kestrel.com", "password": "Password@123", "confirm_password": "Password@123"
    })
    h_alice = {"Authorization": f"Bearer {r_alice.json()['token']}"}

    r_run = client.post("/api/samples/01_happy_apex_furniture.pdf/run", headers=h_alice)
    assert r_run.status_code == 200
    run_id = r_run.json()["run_id"]

    # Alice has 1 run and stats reflect it
    stats_before = client.get("/api/stats", headers=h_alice).json()
    assert stats_before["total"] == 1

    # 2. Bob cannot delete Alice's run
    r_bob = client.post("/api/auth/signup", json={
        "name": "Bob Del", "email": "bob.del@kestrel.com", "password": "Password@123", "confirm_password": "Password@123"
    })
    h_bob = {"Authorization": f"Bearer {r_bob.json()['token']}"}
    r_del_unauth = client.delete(f"/api/runs/{run_id}", headers=h_bob)
    assert r_del_unauth.status_code == 404

    # 3. Alice deletes her run
    r_del = client.delete(f"/api/runs/{run_id}", headers=h_alice)
    assert r_del.status_code == 200
    assert r_del.json()["ok"] is True

    # 4. Run is gone
    assert client.get(f"/api/runs/{run_id}", headers=h_alice).status_code == 404
    assert client.get("/api/runs", headers=h_alice).json() == []

    # 5. Stats adjust automatically back to 0
    stats_after = client.get("/api/stats", headers=h_alice).json()
    assert stats_after["total"] == 0
    assert stats_after["value_processed"] == 0

    # 6. Database tables for that run are cleaned up
    assert db.q("SELECT * FROM runs WHERE id=?", (run_id,)) == []
    assert db.q("SELECT * FROM run_events WHERE run_id=?", (run_id,)) == []
    assert db.q("SELECT * FROM ap_ledger WHERE run_id=?", (run_id,)) == []

