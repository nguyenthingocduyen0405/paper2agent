from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """격리된 임시 SQLite DB로 web.db / web.auth 상태를 초기화한다."""
    from web import auth, db

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    db.reset()
    with auth._fail_lock:
        auth._failures.clear()
        auth._ip_failures.clear()
    db.engine()
    yield db_file
    db.reset()
    with auth._fail_lock:
        auth._failures.clear()
        auth._ip_failures.clear()
