"""
LH 전자조달(ebid.lh.or.kr) 전자입찰공고조회 수집기 — 용역 공고

나라장터 API에는 LH가 조달청에 위탁한 공고(R26BK…)만 올라오고, LH가 자체 조달하는
공고(공고번호 7자리 숫자, 예: 2603342)는 나오지 않아 LH 사이트를 직접 읽습니다.
- 목록: /ebid.et.tp.cmd.BidMasterListCmd.dev  (POST, 10건/페이지, targetRow=1,11,21…)
  s_cstrtnJobGbCd=20(용역), s_tndrdocAcptOpenDtm~EndDtm = 입찰마감일 범위(YYYY/MM/DD)
- 상세: /ebid.et.tp.cmd.BidsrvcsDetailListCmd.dev?bidNum=…&bidDegree=…  (공고일·금액 보조 추출)
- LH 서버 인증서가 파이썬 기본 인증서 목록에 없어 SSL 오류 시 이 세션만 검증 생략
"""
import html as _html
import logging
import re
import time
from datetime import datetime, timedelta

import requests

from ..models import Notice
from .base import BaseCollector

log = logging.getLogger(__name__)

BASE = "https://ebid.lh.or.kr"
LIST_URL = BASE + "/ebid.et.tp.cmd.BidMasterListCmd.dev"
DETAIL_URL = {  # cstrtnJobGbCd → 상세 페이지
    "10": BASE + "/ebid.et.tp.cmd.BidConstructDetailListCmd.dev",
    "20": BASE + "/ebid.et.tp.cmd.BidsrvcsDetailListCmd.dev",
    "30": BASE + "/ebid.et.tp.cmd.BidgdsDetailListCmd.dev",
    "40": BASE + "/ebid.et.tp.cmd.BidctrctgdsDetailListCmd.dev",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Referer": BASE + "/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_ROW = re.compile(
    r"<tr[^>]*onclick=\"fn_dds_open\('(?P<num>\d+)',\s*'(?P<deg>\d+)',\s*'(?P<job>\d+)',\s*'(?P<emg>[YN])'\);\"[^>]*>(?P<body>.*?)</tr>",
    re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TOTAL = re.compile(r"총\s*([\d,]+)\s*건")


def _text(s: str) -> str:
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _fmt_dt(v: str) -> str:
    """'2026/09/30 10:00' / '2026.09.21' / '2026-09-21' → 'YYYY-MM-DD HH:MM'"""
    v = (v or "").strip()
    for f in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(v, f).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return v[:16]


def _to_int(v):
    try:
        return int(re.sub(r"[^\d]", "", str(v))) or None
    except Exception:
        return None


def _make_session() -> requests.Session:
    """LH 서버 인증서 체인이 리눅스(GitHub Actions)의 기본 인증서 목록으로는 검증되지 않아
    (윈도우 PC 에서는 truststore 로 OS 인증서를 써서 됐음) 이 세션은 검증을 끔.
    공개 공고 목록을 읽기만 하므로 위험 없음. truststore 전역 주입은 쓰지 않음 — 주입하면
    verify=False 가 듣지 않아 리눅스에서는 영영 실패."""
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = False
    return s


class LHCollector(BaseCollector):
    name = "lh"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.job_codes = [str(x) for x in (cfg.get("job_codes") or ["20"])]   # 20=용역
        self.detail = bool(cfg.get("fetch_detail", True))
        self.max_pages = int(cfg.get("max_pages", 60))
        self.known: set[str] = set()   # build.py 가 넣어줌: 이미 페이지에 있는 key → 상세 조회 생략
        self.sess = _make_session()

    # ── 목록 ──
    def _list_page(self, job: str, target_row: int, d1: str, d2: str) -> str:
        data = {
            "s_bidNum": "", "s_cstrtnJobGbCd": job, "s_bidnm": "",
            "s_tndrdocAcptOpenDtm": d1, "s_tndrdocAcptEndDtm": d2,
            "s_tndrCtrctMedCd": "", "s_zoneHqCd": "", "s_designPrc": "", "s_zone": "",
            "s_bidProgrsStatus": "", "s_licenceNm": "", "s_licenceCd": "",
            "pageSpec": "default", "targetRow": str(target_row), "devonOrderBy": "",
        }
        last = ""
        for attempt in range(3):
            try:
                r = self.sess.post(LIST_URL, data=data, timeout=(15, 60))
                r.raise_for_status()
                r.encoding = r.apparent_encoding if "charset" not in r.headers.get("content-type", "") else r.encoding
                return r.text
            except requests.RequestException as e:
                last = str(e)
                log.warning("lh 목록 요청 실패(%d/3): %s", attempt + 1, e)
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"LH 목록 3회 실패: {last}")

    def _parse_rows(self, page: str):
        for m in _ROW.finditer(page):
            tds = [_text(x) for x in _TD.findall(m.group("body"))]
            if len(tds) < 8:
                continue
            # 공고번호, 업무, 분류, 입찰건명, 계약방법, 입찰마감일자, 지역본부, 현황
            title = re.sub(r"^\[[^\]]{1,6}\]\s*", "", tds[3])  # 앞의 [지문] 표시 제거
            yield {
                "num": m.group("num"), "deg": m.group("deg"), "job": m.group("job"),
                "urgent": m.group("emg") == "Y",
                "job_nm": tds[1], "kind": tds[2], "title": title, "method": tds[4],
                "close": tds[5], "zone": re.sub(r"\s+", "", tds[6]), "status": tds[7],
            }

    # ── 상세 (공고일·금액) ──
    def _detail(self, num: str, deg: str, job: str) -> dict:
        url = DETAIL_URL.get(job, DETAIL_URL["20"])
        out = {}
        try:
            r = self.sess.get(url, params={"bidNum": num, "bidDegree": deg}, timeout=(15, 60))
            t = _text(r.text)
            m = re.search(r"(?:입찰)?공고일(?:자|시)?\s*[:：]?\s*(\d{4}[./-]\d{2}[./-]\d{2}(?:\s+\d{2}:\d{2})?)", t)
            if m:
                out["notice_date"] = _fmt_dt(m.group(1))
            m = re.search(r"(?:추정가격|설계가격|기초금액|추정금액|사업예산|배정예산)\s*[:：]?\s*([\d,]{5,})\s*원?", t)
            if m:
                out["price"] = _to_int(m.group(1))
            m = re.search(r"(?:공고부서|수요부서|담당부서)\s*[:：]?\s*(\S+)", t)
            if m:
                out["dept"] = m.group(1)[:40]
        except Exception as e:
            log.debug("lh 상세 실패 %s-%s: %s", num, deg, e)
        return out

    def _to_notice(self, row: dict, det: dict) -> Notice:
        job = row["job"]
        return Notice(
            source="lh",
            notice_no=row["num"],
            ord=row["deg"],
            title=row["title"],
            institution="한국토지주택공사",
            demand_institution=det.get("dept") or row["zone"],
            notice_date=det.get("notice_date", ""),
            close_date=_fmt_dt(row["close"]),
            estimated_price=det.get("price"),
            contract_method=row["method"],
            award_method=row["method"],
            notice_kind=row["kind"] + (" 긴급" if row["urgent"] and "긴급" not in row["kind"] else ""),
            service_div=row["job_nm"],
            url=f"{DETAIL_URL.get(job, DETAIL_URL['20'])}?bidNum={row['num']}&bidDegree={row['deg']}",
            raw="",
        )

    def collect(self, lookback_days: int) -> list[Notice]:
        today = datetime.now()
        # 마감일 기준 조회: 아직 마감 전인 공고 전부 (며칠 전 마감된 것도 함께)
        d1 = (today - timedelta(days=3)).strftime("%Y/%m/%d")
        d2 = (today + timedelta(days=730)).strftime("%Y/%m/%d")
        out: dict[str, Notice] = {}
        for job in self.job_codes:
            target, total, page_no = 1, None, 0
            while page_no < self.max_pages:
                page = self._list_page(job, target, d1, d2)
                if total is None:
                    m = _TOTAL.search(page)
                    total = _to_int(m.group(1)) if m else 0
                    log.info("lh 업무=%s 총 %d건", job, total or 0)
                rows = list(self._parse_rows(page))
                if not rows:
                    break
                for row in rows:
                    key = f"lh|{row['num']}|{row['deg']}"
                    det = {}
                    if self.detail and key not in self.known:
                        det = self._detail(row["num"], row["deg"], row["job"])
                        time.sleep(0.3)
                    n = self._to_notice(row, det)
                    # 공고일을 못 읽었고 이전 페이지에도 없는 새 공고면 오늘로 둠 (이전에 있으면 build.py 가 이전 값 유지)
                    if not n.notice_date and key not in self.known:
                        n.notice_date = today.strftime("%Y-%m-%d %H:%M")
                    out[n.key] = n
                target += 10
                page_no += 1
                if total and target > total:
                    break
                time.sleep(0.5)
        return list(out.values())