"""응답 저장소: SQLite 한 파일. 지급 정보(연락처·계좌·주민등록번호)와 서명은 암호화해 둔다."""
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
DATA_DIR.mkdir(parents=True, exist_ok=True)

SENSITIVE_KEYS = ("r_tel", "r_bank", "r_rrn")


def _env_or_file(env: str, filename: str, make) -> str:
    val = os.environ.get(env)
    if val:
        return val
    p = DATA_DIR / filename
    if not p.exists():
        p.write_text(make(), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


SECRET = _env_or_file("ADVISORY_SECRET", "secret_key.txt", lambda: secrets.token_urlsafe(48))
_enc_seed = _env_or_file("ADVISORY_ENC_KEY", "enc_key.txt", lambda: secrets.token_urlsafe(48))
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_enc_seed.encode()).digest()))


def encrypt(data: bytes) -> bytes:
    return _fernet.encrypt(data)


def decrypt(blob: bytes | None) -> bytes | None:
    if not blob:
        return None
    try:
        return _fernet.decrypt(blob)
    except InvalidToken:
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_lock = threading.Lock()
_conn = sqlite3.connect(DATA_DIR / "advisory.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
CREATE TABLE IF NOT EXISTS respondents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,            -- 관리자가 등록한 이름
    note          TEXT NOT NULL DEFAULT '', -- 관리자 메모(소속 등)
    created_at    TEXT NOT NULL,
    opened_at     TEXT,
    updated_at    TEXT,
    submitted_at  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',  -- new | draft | submitted
    data_json     TEXT NOT NULL DEFAULT '{}',   -- 일반 응답
    secret_blob   BLOB,                         -- 암호화한 지급 정보(JSON)
    signature     BLOB                          -- 암호화한 서명 PNG
);
""")
_conn.commit()


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


def list_respondents() -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM respondents ORDER BY id")]


def get_by_token(token: str) -> dict | None:
    with _lock:
        return _row(_conn.execute("SELECT * FROM respondents WHERE token = ?", (token,)).fetchone())


def get(rid: int) -> dict | None:
    with _lock:
        return _row(_conn.execute("SELECT * FROM respondents WHERE id = ?", (rid,)).fetchone())


def add(label: str, note: str = "") -> dict:
    with _lock:
        token = secrets.token_urlsafe(16)
        cur = _conn.execute(
            "INSERT INTO respondents (token, label, note, created_at, data_json) VALUES (?, ?, ?, ?, ?)",
            (token, label, note, now(), json.dumps({"name": label}, ensure_ascii=False)))
        _conn.commit()
        return _row(_conn.execute("SELECT * FROM respondents WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_meta(rid: int, label: str, note: str) -> None:
    with _lock:
        _conn.execute("UPDATE respondents SET label = ?, note = ? WHERE id = ?", (label, note, rid))
        _conn.commit()


def delete(rid: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM respondents WHERE id = ?", (rid,))
        _conn.commit()


def mark_opened(rid: int) -> None:
    with _lock:
        _conn.execute("UPDATE respondents SET opened_at = COALESCE(opened_at, ?) WHERE id = ?", (now(), rid))
        _conn.commit()


def read_secret(r: dict) -> dict:
    raw = decrypt(r.get("secret_blob"))
    return json.loads(raw) if raw else {}


def read_signature(r: dict) -> bytes | None:
    return decrypt(r.get("signature"))


def save(rid: int, data: dict, secret: dict, signature: bytes | None | str, status: str | None) -> dict:
    """signature: bytes 면 교체, '' 면 삭제, None 이면 그대로."""
    with _lock:
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
        _conn.execute(f"UPDATE respondents SET {', '.join(sets)} WHERE id = ?", args)
        _conn.commit()
        return _row(_conn.execute("SELECT * FROM respondents WHERE id = ?", (rid,)).fetchone())


def purge_payment_info() -> int:
    """모든 응답의 지급 정보(연락처·계좌·주민등록번호)를 지운다."""
    with _lock:
        cur = _conn.execute("UPDATE respondents SET secret_blob = NULL WHERE secret_blob IS NOT NULL")
        _conn.commit()
        return cur.rowcount
