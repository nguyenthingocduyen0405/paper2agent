from __future__ import annotations

import time

from web import auth


def test_password_hash_and_verify():
    h1 = auth.hash_password("secret-pass-123")
    h2 = auth.hash_password("secret-pass-123")
    assert h1 != h2  # unique random salt
    assert h1.startswith("scrypt$")
    assert auth.verify_password("secret-pass-123", h1)
    assert not auth.verify_password("wrong-pass", h1)
    assert not auth.verify_password("secret-pass-123", "invalid-format")
    assert not auth.verify_password("secret-pass-123", "bcrypt$1$2$3$aa$bb")


def test_check_new_user_validation():
    assert auth.check_new_user("alice_01", "longpassword", "user") == []
    errs = auth.check_new_user("a!", "short", "superuser")
    assert len(errs) == 3


def test_authenticate_and_username_lockout(temp_db):
    auth.create_user("alice", "correct-password", role="user")

    for _ in range(auth.LOCK_AFTER):
        user, err = auth.authenticate("alice", "wrong-password", client_ip="10.0.0.1")
        assert user is None
        assert "맞지 않습니다" in err

    # Even the correct password is blocked while locked out
    user, err = auth.authenticate("alice", "correct-password", client_ip="10.0.0.1")
    assert user is None
    assert "1분" in err

    # Simulate expiry of lockout window: bucket prunes stale timestamps
    with auth._fail_lock:
        auth._failures["alice"] = [time.time() - auth.LOCK_SEC - 5]
        auth._ip_failures.clear()
    user, err = auth.authenticate("alice", "correct-password", client_ip="10.0.0.1")
    assert user is not None and user["username"] == "alice"
    assert err == ""
    assert "alice" not in auth._failures


def test_authenticate_ip_lockout_across_usernames(temp_db):
    auth.create_user("bob", "correct-password", role="user")

    for i in range(auth.IP_LOCK_AFTER):
        user, _ = auth.authenticate(f"ghost_{i}", "badpass123", client_ip="203.0.113.9")
        assert user is None

    user, err = auth.authenticate("bob", "correct-password", client_ip="203.0.113.9")
    assert user is None
    assert "1분" in err

    # Another IP is not blocked
    user, err = auth.authenticate("bob", "correct-password", client_ip="203.0.113.10")
    assert user is not None and err == ""


def test_sessions_and_password_change_revocation(temp_db):
    u = auth.create_user("carol", "initial-pass", role="user")
    t1 = auth.new_session(u["id"])
    t2 = auth.new_session(u["id"])

    assert auth.user_for_session(t1)["username"] == "carol"
    assert auth.user_for_session(t2)["username"] == "carol"

    auth.end_session(t1)
    assert auth.user_for_session(t1) is None
    assert auth.user_for_session(t2) is not None

    # Password update revokes all remaining sessions
    auth.update_user(u["id"], password="new-password-99")
    assert auth.user_for_session(t2) is None
    assert auth.authenticate("carol", "new-password-99")[0] is not None

    # Disabling user revokes sessions and blocks login
    t3 = auth.new_session(u["id"])
    auth.update_user(u["id"], disabled=True)
    assert auth.user_for_session(t3) is None
    user, err = auth.authenticate("carol", "new-password-99")
    assert user is None and "중지" in err


def test_bearer_service_account(temp_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "rootadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    monkeypatch.setenv("SERVICE_TOKEN", "svc-secret-token")

    admin = auth.ensure_accounts()
    assert admin is not None and admin["role"] == "admin"

    # Service account cannot log in with password
    user, _ = auth.authenticate(auth.SERVICE_USER, "anything")
    assert user is None

    # Bearer token works only when exact
    assert auth.user_for_bearer("Bearer svc-secret-token")["role"] == "service"
    assert auth.user_for_bearer("Bearer wrong-token") is None
    assert auth.user_for_bearer("Basic abc") is None


def test_lockout_bucket_hard_memory_cap(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_TRACKED_KEYS", 5)
    bucket: dict[str, list[float]] = {}
    now = time.time()
    for i in range(12):
        bucket[f"u{i}"] = [now]
        auth._prune_bucket(bucket, f"u{i}", now)
    assert len(bucket) <= 5
