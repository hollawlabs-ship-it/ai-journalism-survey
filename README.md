# 자문의견 요청서 수집 서버

"언론의 인공지능 활용에 대한 연구" 자문의견 요청서를 웹에서 받는 작은 서버.

- **자문인**: 관리자가 등록하면 사람마다 개인 링크(`/r/<토큰>`)가 생긴다. 링크로 들어와 답변을 쓰면 자동 저장되고,
  영수증의 서명은 화면에 직접 그리거나 이미지 파일로 올린다. **완료**를 누르면 제출되고, **수정하기**로 다시 열 수 있다.
- **관리자**: `/admin` (비밀번호 로그인). 자문인 등록·링크 복사, 응답 현황(제출/작성 중/시작 안 함, 답변 수, 서명·지급 정보 유무),
  Word 개별 내려받기, **한꺼번에 내려받기(ZIP)** — 서식(`template.docx`)을 채운 docx(서명 포함)와 `응답요약.csv`(지급 정보 제외).
  자문비 지급 후 **지급 정보 모두 삭제**.
- 연락처·계좌번호·주민등록번호와 서명은 DB 에 암호화해 저장한다(Fernet, 키는 `ADVISORY_ENC_KEY`).

## 구조

| 경로 | 역할 |
|---|---|
| `template.docx` | 원본 서식. 문항 문구는 여기서 자동으로 읽는다 |
| `app/docx_template.py` | 서식 읽기(문항 추출), 빈칸 표식, docx 채우기·서명 삽입 |
| `app/store.py` | SQLite 저장, 암호화 |
| `app/main.py` | 라우트(자문인 API, 관리자 API, ZIP) |
| `app/pages/` | 화면 틀(form, admin, login, message) |
| `app/static/` | CSS, 로고 |

## 로컬 실행

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -X utf8 -m uvicorn app.main:app --port 8768
```

`ADVISORY_ADMIN_PASSWORD` 가 없으면 `data/admin_password.txt` 에 임의 비밀번호를 만들어 쓴다.
redcarpet 의 `.claude/launch.json` 에는 `advisory-form` 으로 등록돼 있다.

## 환경 변수

| 이름 | 뜻 |
|---|---|
| `ADVISORY_ADMIN_PASSWORD` | 관리자 콘솔 비밀번호 |
| `ADVISORY_SECRET` | 로그인 쿠키 서명 키 |
| `ADVISORY_ENC_KEY` | 지급 정보·서명 암호화 키 (바꾸면 기존 자료를 못 읽음) |
| `ADVISORY_DATA` | DB·키 파일 폴더 (기본 `./data`, Render 는 `/data` 디스크) |

## 서식을 바꿀 때

`template.docx` 를 새 서식으로 바꾸고 서버를 다시 시작한다. 빈칸 개수·순서나 체크 항목 보기, 영수증의 `(인)` 자리가
바뀌면 시작할 때 assert 로 알려 준다. 그때는 `docx_template.py` 의 `HEAD_KEYS`·`RECEIPT_KEYS`·`CHECKS` 와
`pages/form.html` 의 입력란을 함께 고친다.

## 배포 (Render)

`render.yaml` 블루프린트. Starter 플랜 + 1GB 디스크(`/data`). 공개 저장소를 `https://render.com/deploy?repo=<URL>` 로
연결하고, 배포 화면에서 `ADVISORY_ADMIN_PASSWORD` 만 입력하면 된다.
