"""자문의견 요청서 수집 서버.

- 자문인: 관리자가 등록하면 생기는 개인 링크(/r/<token>)로 들어와 작성 → 임시저장 → 완료.
- 관리자: /admin 에서 자문인 등록·링크 복사·응답 현황 확인, docx 개별/일괄(ZIP) 내려받기,
  지급 완료 후 지급 정보 일괄 삭제.
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import re
import secrets
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from PIL import Image, ImageChops, ImageOps

from . import store
from .docx_template import CHECKS, Template, clean_text

HERE = Path(__file__).resolve().parent
TEMPLATE = Template(Path(os.environ.get("ADVISORY_TEMPLATE") or HERE.parent / "template.docx"))
FILE_BASE = "자문의견요청서_언론의AI활용연구"
KST = timezone(timedelta(hours=9))

ADMIN_PASSWORD = store._env_or_file(
    "ADVISORY_ADMIN_PASSWORD", "admin_password.txt", lambda: secrets.token_urlsafe(9))
if not os.environ.get("ADVISORY_ADMIN_PASSWORD"):
    print(f"[advisory] ADVISORY_ADMIN_PASSWORD 가 없어 {store.DATA_DIR / 'admin_password.txt'} 의 비밀번호를 씁니다.",
          flush=True)

COOKIE = "advisory_admin"
_session = URLSafeTimedSerializer(store.SECRET, salt="admin-session")
SESSION_MAX_AGE = 60 * 60 * 12

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

TEXT_LIMITS = {"name": 60, "org": 200, "years": 3, "months": 2, "date": 10,
               "r_name": 60, "r_org": 200, "r_tel": 40, "r_bank": 120, "r_rrn": 20}
ANSWER_LIMIT = 30000
FIELD_LABELS = {"name": "성명", "r_name": "영수인", "r_tel": "연락처", "r_bank": "은행 / 계좌번호",
                "r_rrn": "주민등록번호", "date": "작성일", "signature": "서명"}
REQUIRED_ON_SUBMIT = ["name", "r_name", "r_tel", "r_bank", "r_rrn", "date", "signature"]


@app.middleware("http")
async def _headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"   # 개인 링크가 다른 사이트로 새지 않게
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _page(name: str, **marks: str) -> HTMLResponse:
    html = (HERE / "pages" / name).read_text(encoding="utf-8")
    for k, v in marks.items():
        html = html.replace(k, v)
    return HTMLResponse(html)


def _json_for_html(obj) -> str:
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _message_page(title: str, text: str, status: int = 200) -> HTMLResponse:
    resp = _page("message.html", __TITLE__=title, __TEXT__=text)
    resp.status_code = status
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home():
    return _message_page("자문의견 요청서",
                         "안내받으신 개인 링크로 접속해 주십시오. 링크를 잃어버리셨다면 연구자에게 문의해 주십시오.")


# ── 자문인 ─────────────────────────────────────────────────────

def _respondent(token: str) -> dict:
    r = store.get_by_token(token) if re.fullmatch(r"[\w-]{10,64}", token or "") else None
    if not r:
        raise HTTPException(404, "링크가 올바르지 않습니다.")
    return r


def _mask(key: str, val: str) -> str:
    if not val:
        return ""
    if key == "r_rrn":
        d = re.sub(r"\D", "", val)
        return (d[:6] + "-*******") if len(d) >= 6 else "*******"
    digits = [i for i, c in enumerate(val) if c.isdigit()]
    keep = set(digits[-4:])
    return "".join("*" if (c.isdigit() and i not in keep) else c for i, c in enumerate(val))


def _png_data_url(png: bytes | None) -> str | None:
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else None


def _state(r: dict) -> dict:
    secret = store.read_secret(r)
    return {
        "status": r["status"],
        "submittedAt": r["submitted_at"],
        "updatedAt": r["updated_at"],
        "values": json.loads(r["data_json"] or "{}"),
        "masked": {k: _mask(k, secret.get(k, "")) for k in store.SENSITIVE_KEYS},
        "signature": _png_data_url(store.read_signature(r)),
    }


@app.get("/r/{token}", response_class=HTMLResponse)
def respondent_page(token: str):
    try:
        r = _respondent(token)
    except HTTPException:
        return _message_page("링크 오류", "링크가 올바르지 않습니다. 받으신 링크 전체를 그대로 붙여 넣었는지 확인해 주십시오.", 404)
    store.mark_opened(r["id"])
    boot = {**TEMPLATE.schema, "token": token, "answerKeys": TEMPLATE.answer_keys}
    return _page("form.html", __BOOT_JSON__=_json_for_html(boot),
                 __TITLE__=f'{TEMPLATE.schema["title"]} · {TEMPLATE.schema["subtitle"]}')


@app.get("/api/r/{token}")
def respondent_state(token: str):
    return _state(_respondent(token))


def _clean_values(raw: dict) -> tuple[dict, dict]:
    if not isinstance(raw, dict):
        raise HTTPException(400, "잘못된 요청입니다.")
    data, secret = {}, {}
    for k, limit in TEXT_LIMITS.items():
        v = clean_text(raw.get(k))[:limit]
        if k in ("years", "months"):
            v = re.sub(r"\D", "", v)
        if k == "date" and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            v = ""
        (secret if k in store.SENSITIVE_KEYS else data)[k] = v
    for k in TEMPLATE.answer_keys:
        data[k] = clean_text(raw.get(k))[:ANSWER_LIMIT]
    for key, opts in CHECKS:
        v = raw.get(key)
        data[key] = v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < len(opts) else None
    return data, secret


def _clean_signature(data_url) -> bytes | str | None:
    """None: 그대로 둠, '': 지움, bytes: 정리한 PNG."""
    if data_url is None:
        return None
    if data_url == "":
        return ""
    m = re.fullmatch(r"data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=]+)", str(data_url))
    if not m or len(m.group(2)) > 4_000_000:
        raise HTTPException(400, "서명 이미지를 읽지 못했습니다. PNG 또는 JPG 파일(3MB 이하)을 올려 주십시오.")
    try:
        img = Image.open(io.BytesIO(base64.b64decode(m.group(2))))
        img = ImageOps.exif_transpose(img)
        if img.width * img.height > 40_000_000:
            raise ValueError("too large")
        img = img.convert("RGBA")
    except Exception:
        raise HTTPException(400, "서명 이미지를 읽지 못했습니다. PNG 또는 JPG 파일을 올려 주십시오.")
    img.thumbnail((1600, 1600))
    # 흰 종이 위 서명 사진도 (인) 글자를 가리지 않게 밝은 바탕을 투명하게 만든다.
    ink = img.convert("L").point(lambda v: 0 if v > 200 else 255 if v <= 150 else (200 - v) * 255 // 50)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), ink))
    box = img.getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox()
    if not box:
        raise HTTPException(400, "서명이 비어 있습니다. 서명을 다시 해 주십시오.")
    pad = 6
    img = img.crop((max(box[0] - pad, 0), max(box[1] - pad, 0),
                    min(box[2] + pad, img.width), min(box[3] + pad, img.height)))
    img.thumbnail((800, 300))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _save(token: str, payload: dict, status: str | None) -> dict:
    r = _respondent(token)
    data, secret_in = _clean_values(payload.get("values") or {})
    secret = store.read_secret(r)
    for k, v in secret_in.items():   # 비워서 보낸 지급 정보는 저장된 값을 유지
        if v:
            secret[k] = v
    sig = _clean_signature(payload.get("signature"))
    if status == "submitted":
        has_sig = isinstance(sig, bytes) or (sig is None and r.get("signature"))
        values = {**data, **secret, "signature": "y" if has_sig else ""}
        missing = [FIELD_LABELS[k] for k in REQUIRED_ON_SUBMIT if not values.get(k)]
        if missing:
            raise HTTPException(400, "다음 항목을 입력해 주십시오: " + ", ".join(missing))
        if not re.fullmatch(r"\d{6}-?\d{7}", secret.get("r_rrn", "")):
            raise HTTPException(400, "주민등록번호 13자리를 확인해 주십시오.")
    elif r["status"] == "submitted" and status != "draft":
        raise HTTPException(409, "이미 제출되었습니다. 고치려면 '수정하기'를 눌러 주십시오.")
    return _state(store.save(r["id"], data, secret, sig, status))


@app.put("/api/r/{token}")
def respondent_save(token: str, payload: dict = Body(...)):
    return _save(token, payload, None)


@app.post("/api/r/{token}/submit")
def respondent_submit(token: str, payload: dict = Body(...)):
    return _save(token, payload, "submitted")


@app.post("/api/r/{token}/reopen")
def respondent_reopen(token: str):
    r = _respondent(token)
    store.save(r["id"], json.loads(r["data_json"]), store.read_secret(r), None, "draft")
    return _state(store.get(r["id"]))


# ── 관리자 ─────────────────────────────────────────────────────

_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _is_admin(request: Request) -> bool:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return False
    try:
        return _session.loads(raw, max_age=SESSION_MAX_AGE).get("admin") is True
    except BadSignature:
        return False


def require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(401, "다시 로그인해 주십시오.")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not _is_admin(request):
        return _page("login.html", __ERROR__="")
    boot = {"title": TEMPLATE.schema["title"], "subtitle": TEMPLATE.schema["subtitle"],
            "answerCount": len(TEMPLATE.answer_keys)}
    return _page("admin.html", __BOOT_JSON__=_json_for_html(boot))


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    ip, t = _client_ip(request), time.time()
    recent = [x for x in _fails.get(ip, []) if t - x < 600]
    if len(recent) >= 5:
        return _page("login.html", __ERROR__="로그인 시도가 많아 10분 동안 막았습니다.")
    if not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        _fails[ip] = recent + [t]
        time.sleep(0.5)
        return _page("login.html", __ERROR__="비밀번호가 맞지 않습니다.")
    _fails.pop(ip, None)
    resp = RedirectResponse("/admin", status_code=303)
    secure = request.url.scheme == "https"
    resp.set_cookie(COOKIE, _session.dumps({"admin": True}), max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="strict", secure=secure)
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


def _answered(data: dict) -> int:
    return sum(1 for k in TEMPLATE.answer_keys if (data.get(k) or "").strip())


def _summary(r: dict, base_url: str) -> dict:
    data = json.loads(r["data_json"] or "{}")
    secret = store.read_secret(r)
    return {
        "id": r["id"], "label": r["label"], "note": r["note"], "name": data.get("name", ""),
        "status": r["status"], "createdAt": r["created_at"], "openedAt": r["opened_at"],
        "updatedAt": r["updated_at"], "submittedAt": r["submitted_at"],
        "answered": _answered(data), "signature": bool(r["signature"]),
        "payment": all(secret.get(k) for k in store.SENSITIVE_KEYS),
        "link": f"{base_url}/r/{r['token']}",
    }


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/api/admin/respondents", dependencies=[Depends(require_admin)])
def admin_list(request: Request):
    base = _base_url(request)
    return [_summary(r, base) for r in store.list_respondents()]


@app.post("/api/admin/respondents", dependencies=[Depends(require_admin)])
def admin_add(request: Request, payload: dict = Body(...)):
    added = []
    for line in str(payload.get("text", "")).splitlines():
        label, _, note = line.partition(",")
        label, note = clean_text(label)[:60], clean_text(note)[:200]
        if label:
            added.append(store.add(label, note))
    if not added:
        raise HTTPException(400, "등록할 이름을 한 줄에 한 명씩 입력해 주십시오.")
    base = _base_url(request)
    return [_summary(r, base) for r in added]


@app.patch("/api/admin/respondents/{rid}", dependencies=[Depends(require_admin)])
def admin_edit(rid: int, payload: dict = Body(...)):
    r = store.get(rid) or _404()
    label = clean_text(payload.get("label", r["label"]))[:60] or r["label"]
    store.update_meta(rid, label, clean_text(payload.get("note", r["note"]))[:200])
    return {"ok": True}


@app.delete("/api/admin/respondents/{rid}", dependencies=[Depends(require_admin)])
def admin_delete(rid: int):
    store.get(rid) or _404()
    store.delete(rid)
    return {"ok": True}


@app.post("/api/admin/purge-payment", dependencies=[Depends(require_admin)])
def admin_purge():
    return {"cleared": store.purge_payment_info()}


def _404():
    raise HTTPException(404, "없는 자문인입니다.")


def _docx_for(r: dict) -> bytes:
    values = {**json.loads(r["data_json"] or "{}"), **store.read_secret(r)}
    return TEMPLATE.build_docx(values, store.read_signature(r))


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", s or "") or "이름없음"


def _download(data: bytes, filename: str, mime: str) -> Response:
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@app.get("/admin/docx/{rid}", dependencies=[Depends(require_admin)])
def admin_docx(rid: int):
    r = store.get(rid) or _404()
    name = json.loads(r["data_json"] or "{}").get("name") or r["label"]
    return _download(_docx_for(r), f"{FILE_BASE}_{_safe_name(name)}.docx", DOCX_MIME)


def _kst(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(KST).strftime("%Y-%m-%d %H:%M")


STATUS_KO = {"new": "미작성", "draft": "작성 중", "submitted": "제출 완료"}


@app.get("/admin/zip", dependencies=[Depends(require_admin)])
def admin_zip(include_drafts: int = 0):
    rows = [r for r in store.list_respondents()
            if r["status"] == "submitted" or (include_drafts and r["status"] == "draft")]
    if not rows:
        raise HTTPException(404, "내려받을 응답이 없습니다.")
    buf = io.BytesIO()
    sheet = io.StringIO()
    w = csv.writer(sheet)
    q_titles = [it["title"] or s["title"] for s in TEMPLATE.schema["sections"]
                for it in s["items"] if it["type"] == "q"]
    w.writerow(["번호", "등록 이름", "상태", "제출 시각(KST)", "성명", "소속/업무/직급", "근무 연수",
                "후배 지도·데스킹 경험", "생성형 AI 사용 경험", "생성형 AI 사용 빈도", *q_titles])
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, r in enumerate(rows, 1):
            data = json.loads(r["data_json"] or "{}")
            name = _safe_name(data.get("name") or r["label"])
            fname = f"{i:02d}_{FILE_BASE}_{name}.docx"
            while fname in used:
                fname = fname.replace(".docx", "_.docx")
            used.add(fname)
            z.writestr(fname, _docx_for(r))
            career = ""
            if data.get("years") or data.get("months"):
                career = f"{data.get('years') or 0}년 {data.get('months') or 0}개월"
            checks = [c["options"][data[c["key"]]] if isinstance(data.get(c["key"]), int) else ""
                      for c in TEMPLATE.schema["checks"]]
            w.writerow([i, r["label"], STATUS_KO.get(r["status"], r["status"]), _kst(r["submitted_at"]),
                        data.get("name", ""), data.get("org", ""), career, *checks,
                        *[data.get(k, "") for k in TEMPLATE.answer_keys]])
        z.writestr("응답요약.csv", "\ufeff" + sheet.getvalue())
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    return _download(buf.getvalue(), f"자문의견_응답_{stamp}.zip", "application/zip")


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/admin/docx", "/admin/zip")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return _message_page("오류", str(exc.detail), exc.status_code)
