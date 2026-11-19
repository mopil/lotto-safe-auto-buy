"""로컬 SQLite — 구매 내역(lotto_order)·로또 당첨번호(lotto_draw)를 쌓는다.

쿼리는 `%s` 자리표시를 쓴다 (이 파일이 sqlite 의 `?` 로 바꿔 준다). 행은 dict 로 돌려준다.
DB 파일: 환경변수 LOTTO_DB → 없으면 상태 폴더(LOTTO_STATE_DIR, 기본 ~/.lotto-safe)/lotto.sqlite.
GitHub Actions 에선 상태 폴더가 `state` 브랜치라 이 파일도 같이 커밋된다 — 그래서 WAL 이 아니라
journal_mode=DELETE (커밋 뒤엔 파일 하나가 늘 완전한 상태).
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists lotto_order (
    order_no    text primary key,              -- 사이트 주문번호. 연금은 'LP72-' 를 앞에 붙임
    game        text not null default 'lotto645', -- lotto645 · pension720
    round       integer not null,              -- 회차 (게임마다 따로 셈)
    bought_on   text,                          -- KST 'YYYY-MM-DD'
    games       integer,                       -- 게임(장) 수
    numbers     text,                          -- 로또 '[A] 자동: 1 2 3 4 5 6' · 연금 '1조 123456' 줄바꿈 묶음
    result      text,                          -- 로또 사이트 결과 원문 · 연금 공개 당첨번호로 판정한 글
    prize       integer default 0,             -- 당첨금 원 (세전)
    prize_net   integer,                       -- 세후 당첨금 원
    draw_date   text,                          -- 추첨일 'YYYY-MM-DD'
    checked_at  text,                          -- 마지막 조회 KST ISO
    notified_at text                           -- 결과 알림 보낸 KST ISO (null = 아직)
);
create table if not exists lotto_draw (
    round            integer primary key,
    draw_date        text,
    n1 integer, n2 integer, n3 integer, n4 integer, n5 integer, n6 integer,
    bonus            integer,
    first_prize_each integer,
    first_winners    integer,
    sales_amount     integer,
    source           text
);
"""
_NAMED = re.compile(r"%\((\w+)\)s")


def db_path() -> Path:
    if os.environ.get("LOTTO_DB"):
        return Path(os.environ["LOTTO_DB"])
    state_dir = os.environ.get("LOTTO_STATE_DIR") or str(Path.home() / ".lotto-safe")
    return Path(state_dir) / "lotto.sqlite"


def _sql(q: str) -> str:
    return _NAMED.sub(r":\1", q).replace("%s", "?").replace("%%", "%")


def _row(cur: sqlite3.Cursor, row: tuple) -> dict:
    return {d[0]: v for d, v in zip(cur.description, row)}


class Cur:
    def __init__(self, raw: sqlite3.Cursor):
        self.raw = raw

    def execute(self, sql: str, params=None) -> "Cur":
        self.raw.execute(_sql(sql), params if params is not None else ())
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount


class Conn:
    def __init__(self, raw: sqlite3.Connection):
        self.raw = raw

    def execute(self, sql: str, params=None) -> Cur:
        return Cur(self.raw.cursor()).execute(sql, params)

    def executescript(self, sql: str) -> None:
        self.raw.executescript(sql)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


_conn: Conn | None = None
_path: Path | None = None


def connect() -> Conn:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, timeout=60)
    raw.row_factory = _row
    raw.execute("pragma journal_mode=delete")
    raw.execute("pragma busy_timeout=60000")
    return Conn(raw)


@contextmanager
def conn():
    """프로세스당 커넥션 1개 재사용 (경로가 바뀌면 새로 연다). 블록 단위 commit, 예외면 rollback."""
    global _conn, _path
    if _conn is None or _path != db_path():
        close()
        _conn, _path = connect(), db_path()
    c = _conn
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise


def close() -> None:
    global _conn, _path
    if _conn is not None:
        _conn.raw.close()
    _conn, _path = None, None


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
