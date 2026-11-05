"""서식 docx 읽기·채우기.

서식(template.docx)에서 안내문·문항을 뽑아 화면에 쓰고(read_schema), 빈칸에 {{이름}} 표식을
넣어 두었다가(tokenize) 응답과 서명 이미지로 채운 docx 를 만든다(Template.fill).
서식 문구가 바뀌어도 표 구조(빈칸 개수·순서, 체크 항목)가 같으면 코드는 그대로 둔다.
구조가 달라지면 tokenize 의 assert 가 시작할 때 알려 준다.
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
EMPTY_T = '<w:t xml:space="preserve"></w:t>'
SEAL_RUN_TEXT = '<w:t xml:space="preserve">(인)</w:t>'
SIG_REL_ID = "rIdSignature"
SIG_PATH = "word/media/signature.png"

# 인적사항 표의 체크 항목: (값 이름, 서식에 적힌 순서대로의 보기)
CHECKS = [
    ("desk", ["있음", "없음"]),
    ("aiuse", ["현재 사용", "과거 사용했으나 현재 사용하지 않음", "사용 경험 없음"]),
    ("freq", ["거의 매일", "주 2~3회", "주 1회 이하", "거의 사용하지 않음", "사용 경험 없음"]),
]
HEAD_KEYS = ["name", "org"]                                    # 인적사항 표의 빈칸
RECEIPT_KEYS = ["r_name", "r_org", "r_tel", "r_bank", "r_rrn"]  # 영수증 표의 빈칸

_BAD_CHARS = re.compile("[^\n\x20-퟿-�\U00010000-\U0010ffff]")


def clean_text(s) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    s = _BAD_CHARS.sub("", s)
    return s.lstrip("\n").rstrip()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ptext(p) -> str:
    return "".join(n.text or "" for n in p.iter() if n.tag == W + "t")


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def read_schema(xml: str) -> dict:
    """본문을 위에서부터 훑어 안내문·문항·영수증 문구를 뽑는다."""
    body = ET.fromstring(xml).find(W + "body")
    out = {"head": [], "meta": [], "intro": [], "sections": [], "closing": "",
           "receipt": {}, "receipt_note": ""}
    section, pending, n_ans, in_receipt, multi_seen = None, [], 0, False, 0

    for el in body:
        if el.tag == W + "p":
            text = _ptext(el).strip()
            if not text:
                continue
            if in_receipt:
                out["receipt_note"] = text
            elif section is None:
                out["head"].append(text)
            else:
                pending.append(text)
            continue
        if el.tag != W + "tbl":
            continue

        rows = [list(tr.findall(W + "tc")) for tr in el.findall(W + "tr")]
        cells = [tc for r in rows for tc in r]
        if len(cells) == 1:
            paras = [_ptext(p).strip() for p in cells[0].findall(W + "p")]
            first = paras[0] if paras else ""
            if first[:1] in ROMAN:
                section = {"title": first, "items": [{"type": "note", "text": t} for t in pending]}
                out["sections"].append(section)
                pending = []
            elif first == "답변":
                title, guide = None, []
                for t in pending:
                    if re.match(r"^\d+\)\s", t):
                        section["items"].append({"type": "sub", "text": t})
                    elif t.startswith("※"):
                        section["items"].append({"type": "note", "text": t})
                    elif title is None and re.match(r"^\d+(-\d+)?\.\s", t):
                        title = t
                    else:
                        guide.append(t)
                n_ans += 1
                section["items"].append({"type": "q", "key": f"a{n_ans}",
                                         "title": title or "", "guide": guide})
                pending = []
            elif section is None and len(paras) > 1:
                for t in paras:
                    label, _, rest = t.partition("  ")
                    out["intro"].append([_squash(label), rest.strip()] if rest else ["", t])
        elif section is None:
            multi_seen += 1
            if multi_seen == 1:  # 연구과제명 / 연구자
                out["meta"] = [[_squash(_ptext(r[0])), _squash(_ptext(r[1]))] for r in rows]
        else:  # 영수증
            in_receipt = True
            out["closing"] = " ".join(pending)
            pending = []
            cell_paras = [[_squash(_ptext(p)) for p in r[0].findall(W + "p")] for r in rows]
            out["receipt"] = {
                "title": cell_paras[0][0],
                "amount": cell_paras[1][0],
                "purpose": cell_paras[2],
                "to": cell_paras[-1][-1],
            }
    out["n_answers"] = n_ans
    return out


def tokenize(xml: str, n_answers: int) -> tuple[str, dict]:
    """빈칸·체크 표시·날짜·(인) 자리에 {{이름}} 표식을 넣는다."""
    assert "{{" not in xml, "서식 본문에 '{{' 가 이미 들어 있음"
    keys = HEAD_KEYS + [f"a{i}" for i in range(1, n_answers + 1)] + RECEIPT_KEYS

    # 표 칸 안의 빈칸만 입력 자리다(표 사이의 빈 문단은 간격용).
    spots, pos = [], 0
    while (pos := xml.find(EMPTY_T, pos)) >= 0:
        if xml.rfind("<w:tc>", 0, pos) > xml.rfind("</w:tc>", 0, pos):
            spots.append(pos)
        pos += len(EMPTY_T)
    assert len(spots) == len(keys), f"빈칸 수가 다름: 서식 {len(spots)}개, 기대 {len(keys)}개"
    for k, pos in reversed(list(zip(keys, spots))):
        xml = xml[:pos] + '<w:t xml:space="preserve">{{%s}}</w:t>' % k + xml[pos + len(EMPTY_T):]

    blanks = {}

    def one(pattern, key):
        nonlocal xml
        hits = re.findall(pattern, xml)
        assert len(hits) == 1, f"{key} 자리를 정확히 하나 찾지 못함: {hits}"
        blanks[key] = hits[0]
        xml = re.sub(pattern, '<w:t xml:space="preserve">{{%s}}</w:t>' % key, xml, count=1)

    one(r'<w:t xml:space="preserve">(\s+년\s+개월)</w:t>', "career")
    one(r'<w:t xml:space="preserve">(\d{4}\.\s*\d{1,2}\.\s+\.)</w:t>', "date")

    labels = [lab for _, opts in CHECKS for lab in opts]
    for i, lab in enumerate(labels):
        pos = xml.find("☐")
        assert pos >= 0 and xml[pos + 1:].startswith(" " + lab), f"체크 항목 순서가 다름: {lab}"
        xml = xml[:pos] + "{{cb%d}}" % i + xml[pos + 1:]

    # 서명은 영수증의 (인) 글자 바로 앞에 떠 있는 그림으로 넣는다.
    assert xml.count(SEAL_RUN_TEXT) == 1, "영수증의 (인) 자리를 찾지 못함"
    seal = xml.index(SEAL_RUN_TEXT)
    run_start = xml.rfind("<w:r>", 0, seal)
    xml = xml[:run_start] + "{{sig}}" + xml[run_start:]

    # 여러 줄 답변은 문단을 복제해 넣으므로, 답변 빈칸이 속성 없는 <w:p> 로 시작해야 한다.
    for i in range(1, n_answers + 1):
        pos = xml.index("{{a%d}}" % i)
        start = xml.rfind("<w:p>", 0, pos)
        assert start >= 0 and "</w:p>" not in xml[start:pos], f"a{i} 문단 구조가 예상과 다름"
    return xml, blanks


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


def _signature_run(png: bytes) -> str:
    """(인) 글자 위에 겹쳐 놓는 서명 그림(글 뒤가 아니라 앞, 줄 바꿈 없음)."""
    w_px, h_px = _png_size(png)
    emu_h = 500000                       # 약 1.4 cm
    emu_w = int(emu_h * w_px / max(h_px, 1))
    if emu_w > 1250000:                  # 최대 약 3.5 cm (칸 폭 5 cm 안)
        emu_h = int(emu_h * 1250000 / emu_w)
        emu_w = 1250000
    seal_w = 230000                      # "(인)" 글자 폭 어림값
    # (인)은 칸 오른쪽 끝에 붙어 있으므로, 서명의 오른쪽 끝을 (인) 끝에 맞춰 왼쪽으로 펼친다.
    x = seal_w + 20000 - emu_w
    y = 95000 - emu_h // 2
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    pic = "http://schemas.openxmlformats.org/drawingml/2006/picture"
    return (
        '<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="251659264" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="character"><wp:posOffset>{x}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="line"><wp:posOffset>{y}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{emu_w}" cy="{emu_h}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:wrapNone/><wp:docPr id="901" name="서명"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="{a}" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic xmlns:a="{a}"><a:graphicData uri="{pic}"><pic:pic xmlns:pic="{pic}">'
        '<pic:nvPicPr><pic:cNvPr id="901" name="signature.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{SIG_REL_ID}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{emu_w}" cy="{emu_h}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>'
    )


class Template:
    def __init__(self, path: Path):
        self.path = path
        with zipfile.ZipFile(path) as z:
            self.parts = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
        xml = self.parts["word/document.xml"].decode("utf-8")
        s = read_schema(xml)
        self.doc, self.blanks = tokenize(xml, s["n_answers"])
        self.answer_keys = [f"a{i}" for i in range(1, s["n_answers"] + 1)]
        self.schema = {
            "title": s["head"][0],
            "subtitle": s["head"][1] if len(s["head"]) > 1 else "",
            "meta": s["meta"],
            "intro": s["intro"],
            "sections": s["sections"],
            "closing": s["closing"],
            "receipt": s["receipt"],
            "receiptNote": s["receipt_note"],
            "checks": [{"key": k, "options": opts} for k, opts in CHECKS],
        }

    # ── 채우기 ────────────────────────────────────────────────
    def fill_document(self, v: dict, signature_png: bytes | None = None) -> str:
        tpl = self.doc
        checked = []
        for key, opts in CHECKS:
            checked += [v.get(key) == i for i in range(len(opts))]

        def sub(m: re.Match) -> str:
            key, pos = m.group(1), m.start()
            if key == "sig":
                return _signature_run(signature_png) if signature_png else ""
            if re.fullmatch(r"cb\d+", key):
                return "■" if checked[int(key[2:])] else "☐"
            if key == "career":
                y, mo = clean_text(v.get("years")), clean_text(v.get("months"))
                if not y and not mo:
                    return self.blanks["career"]
                return _esc(f"{y or 0}년 {mo or 0}개월")
            if key == "date":
                d = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v.get("date") or "")
                if not d:
                    return self.blanks["date"]
                return f"{d[1]}.  {int(d[2])}.  {int(d[3])}."
            text = clean_text(v.get(key))
            if not text:
                return ""
            lines = text.split("\n")
            if len(lines) == 1 or not re.fullmatch(r"a\d+", key):
                return _esc(" ".join(lines))
            # 여러 줄 답변: 빈칸이 들어 있던 문단의 서식을 그대로 복제해 줄마다 한 문단씩 만든다.
            head = tpl[tpl.rfind("<w:p>", 0, pos):pos]
            return ("</w:t></w:r></w:p>" + head).join(_esc(x) for x in lines)

        return re.sub(r"\{\{(\w+)\}\}", sub, tpl)

    def build_docx(self, v: dict, signature_png: bytes | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in self.parts.items():
                if name == "word/document.xml":
                    data = self.fill_document(v, signature_png).encode("utf-8")
                elif name == "word/_rels/document.xml.rels" and signature_png:
                    rel = (f'<Relationship Id="{SIG_REL_ID}" Type="http://schemas.openxmlformats.org/'
                           f'officeDocument/2006/relationships/image" Target="media/signature.png"/>')
                    data = data.decode("utf-8").replace("</Relationships>", rel + "</Relationships>").encode("utf-8")
                z.writestr(name, data)
            if signature_png:
                z.writestr(SIG_PATH, signature_png)
        return buf.getvalue()
