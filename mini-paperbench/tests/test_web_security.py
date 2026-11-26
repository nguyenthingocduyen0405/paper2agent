from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from web import app as web_app
from web import auth


def _fake_req(headers: dict[str, str]):
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()})


def test_same_origin_checks():
    assert web_app._same_origin(_fake_req({"host": "example.com", "origin": "https://example.com"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "origin": "https://evil.com"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "origin": "null"}))
    assert web_app._same_origin(_fake_req({"host": "example.com", "referer": "https://example.com/login"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "referer": "https://evil.com/phish"}))
    assert web_app._same_origin(_fake_req({"host": "example.com", "sec-fetch-site": "same-origin"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "sec-fetch-site": "cross-site"}))
    # Non-browser API client (no browser origin/referer/sec-fetch headers)
    assert web_app._same_origin(_fake_req({"host": "web:8000", "authorization": "Bearer tok"}))


def test_safe_next():
    assert web_app._safe_next("/jobs/abc?x=1") == "/jobs/abc?x=1"
    assert web_app._safe_next("//evil.com") == "/"
    assert web_app._safe_next("https://evil.com") == "/"
    assert web_app._safe_next("/foo\\bar") == "/"
    assert web_app._safe_next("") == "/"


def test_middleware_and_login_flow(temp_db, monkeypatch):
    # Prevent background orchestrator loop during unit tests
    async def _noop():
        web_app._ready.set()

    monkeypatch.setattr(web_app, "_orchestrate", _noop)
    auth.create_user("tester", "test-pass-123", role="user")

    with TestClient(web_app.app, base_url="http://testserver") as client:
        web_app._ready.set()

        assert client.get("/healthz").json() == {"ok": True}

        # HTML page redirects unauthenticated users to /login
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")

        # API route returns 401 JSON
        r = client.get("/api/jobs/nonexistent", follow_redirects=False)
        assert r.status_code == 401

        # Cross-origin POST is rejected with 403
        r = client.post("/login", data={"username": "tester", "password": "test-pass-123"},
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

        # Same-origin login succeeds and sets session cookie
        r = client.post("/login", data={"username": "tester", "password": "test-pass-123"},
                        headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303
        assert auth.COOKIE in client.cookies

        # Authenticated request to /account succeeds
        r = client.get("/account")
        assert r.status_code == 200

        # Logout revokes session
        r = client.post("/logout", headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/account", follow_redirects=False)
        assert r.status_code == 303
