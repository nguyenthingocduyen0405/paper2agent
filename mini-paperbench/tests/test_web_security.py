from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from web import app as web_app
from web import auth


def _fake_req(headers: dict[str, str], cookies: dict[str, str] | None = None, client_host: str = "127.0.0.1"):
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        cookies=cookies or {},
        client=SimpleNamespace(host=client_host),
    )


def test_same_origin_checks():
    assert web_app._same_origin(_fake_req({"host": "example.com", "origin": "https://example.com"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "origin": "https://evil.com"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "origin": "null"}))
    assert not web_app._same_origin(_fake_req({"host": "", "referer": "/login"}))
    assert web_app._same_origin(_fake_req({"host": "example.com", "referer": "https://example.com/login"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "referer": "https://evil.com/phish"}))
    assert web_app._same_origin(_fake_req({"host": "example.com", "sec-fetch-site": "same-origin"}))
    assert not web_app._same_origin(_fake_req({"host": "example.com", "sec-fetch-site": "cross-site"}))
    # Headerless request with session cookie must be rejected; Bearer-only API client is allowed
    assert not web_app._same_origin(_fake_req({"host": "example.com"}, cookies={auth.COOKIE: "sess"}))
    assert web_app._same_origin(_fake_req({"host": "web:8000", "authorization": "Bearer tok"}))


def test_client_ip_ignores_untrusted_xff():
    # Public peer cannot spoof X-Forwarded-For
    req_public = _fake_req({"x-forwarded-for": "1.2.3.4"}, client_host="8.8.8.8")
    assert web_app._client_ip(req_public) == "8.8.8.8"
    # Trusted private proxy: uses rightmost hop (appended by proxy, not attacker-prepended leftmost)
    req_proxy = _fake_req({"x-forwarded-for": "9.9.9.9, 203.0.113.42"}, client_host="10.42.0.37")
    assert web_app._client_ip(req_proxy) == "203.0.113.42"
    # Cloudflare proxy header takes precedence when peer is trusted private proxy
    req_cf = _fake_req({"cf-connecting-ip": "203.0.113.88", "x-forwarded-for": "1.1.1.1"}, client_host="10.42.0.35")
    assert web_app._client_ip(req_cf) == "203.0.113.88"


def test_safe_next():
    assert web_app._safe_next("/jobs/abc?x=1") == "/jobs/abc?x=1"
    assert web_app._safe_next("//evil.com") == "/"
    assert web_app._safe_next("https://evil.com") == "/"
    assert web_app._safe_next("/foo\\bar") == "/"
    assert web_app._safe_next("") == "/"


def test_parse_arxiv_id_and_ssrf_safety():
    assert web_app._parse_arxiv_id("2201.02177") == "2201.02177"
    assert web_app._parse_arxiv_id("arXiv:2201.02177v2") == "2201.02177v2"
    assert web_app._parse_arxiv_id("https://arxiv.org/abs/2201.02177") == "2201.02177"
    assert web_app._parse_arxiv_id("https://arxiv.org/pdf/2201.02177v1.pdf") == "2201.02177v1"
    assert web_app._parse_arxiv_id("hep-th/9901001") == "hep-th/9901001"
    # SSRF / arbitrary host / path traversal must be rejected
    assert web_app._parse_arxiv_id("http://169.254.169.254/latest/meta-data/") is None
    assert web_app._parse_arxiv_id("https://evil.com/abs/2201.02177") is None
    assert web_app._parse_arxiv_id("https://arxiv.org/abs/../../etc/passwd") is None
    assert web_app._parse_arxiv_id("not-an-arxiv-id") is None

    # Redirect handler must block redirects to non-arXiv hosts (e.g. EC2 IMDS)
    import urllib.error
    import pytest
    handler = web_app._ArxivOnlyRedirect()
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/paper.pdf")


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


def test_security_headers_on_every_response(temp_db):
    from fastapi.testclient import TestClient
    from web import app as web_app

    web_app._ready.set()
    with TestClient(web_app.app) as c:
        for resp in (c.get("/login"), c.post("/login", data={"username": "x", "password": "y"})):
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
            assert "Strict-Transport-Security" not in resp.headers  # plain http
        r = c.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert r.headers.get("Strict-Transport-Security", "").startswith("max-age=")
