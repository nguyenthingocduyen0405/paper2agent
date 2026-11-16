"""데이터베이스: 사용자, 논문, 채점 기준, 작업, 항목별 검증 결과, LLM 호출 기록.

DATABASE_URL
  EC2: postgresql+psycopg://<user>:<pw>@<RDS 주소>:5432/paperagent  (Secret web-secrets)
  로컬 시험: 비워 두면 sqlite:///data/web/paperagent.db
작업 상태의 원본은 jobs.doc(작업 전체 JSON)이다. 나머지 열은 목록과 통계를 위해 doc에서 뽑아 둔 값이다.
파일(논문, 코드, 보고서)은 디스크(/data)와 S3에 있고, DB에는 그 위치만 둔다.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.engine import Connection, Engine

DEFAULT_URL = "sqlite:///data/web/paperagent.db"
md = MetaData()

users = Table(
    "users", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("role", String(16), nullable=False),          # admin | user | service(Slack 도우미 등 API 전용)
    Column("daily_job_limit", Integer),                   # 하루에 만들 수 있는 작업 수. NULL이면 제한 없음
    Column("disabled", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
)

sessions = Table(
    "sessions", md,
    Column("token_hash", String(64), primary_key=True),   # 쿠키 값의 SHA-256. 쿠키 값 자체는 저장하지 않는다
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)

papers = Table(
    "papers", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # 같은 논문인지 가리는 값: 올린 파일의 SHA-256, 예제 논문은 "library:<폴더>"
    Column("fingerprint", String(200), nullable=False, unique=True),
    Column("title", Text),
    Column("filename", Text),
    Column("kind", String(16)),                           # pdf | md | library
    Column("s3_key", Text),                               # 원본 파일 위치(S3)
    Column("created_by", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("created_at", Float, nullable=False),
)

rubrics = Table(
    "rubrics", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", Integer, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
    Column("content_sha", String(64), nullable=False),
    Column("origin", String(16), nullable=False),         # auto(PaperAgent가 설계) | given(함께 올렸거나 예제)
    Column("job_id", String(16)),                         # 이 기준을 처음 쓴(설계한) 작업
    Column("content", Text, nullable=False),              # rubric.json 전체
    Column("leaves", Integer),
    Column("code_dev", Integer),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("paper_id", "content_sha"),
)

rubric_items = Table(
    "rubric_items", md,
    Column("rubric_id", Integer, ForeignKey("rubrics.id", ondelete="CASCADE"), primary_key=True),
    Column("leaf_id", String(128), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("category", String(64)),
    Column("section", Text),
    Column("requirement", Text, nullable=False),
)

jobs = Table(
    "jobs", md,
    Column("id", String(16), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("paper_id", Integer, ForeignKey("papers.id", ondelete="SET NULL")),
    Column("rubric_id", Integer, ForeignKey("rubrics.id", ondelete="SET NULL")),
    Column("title", Text),
    Column("status", String(16), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("llm_model", String(64)),
    Column("mock", Boolean, nullable=False, default=False),
    Column("skip_agent", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float),
    Column("finished_at", Float),
    Column("prepare_sec", Float),
    Column("agent_sec", Float),
    Column("judge_sec", Float),
    Column("score_passed", Integer),
    Column("score_judged", Integer),
    Column("score_errors", Integer),
    Column("archived_at", Float),                         # 결과를 S3에 올린 시각
    Column("doc", Text, nullable=False),
)
Index("ix_jobs_status", jobs.c.status)
Index("ix_jobs_user_created", jobs.c.user_id, jobs.c.created_at)
Index("ix_jobs_paper", jobs.c.paper_id)

item_scores = Table(
    "item_scores", md,
    Column("job_id", String(16), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("leaf_id", String(128), primary_key=True),
    Column("category", String(64)),
    Column("score", Integer),                             # 1 충족, 0 미충족, NULL 검증 오류
    Column("reason", Text),
    Column("error", Text),
    Column("sec", Float),
)

llm_calls = Table(
    "llm_calls", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(16), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("stage", String(16), nullable=False),          # prepare | agent | judge
    Column("step", String(32)),                           # rubric, P1~P5, 검증 항목 id
    Column("model", String(64)),
    Column("calls", Integer, nullable=False, default=1),  # 검증 항목 하나는 다시 묻기까지 합쳐 한 줄
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("cached_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("sec", Float),
    Column("finish_reason", String(32)),
)
Index("ix_calls_job_stage", llm_calls.c.job_id, llm_calls.c.stage)

_engine: Engine | None = None
_lock = threading.Lock()


def url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def engine() -> Engine:
    """처음 부를 때 연결하고 테이블을 만든다. RDS가 아직 켜지는 중이면 예외가 나고, 다음 호출에서 다시 시도한다."""
    global _engine
    with _lock:
        if _engine is None:
            u = url()
            if u.startswith("sqlite"):
                path = u.split("///", 1)[1]
                if path and path != ":memory:":
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                eng = create_engine(u, connect_args={"check_same_thread": False})

                @event.listens_for(eng, "connect")
                def _foreign_keys(dbapi_conn, _record):
                    dbapi_conn.execute("PRAGMA foreign_keys=ON")
            else:
                eng = create_engine(u, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800,
                                    connect_args={"connect_timeout": 5})
            md.create_all(eng)
            _engine = eng
        return _engine


def reset() -> None:
    """시험용: 다른 DATABASE_URL로 다시 연결한다."""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def upsert(conn: Connection, table: Table, values: dict, keys: list[str]) -> None:
    """키가 같은 행이 있으면 고치고 없으면 넣는다(PostgreSQL, SQLite 모두 ON CONFLICT)."""
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(table).values(**values)
    update = {k: stmt.excluded[k] for k in values if k not in keys}
    if update:
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=update)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    conn.execute(stmt)
