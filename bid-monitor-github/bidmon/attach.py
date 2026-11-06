"""HWP 5.0 / HWPX 첨부파일에서 본문 텍스트만 뽑기 (순수 파이썬, olefile 만 필요)"""
import io, re, zlib, zipfile, html, struct, logging, warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
log = logging.getLogger(__name__)

def _hwp5_text(data: bytes) -> str:
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    hdr = ole.openstream("FileHeader").read()
    flags = struct.unpack("<I", hdr[36:40])[0]
    compressed = flags & 1
    if flags & 2:            # 암호화 문서
        return ""
    out = []
    secs = sorted([e for e in ole.listdir() if e[0] == "BodyText" and e[1].startswith("Section")],
                  key=lambda e: int(e[1][7:]))
    for e in secs:
        raw = ole.openstream(e).read()
        if compressed:
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error:
                continue
        i, n = 0, len(raw)
        while i + 4 <= n:
            h = struct.unpack("<I", raw[i:i+4])[0]
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = struct.unpack("<I", raw[i:i+4])[0]; i += 4
            body = raw[i:i+size]; i += size
            if tag == 67:    # HWPTAG_PARA_TEXT
                j, chars = 0, []
                while j + 2 <= len(body):
                    c = struct.unpack("<H", body[j:j+2])[0]; j += 2
                    if c < 32:
                        if c in (1,2,3,4,5,6,7,8,9,11,12,14,15,16,17,18,19,20,21,22,23):
                            j += 14          # 확장/인라인 컨트롤: 8 문자(16바이트) 차지
                        if c in (10, 13):
                            chars.append(" ")
                        continue
                    chars.append(chr(c))
                out.append("".join(chars))
    return "\n".join(out)

def _hwpx_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    t = []
    for nm in z.namelist():
        if re.match(r"Contents/section\d+\.xml", nm):
            x = z.read(nm).decode("utf-8", "ignore")
            x = re.sub(r"<hp:tab[^>]*/>", " ", x)
            t.append(html.unescape(re.sub(r"<[^>]+>", " ", x)))
    return "\n".join(t)

def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return ""
    try:
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:6])
    except Exception:
        return ""

def extract_text(data: bytes) -> str:
    """확장자와 무관하게 내용으로 판별 (LH는 .hwpx 이름으로 HWP5 바이너리를 줌)"""
    try:
        if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return _hwp5_text(data)
        if data[:2] == b"PK":
            return _hwpx_text(data)
        if data[:5] == b"%PDF-":
            return _pdf_text(data)
    except Exception:
        return ""
    return ""

_DT = r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\.?"
_TM = r"(?:[\s,]*(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{2})?\s*분?)?"
_TM2 = r"(?:\s*[~∼–-]\s*(?:오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{2})?\s*분?)?"   # 10:00∼15:00 의 뒤쪽
# 우선순위: 참가신청/등록 → 접수/제출 → 마감
_KW = (r"(?:참가|참여|응모)\s*(?:의향서|신청서|신청|등록)\s*(?:서)?\s*(?:접수|제출|등록|마감|기한|기간|일시)?",
       r"(?:작품|응모작품|제안서|입찰서|입찰|서류|신청서|사업신청서|사업계획서|제출서류|사업수행능력평가서)?\s*(?:접수|제출)\s*(?:마감|기한|기간|일시|일정|일)?",
       r"마감\s*(?:일시|일자|일|기한)?")
_RANGE = r"(?:" + _DT + _TM + r"[^~∼–-]{0,40}[~∼–-]\s*)?" + _DT + _TM + _TM2


def _normalize(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "")
    t = re.sub(r"[’‘'`]\s?(\d{2})\s*\.", r"20\1.", t)                       # ’26.10.23. → 2026.10.23.
    t = re.sub(r"\(\s*[월화수목금토일]\s*요?일?\s*[,)]?\s*", " ", t)          # (월) / ( 목요일) / (월, 10:00 → 제거
    return t


def find_deadline(text: str) -> str:
    """본문에서 접수/제출 마감 일시 → 'YYYY-MM-DD HH:MM' (기간이면 끝 날짜·끝 시각, 없으면 '')"""
    t = _normalize(text)
    for kw in _KW:
        best = ""
        for m in re.finditer(kw + r"[^0-9]{0,30}?" + _RANGE, t):
            g = m.groups()
            # g: [y1,m1,d1,ap1,h1,mi1, y2,m2,d2,ap2,h2,mi2, h3,mi3]
            y, mo, d, ap, hh, mi, h3, mi3 = g[6], g[7], g[8], g[9], g[10], g[11], g[12], g[13]
            if h3:
                hh, mi = h3, mi3
            try:
                h = int(hh) if hh else 18
                if ap == "오후" and h < 12:
                    h += 12
                if h > 23 or not (1 <= int(mo) <= 12) or not (1 <= int(d) <= 31):
                    continue
                val = f"{y}-{int(mo):02d}-{int(d):02d} {h:02d}:{int(mi or 0):02d}"
            except ValueError:
                continue
            if val > best:
                best = val
        if best:
            return best
    return ""


def deadline_from_attachments(sess, urls, max_files=3, max_bytes=12_000_000) -> str:
    """첨부파일(공고문 hwp/hwpx/pdf)을 차례로 내려받아 마감일시를 찾음. 못 찾으면 ''"""
    for url in list(urls)[:max_files]:
        try:
            r = sess.get(url, timeout=(15, 90), stream=True)
            if r.status_code != 200:
                continue
            if int(r.headers.get("content-length") or 0) > max_bytes:
                continue
            data = r.raw.read(max_bytes + 1) if hasattr(r, "raw") else r.content
            if len(data) > max_bytes:
                continue
            dl = find_deadline(extract_text(data))
            if dl:
                return dl
        except Exception as e:
            log.debug("첨부 분석 실패 %s: %s", url, e)
    return ""


# ──────────────────────────────────────────────────────────────────────────
# 공고문 본문에서 상세 정보 추출: 세부 일정 · 심사위원 · 대지위치 · 연면적 · 용도 · 요약
# ──────────────────────────────────────────────────────────────────────────
_SCHED = (
    ("site_brief", r"현장\s*설명회?"),
    ("reg_close", r"(?:참가|참여|응모)\s*(?:의향서|신청서|신청|등록)\s*(?:서)?\s*(?:접수|제출|등록|마감|기한|기간|일시)?"),
    ("qna_close", r"질의\s*(?:서)?\s*(?:접수|제출)\s*(?:마감|기한|기간|일시)?"),
    ("qna_answer", r"질의\s*(?:에\s*대한\s*)?(?:답변|회신|응답)\s*(?:공개|게시|일시|일)?"),
    ("submit_close", r"(?:작품|응모작품|제안서|입찰서|설계도서|제출서류|사업계획서)\s*(?:접수|제출)\s*(?:마감|기한|기간|일시|일정|일)?"),
    ("judge", r"(?:작품\s*)?심사\s*(?:일시|일정|일|기간)"),
    ("announce", r"(?:심사\s*)?결과\s*(?:발표|공개|통보)|당선작\s*(?:발표|공개)"),
)
SCHED_LABEL = {"site_brief": "현장설명회", "reg_close": "참가등록 마감", "qna_close": "질의 접수 마감", "qna_answer": "질의 답변 공개",
               "submit_close": "작품(입찰) 접수 마감", "judge": "심사", "announce": "결과 발표"}
_JURY_NOTE = r"(추후\s*공개|별도\s*공개|사전\s*공개|작품\s*접수\s*(?:마감\s*)?(?:전|후)\s*공개|공개\s*예정|비공개|심사\s*(?:전|당일)\s*공개)"
_NAME = r"([가-힣]{2,4})"
_ORG = r"[(（]\s*([^()（）]{2,40}?)\s*[)）]"
_STOP_WORDS = {"심사위원", "위원장", "위원", "성명", "소속", "직위", "구분", "비고", "예비", "본심", "후보", "명단", "다음과", "같음", "선정", "구성"}


def _find_date(text: str, kw: str) -> str:
    """kw 뒤 40자 내 날짜(기간이면 끝) → 'YYYY-MM-DD HH:MM' / 시각 없으면 'YYYY-MM-DD'"""
    best = ""
    for m in re.finditer("(?:" + kw + r")[^0-9]{0,30}?" + _RANGE, text):
        g = m.groups()
        y, mo, d, ap, hh, mi, h3, mi3 = g[6], g[7], g[8], g[9], g[10], g[11], g[12], g[13]
        if h3:
            hh, mi = h3, mi3
        try:
            if not (1 <= int(mo) <= 12) or not (1 <= int(d) <= 31):
                continue
            val = f"{y}-{int(mo):02d}-{int(d):02d}"
            if hh:
                h = int(hh)
                if ap == "오후" and h < 12:
                    h += 12
                if h <= 23:
                    val += f" {h:02d}:{int(mi or 0):02d}"
        except ValueError:
            continue
        if val > best:
            best = val
    return best


def find_schedule(text: str) -> dict:
    t = _normalize(text)
    out = {}
    for key, kw in _SCHED:
        v = _find_date(t, kw)
        if v:
            out[key] = v
    return out


_JURY_ROLE = r"(?:예\s*비\s*심사\s*위원|예\s*비\s*위원|예\s*비|심사\s*위원장|위원장|심사\s*위원|위원|부위원장|간사)"
_JURY_HEAD = r"(?:심사\s*위원(?:회)?\s*(?:명단|구성|현황|위촉|명부)|심사\s*위원(?:회)?\s*(?:은|는)?\s*다음과\s*같)"
_JURY_END = r"(?:※|제척|기피|회피|\d\s*[)）.]\s*[가-힣]|[가-힣]\.\s|\d+\.\d+\s|심사\s*방법|심사\s*기준|평가\s*방식|평가\s*항목|운영\s*방법|이상\s*\d+\s*(?:명|인))"
_NOT_NAME = {"심사위원", "위원장", "위원", "성명", "소속", "직위", "구분", "비고", "예비", "본심", "후보", "명단", "다음과", "같음", "선정", "구성",
             "번호", "순번", "연번", "직책", "직급", "분야", "전공", "소속기관", "기관명", "건축사", "교수", "대표", "이사", "부장", "팀장", "과장",
             "공개", "추후", "별도", "비공개", "예정", "일원", "백만원", "포함", "이상", "이하", "미만", "등급", "인증", "명", "인"}
_NAME_TOKEN = re.compile(r"^[가-힣]{2,4}$")


def _jury_from_table(seg: str) -> list[dict]:
    """표를 텍스트로 편 형태: '1 심사위원 삼육대학교 정광호 2 사이디엔씨 건축사사무소 이진희 … 1 예비 심사위원 OO공사 박성만'
    → 번호 토큰으로 행을 끊고, 각 행의 마지막 이름꼴 토큰을 성명, 나머지를 소속으로"""
    toks = seg.split()
    rows, cur = [], None
    for tk in toks:
        if re.fullmatch(r"\d{1,2}", tk) and (cur is None or 1 <= int(tk) <= 30):
            if cur: rows.append(cur)
            cur = []
        elif cur is not None:
            cur.append(tk)
    if cur: rows.append(cur)
    members, reserve = [], False
    for row in rows:
        if not row: continue
        line = " ".join(row)
        role = ""
        m = re.match(_JURY_ROLE, line)
        if m:
            role = re.sub(r"\s+", "", m.group(0)); line = line[m.end():].strip()
        if role.startswith("예비"): reserve = True
        elif role: reserve = False
        line = re.sub(r"\s*[(（][^()（）]{0,40}[)）]", "", line)      # 괄호 설명 제거
        parts = line.split()
        # 성명: 뒤에서부터 이름꼴 토큰 (한글 2~4자, 소속 같은 말 제외). 그 뒤는 비고로 봄
        ni = None
        for i in range(len(parts) - 1, -1, -1):
            w = parts[i]
            if _NAME_TOKEN.match(w) and w not in _NOT_NAME and not re.search(r"(?:대학교|공사|공단|건설|사무소|연구원|연구소|협회|산업|센터|학과|시청|군청|구청|도청)$", w):
                ni = i; break
        if ni is None: continue
        name = parts[ni]
        org = " ".join(parts[:ni]).strip(" -·:")
        if not org and members: org = ""
        if reserve: role = "예비"
        elif role in ("위원장", "심사위원장"): role = "위원장"
        else: role = ""
        if name in {x["name"] for x in members}: continue
        d = {"name": name, "org": org}
        if role: d["role"] = role
        members.append(d)
        if len(members) >= 20: break
    return members


def find_jury(text: str) -> dict:
    """{'members': [{'name','org','role'?}], 'note': '추후 공개' ...}  — 공고문에 심사위원 항목이 있을 때만"""
    t = re.sub(r"[ \t]+", " ", text or "")
    out = {}
    # 1) 명단 표: '심사위원회 명단' 머리말 뒤 ~ 다음 절 전까지
    for m in re.finditer(_JURY_HEAD, t):
        seg = t[m.end(): m.end() + 2500]
        e = re.search(_JURY_END, seg[40:])
        if e: seg = seg[:40 + e.start()]
        seg = re.sub(r"^\s*(?:번\s*호|순\s*번|연\s*번|구\s*분|소\s*속|성\s*명|직\s*위|직\s*책|분\s*야|비\s*고|\s)+", " ", seg)
        if not re.search(r"(?:번\s*호|순\s*번|연\s*번|성\s*명|소\s*속)", t[m.end(): m.end() + 120]) and not re.search(r"\b1\s", seg[:80]):
            continue
        members = _jury_from_table(seg)
        if len(members) >= 2:
            out["members"] = members
            break
    # 2) '홍길동(OO대학교)' 꼴
    if "members" not in out:
        m = re.search(r"심사\s*위원(?:회|단)?\s*(?:명단|구성|현황|소개)?", t)
        if not m: return {}
        seg = t[m.start(): m.start() + 1500]
        members, seen = [], set()
        for mm in re.finditer(_NAME + r"\s*" + _ORG, seg):
            name, org = mm.group(1), mm.group(2).strip()
            if name in _NOT_NAME or any(w in name for w in ("위원", "심사", "소속")): continue
            if re.search(r"\d{4}|㎡|원$|억|만|등급|이상|포함", org): continue
            if not re.search(r"(?:대학|공사|공단|건축|설계|연구|협회|사무소|엔지니어링|구청|시청|청|원|센터|학교)", org): continue
            if name not in seen:
                seen.add(name); members.append({"name": name, "org": org})
            if len(members) >= 15: break
        if len(members) >= 2:
            out["members"] = members
    # 3) 공개 시점 안내
    m = re.search(r"심사\s*위원[^.]{0,60}?" + _JURY_NOTE, t)
    if m:
        out["note"] = re.sub(r"\s+", " ", m.group(1))
    return out


def find_site(text: str) -> dict:
    t = _normalize(text)
    out = {}
    m = re.search(r"(?:대지\s*위치|사업\s*위치|사업\s*대상지|위\s*치|소재지)\s*[:：]?\s*([^\n·|]{4,70}?)(?=\s{2,}|\s(?:[○◦▪\-□■●◎]|[가-힣]{1,3}\s*[:：])|\s(?:대지|연면적|용도|규모|지역|지구)\b|$)", t)
    if m:
        v = m.group(1).strip(" .,;:")
        if re.search(r"(?:시|군|구|도|읍|면|동|리|로|길)\b|[0-9]", v):
            out["site"] = v[:70]
    m = re.search(r"연\s*면\s*적\s*[:：]?\s*(?:약\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(㎡|m2|m²|평)", t)
    if m:
        out["area"] = m.group(1).replace(",", "") + ("㎡" if m.group(2) != "평" else "평")
    m = re.search(r"(?:주\s*)?용\s*도\s*[:：]?\s*([가-힣A-Za-z0-9·,()（） ]{2,40}?)(?=\s{2,}|\s(?:[○◦▪\-□■●◎]|규모|연면적|대지|구조|층수)|$)", t)
    if m:
        v = m.group(1).strip(" .,;:")
        if 2 <= len(v) <= 40 and not re.search(r"지역|지구$", v):
            out["use"] = v
    m = re.search(r"(?:규\s*모)\s*[:：]?\s*((?:지하\s*\d+\s*층\s*[,/~∼]?\s*)?지상\s*\d+\s*층)", t)
    if m:
        out["scale"] = re.sub(r"\s+", "", m.group(1)).replace(",", "/")
    return out


def make_summary(text: str, limit: int = 320) -> str:
    """공고문에서 사업개요·과업개요 문단을 우선 뽑아 짧은 요약으로"""
    t = _normalize(text)
    for kw in (r"(?:사업|과업|공모)\s*(?:의\s*)?개요", r"(?:사업|과업)\s*(?:의\s*)?목적", r"(?:사업|과업)\s*내용", r"과업\s*(?:의\s*)?범위"):
        m = re.search(kw, t)
        if m:
            seg = t[m.end(): m.end() + limit * 2].lstrip(" :：.-")
            cut = re.search(r"\s(?:\d{1,2}\.\s|제\s*\d+\s*조|[ⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\.|[나다라마바사아자차]\.\s)", seg)
            if cut and cut.start() > 40:
                seg = seg[:cut.start()]
            seg = re.sub(r"(?:^|\s)[○◦▪□■●◎]\s", " · ", seg)
            seg = re.sub(r"\s{2,}", " ", seg).strip(" ·")
            if len(seg) > 40:
                return seg if len(seg) <= limit else seg[:limit].rsplit(" ", 1)[0] + "…"
    return ""


def extract_info(text: str) -> dict:
    """공고문 텍스트 → {deadline, schedule, jury, site, area, use, scale, summary} (없는 항목은 생략)"""
    if not text or len(text) < 50:
        return {}
    info = {}
    dl = find_deadline(text)
    if dl:
        info["deadline"] = dl
    sc = find_schedule(text)
    if sc:
        info["schedule"] = sc
    jr = find_jury(text)
    if jr:
        info["jury"] = jr
    info.update(find_site(text))
    sm = make_summary(text)
    if sm:
        info["summary"] = sm
    return info


def _fetch(sess, url, max_bytes):
    r = sess.get(url, timeout=(15, 90), stream=True)
    if r.status_code != 200 or int(r.headers.get("content-length") or 0) > max_bytes:
        return None, ""
    data = r.raw.read(max_bytes + 1) if hasattr(r, "raw") else r.content
    if len(data) > max_bytes:
        return None, ""
    name = ""
    cd = r.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m:
        from urllib.parse import unquote
        name = unquote(m.group(1)).strip()
        try:
            name = name.encode("latin-1").decode("utf-8")
        except Exception:
            pass
    return data, name


def info_from_attachments(sess, files, max_files=2, max_bytes=12_000_000) -> dict:
    """files: [{'name','url'}] 또는 url 목록. 공고문으로 보이는 파일부터 내려받아 extract_info. 여러 파일 결과는 합침(앞선 파일 우선)"""
    items = [f if isinstance(f, dict) else {"name": "", "url": f} for f in files]
    # '공고'가 들어간 파일 → 지침서/과업 → 나머지 순
    def rank(f):
        n = f.get("name") or ""
        return (0 if "공고" in n else 1 if re.search(r"지침|과업|안내", n) else 2, -(n.lower().endswith((".hwp", ".hwpx", ".pdf"))))
    merged = {}
    for f in sorted(items, key=rank)[:max_files]:
        try:
            data, _ = _fetch(sess, f["url"], max_bytes)
            if not data:
                continue
            info = extract_info(extract_text(data))
            for k, v in info.items():
                if k == "schedule":
                    merged.setdefault("schedule", {}); [merged["schedule"].setdefault(a, b) for a, b in v.items()]
                else:
                    merged.setdefault(k, v)
        except Exception as e:
            log.debug("첨부 분석 실패 %s: %s", f.get("url"), e)
    return merged
