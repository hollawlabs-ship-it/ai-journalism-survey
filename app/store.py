"""응답 저장소. 지급 정보(연락처·계좌·주민등록번호)와 서명은 암호화해 둔다.

- `DATABASE_URL` 이 있으면 PostgreSQL (Render 무료 플랜: 서버 파일이 재시작마다 지워지므로 DB 에 둔다)
- 없으면 `ADVISORY_DATA` 폴더의 SQLite 한 파일 (로컬 실행)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.environ.get("ADVISORY_DATA") or Path(__file__).resolve().parent.parent / "data")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PG = bool(DATABASE_URL)

SENSITIVE_KEYS = ("r_tel", "r_bank", "r_rrn")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS respondents (
    id            {id},
    token         TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,            -- 관리자가 등록한 이름
    note          TEXT NOT NULL DEFAULT '', -- 관리자 메모(소속 등)
    created_at    TEXT NOT NULL,
    opened_at     TEXT,
    updated_at    TEXT,
    submitted_at  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',  -- new | draft | submitted
    data_json     TEXT NOT NULL DEFAULT '{{}}', -- 일반 응답
    secret_blob   {blob},                       -- 암호화한 지급 정보(JSON)
    signature     {blob}                        -- 암호화한 서명 PNG
);
CREATE TABLE IF NOT EXISTS kv (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);
"""

_lock = threading.Lock()

if PG:
    import psycopg
    from psycopg.rows import dict_row

    def _connect():
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=15)

    _pg = None

    def _run(sql: str, args=(), fetch: str = "") -> object:
        """sql 의 ? 자리표시를 %s 로 바꿔 실행. 연결이 끊겼으면 한 번 다시 붙는다."""
        global _pg
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                if _pg is None or _pg.closed:
                    _pg = _connect()
                cur = _pg.execute(sql, args)
                if fetch == "one":
                    r = cur.fetchone()
                    return _fix(r) if r else None
                if fetch == "all":
                    return [_fix(r) for r in cur.fetchall()]
                return cur.rowcount
            except psycopg.OperationalError:
                try:
                    if _pg is not None:
                        _pg.close()
                finally:
                    _pg = None
                if attempt == 2:
                    raise
        return None

    def _fix(r: dict) -> dict:
        return {k: (bytes(v) if isinstance(v, memoryview) else v) for k, v in r.items()}

    with _lock:
        for stmt in _SCHEMA.format(id="SERIAL PRIMARY KEY", blob="BYTEA").split(";"):
            if stmt.strip():
                _run(stmt)
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DATA_DIR / "advisory.db", check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(_SCHEMA.format(id="INTEGER PRIMARY KEY AUTOINCREMENT", blob="BLOB"))
    _conn.commit()

    def _run(sql: str, args=(), fetch: str = "") -> object:
        cur = _conn.execute(sql, args)
        if fetch == "one":
            r = cur.fetchone()
            _conn.commit()
            return dict(r) if r else None
        if fetch == "all":
            rows = [dict(r) for r in cur.fetchall()]
            _conn.commit()
            return rows
        _conn.commit()
        return cur.rowcount


def _env_or_stored(env: str, name: str, make) -> str:
    """환경 변수 → (PostgreSQL 이면 DB 의 kv 표 / 아니면 data 폴더 파일) 순서로 찾고, 없으면 만들어 둔다."""
    val = os.environ.get(env)
    if val:
        return val
    if PG:
        with _lock:
            _run("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO NOTHING", (name, make()))
            return _run("SELECT v FROM kv WHERE k = ?", (name,), "one")["v"]
    p = DATA_DIR / name
    if not p.exists():
        p.write_text(make(), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def stored_where(name: str) -> str:
    return "DB 의 kv 표" if PG else str(DATA_DIR / name)


SECRET = _env_or_stored("ADVISORY_SECRET", "secret_key.txt", lambda: secrets.token_urlsafe(48))
_enc_seed = _env_or_stored("ADVISORY_ENC_KEY", "enc_key.txt", lambda: secrets.token_urlsafe(48))
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_enc_seed.encode()).digest()))


def encrypt(data: bytes) -> bytes:
    return _fernet.encrypt(data)


def decrypt(blob: bytes | None) -> bytes | None:
    if not blob:
        return None
    try:
        return _fernet.decrypt(bytes(blob))
    except InvalidToken:
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_respondents() -> list[dict]:
    with _lock:
        return _run("SELECT * FROM respondents ORDER BY id", (), "all")


def get_by_token(token: str) -> dict | None:
    with _lock:
        return _run("SELECT * FROM respondents WHERE token = ?", (token,), "one")


def get(rid: int) -> dict | None:
    with _lock:
        return _run("SELECT * FROM respondents WHERE id = ?", (rid,), "one")


# 개인 링크 코드: 헷갈리는 글자(0·o, 1·l·i)를 뺀 소문자·숫자 31종 10자 → 약 8×10^14 가지
TOKEN_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
TOKEN_LEN = 10


def new_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))


def add(label: str, note: str = "") -> dict:
    with _lock:
        token = new_token()
        while _run("SELECT 1 AS x FROM respondents WHERE token = ?", (token,), "one"):
            token = new_token()
        return _run(
            "INSERT INTO respondents (token, label, note, created_at, data_json) VALUES (?, ?, ?, ?, ?) RETURNING *",
            (token, label, note, now(), json.dumps({"name": label}, ensure_ascii=False)), "one")


def update_meta(rid: int, label: str, note: str) -> None:
    with _lock:
        _run("UPDATE respondents SET label = ?, note = ? WHERE id = ?", (label, note, rid))


def delete(rid: int) -> None:
    with _lock:
        _run("DELETE FROM respondents WHERE id = ?", (rid,))


def mark_opened(rid: int) -> None:
    with _lock:
        _run("UPDATE respondents SET opened_at = COALESCE(opened_at, ?) WHERE id = ?", (now(), rid))


def read_secret(r: dict) -> dict:
    raw = decrypt(r.get("secret_blob"))
    return json.loads(raw) if raw else {}


def read_signature(r: dict) -> bytes | None:
    return decrypt(r.get("signature"))


def save(rid: int, data: dict, secret: dict, signature: bytes | None | str, status: str | None) -> dict:
    """signature: bytes 면 교체, '' 면 삭제, None 이면 그대로."""
    sets = ["data_json = ?", "secret_blob = ?", "updated_at = ?"]
    args: list = [json.dumps(data, ensure_ascii=False),
                  encrypt(json.dumps(secret, ensure_ascii=False).encode()) if secret else None, now()]
    if isinstance(signature, bytes):
        sets.append("signature = ?"); args.append(encrypt(signature))
    elif signature == "":
        sets.append("signature = NULL")
    if status == "submitted":
        sets += ["status = 'submitted'", "submitted_at = ?"]; args.append(now())
    elif status == "draft":
        sets += ["status = 'draft'", "submitted_at = NULL"]
    else:  # 상태는 그대로 두되, 처음 저장이면 작성 중으로
        sets.append("status = CASE status WHEN 'new' THEN 'draft' ELSE status END")
    args.append(rid)
    with _lock:
        return _run(f"UPDATE respondents SET {', '.join(sets)} WHERE id = ? RETURNING *", args, "one")


def purge_payment_info() -> int:
    """모든 응답의 지급 정보(연락처·계좌·주민등록번호)를 지운다."""
    with _lock:
        return _run("UPDATE respondents SET secret_blob = NULL WHERE secret_blob IS NOT NULL")
